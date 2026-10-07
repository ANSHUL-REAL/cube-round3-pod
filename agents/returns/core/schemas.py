"""The Judgment Agent's structured output, `judgment/v1` (§11.4). The Pydantic models are the source of truth.

Rules: no recursion, no free-form maps, `additionalProperties: false` everywhere, closed enums, and **no
disposition field** (the rules engine decides the disposition, never the model). Numeric ranges and alias
formats are validated here in code; the schema sent to Gemini (`gemini_response_schema()`) is a flattened copy
without the keywords Gemini does not list as supported (e.g. `minimum`, `maxLength`, `pattern`).
"""

from __future__ import annotations

import copy
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

SCHEMA_VERSION = "judgment/v1"

UncertaintyReason = Literal[
    "bad_photo",
    "blur",
    "occlusion",
    "missing_angle",
    "barcode_unreadable",
    "marking_not_visible",
    "ocr_conflict",
    "visual_conflict",
    "similar_product",
    "component_area_not_visible",
    "component_not_photo_verifiable",
    "condition_ambiguous",
    "packaging_state_unclear",
    "reference_insufficient",
    "insufficient_product_body_evidence",
    "contradictory_evidence",
    "other",
]
PhotoView = Literal[
    "front",
    "back",
    "left",
    "right",
    "top",
    "bottom",
    "label",
    "packaging",
    "interior_of_packaging",
    "accessories",
    "contents_layout",
]
VisibleRegion = Literal[
    "product_body", "model_label", "barcode_area", "accessory_area", "interior_of_packaging", "outer_carton"
]
PhotoIssue = Literal["blur", "glare", "occlusion", "out_of_frame", "too_dark", "too_far", "none"]
UnitPresenceStatus = Literal["product_present", "empty_packaging", "non_product_contents", "uncertain"]
IdentityMatch = Literal["yes", "no", "uncertain"]
IdentifierKind = Literal["model_number", "brand", "sku_text", "barcode_text", "other_marking"]
IdentifierLocation = Literal["product_body", "packaging", "accessory", "unknown"]
FeatureResult = Literal["match", "mismatch", "not_visible"]
RiskFlag = Literal[
    "model_mismatch",
    "variant_mismatch",
    "brand_mismatch",
    "label_mismatch",
    "packaging_product_mismatch",
    "possible_product_swap",
    "counterfeit_indicators",
    "foreign_object",
]
Visibility = Literal["observed_present", "observed_absent_in_clear_view", "not_visible", "conflicting"]
ComponentStatus = Literal["present", "missing", "uncertain"]
PackagingState = Literal[
    "factory_sealed_intact",
    "opened_packaging_intact",
    "packaging_damaged",
    "packaging_missing",
    "not_visible",
]
DefectType = Literal[
    "scratch",
    "scuff",
    "dent",
    "crack",
    "chip",
    "tear",
    "stain",
    "discoloration",
    "deformation",
    "residue_or_dirt",
    "signs_of_use",
    "label_damage",
    "water_damage",
    "burn",
    "other",
]
Severity = Literal["minor", "moderate", "severe"]
SignsOfUse = Literal["none_visible", "light", "moderate", "heavy", "not_determinable"]
Cleanliness = Literal["clean", "dirty", "not_determinable"]
GradeCode = Literal["new", "used_like_new", "used_very_good", "used_good", "used_acceptable"]
ObservedState = Literal[
    "factory_sealed", "opened_unused", "signs_of_use", "damaged", "empty_box", "uncertain"
]
RetakeTarget = Literal[
    "model_label_closeup",
    "barcode_closeup",
    "accessory_area_top_down",
    "interior_of_packaging",
    "damage_closeup",
    "back_view",
    "side_view",
    "full_item_front",
]
UncertaintyArea = Literal["identity", "completeness", "condition", "unit_presence"]

