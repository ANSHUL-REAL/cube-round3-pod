"""Unit economics (§18.3). Every figure states its currency, method and `n`; synthetic
values (recovery uplift) are labelled `synthetic: true` everywhere they appear, per the
honesty rules in `RULES.md` §6 ("It works well" is not a result).

Actual spend on the free tier is $0 for every run in this repository. `cost_usd_micros`
on `rm.inspection_runs` (§8.8, `llm/pricing.py`) is the **paid-equivalent** cost — what the
same tokens would cost on the paid Standard tier — so unit economics stay meaningful without
pretending money was spent.
"""

from __future__ import annotations

from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from psycopg import AsyncConnection

from returns_manager.config import REPO_ROOT, Settings
from returns_manager.observability.metrics import MetricsService, metric, window_since
from returns_manager.reference.models import FxV1

FX_FILE = REPO_ROOT / "reference" / "pricing" / "fx.yaml"

# The sister Prep track's stated cost band (§18.3 sanity anchor) — context from another
# track, not a Returns requirement. Not derived from any measurement of this system.
PREP_COST_BAND_USD = (Decimal("0.40"), Decimal("1.10"))

STAGE_KINDS = ("judgment", "escalation", "audit")

# §18.3: "the expected recovery of the engine's decisions minus that of a baseline policy
# (e.g. 'liquidate all opened returns')". This is the literal baseline the section names.
BASELINE_ROUTE = "liquidate"


@lru_cache(maxsize=2)
def load_fx(path: Path = FX_FILE) -> FxV1:
    return FxV1.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))


def usd_to_inr(usd: Decimal, fx: FxV1 | None = None) -> Decimal:
    rate = Decimal((fx or load_fx()).usd_inr)
    return usd * rate


def micros_to_usd(micros: int | float) -> Decimal:
    return Decimal(micros) / Decimal(1_000_000)


def _round2(d: Decimal) -> float:
    return float(d.quantize(Decimal("0.0001")))


