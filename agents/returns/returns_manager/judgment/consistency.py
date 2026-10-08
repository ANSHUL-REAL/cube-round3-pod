"""Consistency rules C01-C14 (§11.8). Each rule has an ID, a source and an action; every change it makes is
recorded as a validator action. Rules only ever move a verdict towards `uncertain` (or record a flag); they
never invent a pass. C14 flags prompt-injection text and never changes a verdict by itself.
"""

from __future__ import annotations

import re
from typing import cast

from returns_manager.judgment.types import JudgmentContext, ValidationReport
from returns_manager.llm.schemas import (
    JudgmentV1,
    RetakeRequest,
    RetakeTarget,
    Uncertainty,
    UncertaintyArea,
    UncertaintyReason,
)

# Retake target that would resolve each retake-resolvable uncertainty reason (C13). Reasons not listed here
# (e.g. component_not_photo_verifiable, contradictory_evidence) are not solved by another photo.
RETAKE_FOR_REASON: dict[str, RetakeTarget] = {
    "bad_photo": "full_item_front",
    "blur": "full_item_front",
    "occlusion": "full_item_front",
    "missing_angle": "back_view",
    "barcode_unreadable": "barcode_closeup",
    "marking_not_visible": "model_label_closeup",
    "similar_product": "model_label_closeup",
    "insufficient_product_body_evidence": "model_label_closeup",
    "component_area_not_visible": "accessory_area_top_down",
    "packaging_state_unclear": "interior_of_packaging",
    "condition_ambiguous": "damage_closeup",
}
RETAKE_INSTRUCTION: dict[RetakeTarget, str] = {
    "model_label_closeup": "Photograph the model/rating label on the product body up close, in focus.",
    "barcode_closeup": "Photograph the barcode label flat and in focus, filling most of the frame.",
    "accessory_area_top_down": "Open the packaging and photograph all contents from directly above.",
    "interior_of_packaging": "Photograph the inside of the packaging so the seal and contents are visible.",
    "damage_closeup": "Photograph the area of wear or damage up close, with good light.",
    "back_view": "Photograph the back of the item.",
    "side_view": "Photograph the item from the side.",
    "full_item_front": "Photograph the whole item from the front, steady and well lit.",
}
_IMPERATIVE = re.compile(r"\b(ignore|restock|mark\s+as|approve|condition|refund|new)\b", re.IGNORECASE)
_REGIONS_FOR_ABSENCE = {"accessory_area", "interior_of_packaging"}


