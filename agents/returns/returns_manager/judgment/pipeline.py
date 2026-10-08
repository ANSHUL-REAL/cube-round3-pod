"""The deterministic pipeline after the model (§11.7), in its required order:

referential validation → consistency rules → identity fusion → completeness arithmetic → condition gates →
escalation decision → claim signals → disposition engine → review and sign-off flags.
The result also carries the fixed-contract checks (§14.2), each with a PASS/FAIL/UNCERTAIN verdict, integer
confidence (basis points) and a plain-English detail that cites the photo slots.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from returns_manager.disposition.engine import DispositionDecision, DispositionInputs, MissingPart, decide
from returns_manager.judgment.claims import claim_signals
from returns_manager.judgment.completeness import compute_completeness
from returns_manager.judgment.consistency import apply_consistency
from returns_manager.judgment.escalation import escalation_triggers
from returns_manager.judgment.fusion import fuse_identity, unit_presence
from returns_manager.judgment.grading import grade_condition
from returns_manager.judgment.referential import validate_references
from returns_manager.judgment.types import (
    ClaimSignals,
    CompletenessResult,
    ConditionResult,
    FusedIdentity,
    JudgmentContext,
    UnitPresenceResult,
    ValidationReport,
)
from returns_manager.llm.schemas import JudgmentV1

Verdict = Literal["PASS", "FAIL", "UNCERTAIN"]
ReturnTarget = Literal["awaiting_operator", "awaiting_review", "awaiting_signoff"]


@dataclass(frozen=True)
class Check:
    check_key: str
    verdict: Verdict
    confidence_bp: int
    detail: str
    source: Literal["model", "deterministic"]


@dataclass(frozen=True)
class PhotoGate:
    """Deterministic quality-gate facts of the current photo set (§9.2)."""

    non_fail_photos: int
    acknowledged_warnings: bool
    integrity_flags: tuple[str, ...] = ()  # e.g. possible_reused_photo


@dataclass(frozen=True)
class PipelineResult:
    judgment: JudgmentV1
    report: ValidationReport
    presence: UnitPresenceResult
    identity: FusedIdentity
    completeness: CompletenessResult
    condition: ConditionResult
    claims: ClaimSignals
    escalation_triggers: tuple[str, ...]
    inputs: DispositionInputs
    decision: DispositionDecision
    checks: tuple[Check, ...]
    requires_review: bool
    review_reasons: tuple[str, ...]
    target_status: ReturnTarget


def _photos(aliases: tuple[str, ...] | list[str]) -> str:
    return ", ".join(dict.fromkeys(aliases)) or "no photo"


def build_inputs(
    ctx: JudgmentContext,
    j: JudgmentV1,
    presence: UnitPresenceResult,
    identity: FusedIdentity,
    completeness: CompletenessResult,
    condition: ConditionResult,
    flags: tuple[str, ...],
    *,
    auto_disposition_enabled: bool,
) -> DispositionInputs:
    card = ctx.card
    parts = {c.component_id: c for c in completeness.components}
    value = card.value
    return DispositionInputs(
        inspection_state="complete",
        skip_reason=None,
        usable_photo_count=sum(1 for r in j.photo_reports if r.usable),
        unit_presence=presence.status,
        identity=identity.identity_match,
        actual_sku=identity.actual_sku,
        completeness_status=completeness.status,
        essential_missing=tuple(MissingPart(c, parts[c].replaceable) for c in completeness.essential_missing),
        nonessential_missing=completeness.nonessential_missing,
        essential_uncertain=tuple(
            MissingPart(c, parts[c].replaceable) for c in completeness.essential_uncertain
        ),
        nonessential_uncertain=tuple(
            c for c in completeness.uncertain_components if c not in completeness.essential_uncertain
        ),
        cosmetic_grade=condition.cosmetic_grade,
        listing_blockers=condition.listing_blockers,
        blockers_undetermined=condition.blockers_undetermined,
        max_severity=condition.max_severity,
        new_only=ctx.policy.new_only,
        opened_item_route=ctx.policy.opened_item_route,
        damaged_item_route=ctx.policy.damaged_item_route,
        list_price_minor=value.list_price.amount_minor,
        currency=value.list_price.currency,
        recovery_rate_bp=tuple(sorted(value.recovery_rate_bp.model_dump().items())),
        refurbish_cost_minor=value.refurbish_cost.amount_minor,
        restock_used_grades=tuple(ctx.params.restock_used_grades),
        refurbish_min_net_gain_minor=ctx.params.refurbish_min_net_gain.amount_minor,
        dispose_max_salvage_minor=ctx.params.dispose_max_salvage.amount_minor,
        high_value_threshold_minor=ctx.params.high_value_threshold.amount_minor,
        auto_disposition_enabled=auto_disposition_enabled,
        flags=flags,
    )


def build_checks(
    ctx: JudgmentContext,
    gate: PhotoGate,
    presence: UnitPresenceResult,
    identity: FusedIdentity,
    completeness: CompletenessResult,
    condition: ConditionResult,
    decision: DispositionDecision,
) -> tuple[Check, ...]:
    checks: list[Check] = []
    if gate.non_fail_photos >= 2:
        checks.append(
            Check(
                "photo_quality",
                "PASS",
                10000,
                f"{gate.non_fail_photos} photos passed the quality gate.",
                "deterministic",
            )
        )
    elif gate.acknowledged_warnings:
        checks.append(
            Check(
                "photo_quality",
                "UNCERTAIN",
                10000,
                "Fewer than 2 usable photos; operator acknowledged the quality warnings.",
                "deterministic",
            )
        )
    else:
        checks.append(Check("photo_quality", "FAIL", 10000, "Fewer than 2 usable photos.", "deterministic"))

    ev = _photos(list(presence.evidence_photos))
    if presence.status == "product_present":
        checks.append(
            Check("unit_presence", "PASS", 10000, f"The product is in the package ({ev}).", "model")
        )
    elif presence.clearly_evidenced:
        what = (
            "empty packaging" if presence.status == "empty_packaging" else "contents that are not the product"
        )
        checks.append(
            Check("unit_presence", "FAIL", 10000, f"Returned package holds {what} ({ev}).", "model")
        )
    else:
        checks.append(
            Check(
                "unit_presence",
                "UNCERTAIN",
                0,
                "Cannot tell from the provided photos whether the product is in the package.",
                "model",
            )
        )

    ip = _photos(list(identity.evidence_photos))
    if identity.identity_match == "yes":
        detail = (
            f"Matches the catalogue card for {ctx.ordered_sku} on the product body ({ip}); "
            f"barcode: {identity.barcode_status}."
        )
        checks.append(Check("identity", "PASS", identity.confidence_bp, detail, "model"))
    elif identity.identity_match == "no":
        actual = f"; looks like {identity.actual_sku}" if identity.actual_sku else ""
        checks.append(
            Check(
                "identity",
                "FAIL",
                identity.confidence_bp,
                f"Not the ordered item {ctx.ordered_sku}{actual} ({ip}).",
                "model",
            )
        )
    else:
        checks.append(
            Check(
                "identity",
                "UNCERTAIN",
                identity.confidence_bp,
                f"Identity not verified: {identity.reasons[0].replace('_', ' ')}.",
                "model",
            )
        )

    if completeness.status == "complete":
        checks.append(
            Check(
                "completeness",
                "PASS",
                10000,
                f"All expected parts present: {completeness.parts_list}.",
                "model",
            )
        )
    elif completeness.status == "incomplete":
        checks.append(
            Check("completeness", "FAIL", 10000, f"Missing: {completeness.parts_missing}.", "model")
        )
    else:
        checks.append(
            Check(
                "completeness",
                "UNCERTAIN",
                0,
                f"Not visible in the provided photos: {completeness.parts_uncertain}.",
                "model",
            )
        )
    for c in completeness.components:
        key = f"component:{c.component_id}"
        if c.status == "present":
            checks.append(
                Check(key, "PASS", c.confidence_bp, f"{c.name} present ({_photos(list(c.photos))}).", "model")
            )
        elif c.status == "missing":
            checks.append(
                Check(
                    key,
                    "FAIL",
                    c.confidence_bp,
                    f"{c.name}: {c.missing_quantity} missing ({_photos(list(c.photos))}).",
                    "model",
                )
            )
        else:
            why = (c.reason or "not visible").replace("_", " ")
            checks.append(Check(key, "UNCERTAIN", c.confidence_bp, f"{c.name}: {why}.", "model"))

    if condition.cosmetic_grade is not None:
        phrase = f' Rubric: "{condition.phrases_matched[0]}"' if condition.phrases_matched else ""
        checks.append(
            Check(
                "condition_grade",
                "PASS",
                condition.confidence_bp,
                f"{condition.amazon_condition}.{phrase} Functional test not performed.",
                "model",
            )
        )
    else:
        why = (condition.uncertainty_reason or "condition_ambiguous").replace("_", " ")
        checks.append(
            Check(
                "condition_grade",
                "UNCERTAIN",
                condition.confidence_bp,
                f"Grade not determinable: {why}.",
                "model",
            )
        )

    if condition.listing_blockers:
        checks.append(
            Check(
                "relistable_as_is",
                "FAIL",
                10000,
                "Not relistable as-is: "
                + ", ".join(b.replace("_", " ") for b in condition.listing_blockers)
                + ".",
                "deterministic",
            )
        )
    elif condition.blockers_undetermined:
        checks.append(
            Check(
                "relistable_as_is",
                "UNCERTAIN",
                0,
                "Cannot determine: "
                + ", ".join(b.replace("_", " ") for b in condition.blockers_undetermined)
                + ".",
                "deterministic",
            )
        )
    else:
        checks.append(
            Check(
                "relistable_as_is",
                "PASS",
                10000,
                "No listing blockers observed in the provided photos.",
                "deterministic",
            )
        )

    if decision.recommended_disposition is None:
        checks.append(
            Check(
                "category_policy",
                "UNCERTAIN",
                10000,
                f"No route computed: {decision.no_recommendation_reason}.",
                "deterministic",
            )
        )
    elif decision.rule_id in ("R07", "R08"):
        checks.append(
            Check(
                "category_policy",
                "FAIL",
                10000,
                f"Route set by category policy {ctx.policy.policy_id} ({decision.rule_id}).",
                "deterministic",
            )
        )
    else:
        checks.append(
            Check(
                "category_policy",
                "PASS",
                10000,
                f"Route {decision.recommended_disposition} is allowed by {ctx.policy.policy_id}.",
                "deterministic",
            )
        )
    return tuple(checks)


def run_pipeline(
    judgment: JudgmentV1,
    ctx: JudgmentContext,
    *,
    photo_gate: PhotoGate,
    rules_version: str,
    auto_disposition_enabled: bool = True,
    operator_state: str | None = None,
) -> PipelineResult:
    j, rep = validate_references(judgment, ctx)
    j = apply_consistency(j, ctx, rep)
    presence = unit_presence(j)
    identity = fuse_identity(j, ctx)
    completeness = compute_completeness(j, ctx, rep)
    condition = grade_condition(j, ctx, completeness)
    flags = tuple(dict.fromkeys([*rep.flags, *identity.risk_flags, *photo_gate.integrity_flags]))
    triggers = escalation_triggers(
        identity,
        completeness,
        condition,
        presence,
        flags=flags,
        item_value_minor=ctx.card.value.list_price.amount_minor,
        high_value_threshold_minor=ctx.params.high_value_threshold.amount_minor,
        operator_state=operator_state,
        model_observed_state=j.model_observed_state,
    )
    severe = tuple(dict.fromkeys(d.photo for d in j.condition.observations if d.severity == "severe"))
    claims = claim_signals(presence, identity, completeness, condition, severe)
    inputs = build_inputs(
        ctx,
        j,
        presence,
        identity,
        completeness,
        condition,
        flags,
        auto_disposition_enabled=auto_disposition_enabled,
    )
    decision = decide(inputs, rules_version)
    checks = build_checks(ctx, photo_gate, presence, identity, completeness, condition, decision)

    review = list(decision.review_reasons)
    if triggers:
        review.append("escalation_triggered")  # escalation agent is P9; a human reviews meanwhile
    if photo_gate.acknowledged_warnings and photo_gate.non_fail_photos < 2:
        review.append("photo_quality_acknowledged")
    requires_review = decision.requires_review or bool(triggers) or "photo_quality_acknowledged" in review
    if requires_review:
        target: ReturnTarget = "awaiting_review"
    elif decision.requires_signoff:
        target = "awaiting_signoff"
    else:
        target = "awaiting_operator"
    return PipelineResult(
        judgment=j,
        report=rep,
        presence=presence,
        identity=identity,
        completeness=completeness,
        condition=condition,
        claims=claims,
        escalation_triggers=triggers,
        inputs=inputs,
        decision=decision,
        checks=checks,
        requires_review=requires_review,
        review_reasons=tuple(dict.fromkeys(review)),
        target_status=target,
    )
