"""Threshold sweep for auto-acceptance (§21.5), one check at a time.

For a fixed confidence threshold (basis points), a unit is "auto-decided" when its
confidence is at or above the threshold; everything else goes to human review. Sweeping
the threshold trades review load against the risk of auto-accepting a wrong decision.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

DEFAULT_THRESHOLDS_BP: tuple[int, ...] = tuple(range(0, 10001, 500))


@dataclass(frozen=True)
class ThresholdPoint:
    threshold_bp: int
    auto_decided_rate: float  # fraction of all units this threshold would auto-accept
    false_accept_rate: float | None  # fraction of auto-accepted units that were WRONG
    review_load: float  # 1 - auto_decided_rate
    n_auto_decided: int
    n: int


def sweep(
    confidences_bp: Sequence[int],
    correct: Sequence[bool],
    *,
    thresholds_bp: Sequence[int] = DEFAULT_THRESHOLDS_BP,
) -> list[ThresholdPoint]:
    """§21.5: tabulate auto-decided rate / false-accept rate / review load per threshold."""
    if len(confidences_bp) != len(correct):
        raise ValueError("sweep: confidences_bp and correct must be the same length")
    n = len(confidences_bp)
    if n == 0:
        raise ValueError("sweep: at least one unit is required")

    points: list[ThresholdPoint] = []
    for t in thresholds_bp:
        auto = [ok for conf, ok in zip(confidences_bp, correct, strict=True) if conf >= t]
        n_auto = len(auto)
        false_accepts = sum(1 for ok in auto if not ok)
        points.append(
            ThresholdPoint(
                threshold_bp=t,
                auto_decided_rate=n_auto / n,
                false_accept_rate=(false_accepts / n_auto) if n_auto else None,
                review_load=1 - n_auto / n,
                n_auto_decided=n_auto,
                n=n,
            )
        )
    return points


def choose_operating_point(
    points: Sequence[ThresholdPoint], *, max_false_accept_rate: float
) -> tuple[ThresholdPoint | None, str]:
    """The most permissive threshold (lowest, i.e. highest auto-decided rate / lowest
    review load) whose false-accept rate does not exceed `max_false_accept_rate`.

    Returns `(point_or_None, justification)`. `points` should be sorted by ascending
    threshold (as `sweep()` returns them for the default/typical thresholds_bp).
    """
    candidates = [
        p for p in points if p.false_accept_rate is not None and p.false_accept_rate <= max_false_accept_rate
    ]
    if not candidates:
        return None, (
            f"no threshold in the sweep keeps the false-accept rate at or below "
            f"{max_false_accept_rate:.2%}; every threshold either auto-accepts nothing "
            "or exceeds the risk bar - review the check itself before shipping auto-acceptance"
        )
    chosen = min(candidates, key=lambda p: p.threshold_bp)
    return chosen, (
        f"threshold={chosen.threshold_bp}bp is the lowest (most permissive) threshold in the "
        f"sweep whose false-accept rate ({chosen.false_accept_rate:.2%}) is at or below the "
        f"{max_false_accept_rate:.2%} bar, auto-deciding {chosen.auto_decided_rate:.2%} of units "
        f"(review load {chosen.review_load:.2%})"
    )
