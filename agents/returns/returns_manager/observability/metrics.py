"""Metrics (§18.2): every value is `{value, n, window, method}`; `n=0` renders as "no data",
never a bare `0` — a rate of zero and an absence of data must never look the same.

All queries are tenant-scoped (`db.transaction(org_id)`); there is no cross-org aggregate.
Metrics read `rm.inspection_runs`, `rm.inspection_results`, `rm.returns`, `rm.inspection_jobs`,
`rm.overrides`, `rm.signoffs`, `rm.audit_findings` — every column used here was already
written by P5-P9; no new migration is needed for this phase.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from psycopg import AsyncConnection

NO_DATA = "no data"

_WINDOW_RE = re.compile(r"^(\d+)(h|d)$")


def parse_window(window: str) -> timedelta | None:
    """`"24h"` / `"7d"` -> a timedelta; `"all"` -> None (no lower bound). Raises ValueError otherwise."""
    if window == "all":
        return None
    m = _WINDOW_RE.match(window)
    if not m:
        raise ValueError(f"invalid window {window!r}; expected e.g. '24h', '7d', or 'all'")
    n, unit = int(m.group(1)), m.group(2)
    if n <= 0:
        raise ValueError(f"invalid window {window!r}: must be positive")
    return timedelta(hours=n) if unit == "h" else timedelta(days=n)


def window_since(window: str, *, now: datetime | None = None) -> datetime | None:
    delta = parse_window(window)
    if delta is None:
        return None
    return (now or datetime.now(UTC)) - delta


@dataclass(frozen=True)
class MetricValue:
    """One measurement. `n` is the sample size the value was computed over; `method` says how."""

    value: Any
    n: int
    window: str
    method: str

    def to_dict(self) -> dict[str, Any]:
        return {"value": self.value, "n": self.n, "window": self.window, "method": self.method}


def metric(value: Any, n: int, window: str, method: str) -> MetricValue:
    """Build a MetricValue, substituting the literal string "no data" when `n == 0`."""
    if n == 0:
        return MetricValue(value=NO_DATA, n=0, window=window, method=method)
    return MetricValue(value=value, n=n, window=window, method=method)


def _round(x: float | None, digits: int = 2) -> float | None:
    return None if x is None else round(float(x), digits)


class MetricsService:
    """§18.2 metrics, computed on demand from the org's own rows."""

    def __init__(self, conn: AsyncConnection[Any], org_id: str) -> None:
        self.conn = conn
        self.org_id = org_id

    # ── Throughput & latency ────────────────────────────────────────────────

    async def throughput_per_hour(self, window: str) -> MetricValue:
        since = window_since(window)
        cur = await self.conn.execute(
            "SELECT count(*) AS n FROM rm.returns "
            "WHERE org_id = %s AND status = 'finalized' AND (%s::timestamptz IS NULL OR finalized_at >= %s)",
            (self.org_id, since, since),
        )
        row = await cur.fetchone()
        assert row is not None  # bare aggregate SELECT always returns exactly one row
        n = int(row["n"])
        hours = max((parse_window(window) or timedelta(days=1)).total_seconds() / 3600, 1.0)
        return metric(
            _round(n / hours, 3),
            n,
            window,
            "count(returns.status='finalized' AND finalized_at in window) / window hours",
        )

    async def _percentiles_ms(
        self, sql: str, params: tuple[Any, ...], method: str, window: str
    ) -> MetricValue:
        cur = await self.conn.execute(sql, params)
        row = await cur.fetchone()
        assert row is not None  # bare aggregate SELECT always returns exactly one row
        n = int(row["n"])
        if n == 0:
            return metric(None, 0, window, method)
        return metric(
            {"p50_ms": _round(row["p50"]), "p95_ms": _round(row["p95"]), "mean_ms": _round(row["mean"])},
            n,
            window,
            method,
        )

    async def model_latency_ms(self, window: str) -> MetricValue:
        since = window_since(window)
        return await self._percentiles_ms(
            "SELECT count(*) AS n, "
            "percentile_cont(0.5) WITHIN GROUP (ORDER BY latency_ms) AS p50, "
            "percentile_cont(0.95) WITHIN GROUP (ORDER BY latency_ms) AS p95, "
            "avg(latency_ms) AS mean "
            "FROM rm.inspection_runs "
            "WHERE org_id = %s AND status = 'completed' AND latency_ms IS NOT NULL "
            "AND (%s::timestamptz IS NULL OR completed_at >= %s)",
            (self.org_id, since, since),
            "p50/p95/mean of inspection_runs.latency_ms where status='completed'",
            window,
        )

    async def queue_wait_ms(self, window: str) -> MetricValue:
        since = window_since(window)
        return await self._percentiles_ms(
            "SELECT count(*) AS n, "
            "percentile_cont(0.5) WITHIN GROUP (ORDER BY wait_ms) AS p50, "
            "percentile_cont(0.95) WITHIN GROUP (ORDER BY wait_ms) AS p95, "
            "avg(wait_ms) AS mean FROM ("
            "  SELECT extract(epoch FROM (ir.started_at - ij.created_at)) * 1000 AS wait_ms"
            "  FROM rm.inspection_runs ir JOIN rm.inspection_jobs ij"
            "    ON ij.org_id = ir.org_id AND ij.job_id = ir.job_id"
            "  WHERE ir.org_id = %s AND ir.started_at IS NOT NULL"
            "    AND (%s::timestamptz IS NULL OR ir.started_at >= %s)"
            ") w",
            (self.org_id, since, since),
            "p50/p95/mean of (inspection_runs.started_at - inspection_jobs.created_at)",
            window,
        )

    async def capture_to_decision_ms(self, window: str) -> MetricValue:
        since = window_since(window)
        return await self._percentiles_ms(
            "SELECT count(*) AS n, "
            "percentile_cont(0.5) WITHIN GROUP (ORDER BY ms) AS p50, "
            "percentile_cont(0.95) WITHIN GROUP (ORDER BY ms) AS p95, "
            "avg(ms) AS mean FROM ("
            "  SELECT extract(epoch FROM (ir.completed_at - r.submitted_at)) * 1000 AS ms"
            "  FROM rm.inspection_runs ir JOIN rm.returns r"
            "    ON r.org_id = ir.org_id AND r.return_id = ir.return_id"
            "  WHERE ir.org_id = %s AND ir.kind = 'judgment' AND ir.status = 'completed'"
            "    AND r.submitted_at IS NOT NULL"
            "    AND (%s::timestamptz IS NULL OR ir.completed_at >= %s)"
            ") w",
            (self.org_id, since, since),
            "p50/p95/mean of (first judgment inspection_runs.completed_at - returns.submitted_at)",
            window,
        )

    # ── Rule-2 metric: requests / tool calls per inspection ─────────────────

    async def requests_per_inspection(self, window: str) -> MetricValue:
        since = window_since(window)
        cur = await self.conn.execute(
            "SELECT count(*) AS n, avg(api_requests) AS mean, "
            "percentile_cont(0.95) WITHIN GROUP (ORDER BY api_requests) AS p95 "
            "FROM rm.inspection_runs "
            "WHERE org_id = %s AND kind = 'judgment' AND status = 'completed' "
            "AND (%s::timestamptz IS NULL OR completed_at >= %s)",
            (self.org_id, since, since),
        )
        row = await cur.fetchone()
        assert row is not None  # bare aggregate SELECT always returns exactly one row
        n = int(row["n"])
        return metric(
            None if n == 0 else {"mean": _round(row["mean"], 3), "p95": _round(row["p95"], 3)},
            n,
            window,
            "mean/p95 of inspection_runs.api_requests for kind='judgment' (the Rule-2 metric; target <= 1.2)",
        )

    async def tool_calls_per_inspection(self, window: str) -> MetricValue:
        since = window_since(window)
        cur = await self.conn.execute(
            "SELECT count(*) AS n, avg(tool_calls) AS mean FROM rm.inspection_runs "
            "WHERE org_id = %s AND kind = 'judgment' AND status = 'completed' "
            "AND (%s::timestamptz IS NULL OR completed_at >= %s)",
            (self.org_id, since, since),
        )
        row = await cur.fetchone()
        assert row is not None  # bare aggregate SELECT always returns exactly one row
        n = int(row["n"])
        return metric(
            _round(row["mean"], 3), n, window, "mean of inspection_runs.tool_calls for kind='judgment'"
        )

    # ── Tokens ───────────────────────────────────────────────────────────────

    async def tokens_per_inspection(self, window: str) -> MetricValue:
        since = window_since(window)
        cur = await self.conn.execute(
            "SELECT count(*) AS n, "
            "avg((usage->>'total_input_tokens')::numeric) AS mean_input, "
            "avg((usage->>'total_output_tokens')::numeric) AS mean_output, "
            "avg((usage->>'total_cached_tokens')::numeric) AS mean_cached, "
            "sum((usage->>'total_cached_tokens')::numeric) AS sum_cached, "
            "sum((usage->>'total_input_tokens')::numeric) AS sum_input "
            "FROM rm.inspection_runs "
            "WHERE org_id = %s AND status = 'completed' AND usage != '{}'::jsonb "
            "AND (%s::timestamptz IS NULL OR completed_at >= %s)",
            (self.org_id, since, since),
        )
        row = await cur.fetchone()
        assert row is not None  # bare aggregate SELECT always returns exactly one row
        n = int(row["n"])
        if n == 0:
            return metric(None, 0, window, "mean tokens per inspection from inspection_runs.usage")
        sum_input = float(row["sum_input"] or 0)
        sum_cached = float(row["sum_cached"] or 0)
        cached_share = _round(sum_cached / sum_input, 4) if sum_input > 0 else None
        return metric(
            {
                "mean_input": _round(row["mean_input"]),
                "mean_output": _round(row["mean_output"]),
                "mean_cached": _round(row["mean_cached"]),
                "cached_share": cached_share,
            },
            n,
            window,
            "mean of inspection_runs.usage fields; cached_share = sum(cached)/sum(input)",
        )

    # ── Rates (all "n" = the denominator population) ────────────────────────

    async def _rate(
        self, numerator_sql: str, denominator_sql: str, params: tuple[Any, ...], method: str, window: str
    ) -> MetricValue:
        cur = await self.conn.execute(f"SELECT ({numerator_sql}) AS num, ({denominator_sql}) AS den", params)
        row = await cur.fetchone()
        assert row is not None  # bare aggregate SELECT always returns exactly one row
        den = int(row["den"] or 0)
        if den == 0:
            return metric(None, 0, window, method)
        num = int(row["num"] or 0)
        return metric(_round(num / den, 4), den, window, method)

    async def uncertain_rate(self, window: str) -> MetricValue:
        since = window_since(window)
        p = (self.org_id, since, since, self.org_id, since, since)
        return await self._rate(
            "SELECT count(*) FROM rm.inspection_results WHERE org_id = %s "
            "AND (identity_match = 'uncertain' OR completeness_status = 'uncertain') "
            "AND (%s::timestamptz IS NULL OR created_at >= %s)",
            "SELECT count(*) FROM rm.inspection_results WHERE org_id = %s "
            "AND (%s::timestamptz IS NULL OR created_at >= %s)",
            p,
            "count(identity_match|completeness_status='uncertain') / count(inspection_results)",
            window,
        )

    async def retake_rate(self, window: str) -> MetricValue:
        since = window_since(window)
        p = (self.org_id, since, since, self.org_id, since, since)
        return await self._rate(
            "SELECT count(*) FROM rm.inspection_results WHERE org_id = %s "
            "AND jsonb_array_length(retake_requests) > 0 "
            "AND (%s::timestamptz IS NULL OR created_at >= %s)",
            "SELECT count(*) FROM rm.inspection_results WHERE org_id = %s "
            "AND (%s::timestamptz IS NULL OR created_at >= %s)",
            p,
            "count(retake_requests non-empty) / count(inspection_results)",
            window,
        )

    async def escalation_rate(self, window: str) -> MetricValue:
        since = window_since(window)
        p = (self.org_id, since, since, self.org_id, since, since)
        return await self._rate(
            "SELECT count(*) FROM rm.inspection_results WHERE org_id = %s "
            "AND escalation_state != 'not_triggered' "
            "AND (%s::timestamptz IS NULL OR created_at >= %s)",
            "SELECT count(*) FROM rm.inspection_results WHERE org_id = %s "
            "AND (%s::timestamptz IS NULL OR created_at >= %s)",
            p,
            "count(escalation_state != 'not_triggered') / count(results) (§18.5: investigate if > 25%)",
            window,
        )

    async def audit_disagreement_rate(self, window: str) -> MetricValue:
        since = window_since(window)
        p = (self.org_id, since, since, self.org_id, since, since)
        return await self._rate(
            "SELECT count(*) FROM rm.audit_findings WHERE org_id = %s "
            "AND cardinality(disagreements) > 0 "
            "AND (%s::timestamptz IS NULL OR created_at >= %s)",
            "SELECT count(*) FROM rm.audit_findings WHERE org_id = %s "
            "AND (%s::timestamptz IS NULL OR created_at >= %s)",
            p,
            "count(audit_findings with >=1 disagreement) / count(audit_findings)",
            window,
        )

    async def override_rate(self, window: str) -> MetricValue:
        since = window_since(window)
        p = (self.org_id, since, since, self.org_id, since, since)
        return await self._rate(
            "SELECT count(DISTINCT return_id) FROM rm.overrides WHERE org_id = %s "
            "AND (%s::timestamptz IS NULL OR created_at >= %s)",
            "SELECT count(*) FROM rm.returns WHERE org_id = %s AND status = 'finalized' "
            "AND (%s::timestamptz IS NULL OR finalized_at >= %s)",
            p,
            "count(distinct returns with >=1 override) / count(finalized returns)",
            window,
        )

    async def signoff_approval_rate(self, window: str) -> MetricValue:
        since = window_since(window)
        p = (self.org_id, since, since, self.org_id, since, since)
        return await self._rate(
            "SELECT count(*) FROM rm.signoffs WHERE org_id = %s AND decision = 'approved' "
            "AND (%s::timestamptz IS NULL OR created_at >= %s)",
            "SELECT count(*) FROM rm.signoffs WHERE org_id = %s "
            "AND (%s::timestamptz IS NULL OR created_at >= %s)",
            p,
            "count(signoffs.decision='approved') / count(signoffs)",
            window,
        )

    async def needs_attention_rate(self, window: str) -> MetricValue:
        since = window_since(window)
        p = (self.org_id, since, since, self.org_id, since, since)
        return await self._rate(
            "SELECT count(*) FROM rm.returns WHERE org_id = %s AND status = 'needs_attention' "
            "AND (%s::timestamptz IS NULL OR created_at >= %s)",
            "SELECT count(*) FROM rm.returns WHERE org_id = %s "
            "AND (%s::timestamptz IS NULL OR created_at >= %s)",
            p,
            "count(returns.status='needs_attention') / count(returns)",
            window,
        )

    async def error_rate_by_class(self, window: str) -> MetricValue:
        since = window_since(window)
        cur = await self.conn.execute(
            "SELECT count(*) AS n FROM rm.inspection_runs WHERE org_id = %s "
            "AND (%s::timestamptz IS NULL OR started_at >= %s)",
            (self.org_id, since, since),
        )
        total_row = await cur.fetchone()
        total = int(total_row["n"]) if total_row else 0
        if total == 0:
            return metric(
                None, 0, window, "count(inspection_runs.status='failed') by error_class / count(runs)"
            )
        cur = await self.conn.execute(
            "SELECT coalesce(error_class, 'unknown') AS error_class, count(*) AS n "
            "FROM rm.inspection_runs WHERE org_id = %s AND status = 'failed' "
            "AND (%s::timestamptz IS NULL OR started_at >= %s) "
            "GROUP BY error_class ORDER BY n DESC",
            (self.org_id, since, since),
        )
        rows = await cur.fetchall()
        by_class = {r["error_class"]: _round(int(r["n"]) / total, 4) for r in rows}
        return metric(
            by_class, total, window, "count(inspection_runs.status='failed') by error_class / count(runs)"
        )

    # ── Integrity counters ───────────────────────────────────────────────────

    async def integrity_counters(self, window: str) -> MetricValue:
        """§18.2: invented references/quotes (referential validation, §11.9), injection and
        reused-photo review flags (disposition/engine.py REVIEW_FLAGS)."""
        since = window_since(window)
        cur = await self.conn.execute(
            "SELECT count(*) AS n, "
            "coalesce(sum((validation_report->>'invented_reference_count')::int), 0) AS refs, "
            "coalesce(sum((validation_report->>'invented_quote_count')::int), 0) AS quotes "
            "FROM rm.inspection_runs WHERE org_id = %s AND validation_report IS NOT NULL "
            "AND (%s::timestamptz IS NULL OR completed_at >= %s)",
            (self.org_id, since, since),
        )
        row = await cur.fetchone()
        assert row is not None  # bare aggregate SELECT always returns exactly one row
        n = int(row["n"])
        if n == 0:
            return metric(None, 0, window, "sums from inspection_runs.validation_report / review_reasons")
        cur = await self.conn.execute(
            "SELECT "
            "count(*) FILTER (WHERE 'injection_attempt_suspected' = ANY(review_reasons)) AS injection, "
            "count(*) FILTER (WHERE 'possible_reused_photo' = ANY(review_reasons)) AS reused_photo "
            "FROM rm.inspection_results WHERE org_id = %s "
            "AND (%s::timestamptz IS NULL OR created_at >= %s)",
            (self.org_id, since, since),
        )
        flags_row = await cur.fetchone()
        assert flags_row is not None
        return metric(
            {
                "invented_reference_count": int(row["refs"]),
                "invented_quote_count": int(row["quotes"]),
                "injection_flag_count": int(flags_row["injection"]),
                "reused_photo_flag_count": int(flags_row["reused_photo"]),
            },
            n,
            window,
            "sum(validation_report.invented_reference_count/invented_quote_count) over "
            "inspection_runs; count(review_reasons contains injection_attempt_suspected / "
            "possible_reused_photo) over inspection_results",
        )

    # ── Distributions ────────────────────────────────────────────────────────

    async def disposition_distribution(self, window: str) -> MetricValue:
        since = window_since(window)
        cur = await self.conn.execute(
            "SELECT count(*) AS n FROM rm.inspection_results WHERE org_id = %s "
            "AND (%s::timestamptz IS NULL OR created_at >= %s)",
            (self.org_id, since, since),
        )
        total_row = await cur.fetchone()
        total = int(total_row["n"]) if total_row else 0
        if total == 0:
            return metric(None, 0, window, "distribution of inspection_results.recommended_disposition")
        cur = await self.conn.execute(
            "SELECT coalesce(recommended_disposition, 'null') AS route, count(*) AS n "
            "FROM rm.inspection_results WHERE org_id = %s "
            "AND (%s::timestamptz IS NULL OR created_at >= %s) "
            "GROUP BY route ORDER BY n DESC",
            (self.org_id, since, since),
        )
        rows = await cur.fetchall()
        dist = {r["route"]: _round(int(r["n"]) / total, 4) for r in rows}
        return metric(dist, total, window, "distribution of inspection_results.recommended_disposition")

    async def quota_used_today(self, model_id: str, daily_budget: int) -> MetricValue:
        """Not windowed (a snapshot of today's Pacific-day counter, §10.4a)."""
        cur = await self.conn.execute(
            "SELECT requests_used FROM rm.model_request_ledger "
            "WHERE model_id = %s AND quota_day = current_date",
            (model_id,),
        )
        row = await cur.fetchone()
        used = int(row["requests_used"]) if row else 0
        return metric(
            {"used": used, "budget": daily_budget, "remaining": max(daily_budget - used, 0)},
            1,
            "today",
            "rm.model_request_ledger.requests_used for quota_day = current_date (Pacific)",
        )

    # ── Summary ──────────────────────────────────────────────────────────────

    async def summary(self, window: str) -> dict[str, dict[str, Any]]:
        """Every §18.2 metric this service computes, for one org and one window."""
        return {
            "throughput_per_hour": (await self.throughput_per_hour(window)).to_dict(),
            "model_latency_ms": (await self.model_latency_ms(window)).to_dict(),
            "queue_wait_ms": (await self.queue_wait_ms(window)).to_dict(),
            "capture_to_decision_ms": (await self.capture_to_decision_ms(window)).to_dict(),
            "requests_per_inspection": (await self.requests_per_inspection(window)).to_dict(),
            "tool_calls_per_inspection": (await self.tool_calls_per_inspection(window)).to_dict(),
            "tokens_per_inspection": (await self.tokens_per_inspection(window)).to_dict(),
            "uncertain_rate": (await self.uncertain_rate(window)).to_dict(),
            "retake_rate": (await self.retake_rate(window)).to_dict(),
            "escalation_rate": (await self.escalation_rate(window)).to_dict(),
            "audit_disagreement_rate": (await self.audit_disagreement_rate(window)).to_dict(),
            "override_rate": (await self.override_rate(window)).to_dict(),
            "signoff_approval_rate": (await self.signoff_approval_rate(window)).to_dict(),
            "needs_attention_rate": (await self.needs_attention_rate(window)).to_dict(),
            "error_rate_by_class": (await self.error_rate_by_class(window)).to_dict(),
            "disposition_distribution": (await self.disposition_distribution(window)).to_dict(),
            "integrity_counters": (await self.integrity_counters(window)).to_dict(),
        }
