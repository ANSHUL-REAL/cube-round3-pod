"""Selective-prediction reporting (§21.2): how to score a check that can say `uncertain`.

Report all four together, always - never only the flattering one (RULES.md §6.2, "'It
works well' is not a result"; the prompt's own instruction at §21.2).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

UNCERTAIN = "uncertain"


@dataclass(frozen=True)
class SelectivePredictionReport:
    strict_accuracy: float  # uncertain counts as wrong, even if gold was also uncertain
    coverage: float  # fraction of units the agent decided (did not say uncertain)
    selective_accuracy: float | None  # accuracy on decided units only; None if none decided
    unnecessary_uncertain_rate: float | None  # uncertain when gold was clear; None if gold never clear
    n: int


def selective_prediction_report(
    agent: Sequence[str], gold: Sequence[str], *, uncertain_value: str = UNCERTAIN
) -> SelectivePredictionReport:
    if len(agent) != len(gold):
        raise ValueError("selective_prediction_report: agent and gold must be the same length")
    n = len(agent)
    if n == 0:
        raise ValueError("selective_prediction_report: at least one unit is required")

    pairs = list(zip(agent, gold, strict=True))

    # Strict accuracy: an "uncertain" prediction is never counted as correct, even when
    # gold happens to also be "uncertain" - the agent gets no credit for hedging.
    strict_correct = sum(1 for a, g in pairs if a != uncertain_value and a == g)
    strict_accuracy = strict_correct / n

    decided = [(a, g) for a, g in pairs if a != uncertain_value]
    coverage = len(decided) / n
    selective_accuracy = (sum(1 for a, g in decided if a == g) / len(decided)) if decided else None

    clear_gold = [(a, g) for a, g in pairs if g != uncertain_value]
    unnecessary_uncertain_rate = (
        (sum(1 for a, g in clear_gold if a == uncertain_value) / len(clear_gold)) if clear_gold else None
    )

    return SelectivePredictionReport(
        strict_accuracy=strict_accuracy,
        coverage=coverage,
        selective_accuracy=selective_accuracy,
        unnecessary_uncertain_rate=unnecessary_uncertain_rate,
        n=n,
    )
