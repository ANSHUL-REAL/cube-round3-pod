"""`eval seal` (§21.0, §19 "eval seal quota checks ... are tested"):

- Refuses fewer than `MIN_SEALED_UNITS` (50) units unless `--dev-mini`.
- Refuses to seal if any coverage quota is missed (10 official scenarios >= 3 each;
  lighting=poor >= 10; angle=oblique >= 10; blur=slight >= 8;
  ambiguity=genuinely_ambiguous >= 8; product unseen in dev >= 15), unless `--dev-mini`.
- Rejects any unit whose product AND photos already appear in `fixtures/` (held-out
  means held out; a unit fixtures already saw twice is not a fair test).
- Hashes the sealed set (RFC 8785 JCS + SHA-256, same canonicalizer as everything else
  hashed in this repo) so a later `eval run` can detect the set changed under it.
- `labels_before_run_guard`: separately, before `eval run` touches a unit, both
  independent human labels (§8.10) must already be on file - the agent must never see
  gold or run before both are in, and this function is the check `eval run` calls first.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from returns_manager.canonical.jcs import canonical_bytes
from returns_manager.eval.models import OFFICIAL_SCENARIOS, UnitMeta

MIN_SEALED_UNITS = 50


class SealRefused(ValueError):
    """Raised instead of producing a sealed set that doesn't meet §21.0's bar."""


@dataclass(frozen=True)
class CoverageQuota:
    name: str
    required: int
    actual: int

    @property
    def met(self) -> bool:
        return self.actual >= self.required


@dataclass(frozen=True)
class QuotaCheckResult:
    quotas: tuple[CoverageQuota, ...]
    all_met: bool
    missing_summary: str


def check_coverage_quotas(units: Sequence[UnitMeta]) -> QuotaCheckResult:
    """§21.0's six coverage quotas. A unit can count toward several at once."""
    quotas: list[CoverageQuota] = []
    for scenario in OFFICIAL_SCENARIOS:
        actual = sum(1 for u in units if scenario in u.scenario_codes)
        quotas.append(CoverageQuota(f"scenario:{scenario}", 3, actual))
    quotas.append(CoverageQuota("lighting:poor", 10, sum(1 for u in units if u.lighting == "poor")))
    quotas.append(CoverageQuota("angle:oblique", 10, sum(1 for u in units if u.angle == "oblique")))
    quotas.append(CoverageQuota("blur:slight", 8, sum(1 for u in units if u.blur == "slight")))
    quotas.append(
        CoverageQuota(
            "ambiguity:genuinely_ambiguous", 8, sum(1 for u in units if u.ambiguity == "genuinely_ambiguous")
        )
    )
    quotas.append(
        CoverageQuota("product_unseen_in_dev", 15, sum(1 for u in units if not u.product_seen_in_dev))
    )
    missing = [f"{q.name} ({q.actual}/{q.required})" for q in quotas if not q.met]
    return QuotaCheckResult(
        quotas=tuple(quotas),
        all_met=not missing,
        missing_summary="all quotas met" if not missing else "; ".join(missing),
    )


def find_fixture_overlap(
    units: Sequence[UnitMeta],
    unit_sku: dict[str, str],
    unit_photo_hashes: dict[str, frozenset[str]],
    fixture_skus: frozenset[str],
    fixture_photo_hashes: frozenset[str],
) -> tuple[str, ...]:
    """Unit IDs to reject: BOTH the SKU and at least one photo hash already appear in
    `fixtures/` (matching the CLAUDE.md rule verbatim: product *and* photos, not either
    alone - a fixture can reuse a product's card without reusing its actual photographs).
    """
    rejected = []
    for u in units:
        sku = unit_sku.get(u.unit_id)
        photos = unit_photo_hashes.get(u.unit_id, frozenset())
        if sku is not None and sku in fixture_skus and (photos & fixture_photo_hashes):
            rejected.append(u.unit_id)
    return tuple(rejected)


@dataclass(frozen=True)
class SealResult:
    unit_ids: tuple[str, ...]
    dev_mini: bool
    quota_result: QuotaCheckResult
    content_sha256: str


def _sealed_set_hash(unit_ids: Sequence[str]) -> str:
    import hashlib

    payload = {"schema": "rm/eval-seal/v1", "unit_ids": sorted(unit_ids)}
    return hashlib.sha256(canonical_bytes(payload)).hexdigest()


def seal(units: Sequence[UnitMeta], *, dev_mini: bool = False) -> SealResult:
    """Raises `SealRefused` (exit 4-equivalent at the CLI layer) instead of ever
    producing an under-covered or under-sized sealed set - there is no "sealed but
    with a warning" outcome."""
    if not units:
        raise SealRefused("no units to seal")

    ids = [u.unit_id for u in units]
    if len(ids) != len(set(ids)):
        raise SealRefused("duplicate unit_id in the candidate set")

    if not dev_mini and len(units) < MIN_SEALED_UNITS:
        raise SealRefused(
            f"only {len(units)} units; need at least {MIN_SEALED_UNITS} unless run with --dev-mini "
            "(dev tooling checks only - never reported as the eval)"
        )

    quota_result = check_coverage_quotas(units)
    if not dev_mini and not quota_result.all_met:
        raise SealRefused(f"coverage quotas not met: {quota_result.missing_summary}")

    return SealResult(
        unit_ids=tuple(ids),
        dev_mini=dev_mini,
        quota_result=quota_result,
        content_sha256=_sealed_set_hash(ids),
    )


def labels_before_run_guard(unit_ids: Sequence[str], labels_present: dict[str, int]) -> tuple[str, ...]:
    """§8.10: two humans label independently before the agent runs. Returns the unit_ids
    `eval run` must refuse to run because fewer than 2 independent labels are on file -
    the agent must never see a unit gold hasn't been locked in for yet."""
    return tuple(u for u in unit_ids if labels_present.get(u, 0) < 2)
