"""Retrospective policy tuning (§21.6): re-run `decide()` over the evaluated units' own
stored `DispositionInputs` under alternative parameters and report the disposition mix and
synthetic recovery deltas. Pure computation over stored results - no model calls, no
quota spent, and nothing about the underlying identity/completeness/condition facts
changes; only the deterministic engine's parameters do.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace

from returns_manager.disposition.engine import DispositionDecision, DispositionInputs, decide


@dataclass(frozen=True)
class PolicyVariant:
    name: str
    apply: Callable[[DispositionInputs], DispositionInputs]


def restock_used_grades_variant(name: str, grades: Sequence[str]) -> PolicyVariant:
    """e.g. `[used_like_new]` vs `[used_like_new, used_very_good, used_good]` (§21.6 example)."""
    grades_t = tuple(grades)
    return PolicyVariant(name, lambda inp: replace(inp, restock_used_grades=grades_t))


def refurbish_min_net_gain_variant(name: str, min_net_gain_minor: int) -> PolicyVariant:
    """A different refurbish margin (§21.6 example: "different refurbish margins")."""
    return PolicyVariant(name, lambda inp: replace(inp, refurbish_min_net_gain_minor=min_net_gain_minor))


def _expected_recovery_for_route(decision: DispositionDecision) -> int:
    if decision.recommended_disposition is None:
        return 0
    return dict(decision.expected_recovery_minor).get(decision.recommended_disposition, 0)


@dataclass(frozen=True)
class PolicyTuningResult:
    variant_name: str
    n: int
    disposition_mix: dict[str, int]  # route (or "null") -> count
    recovery_delta_total_minor: int  # sum(variant's chosen-route recovery - baseline's)
    recovery_delta_mean_minor: float
    method: str


def rerun_under_variant(
    stored_inputs: Sequence[DispositionInputs],
    baseline_decisions: Sequence[DispositionDecision],
    rules_version: str,
    variant: PolicyVariant,
) -> PolicyTuningResult:
    """Re-decide every unit's stored inputs under `variant`, and diff against the
    baseline decisions that were actually recorded for those same units."""
    if len(stored_inputs) != len(baseline_decisions):
        raise ValueError("rerun_under_variant: stored_inputs and baseline_decisions must align 1:1")
    n = len(stored_inputs)
    if n == 0:
        raise ValueError("rerun_under_variant: at least one unit is required")

    mix: Counter[str] = Counter()
    delta_total = 0
    for inp, baseline in zip(stored_inputs, baseline_decisions, strict=True):
        varied_decision = decide(variant.apply(inp), rules_version)
        mix[varied_decision.recommended_disposition or "null"] += 1
        delta_total += _expected_recovery_for_route(varied_decision) - _expected_recovery_for_route(baseline)

    return PolicyTuningResult(
        variant_name=variant.name,
        n=n,
        disposition_mix=dict(mix),
        recovery_delta_total_minor=delta_total,
        recovery_delta_mean_minor=delta_total / n,
        method=(
            f"decide() re-run per unit under variant {variant.name!r}; recovery delta = "
            "sum(variant's expected_recovery_minor[its own recommended_disposition] - "
            "baseline's same) over all n units; synthetic values, same as §18.3"
        ),
    )
