"""Claim signals for Recovery Manager (§12.5). Deterministic; each cites its basis.

Recovery needs to tell "the evidence contradicts the charge" (`no`) from "the evidence is silent"
(`uncertain`), so `uncertain` is never collapsed into `no`. Damage signals are observations, not attribution:
who caused the damage is not observable in photos.
"""

from __future__ import annotations

from returns_manager.judgment.types import (
    ClaimSignal,
    ClaimSignals,
    CompletenessResult,
    ConditionResult,
    FusedIdentity,
    UnitPresenceResult,
)


def claim_signals(
    presence: UnitPresenceResult,
    identity: FusedIdentity,
    completeness: CompletenessResult,
    condition: ConditionResult,
    severe_defect_photos: tuple[str, ...] = (),
) -> ClaimSignals:
    if presence.clearly_evidenced:
        not_returned = ClaimSignal("yes", ("unit_presence:" + presence.status,), presence.evidence_photos)
    elif presence.status == "product_present" and identity.identity_match == "yes":
        not_returned = ClaimSignal(
            "no", ("unit_presence:product_present", "identity:yes"), identity.evidence_photos
        )
    else:
        not_returned = ClaimSignal(
            "uncertain", ("unit_presence:" + presence.status,), presence.evidence_photos
        )

    if presence.status == "product_present" and identity.identity_match == "no":
        basis = ("identity:no", *identity.reasons) + (
            (f"actual_sku:{identity.actual_sku}",) if identity.actual_sku else ()
        )
        wrong = ClaimSignal("yes", basis, identity.evidence_photos)
    elif identity.identity_match == "yes":
        wrong = ClaimSignal("no", ("identity:yes", *identity.reasons), identity.evidence_photos)
    else:
        wrong = ClaimSignal("uncertain", (f"identity:{identity.identity_match}", *identity.reasons), ())

    packaging_damaged = condition.packaging_state == "packaging_damaged"
    if (
        condition.max_severity == "severe"
        or "damaged_difficult_to_use" in condition.listing_blockers
        or (packaging_damaged and condition.max_severity in ("moderate", "severe"))
    ):
        damaged = ClaimSignal(
            "yes",
            (f"max_severity:{condition.max_severity}", f"packaging:{condition.packaging_state}"),
            severe_defect_photos,
        )
    elif condition.cosmetic_grade in ("new", "used_like_new") and condition.max_severity == "none":
        damaged = ClaimSignal(
            "no", (f"condition_grade:{condition.cosmetic_grade}", "no_defects_observed"), ()
        )
    else:
        damaged = ClaimSignal("uncertain", (f"max_severity:{condition.max_severity}",), severe_defect_photos)

    missing = tuple(
        c.name if c.missing_quantity == 1 else f"{c.name} x{c.missing_quantity}"
        for c in completeness.components
        if c.status == "missing"
    )
    unsure = tuple(c.name for c in completeness.components if c.status == "uncertain")
    return ClaimSignals(not_returned, wrong, damaged, missing, unsure)
