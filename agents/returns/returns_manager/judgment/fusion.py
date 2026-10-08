"""Identity fusion (§11.9): barcode evidence + the model's (validated) identity verdict → the final identity.

Packaging identity is not product identity: a barcode that matches the ordered SKU never, on its own, makes
the identity `yes`; it needs the model's product-body evidence too. Every table row is a branch below
(tested one by one). Two cases the table does not cover are resolved conservatively and recorded as reasons:
- barcodes for both the ordered and another SKU decoded (`conflicting`) → `uncertain`/`conflict`;
- a model `no` without a critical product-body mismatch → `uncertain`/`weak` (the table only allows `no`
  with such a mismatch).
The table's "conflict-free" strength is reported as `weak` because the contract's strengths are
strong|moderate|weak|conflict (§14.2b).
"""

from __future__ import annotations

from returns_manager.judgment.types import (
    BarcodeStatus,
    FusedIdentity,
    JudgmentContext,
    UnitPresenceResult,
    to_bp,
)
from returns_manager.llm.schemas import JudgmentV1
from returns_manager.reference.models import ProductCardV1


def _codes(card: ProductCardV1) -> set[str]:
    ids = card.identifiers
    values = {*ids.barcode_values, ids.fnsku or "", ids.gtin or "", ids.asin or ""}
    return {v.strip().upper() for v in values if v and v.strip()}


def barcode_status(ctx: JudgmentContext) -> tuple[BarcodeStatus, str | None]:
    """Status over all decodes of all return photos, plus the other SKU a code maps to (if any)."""
    if not ctx.barcodes:
        return "none_decoded", None
    ordered = _codes(ctx.card)
    others = {sku: _codes(card) for sku, card in ctx.other_cards.items() if sku != ctx.ordered_sku}
    hit_ordered = False
    other_hits: set[str] = set()
    for decode in ctx.barcodes:
        value = decode.value.strip().upper()
        if value in ordered:
            hit_ordered = True
        other_hits |= {sku for sku, codes in others.items() if value in codes}
    if hit_ordered and other_hits:
        return "conflicting", None
    if hit_ordered:
        return "matches_ordered", None
    if len(other_hits) == 1:
        return "matches_other_sku", next(iter(other_hits))
    if other_hits:
        return "conflicting", None
    return "unknown_code", None


def unit_presence(j: JudgmentV1) -> UnitPresenceResult:
    usable = {r.photo for r in j.photo_reports if r.usable}
    photos = tuple(dict.fromkeys(e.photo for e in j.unit_presence.evidence))
    clearly = j.unit_presence.status in ("empty_packaging", "non_product_contents") and bool(
        set(photos) & usable
    )
    return UnitPresenceResult(
        status=j.unit_presence.status, clearly_evidenced=clearly, evidence_photos=photos
    )


def fuse_identity(j: JudgmentV1, ctx: JudgmentContext) -> FusedIdentity:
    ident = j.identity
    presence = unit_presence(j)
    status, other_sku = barcode_status(ctx)
    critical = {f.id: f for f in ctx.card.distinguishing_features if f.importance == "critical"}
    body = {fid for fid, f in critical.items() if f.location == "product_body"}
    body_matches = sum(1 for fc in ident.feature_checks if fc.result == "match" and fc.feature_id in body)
    body_mismatches = sum(
        1 for fc in ident.feature_checks if fc.result == "mismatch" and fc.feature_id in body
    )
    flags: list[str] = list(ident.risk_flags)
    if status == "unknown_code":
        flags.append("unknown_barcode")
    photos = tuple(
        dict.fromkeys(
            [e.photo for e in ident.evidence] + [fc.photo for fc in ident.feature_checks if fc.photo]
        )
    )
    conf = to_bp(ident.confidence)
    model = ident.identity_match

    def out(
        match: str, strength: str, reason: str, actual: str | None = None, extra: str | None = None
    ) -> FusedIdentity:
        f = [*flags, extra] if extra and extra not in flags else flags
        return FusedIdentity(
            identity_match=match,  # type: ignore[arg-type]
            strength=strength,  # type: ignore[arg-type]
            barcode_status=status,
            risk_flags=tuple(dict.fromkeys(f)),
            reasons=(reason,),
            actual_sku=actual,
            confidence_bp=conf,
            evidence_photos=photos,
        )

    # Row 1: nothing (or not the product) came back, clearly evidenced.
    if presence.clearly_evidenced:
        return out("no", "strong", "item_not_present_clearly_evidenced")
    if status == "conflicting":
        return out("uncertain", "conflict", "barcodes_for_different_skus_decoded")
    if model == "uncertain":  # last row: any barcode + model uncertain
        if status == "matches_ordered":
            return out("uncertain", "weak", "packaging_barcode_matches_but_product_body_unverified")
        return out("uncertain", "weak", ident.uncertainty_reason or "model_uncertain")
    if status == "matches_ordered":
        if model == "yes":
            return out("yes", "strong", "barcode_matches_and_product_body_features_match")
        return out(
            "uncertain",
            "conflict",
            "barcode_matches_ordered_but_product_differs",
            extra="possible_product_swap",
        )
    if status == "matches_other_sku":
        if model == "no" and ident.likely_actual_sku in (other_sku, None):
            return out("no", "strong", "barcode_identifies_another_catalogued_sku", actual=other_sku)
        if model == "no":
            return out("uncertain", "conflict", "barcode_and_model_name_different_other_skus")
        return out("uncertain", "conflict", "barcode_identifies_another_sku_but_model_says_match")
    # unknown_code / none_decoded
    if model == "yes":
        if body_matches >= 2:
            return out("yes", "moderate", "two_or_more_critical_product_body_features_match")
        return out("uncertain", "weak", "insufficient_product_body_evidence")
    if body_mismatches >= 1:
        return out("no", "moderate", "critical_product_body_feature_mismatch", actual=ident.likely_actual_sku)
    return out("uncertain", "weak", "model_no_without_critical_product_body_mismatch")
