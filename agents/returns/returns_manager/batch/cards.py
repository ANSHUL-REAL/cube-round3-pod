"""Synthesizes an in-memory ProductCardV1 straight from a batch "before" row's own given
data (parts_list, sku, asin, category). Nothing is invented by a model here: every field
comes directly from the seller-supplied CSV or a documented synthetic default (price/
recovery-rate placeholders, same convention as `reference/reference/onboarding.py`'s
draft cards). The card is never written to `reference/products/`; it exists only for the
duration of one batch run.
"""

from __future__ import annotations

from returns_manager.batch.parts import ParsedPart
from returns_manager.reference.models import (
    ComponentSource,
    DistinguishingFeature,
    MoneyMinor,
    ProductCardV1,
    ProductComponent,
    ProductIdentifiers,
    ProductValue,
    RecoveryRateBp,
)

VALID_CATEGORIES = frozenset(
    {"beauty_topical", "electronics", "grocery_ingestible", "home_kitchen", "pet", "toys_games"}
)

DEFAULT_LIST_PRICE_MINOR = 999900  # INR 9,999 - synthetic placeholder, see ProductValue.synthetic
DEFAULT_REFURBISH_COST_MINOR = 50000  # INR 500
DEFAULT_RECOVERY_RATE_BP = RecoveryRateBp(
    restock_new=10000, restock_used=6500, refurbish=5500, liquidate=2000, dispose=0
)


def build_card(
    *,
    org_id: str,
    sku: str,
    asin: str | None,
    category_key: str,
    parts: list[ParsedPart],
    list_price_minor: int = DEFAULT_LIST_PRICE_MINOR,
) -> ProductCardV1:
    src = ComponentSource(type="seller_catalogue", ref=f"batch import: unit before-record for {sku}")
    components = [
        ProductComponent(
            id=p.component_id,
            name=p.name,
            quantity=p.quantity,
            essential=p.essential,
            replaceable=p.replaceable,
            verifiable_by_photo=True,
            visual_cues=f"matches the parts-list entry {p.name!r} for {sku}",
            source=src,
        )
        for p in parts
    ] or [
        ProductComponent(
            id="main_unit",
            name="main unit",
            quantity=1,
            essential=True,
            replaceable=False,
            verifiable_by_photo=True,
            visual_cues=f"item matching the catalogue reference photo for {sku}",
            source=src,
        )
    ]
    return ProductCardV1(
        schema_="product-card/v1",
        version="1.0.0",
        org_id=org_id,
        sku=sku,
        identifiers=ProductIdentifiers(asin=asin or None, barcode_values=[]),
        title=f"{sku} (batch import, no catalogue title supplied)",
        brand="Unknown",
        category_key=category_key,
        distinguishing_features=[
            # judgment.fusion.fuse_identity requires >= 2 matched critical product_body
            # features (or a barcode) before it will ever fuse identity to "yes" - a single
            # feature is always downgraded to "uncertain" regardless of the model's own
            # confidence (§11.9, and see build-log F-021). With no real catalogue features to
            # draw on for a batch-imported SKU, two independent, honestly generic checks
            # (overall look; brand/model markings or shape) are enough to let a genuinely
            # matching photo pair actually confirm identity, without asserting anything more
            # specific than "matches the reference photo" for either one.
            DistinguishingFeature(
                id="df_catalog_appearance",
                description="Overall appearance (shape, colour, proportions) matching the reference "
                "(before-sale) photo supplied for this SKU",
                location="product_body",
                importance="critical",
            ),
            DistinguishingFeature(
                id="df_catalog_markings",
                description="Brand name, model markings or other printed/embossed text matching the "
                "reference (before-sale) photo supplied for this SKU",
                location="product_body",
                importance="critical",
            ),
        ],
        similar_skus=[],
        components=components,
        reference_images=[],
        consumable=False,
        value=ProductValue(
            synthetic=True,
            list_price=MoneyMinor(amount_minor=list_price_minor, currency="INR"),
            recovery_rate_bp=DEFAULT_RECOVERY_RATE_BP,
            refurbish_cost=MoneyMinor(amount_minor=DEFAULT_REFURBISH_COST_MINOR, currency="INR"),
        ),
        provenance_notes=(
            f"Synthesized for a standalone batch run from the seller-supplied before-record for {sku}; "
            "parts_list is transcribed as given, essential/replaceable classification follows the "
            "first-part-is-root convention. Not published to reference/products/. Price and recovery-rate "
            "figures are synthetic placeholders (no real pricing was supplied)."
        ),
    )
