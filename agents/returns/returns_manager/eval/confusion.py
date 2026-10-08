"""Per-check FP/FN (§21.1), confusion matrices, and failure-mode tagging (§21.4).

§21.1 fixes what "positive" means per check, and — this is the part that is easy to get
backwards — which direction is FP and which is FN. In every row, FN is the direction that
harms the customer or the business (item passed off as better/more-present/more-complete
than it is); FP is the safe-but-wrong direction (a false alarm). This module encodes that
table directly so a caller never has to re-derive it.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field

from returns_manager.eval.models import (
    CONDITION_GRADE_ORDER,
    FAILURE_MODES,
    UNIT_PRESENCE_EMPTY_VALUES,
    EvaluatedUnit,
)


@dataclass(frozen=True)
class FpFn:
    """One check's false positives and false negatives, with the plain-English direction
    each one means, so a report never needs a legend to be read correctly."""

    check: str
    positive_meaning: str
    fp: int
    fp_meaning: str
    fn: int
    fn_meaning: str
    n: int


def unit_presence_fp_fn(agent: Sequence[str], gold: Sequence[str]) -> FpFn:
    """§21.1 row 1: positive = item not returned (empty/non-product)."""
    if len(agent) != len(gold):
        raise ValueError("unit_presence_fp_fn: sequences must be the same length")
    fp = sum(
        1
        for a, g in zip(agent, gold, strict=True)
        if a in UNIT_PRESENCE_EMPTY_VALUES and g not in UNIT_PRESENCE_EMPTY_VALUES
    )
    fn = sum(
        1
        for a, g in zip(agent, gold, strict=True)
        if a not in UNIT_PRESENCE_EMPTY_VALUES and g in UNIT_PRESENCE_EMPTY_VALUES
    )
    return FpFn(
        "unit_presence",
        "item not returned (empty/non-product)",
        fp,
        "flagged empty, but the item was there",
        fn,
        "empty box passed as a real return",
        len(agent),
    )


def identity_fp_fn(agent: Sequence[str], gold: Sequence[str]) -> FpFn:
    """§21.1 row 2: positive = wrong item (identity_match = no)."""
    if len(agent) != len(gold):
        raise ValueError("identity_fp_fn: sequences must be the same length")
    fp = sum(1 for a, g in zip(agent, gold, strict=True) if a == "no" and g != "no")
    fn = sum(1 for a, g in zip(agent, gold, strict=True) if a != "no" and g == "no")
    return FpFn(
        "identity",
        "wrong item (identity_match = no)",
        fp,
        "flagged wrong, but it was right",
        fn,
        "wrong item passed as right",
        len(agent),
    )


def completeness_fp_fn(agent: Sequence[str], gold: Sequence[str]) -> FpFn:
    """§21.1 row 3: positive = incomplete (>=1 component missing)."""
    if len(agent) != len(gold):
        raise ValueError("completeness_fp_fn: sequences must be the same length")
    fp = sum(1 for a, g in zip(agent, gold, strict=True) if a == "incomplete" and g != "incomplete")
    fn = sum(1 for a, g in zip(agent, gold, strict=True) if a != "incomplete" and g == "incomplete")
    return FpFn(
        "completeness",
        "incomplete (>=1 component missing)",
        fp,
        "said missing, but complete",
        fn,
        "said complete, but something was missing",
        len(agent),
    )


def condition_fp_fn(
    agent: Sequence[str], gold: Sequence[str], *, grade_order: Sequence[str] = CONDITION_GRADE_ORDER
) -> FpFn:
    """§21.1 row 4 (ordinal, ranked best=0 .. worst=len-1):
    FP = under-grade (system says WORSE than gold - a safe false alarm);
    FN = over-grade (system says BETTER than gold - the customer gets less than listed).
    Pairs where either side is "uncertain" are excluded (no ordinal rank to compare)."""
    if len(agent) != len(gold):
        raise ValueError("condition_fp_fn: sequences must be the same length")
    rank = {g: i for i, g in enumerate(grade_order)}
    fp = fn = 0
    compared = 0
    for a, g in zip(agent, gold, strict=True):
        if a not in rank or g not in rank:
            continue
        compared += 1
        if rank[a] > rank[g]:  # higher rank index = worse grade
            fp += 1
        elif rank[a] < rank[g]:
            fn += 1
    return FpFn(
        "condition",
        "N/A (ordinal; ranked by CONDITION_GRADE_ORDER)",
        fp,
        "under-grade: system says worse than gold",
        fn,
        "over-grade: system says better than gold (customer gets less than listed)",
        compared,
    )


def condition_error_label(
    agent: str, gold: str, *, grade_order: Sequence[str] = CONDITION_GRADE_ORDER
) -> str:
    """§21.9 `condition_error` column: exact | over_grade_by_N | under_grade_by_N | agent_uncertain."""
    rank = {g: i for i, g in enumerate(grade_order)}
    if agent not in rank or gold not in rank:
        return "agent_uncertain"
    delta = rank[agent] - rank[gold]
    if delta == 0:
        return "exact"
    if delta < 0:
        return f"over_grade_by_{-delta}"
    return f"under_grade_by_{delta}"


@dataclass(frozen=True)
class DispositionConfusion:
    """§21.1 row 5: full confusion matrix plus the two named dangerous cells."""

    matrix: dict[str, dict[str, int]]  # matrix[gold][agent] = count
    n: int
    restock_when_gold_not_restock: int  # dangerous: item relisted despite gold saying otherwise
    dispose_when_gold_recoverable: int  # value loss: destroyed something gold said was recoverable


_RECOVERABLE_ROUTES = frozenset({"restock", "refurbish", "liquidate"})


def disposition_confusion(agent: Sequence[str | None], gold: Sequence[str | None]) -> DispositionConfusion:
    if len(agent) != len(gold):
        raise ValueError("disposition_confusion: sequences must be the same length")
    routes = ("restock", "refurbish", "liquidate", "dispose", "null")
    matrix: dict[str, dict[str, int]] = {g: dict.fromkeys(routes, 0) for g in routes}
    restock_dangerous = 0
    dispose_value_loss = 0
    n = 0
    for a, g in zip(agent, gold, strict=True):
        a_key = a or "null"
        g_key = g or "null"
        matrix[g_key][a_key] += 1
        n += 1
        if a == "restock" and g != "restock":
            restock_dangerous += 1
        if a == "dispose" and g in _RECOVERABLE_ROUTES:
            dispose_value_loss += 1
    return DispositionConfusion(matrix, n, restock_dangerous, dispose_value_loss)


@dataclass(frozen=True)
class FailureModeCounts:
    counts: dict[str, int] = field(default_factory=dict)
    examples: dict[str, tuple[str, ...]] = field(default_factory=dict)  # mode -> up to 3 unit_ids


def tag_failure_mode(unit: EvaluatedUnit) -> str | None:
    """Assigns one §21.4 failure-mode tag to a disagreeing unit, or None if agent == gold
    everywhere that matters. Checked in a fixed priority order so a unit with several
    problems still gets exactly one tag - the report's examples section pulls up to 3
    real units per mode, so double-tagging would just hide a mode's own example.

    Out of scope here: `refusal` and `schema_error` apply to a unit whose inspection run
    never produced a decided AgentResult at all (a safety block or a validation failure);
    that population is `observability.metrics.MetricsService.error_rate_by_class`, not a
    disagreement between an AgentResult and gold, so this tagger cannot assign them - a
    caller building `EvaluatedUnit`s from real inspection_runs rows should tag those two
    modes itself, from `error_class`, before calling this function.
    """
    a, g, meta = unit.agent, unit.gold, unit.meta

    if a.identity != g.identity:
        if meta.blur == "slight" or meta.blur == "heavy":
            return "blur"
        if meta.lighting == "poor":
            return "poor_lighting"
        if a.identity == "no" and g.identity == "yes":
            return "wrong_sku_similar_product"
        return "barcode_failure"

    if a.completeness != g.completeness:
        if set(a.parts_missing) - set(g.parts_missing):
            return "false_missing_component"
        return "accessory_not_visible"

    condition_err = condition_error_label(a.condition, g.condition)
    if condition_err not in ("exact", "agent_uncertain"):
        if condition_err.startswith("over_grade"):
            return "overgrade"
        if condition_err.startswith("under_grade"):
            return "undergrade"
    if condition_err == "agent_uncertain" and g.condition != "uncertain":
        return "condition_ambiguity"

    if a.disposition != g.disposition:
        return "wrong_disposition"

    if a.requires_review and "policy_mismatch" in a.uncertainty_reasons:
        return "policy_mismatch"

    return None


def tag_failure_modes(units: Sequence[EvaluatedUnit]) -> tuple[list[EvaluatedUnit], FailureModeCounts]:
    """Returns the units with `failure_mode` filled in, plus the aggregate counts/examples
    the report renders (§21.4: 1-3 concrete unit examples per mode)."""
    tagged: list[EvaluatedUnit] = []
    counter: Counter[str] = Counter()
    examples: dict[str, list[str]] = {}
    for unit in units:
        mode = tag_failure_mode(unit)
        if mode is not None:
            if mode not in FAILURE_MODES:
                raise ValueError(f"tag_failure_mode produced an unknown mode {mode!r}")
            counter[mode] += 1
            examples.setdefault(mode, [])
            if len(examples[mode]) < 3:
                examples[mode].append(unit.meta.unit_id)
        tagged.append(
            EvaluatedUnit(
                meta=unit.meta,
                human_a=unit.human_a,
                human_b=unit.human_b,
                gold=unit.gold,
                agent=unit.agent,
                failure_mode=mode,
                notes=unit.notes,
            )
        )
    return tagged, FailureModeCounts(
        counts=dict(counter), examples={k: tuple(v) for k, v in examples.items()}
    )