class EconomicsService:
    """§18.3. One instance per (connection, org); all figures are for that org's own rows."""

    def __init__(self, conn: AsyncConnection[Any], org_id: str, settings: Settings) -> None:
        self.conn = conn
        self.org_id = org_id
        self.settings = settings
        self.metrics = MetricsService(conn, org_id)

    async def cost_by_stage(self, window: str) -> dict[str, dict[str, Any]]:
        """Mean/p95 paid-equivalent cost per inspection, in USD and INR, for each stage."""
        since = window_since(window)
        fx = load_fx()
        out: dict[str, dict[str, Any]] = {}
        for kind in STAGE_KINDS:
            cur = await self.conn.execute(
                "SELECT count(*) AS n, avg(cost_usd_micros) AS mean, "
                "percentile_cont(0.95) WITHIN GROUP (ORDER BY cost_usd_micros) AS p95 "
                "FROM rm.inspection_runs WHERE org_id = %s AND kind = %s "
                "AND cost_usd_micros IS NOT NULL "
                "AND (%s::timestamptz IS NULL OR completed_at >= %s)",
                (self.org_id, kind, since, since),
            )
            row = await cur.fetchone()
            assert row is not None  # bare aggregate SELECT always returns exactly one row
            n = int(row["n"])
            if n == 0:
                out[kind] = metric(
                    None, 0, window, "mean/p95 of inspection_runs.cost_usd_micros (paid-equivalent)"
                ).to_dict()
                continue
            mean_usd = micros_to_usd(row["mean"])
            p95_usd = micros_to_usd(row["p95"])
            out[kind] = metric(
                {
                    "mean_usd": _round2(mean_usd),
                    "p95_usd": _round2(p95_usd),
                    "mean_inr": _round2(usd_to_inr(mean_usd, fx)),
                    "p95_inr": _round2(usd_to_inr(p95_usd, fx)),
                },
                n,
                window,
                "mean/p95 of inspection_runs.cost_usd_micros (paid-equivalent; free tier used), "
                f"USD -> INR at {fx.usd_inr} ({fx.as_of}, {fx.note})",
            ).to_dict()
        return out

    async def cost_per_inspection(self, window: str) -> dict[str, Any]:
        """Blended cost of one unit's full pipeline: judgment always runs; escalation and
        audit are weighted by their measured/configured rates (§18.3 "split by stage")."""
        by_stage = await self.cost_by_stage(window)
        judgment = by_stage["judgment"]
        if judgment["n"] == 0:
            return metric(
                None, 0, window, "blended judgment + escalation_rate*escalation + audit_rate*audit"
            ).to_dict()

        escalation = by_stage["escalation"]
        audit = by_stage["audit"]
        escalation_rate_metric = (await self.metrics.escalation_rate(window)).to_dict()
        escalation_rate = escalation_rate_metric["value"] if escalation_rate_metric["n"] > 0 else 0.0
        audit_sample_rate = self.settings.rm_audit_sample_rate  # configured, not measured

        mean_usd = Decimal(str(judgment["value"]["mean_usd"]))
        if escalation["n"] > 0:
            mean_usd += Decimal(str(escalation_rate)) * Decimal(str(escalation["value"]["mean_usd"]))
        if audit["n"] > 0:
            mean_usd += Decimal(str(audit_sample_rate)) * Decimal(str(audit["value"]["mean_usd"]))

        fx = load_fx()
        return metric(
            {"mean_usd": _round2(mean_usd), "mean_inr": _round2(usd_to_inr(mean_usd, fx))},
            judgment["n"],
            window,
            f"judgment.mean_usd + escalation_rate({escalation_rate})*escalation.mean_usd + "
            f"configured audit_sample_rate({audit_sample_rate})*audit.mean_usd; n = judgment inspections",
        ).to_dict()

    async def projected_monthly_cost(self, window: str, volume: int) -> dict[str, Any]:
        """`volume` finalized units per month at the current blended cost per inspection."""
        per_inspection = await self.cost_per_inspection(window)
        if per_inspection["n"] == 0:
            return metric(None, 0, window, "volume * cost_per_inspection.mean_usd").to_dict()
        mean_usd = Decimal(str(per_inspection["value"]["mean_usd"]))
        total_usd = mean_usd * Decimal(volume)
        fx = load_fx()
        return metric(
            {
                "volume": volume,
                "total_usd": _round2(total_usd),
                "total_inr": _round2(usd_to_inr(total_usd, fx)),
            },
            per_inspection["n"],
            window,
            f"volume({volume}) * cost_per_inspection.mean_usd({_round2(mean_usd)}); "
            "paid-equivalent, not actual free-tier spend",
        ).to_dict()

    async def recovery_uplift(self, window: str) -> dict[str, Any]:
        """Synthetic: the engine's chosen route's recorded expected recovery minus the
        'liquidate all opened returns' baseline, summed over evaluated units (§18.3)."""
        since = window_since(window)
        cur = await self.conn.execute(
            "SELECT disposition, recommended_disposition FROM rm.inspection_results "
            "WHERE org_id = %s AND recommended_disposition IS NOT NULL "
            "AND (%s::timestamptz IS NULL OR created_at >= %s)",
            (self.org_id, since, since),
        )
        rows = await cur.fetchall()
        n = 0
        actual_total = 0
        baseline_total = 0
        currency = "INR"
        for row in rows:
            disp = row["disposition"] or {}
            per_route = dict(disp.get("expected_recovery_minor") or [])
            recommended = row["recommended_disposition"]
            if recommended not in per_route or BASELINE_ROUTE not in per_route:
                continue
            actual_total += int(per_route[recommended])
            baseline_total += int(per_route[BASELINE_ROUTE])
            currency = disp.get("currency", currency)
            n += 1
        if n == 0:
            return metric(
                None,
                0,
                window,
                "synthetic: sum(expected_recovery_minor[recommended] - [liquidate]) over units",
            ).to_dict()
        uplift_minor = actual_total - baseline_total
        return metric(
            {
                "synthetic": True,
                "currency": currency,
                "uplift_total_minor": uplift_minor,
                "uplift_mean_minor": round(uplift_minor / n, 2),
                "baseline_policy": "liquidate all opened returns",
            },
            n,
            window,
            "synthetic: sum over evaluated units of "
            "inspection_results.disposition.expected_recovery_minor[recommended_disposition] "
            "minus the same unit's expected_recovery_minor['liquidate']; both from the pure "
            "disposition engine (disposition/engine.py), never from a real sale",
        ).to_dict()

    def sanity_anchor(self, judgment_cost_by_stage: dict[str, Any]) -> dict[str, Any]:
        """Report our judgment-stage cost next to the sister Prep track's stated band.
        Context from another track, not a Returns requirement (§18.3)."""
        lo, hi = PREP_COST_BAND_USD
        return {
            "prep_track_band_usd_per_unit": {"low": float(lo), "high": float(hi)},
            "our_judgment_cost_usd_per_inspection": judgment_cost_by_stage.get("value"),
            "note": "context from another track (Prep), not a Returns requirement or comparability claim",
        }

    async def report(self, window: str, volume: int) -> dict[str, Any]:
        by_stage = await self.cost_by_stage(window)
        return {
            "window": window,
            "cost_by_stage": by_stage,
            "cost_per_inspection": await self.cost_per_inspection(window),
            "projected_monthly_cost": await self.projected_monthly_cost(window, volume),
            "recovery_uplift": await self.recovery_uplift(window),
            "sanity_anchor": self.sanity_anchor(by_stage["judgment"]),
        }
