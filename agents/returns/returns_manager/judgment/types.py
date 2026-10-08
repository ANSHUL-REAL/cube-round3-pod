"""Data carried through the deterministic pipeline after the model (§11.7). Pure data, no I/O."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Literal

from returns_manager.llm.schemas import GradeCode, IdentityMatch, UnitPresenceStatus
from returns_manager.reference.models import (
    CategoryPolicyV1,
    ConditionRubricV1,
    DispositionParamsV1,
    ProductCardV1,
)

Strength = Literal["strong", "moderate", "weak", "conflict"]
BarcodeStatus = Literal["matches_ordered", "matches_other_sku", "unknown_code", "none_decoded", "conflicting"]
Tri = Literal["yes", "no", "uncertain"]
CompletenessStatus = Literal["complete", "incomplete", "uncertain"]
Blocker = Literal[
    "essential_component_missing",
    "damaged_difficult_to_use",
    "not_clean",
    "functional_test_required",
    "category_new_only_opened",
    "consumable_used",
]
Route = Literal["restock", "refurbish", "liquidate", "dispose"]


def to_bp(confidence: float) -> int:
    """Model confidence (0..1 float) → integer basis points, half-even (no floats in hashed payloads, §7)."""
    return int((Decimal(str(confidence)) * 10000).quantize(Decimal(1), rounding=ROUND_HALF_EVEN))


@dataclass(frozen=True)
class BarcodeDecode:
    photo: str  # alias P1..P3
    value: str
    format: str


@dataclass(frozen=True)
class EffectivePolicy:
    """Category policy values after org overrides (§8.4); the source types live in the policy file."""

    policy_id: str
    version: str
    content_sha256: str
    org_overrides_sha256: str | None
    listing_conditions_allowed: tuple[str, ...]
    consumable_ingestible_or_topical: bool
    consumable_items_where_any_part_used_prohibited: bool
    functional_verification_required_for_used: bool
    opened_item_route: Route
    damaged_item_route: Route

    @property
    def new_only(self) -> bool:
        return tuple(self.listing_conditions_allowed) == ("new",)

    @classmethod
    def from_policy(
        cls,
        policy: CategoryPolicyV1,
        overrides: dict[str, object] | None = None,
        overrides_sha: str | None = None,
    ) -> EffectivePolicy:
        values: dict[str, object] = {k: f.value for k, f in policy.fields.items()}
        values.update(overrides or {})

        def flag(key: str) -> bool:
            return bool(values.get(key, False))

        def route(key: str) -> Route:
            v = values.get(key)
            if v not in ("restock", "refurbish", "liquidate", "dispose"):
                raise ValueError(f"policy {policy.policy_id}: {key} must be a route, got {v!r}")
            return v

        allowed = values.get("listing_conditions_allowed", [])
        if not isinstance(allowed, list):
            raise ValueError(f"policy {policy.policy_id}: listing_conditions_allowed must be a list")
        return cls(
            policy_id=policy.policy_id,
            version=policy.version,
            content_sha256=policy.content_sha256 or "",
            org_overrides_sha256=overrides_sha,
            listing_conditions_allowed=tuple(str(x) for x in allowed),
            consumable_ingestible_or_topical=flag("consumable_ingestible_or_topical"),
            consumable_items_where_any_part_used_prohibited=flag(
                "consumable_items_where_any_part_used_prohibited"
            ),
            functional_verification_required_for_used=flag("functional_verification_required_for_used"),
            opened_item_route=route("opened_item_route"),
            damaged_item_route=route("damaged_item_route"),
        )


@dataclass(frozen=True)
class JudgmentContext:
    """Everything the deterministic pipeline checks the model's output against (all sent to the model)."""

    org_id: str
    ordered_sku: str
    card: ProductCardV1
    other_cards: dict[str, ProductCardV1]
    rubric: ConditionRubricV1
    policy: EffectivePolicy
    params: DispositionParamsV1
    photo_aliases: tuple[str, ...]
    reference_aliases: tuple[str, ...]
    crop_aliases: tuple[str, ...] = ()
    barcodes: tuple[BarcodeDecode, ...] = ()
    rubric_text_sent: str = ""

    @property
    def all_aliases(self) -> frozenset[str]:
        return frozenset(self.photo_aliases + self.reference_aliases + self.crop_aliases)


@dataclass(frozen=True)
class ValidatorAction:
    rule_id: str
    target: str
    before: str
    after: str
    reason: str


@dataclass(frozen=True)
class FusedIdentity:
    identity_match: IdentityMatch
    strength: Strength
    barcode_status: BarcodeStatus
    risk_flags: tuple[str, ...]
    reasons: tuple[str, ...]
    actual_sku: str | None
    confidence_bp: int
    evidence_photos: tuple[str, ...]


@dataclass(frozen=True)
class ComponentResult:
    component_id: str
    name: str
    expected: int
    observed: int | None
    status: Literal["present", "missing", "uncertain"]
    essential: bool | None
    replaceable: bool | None
    verifiable_by_photo: bool
    missing_quantity: int
    photos: tuple[str, ...]
    confidence_bp: int
    reason: str | None


@dataclass(frozen=True)
class CompletenessResult:
    status: CompletenessStatus
    components: tuple[ComponentResult, ...]
    essential_missing: tuple[str, ...]
    nonessential_missing: tuple[str, ...]
    uncertain_components: tuple[str, ...]
    essential_uncertain: tuple[str, ...]
    parts_list: str
    parts_missing: str
    parts_uncertain: str
    flags: tuple[str, ...]


@dataclass(frozen=True)
class ConditionResult:
    cosmetic_grade: GradeCode | None
    amazon_condition: str  # the rubric's label, e.g. "Used - Good", or "uncertain"
    listing_blockers: tuple[Blocker, ...]
    blockers_undetermined: tuple[Blocker, ...]
    relistable_as_is: bool | None
    packaging_state: str
    signs_of_use: str
    cleanliness: str
    max_severity: Literal["none", "minor", "moderate", "severe"]
    phrases_matched: tuple[str, ...]
    confidence_bp: int
    uncertainty_reason: str | None
    functional_check: Literal["not_performed"] = "not_performed"


@dataclass(frozen=True)
class ClaimSignal:
    value: Tri
    basis: tuple[str, ...]
    evidence: tuple[str, ...]


@dataclass(frozen=True)
class ClaimSignals:
    item_not_returned: ClaimSignal
    wrong_item_returned: ClaimSignal
    returned_damaged: ClaimSignal
    parts_missing: tuple[str, ...]
    parts_uncertain: tuple[str, ...]


@dataclass(frozen=True)
class UnitPresenceResult:
    status: UnitPresenceStatus
    clearly_evidenced: bool
    evidence_photos: tuple[str, ...]


@dataclass
class ValidationReport:
    actions: list[ValidatorAction] = field(default_factory=list)
    invented_reference_count: int = 0
    invented_quote_count: int = 0
    flags: list[str] = field(default_factory=list)
    component_reasons: dict[str, str] = field(default_factory=dict)

    def act(self, rule_id: str, target: str, before: object, after: object, reason: str) -> None:
        self.actions.append(ValidatorAction(rule_id, target, str(before), str(after), reason))

    def flag(self, name: str) -> None:
        if name not in self.flags:
            self.flags.append(name)
