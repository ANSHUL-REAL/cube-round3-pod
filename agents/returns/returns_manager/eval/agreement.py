"""Agreement statistics (§21.3): Cohen's kappa (nominal), weighted quadratic kappa
(ordinal, for `condition`), raw % agreement, and 95% bootstrap confidence intervals
(1000 resamples, seeded) for every headline number.

Computed for four pairings: labeller A vs B, model vs adjudicated gold, model vs each
labeller, model vs audit model. Callers pick the pairing by choosing which two label
lists to pass in; this module only computes the statistic.
"""

from __future__ import annotations

import random
import warnings
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from sklearn.exceptions import UndefinedMetricWarning  # type: ignore[import-untyped]
from sklearn.metrics import cohen_kappa_score  # type: ignore[import-untyped]

DEFAULT_BOOTSTRAP_RESAMPLES = 1000
DEFAULT_SEED = 20260925  # fixed so a CI is reproducible run to run, not a moving target


@dataclass(frozen=True)
class AgreementResult:
    """A single headline agreement number, always with its own n and CI (§21.3)."""

    statistic: str  # "cohen_kappa" | "weighted_kappa_quadratic" | "raw_agreement"
    value: float
    n: int
    ci_low: float
    ci_high: float
    method: str


def raw_agreement(a: Sequence[str], b: Sequence[str]) -> float:
    """Fraction of positions where a[i] == b[i]. Undefined (raises) on empty input."""
    if len(a) != len(b):
        raise ValueError("raw_agreement: sequences must be the same length")
    if not a:
        raise ValueError("raw_agreement: at least one label pair is required")
    return sum(1 for x, y in zip(a, b, strict=True) if x == y) / len(a)


def cohen_kappa_nominal(a: Sequence[str], b: Sequence[str]) -> float:
    """§21.3: unweighted Cohen's kappa for a nominal check (identity, completeness, ...)."""
    return float(cohen_kappa_score(list(a), list(b)))


def weighted_kappa_ordinal(a: Sequence[str], b: Sequence[str], labels_order: Sequence[str]) -> float:
    """§21.3: quadratic-weighted Cohen's kappa for an ordinal check (condition grade).

    `labels_order` fixes the ordinal scale (best..worst or worst..best - direction does
    not matter to the quadratic weighting, only relative distance does) so the weighting
    reflects "how far apart", not alphabetical order.
    """
    return float(cohen_kappa_score(list(a), list(b), labels=list(labels_order), weights="quadratic"))


def bootstrap_ci(
    statistic_fn: Callable[[Sequence[str], Sequence[str]], float],
    a: Sequence[str],
    b: Sequence[str],
    *,
    resamples: int = DEFAULT_BOOTSTRAP_RESAMPLES,
    seed: int = DEFAULT_SEED,
    confidence: float = 0.95,
) -> tuple[float, float]:
    """95% bootstrap CI (§21.3): resample paired (a[i], b[i]) with replacement `resamples`
    times, recompute `statistic_fn`, take the percentile interval. Seeded for
    reproducibility; with n ~= 50 the interval is wide, and that width is the point -
    never hide it behind a single point estimate.

    Returns `(nan, nan)`, not a raise, when the statistic is undefined for every single
    resample - which happens whenever `a` and `b` are both a single constant class (e.g.
    two labellers who agreed on every unit for this check). Total agreement is a real,
    good outcome that must still produce a report row, not a crash.
    """
    n = len(a)
    if n != len(b):
        raise ValueError("bootstrap_ci: sequences must be the same length")
    if n == 0:
        raise ValueError("bootstrap_ci: at least one label pair is required")
    rng = random.Random(seed)  # noqa: S311 - statistical resampling, not cryptography
    values: list[float] = []
    with warnings.catch_warnings():
        # A resample that happens to share only one label is a legitimate, expected
        # outcome of resampling small n, not a bug: sklearn returns nan for it (caught
        # below), and warns every time it does. 1000 resamples would otherwise print
        # 1000 warnings for something already handled.
        warnings.simplefilter("ignore", category=UndefinedMetricWarning)
        warnings.simplefilter("ignore", category=UserWarning)
        for _ in range(resamples):
            idx = [rng.randrange(n) for _ in range(n)]
            ra = [a[i] for i in idx]
            rb = [b[i] for i in idx]
            try:
                values.append(statistic_fn(ra, rb))
            except ValueError:
                # A resample with a single shared class is undefined for kappa; skip it
                # rather than let one bad resample crash the whole CI (sklearn returns
                # nan for this case rather than raising, so this branch is defensive,
                # not the common path).
                continue
    values = [v for v in values if v == v]  # drop NaNs (sklearn's undefined-kappa case)
    if not values:
        return float("nan"), float("nan")
    values.sort()
    lo_idx = int((1 - confidence) / 2 * len(values))
    hi_idx = int((1 + confidence) / 2 * len(values)) - 1
    hi_idx = min(hi_idx, len(values) - 1)
    return values[lo_idx], values[hi_idx]