def apply_consistency(j: JudgmentV1, ctx: JudgmentContext, rep: ValidationReport) -> JudgmentV1:
    j = j.model_copy(deep=True)
    usable = {r.photo for r in j.photo_reports if r.usable}
    absence_capable = any(r.usable and _REGIONS_FOR_ABSENCE & set(r.visible_regions) for r in j.photo_reports)
    packaging = j.condition.packaging_state
    card_components = {c.id: c for c in ctx.card.components}

    def uncertain(area: UncertaintyArea, reason: UncertaintyReason, detail: str) -> None:
        j.uncertainties.append(Uncertainty(area=area, reason=reason, detail=detail[:200]))

    def cites_only_unusable(photos: set[str]) -> bool:
        """True when every cited image is a return photo the model itself called unusable (C10)."""
        return bool(photos) and not (photos & usable) and all(p in ctx.photo_aliases for p in photos)

    # ── components: C01-C04, C10 ───────────────────────────────────────────
    for c in j.completeness.components:
        meta = card_components[c.component_id]
        target = f"component:{c.component_id}"
        if c.status == "missing" and c.visibility != "observed_absent_in_clear_view":
            rep.act("C01", target, "missing", "uncertain", "absence not observed in clear view")
            c.status = "uncertain"
            rep.component_reasons[c.component_id] = "component_area_not_visible"
        if c.status == "missing" and not absence_capable:
            rep.act(
                "C02", target, "missing", "uncertain", "no usable photo shows the accessory/interior area"
            )
            c.status = "uncertain"
            rep.component_reasons[c.component_id] = "component_area_not_visible"
        if c.observed_quantity is not None and c.observed_quantity > meta.quantity:
            if c.status != "present":
                rep.act("C03", target, c.status, "present", "more than the expected quantity observed")
            c.status = "present"
            rep.flag("excess_quantity")
        if not meta.verifiable_by_photo and packaging != "factory_sealed_intact" and c.status != "uncertain":
            rep.act("C04", target, c.status, "uncertain", "not photo-verifiable and packaging not sealed")
            c.status = "uncertain"
            rep.component_reasons[c.component_id] = "component_not_photo_verifiable"
        if c.status != "uncertain" and cites_only_unusable(set(c.photos)):
            rep.act("C10", target, c.status, "uncertain", "evidence only from photos marked unusable")
            c.status = "uncertain"
            rep.component_reasons[c.component_id] = "bad_photo"
        if c.status == "uncertain" and c.component_id not in rep.component_reasons:
            rep.component_reasons[c.component_id] = j.completeness.uncertainty_reason or (
                "component_area_not_visible" if c.visibility == "not_visible" else "visual_conflict"
            )

    # ── unit presence: C10 ──────────────────────────────────────────────────
    presence = j.unit_presence
    if presence.status != "uncertain" and cites_only_unusable({e.photo for e in presence.evidence}):
        rep.act("C10", "unit_presence", presence.status, "uncertain", "evidence only from unusable photos")
        presence.status = "uncertain"
        uncertain("unit_presence", "bad_photo", "Unit presence was supported only by photos marked unusable.")

    # ── identity: C05, C06, C07, C10 ─────────────────────────────────────────
    ident = j.identity
    critical = {f.id: f for f in ctx.card.distinguishing_features if f.importance == "critical"}
    body_critical = {fid for fid, f in critical.items() if f.location == "product_body"}
    if ident.identity_match == "yes":
        if any(fc.result == "mismatch" and fc.feature_id in critical for fc in ident.feature_checks):
            rep.act("C05", "identity", "yes", "uncertain", "a critical feature mismatches")
            ident.identity_match = "uncertain"
            ident.uncertainty_reason = "contradictory_evidence"
            uncertain(
                "identity", "contradictory_evidence", "Identity 'yes' but a critical feature mismatches."
            )
        elif not any(fc.result == "match" and fc.feature_id in body_critical for fc in ident.feature_checks):
            rep.act("C06", "identity", "yes", "uncertain", "no critical product-body feature matched")
            ident.identity_match = "uncertain"
            ident.uncertainty_reason = "insufficient_product_body_evidence"
            uncertain(
                "identity",
                "insufficient_product_body_evidence",
                "No critical feature on the product body was matched; "
                "packaging alone does not prove identity.",
            )
    clearly_absent = presence.status in ("empty_packaging", "non_product_contents") and bool(
        {e.photo for e in presence.evidence} & usable
    )
    if presence.status != "product_present" and ident.identity_match == "yes":
        new = "no" if clearly_absent else "uncertain"
        rep.act("C07", "identity", "yes", new, f"unit presence is {presence.status}")
        ident.identity_match = new  # type: ignore[assignment]
        if new == "uncertain":
            ident.uncertainty_reason = "contradictory_evidence"
    cited = {e.photo for e in ident.evidence} | {o.photo for o in ident.observed_identifiers}
    cited |= {fc.photo for fc in ident.feature_checks if fc.photo}
    if ident.identity_match != "uncertain" and cites_only_unusable(cited):
        rep.act("C10", "identity", ident.identity_match, "uncertain", "evidence only from unusable photos")
        ident.identity_match = "uncertain"
        ident.uncertainty_reason = "bad_photo"
        uncertain("identity", "bad_photo", "Identity evidence came only from photos marked unusable.")

    # ── condition: C08, C09, C10 ─────────────────────────────────────────────
    cond = j.condition
    grade = cond.proposed_grade
    if grade.grade_code == "new" and (
        packaging != "factory_sealed_intact" or cond.signs_of_use != "none_visible" or cond.observations
    ):
        rep.act("C08", "condition_grade", "new", None, "'new' needs sealed packaging, no use and no defects")
        grade.grade_code = None
        grade.uncertainty_reason = "condition_ambiguous"
        uncertain(
            "condition", "condition_ambiguous", "Grade 'New' contradicted by packaging, use or defects."
        )
    if grade.grade_code == "used_like_new" and (cond.observations or cond.signs_of_use != "none_visible"):
        rep.act("C09", "condition_grade", "used_like_new", None, "'Like New' allows no signs of wear")
        grade.grade_code = None
        grade.uncertainty_reason = "condition_ambiguous"
        uncertain("condition", "condition_ambiguous", "Grade 'Like New' contradicted by observed wear.")
    if grade.grade_code is not None and not (usable & set(ctx.photo_aliases)):
        rep.act("C10", "condition_grade", grade.grade_code, None, "no usable return photo")
        grade.grade_code = None
        grade.uncertainty_reason = "bad_photo"
        uncertain("condition", "bad_photo", "No usable return photo to grade the condition from.")

    # ── observed state: C11, C12 ─────────────────────────────────────────────
    state = j.model_observed_state
    # An 'uncertain' observed state asserts nothing, so it cannot contradict the unit-presence verdict.
    if state != "uncertain" and (state == "empty_box") != (presence.status == "empty_packaging"):
        rep.act("C11", "observed_state/unit_presence", f"{state}/{presence.status}", "uncertain", "disagree")
        j.model_observed_state = "uncertain"
        if presence.status != "uncertain":
            presence.status = "uncertain"
            uncertain("unit_presence", "contradictory_evidence", "Observed state and unit presence disagree.")
            if ident.identity_match == "no" and clearly_absent:
                # the C07 'no' rested on clearly evidenced absence, which is no longer established
                ident.identity_match = "uncertain"
                ident.uncertainty_reason = "contradictory_evidence"
    if j.model_observed_state == "factory_sealed" and packaging != "factory_sealed_intact":
        rep.act("C12", "observed_state", "factory_sealed", "uncertain", f"packaging is {packaging}")
        j.model_observed_state = "uncertain"

    # ── C14: instruction-like text in photos → review flag only ──────────────
    if any(_IMPERATIVE.search(t.text) for t in j.untrusted_text_observed):
        rep.flag("injection_attempt_suspected")
        rep.act(
            "C14", "untrusted_text_observed", "", "injection_attempt_suspected", "imperative text in photo"
        )

    # ── C13: every retake-resolvable uncertainty gets a retake request ──────
    reasons: list[str] = [u.reason for u in j.uncertainties]
    if ident.identity_match == "uncertain" and ident.uncertainty_reason:
        reasons.append(ident.uncertainty_reason)
    if grade.grade_code is None and grade.uncertainty_reason:
        reasons.append(grade.uncertainty_reason)
    reasons += [
        rep.component_reasons[c.component_id] for c in j.completeness.components if c.status == "uncertain"
    ]
    have = {r.target for r in j.retake_requests}
    for reason in reasons:
        retake: RetakeTarget | None = RETAKE_FOR_REASON.get(reason)
        if retake is not None and retake not in have:
            j.retake_requests.append(
                RetakeRequest(
                    target=retake,
                    reason=cast(UncertaintyReason, reason),
                    instruction=RETAKE_INSTRUCTION[retake],
                )
            )
            have.add(retake)
            rep.act("C13", "retake_requests", "", retake, f"no retake requested for '{reason}'")
    return j