GRADE_ORDER: tuple[GradeCode, ...] = (
    "new",
    "used_like_new",
    "used_very_good",
    "used_good",
    "used_acceptable",
)


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _check_box(v: list[int] | None) -> list[int] | None:
    if v is None:
        return None
    if len(v) != 4 or any(not 0 <= c <= 1000 for c in v):
        raise ValueError("box_2d must be [ymin, xmin, ymax, xmax] with integers 0-1000")
    ymin, xmin, ymax, xmax = v
    if ymin >= ymax or xmin >= xmax:
        raise ValueError("box_2d must have ymin < ymax and xmin < xmax")
    return v


def _check_confidence(v: float) -> float:
    if not 0.0 <= v <= 1.0:
        raise ValueError("confidence must be between 0 and 1")
    return v


class EvidenceRef(_Strict):
    photo: str = Field(description="Alias of a provided image: P1-P3, R1.., C1..")
    box_2d: list[int] | None = Field(
        default=None, min_length=4, max_length=4, description="[ymin, xmin, ymax, xmax], 0-1000"
    )
    observation: str = Field(max_length=200, description="What is visible, at most 200 characters")

    _box = field_validator("box_2d")(_check_box)


class PhotoReport(_Strict):
    photo: str
    usable: bool
    views: list[PhotoView]
    visible_regions: list[VisibleRegion]
    issues: list[PhotoIssue]


class UnitPresence(_Strict):
    status: UnitPresenceStatus
    evidence: list[EvidenceRef]


class ObservedIdentifier(_Strict):
    kind: IdentifierKind
    value: str = Field(max_length=120)
    photo: str
    location: IdentifierLocation


class FeatureCheck(_Strict):
    feature_id: str = Field(description="A df_* id from the product card")
    result: FeatureResult
    photo: str | None


class Identity(_Strict):
    identity_match: IdentityMatch
    observed_identifiers: list[ObservedIdentifier]
    feature_checks: list[FeatureCheck]
    risk_flags: list[RiskFlag]
    likely_actual_sku: str | None = Field(description="Only a SKU listed in similar_skus, else null")
    uncertainty_reason: UncertaintyReason | None
    confidence: float
    evidence: list[EvidenceRef]

    _conf = field_validator("confidence")(_check_confidence)


class ComponentObservation(_Strict):
    component_id: str = Field(description="A component id from the parts list")
    observed_quantity: int | None
    visibility: Visibility
    status: ComponentStatus
    photos: list[str]
    confidence: float

    _conf = field_validator("confidence")(_check_confidence)

    @field_validator("observed_quantity")
    @classmethod
    def _non_negative(cls, v: int | None) -> int | None:
        if v is not None and v < 0:
            raise ValueError("observed_quantity must be >= 0")
        return v


class UnexpectedItem(_Strict):
    description: str = Field(max_length=200)
    photo: str


class Completeness(_Strict):
    components: list[ComponentObservation]
    unexpected_items: list[UnexpectedItem]
    uncertainty_reason: UncertaintyReason | None


class DefectObservation(_Strict):
    defect_type: DefectType
    severity: Severity
    location_note: str = Field(max_length=200)
    photo: str
    box_2d: list[int] | None = Field(default=None, min_length=4, max_length=4)
    confidence: float

    _box = field_validator("box_2d")(_check_box)
    _conf = field_validator("confidence")(_check_confidence)


class ProposedGrade(_Strict):
    grade_code: GradeCode | None
    rubric_phrases_matched: list[str] = Field(description="Verbatim substrings of the provided rubric text")
    uncertainty_reason: UncertaintyReason | None
    confidence: float

    _conf = field_validator("confidence")(_check_confidence)


class Condition(_Strict):
    packaging_state: PackagingState
    observations: list[DefectObservation]
    signs_of_use: SignsOfUse
    cleanliness: Cleanliness
    outer_shipping_damage_observed: bool | None
    functional_check: Literal["not_performed"]
    proposed_grade: ProposedGrade


class RetakeRequest(_Strict):
    target: RetakeTarget
    reason: UncertaintyReason
    instruction: str = Field(max_length=200)