_UNDEFINED_SUFFIX = " (undefined: only one class present in this pairing - total agreement)"


def _kappa_method(base: str, value: float) -> str:
    return base + (_UNDEFINED_SUFFIX if value != value else "")  # value != value <=> NaN


def agreement_report(
    a: Sequence[str],
    b: Sequence[str],
    *,
    ordinal_labels: Sequence[str] | None = None,
    resamples: int = DEFAULT_BOOTSTRAP_RESAMPLES,
    seed: int = DEFAULT_SEED,
) -> dict[str, AgreementResult]:
    """All three §21.3 statistics for one pairing, each with its own bootstrap CI.

    `ordinal_labels`: pass the ordinal scale (e.g. CONDITION_GRADE_ORDER) to also compute
    weighted quadratic kappa; omit it for a purely nominal check. Kappa is mathematically
    undefined (reported as NaN, not an error) when both sequences are a single constant
    class - i.e. every rater agreed on every unit for that check, a good, real outcome.
    """
    n = len(a)
    out: dict[str, AgreementResult] = {}

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", category=UndefinedMetricWarning)
        warnings.simplefilter("ignore", category=UserWarning)

        raw_lo, raw_hi = bootstrap_ci(raw_agreement, a, b, resamples=resamples, seed=seed)
        out["raw_agreement"] = AgreementResult(
            "raw_agreement", raw_agreement(a, b), n, raw_lo, raw_hi, "fraction exact match; 95% bootstrap CI"
        )

        kappa_value = cohen_kappa_nominal(a, b)
        kappa_lo, kappa_hi = bootstrap_ci(cohen_kappa_nominal, a, b, resamples=resamples, seed=seed)
        out["cohen_kappa"] = AgreementResult(
            "cohen_kappa",
            kappa_value,
            n,
            kappa_lo,
            kappa_hi,
            _kappa_method(
                "sklearn.metrics.cohen_kappa_score; 95% bootstrap CI, 1000 resamples, seeded", kappa_value
            ),
        )

        if ordinal_labels is not None:

            def _wk(x: Sequence[str], y: Sequence[str]) -> float:
                return weighted_kappa_ordinal(x, y, ordinal_labels)

            wk_value = _wk(a, b)
            wk_lo, wk_hi = bootstrap_ci(_wk, a, b, resamples=resamples, seed=seed)
            out["weighted_kappa_quadratic"] = AgreementResult(
                "weighted_kappa_quadratic",
                wk_value,
                n,
                wk_lo,
                wk_hi,
                _kappa_method(
                    "sklearn.metrics.cohen_kappa_score(weights='quadratic'); 95% bootstrap CI, "
                    "1000 resamples, seeded",
                    wk_value,
                ),
            )

    return out
