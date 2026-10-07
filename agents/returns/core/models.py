"""Pydantic v2 models for reference data (§8)."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ReferenceBaseModel(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class MoneyMinor(ReferenceBaseModel):
    amount_minor: int = Field(..., ge=0, description="Amount in minor units (e.g. paise, cents)")
    currency: str = Field("INR", min_length=3, max_length=3)


class RecoveryRateBp(ReferenceBaseModel):
    restock_new: int = Field(..., ge=0, le=10000)
    restock_used: int = Field(..., ge=0, le=10000)
    refurbish: int = Field(..., ge=0, le=10000)
    liquidate: int = Field(..., ge=0, le=10000)
    dispose: int = Field(0, ge=0, le=10000)


class ProductIdentifiers(ReferenceBaseModel):
    asin: str | None = None
    fnsku: str | None = None
    gtin: str | None = None
    model_numbers: list[str] = Field(default_factory=list)
    barcode_values: list[str] = Field(default_factory=list)


FeatureLocation = Literal["product_body", "packaging", "accessory"]
FeatureImportance = Literal["critical", "supporting"]


class DistinguishingFeature(ReferenceBaseModel):
    id: str = Field(..., pattern=r"^df_[a-z0-9_]+$")
    description: str
    location: FeatureLocation
    importance: FeatureImportance


class SimilarSku(ReferenceBaseModel):
    sku: str
    differs_by: list[str]


class ComponentSource(ReferenceBaseModel):
    type: str
    ref: str | None = None
    retrieved_at: str | None = None


class ProductComponent(ReferenceBaseModel):
    id: str = Field(..., pattern=r"^[a-z0-9_]+$")
    name: str
    quantity: int = Field(1, ge=1)
    essential: bool | None = True
    replaceable: bool | None = False
    verifiable_by_photo: bool = True
    visual_cues: str
    source: ComponentSource


ReferenceImageView = Literal[
    "front",
    "back",
    "left",
    "right",
    "top",
    "bottom",
    "label",
    "packaging",
    "accessories",
    "contents_layout",
]


class ReferenceImage(ReferenceBaseModel):
    id: str = Field(..., pattern=r"^ref_[a-z0-9_]+$")
    view: ReferenceImageView
    path: str
    sha256: str = Field(..., pattern=r"^[0-9a-f]{64}$")


class ProductValue(ReferenceBaseModel):
    synthetic: bool = True
    list_price: MoneyMinor
    recovery_rate_bp: RecoveryRateBp
    refurbish_cost: MoneyMinor


class ProductCardV1(ReferenceBaseModel):
    schema_: Literal["product-card/v1"] = Field("product-card/v1", alias="schema")
    version: str = Field(..., pattern=r"^\d+\.\d+\.\d+$")
    org_id: str = Field(..., pattern=r"^[a-z0-9_]+$")
    sku: str = Field(..., pattern=r"^[A-Z0-9_-]+$")
    identifiers: ProductIdentifiers
    title: str
    brand: str
    category_key: str
    distinguishing_features: list[DistinguishingFeature] = Field(default_factory=list)
    similar_skus: list[SimilarSku] = Field(default_factory=list)
    components: list[ProductComponent] = Field(..., min_length=1)
    reference_images: list[ReferenceImage] = Field(default_factory=list)
    consumable: bool = Field(
        False,
        description="The item itself is consumed in use (e.g. candles). Drives the 'consumable_used' listing "
        "blocker for categories whose policy prohibits used consumables (Home: 'Consumable items where any "
        "part has been used').",
    )
    value: ProductValue
    provenance_notes: str | None = None
    content_sha256: str | None = None

    @field_validator("components")
    @classmethod
    def validate_unique_component_ids(cls, v: list[ProductComponent]) -> list[ProductComponent]:
        ids = [c.id for c in v]
        if len(ids) != len(set(ids)):
            raise ValueError("component ids must be unique within a product card")
        return v


RubricGradeCode = Literal["new", "used_like_new", "used_very_good", "used_good", "used_acceptable"]
VerificationStatus = Literal["verified", "unverified_substitute"]


class RubricGrade(ReferenceBaseModel):
    code: RubricGradeCode
    label: str
    text: str


class UnacceptableCondition(ReferenceBaseModel):
    code: str
    text: str


class ConditionRubricV1(ReferenceBaseModel):
    schema_: Literal["condition-rubric/v1"] = Field("condition-rubric/v1", alias="schema")
    version: str = Field(..., pattern=r"^\d+\.\d+\.\d+$")
    snapshot_id: str
    source_id: str
    source_marketplace: str
    applies_to_marketplace: str
    verification_status: VerificationStatus
    category_key: str
    source_pages: list[int]
    grades: list[RubricGrade]
    unacceptable_conditions: list[UnacceptableCondition]
    content_sha256: str | None = None


PolicySourceType = Literal["amazon_guideline", "business_policy", "assumption"]


class PolicyField(ReferenceBaseModel):
    value: Any
    source_type: PolicySourceType
    source_ref: str


class CategoryPolicyV1(ReferenceBaseModel):
    schema_: Literal["category-policy/v1"] = Field("category-policy/v1", alias="schema")
    version: str = Field(..., pattern=r"^\d+\.\d+\.\d+$")
    policy_id: str
    category_key: str
    source_marketplace: str
    applies_to_marketplace: str
    verification_status: VerificationStatus
    fields: dict[str, PolicyField]
    content_sha256: str | None = None


class SkuCategoryEntry(ReferenceBaseModel):
    category_key: str
    rationale: str
    status: str = "interpretation_needs_verification"


class SkuCategoryMapV1(ReferenceBaseModel):
    schema_: Literal["sku-category-map/v1"] = Field("sku-category-map/v1", alias="schema")
    version: str = Field(..., pattern=r"^\d+\.\d+\.\d+$")
    mapping: dict[str, SkuCategoryEntry]
    content_sha256: str | None = None


class DispositionParamsV1(ReferenceBaseModel):
    schema_: Literal["disposition-params/v1"] = Field("disposition-params/v1", alias="schema")
    version: str = Field(..., pattern=r"^\d+\.\d+\.\d+$")
    restock_used_grades: list[str]
    refurbish_min_net_gain: MoneyMinor
    dispose_max_salvage: MoneyMinor
    high_value_threshold: MoneyMinor
    content_sha256: str | None = None


class QualityGateV1(ReferenceBaseModel):
    schema_: Literal["quality-gate/v1"] = Field("quality-gate/v1", alias="schema")
    version: str = Field(..., pattern=r"^\d+\.\d+\.\d+$")
    calibration_notes: str
    calibrated_at: str
    calibrated_by: str
    thresholds: dict[str, Any]
    content_sha256: str | None = None


class ModelPricingV1(ReferenceBaseModel):
    schema_: Literal["model-pricing/v1"] = Field("model-pricing/v1", alias="schema")
    version: str = Field(..., pattern=r"^\d+\.\d+\.\d+$")
    retrieved_at: str
    source: str
    currency: str = "USD"
    account_tier: str = "free"
    per_million_tokens: dict[str, dict[str, Any]]
    free_tier: dict[str, Any]
    content_sha256: str | None = None


class FxV1(ReferenceBaseModel):
    schema_: Literal["fx/v1"] = Field("fx/v1", alias="schema")
    version: str = Field(..., pattern=r"^\d+\.\d+\.\d+$")
    usd_inr: str
    as_of: str
    source: str
    note: str
    content_sha256: str | None = None


class SourceDocument(ReferenceBaseModel):
    id: str
    title: str
    url: str
    retrieved_at: str
    sha256_of_downloaded_bytes: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    pages: int = Field(..., gt=0)
    license_note: str


class SourcesV1(ReferenceBaseModel):
    schema_: Literal["sources/v1"] = Field("sources/v1", alias="schema")
    version: str = Field(..., pattern=r"^\d+\.\d+\.\d+$")
    sources: list[SourceDocument]
    content_sha256: str | None = None


# reference/_schemas/<name>.schema.json is generated from these models (§8.1: JSON Schema AND Pydantic model).
SCHEMA_FILES: dict[str, type[BaseModel]] = {
    "category-policy": CategoryPolicyV1,
    "condition-rubric": ConditionRubricV1,
    "disposition-params": DispositionParamsV1,
    "fx": FxV1,
    "model-pricing": ModelPricingV1,
    "product-card": ProductCardV1,
    "quality-gate": QualityGateV1,
    "sku-category-map": SkuCategoryMapV1,
    "sources": SourcesV1,
}


def reference_json_schema(name: str) -> dict[str, Any]:
    return SCHEMA_FILES[name].model_json_schema(by_alias=True, mode="validation")