class Uncertainty(_Strict):
    area: UncertaintyArea
    reason: UncertaintyReason
    detail: str = Field(max_length=200)


class UntrustedText(_Strict):
    photo: str
    text: str = Field(max_length=300)


class JudgmentV1(_Strict):
    schema_version: Literal["judgment/v1"]
    photo_reports: list[PhotoReport]
    unit_presence: UnitPresence
    identity: Identity
    completeness: Completeness
    condition: Condition
    model_observed_state: ObservedState
    retake_requests: list[RetakeRequest]
    uncertainties: list[Uncertainty]
    untrusted_text_observed: list[UntrustedText]


# ── Gemini-compatible schema export ─────────────────────────────────────────

# Keywords the Gemini structured-output docs list as supported (§1.8). Anything else is dropped from the
# exported schema; the Pydantic model above still enforces it on the returned JSON.
_SUPPORTED_KEYS = {
    "type",
    "title",
    "description",
    "enum",
    "format",
    "properties",
    "required",
    "additionalProperties",
    "items",
    "prefixItems",
    "minItems",
    "maxItems",
}


def _resolve(node: Any, defs: dict[str, Any]) -> Any:
    if isinstance(node, list):
        return [_resolve(n, defs) for n in node]
    if not isinstance(node, dict):
        return node
    if "$ref" in node:
        name = node["$ref"].rsplit("/", 1)[-1]
        return _resolve(copy.deepcopy(defs[name]), defs)
    if "anyOf" in node:
        variants = [_resolve(v, defs) for v in node["anyOf"]]
        non_null = [v for v in variants if v.get("type") != "null"]
        if len(non_null) == 1 and len(variants) == 2:
            # Optional[X] → X with "null" added to its type (and to its enum, if any).
            merged = dict(non_null[0])
            base_type = merged.get("type")
            merged["type"] = [base_type, "null"] if isinstance(base_type, str) else base_type
            if "enum" in merged:
                merged["enum"] = [*merged["enum"], None]
            if "description" in node:
                merged["description"] = node["description"]
            return _clean(merged)
        raise ValueError(f"unsupported union in judgment schema: {node}")
    resolved: dict[str, Any] = {}
    for key, value in node.items():
        if key == "properties":  # a map of field name → schema, not a schema itself
            resolved[key] = {name: _resolve(sub, defs) for name, sub in value.items()}
        else:
            resolved[key] = _resolve(value, defs)
    return _clean(resolved)


def _clean(node: dict[str, Any]) -> dict[str, Any]:
    """Keep only supported keywords of an already-resolved schema node (field maps are kept whole)."""
    out: dict[str, Any] = {}
    for key, value in node.items():
        if key == "properties" or key in _SUPPORTED_KEYS:  # field maps are kept whole
            out[key] = value
    if "const" in node:  # Literal with a single value
        out["enum"] = [node["const"]]
        out.setdefault("type", "string")
    if out.get("type") == "object" and "properties" in out:
        # Every property is required and nullable fields are explicit, so the model always emits them.
        out["required"] = list(out["properties"])
    return out


def gemini_response_schema() -> dict[str, Any]:
    """The flattened JSON schema sent as `response_format.schema`: no `$defs`, no unsupported keywords."""
    raw = JudgmentV1.model_json_schema()
    defs = raw.pop("$defs", {})
    schema = _resolve(raw, defs)
    assert isinstance(schema, dict)
    return schema


def schema_depth(node: Any, depth: int = 0) -> int:
    """Nesting depth of objects (for the 'keep it Gemini-friendly' budget)."""
    if isinstance(node, dict):
        here = depth + 1 if node.get("type") == "object" else depth
        children = [schema_depth(v, here) for k, v in node.items() if k in ("properties", "items")]
        nested = [schema_depth(v, here) for v in node.get("properties", {}).values()]
        return max([here, *children, *nested])
    if isinstance(node, list):
        return max((schema_depth(n, depth) for n in node), default=depth)
    return depth
