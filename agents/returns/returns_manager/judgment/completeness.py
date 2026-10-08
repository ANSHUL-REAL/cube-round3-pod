"""Completeness arithmetic (§11.10), after C01-C04.

- A resolved component whose observed count is known and below the expected quantity is partly missing:
  status `missing`, missing_quantity = expected - observed (e.g. 2 of 3 candles).
- `complete`: every component present · `incomplete`: ≥1 resolved missing · `uncertain`: otherwise.
- Flat strings use the official format: `;`-separated, `name` or `name xN` (e.g. `candle x3;gift box`).
"""

from __future__ import annotations

from returns_manager.judgment.types import (
    CompletenessResult,
    ComponentResult,
    JudgmentContext,
    ValidationReport,
    to_bp,
)
from returns_manager.llm.schemas import JudgmentV1


def _fmt(name: str, qty: int) -> str:
    return name if qty == 1 else f"{name} x{qty}"


def compute_completeness(j: JudgmentV1, ctx: JudgmentContext, rep: ValidationReport) -> CompletenessResult:
    meta = {c.id: c for c in ctx.card.components}
    results: list[ComponentResult] = []
    for obs in j.completeness.components:
        m = meta[obs.component_id]
        status = obs.status
        observed = obs.observed_quantity
        missing_qty = 0
        if status == "present" and observed is not None and observed < m.quantity:
            rep.act(
                "COMP-PARTIAL",
                f"component:{m.id}",
                "present",
                "missing",
                f"{observed} of {m.quantity} observed",
            )
            status = "missing"
        if status == "missing":
            missing_qty = (
                m.quantity - observed if observed is not None and observed < m.quantity else m.quantity
            )
            missing_qty = max(missing_qty, 1)
        results.append(
            ComponentResult(
                component_id=m.id,
                name=m.name,
                expected=m.quantity,
                observed=observed,
                status=status,
                essential=m.essential,
                replaceable=m.replaceable,
                verifiable_by_photo=m.verifiable_by_photo,
                missing_quantity=missing_qty,
                photos=tuple(obs.photos),
                confidence_bp=to_bp(obs.confidence),
                reason=rep.component_reasons.get(m.id) if status == "uncertain" else None,
            )
        )
    missing = [r for r in results if r.status == "missing"]
    unsure = [r for r in results if r.status == "uncertain"]
    if missing:
        status_all = "incomplete"
    elif unsure:
        status_all = "uncertain"
    else:
        status_all = "complete"
    # essential: None means "not sourced" (§8.2) — treated as essential, the conservative reading.
    return CompletenessResult(
        status=status_all,  # type: ignore[arg-type]
        components=tuple(results),
        essential_missing=tuple(r.component_id for r in missing if r.essential is not False),
        nonessential_missing=tuple(r.component_id for r in missing if r.essential is False),
        uncertain_components=tuple(r.component_id for r in unsure),
        essential_uncertain=tuple(r.component_id for r in unsure if r.essential is not False),
        parts_list=";".join(_fmt(r.name, r.expected) for r in results),
        parts_missing=";".join(_fmt(r.name, r.missing_quantity) for r in missing),
        parts_uncertain=";".join(_fmt(r.name, r.expected) for r in unsure),
        flags=tuple(f for f in rep.flags if f == "excess_quantity"),
    )
