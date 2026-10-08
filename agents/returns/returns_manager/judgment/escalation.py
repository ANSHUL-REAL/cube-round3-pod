"""Escalation triggers (§11.12). Pure; the escalation session itself is phase P9.

Until P9 runs escalations, a triggered inspection is sent to human review with the trigger list recorded
(`escalation_state = triggered_not_run`), so nothing that would have been escalated flows through unchecked.
"""

from __future__ import annotations

from returns_manager.judgment.types import (
    CompletenessResult,
    ConditionResult,
    FusedIdentity,
    UnitPresenceResult,
)

# Operator tap vs model: combinations that "strongly disagree" (§11.12).
_STRONG_DISAGREEMENT = {
    ("damaged", "none_visible"),
    ("damaged", "used_like_new"),
    ("damaged", "new"),
    ("empty_box", "product_present"),
    ("factory_sealed", "signs_of_use"),
    ("factory_sealed", "damaged"),
}


def escalation_triggers(
    identity: FusedIdentity,
    completeness: CompletenessResult,
    condition: ConditionResult,
    presence: UnitPresenceResult,
    *,
    flags: tuple[str, ...],
    item_value_minor: int,
    high_value_threshold_minor: int,
    operator_state: str | None,
    model_observed_state: str,
) -> tuple[str, ...]:
    out: list[str] = []
    if identity.identity_match == "uncertain" and identity.strength == "conflict":
        out.append("identity_conflict")
    if completeness.essential_uncertain:
        out.append("essential_component_uncertain")
    if condition.cosmetic_grade is None and item_value_minor >= high_value_threshold_minor:
        out.append("high_value_condition_uncertain")
    if "possible_product_swap" in identity.risk_flags:
        out.append("possible_product_swap")
    if "injection_attempt_suspected" in flags:
        out.append("injection_attempt_suspected")
    if presence.status == "uncertain":
        out.append("unit_presence_uncertain")
    if operator_state is not None:
        model_views = {
            model_observed_state,
            condition.signs_of_use,
            condition.cosmetic_grade or "",
            presence.status,
        }
        if any((operator_state, m) in _STRONG_DISAGREEMENT for m in model_views):
            out.append("operator_model_strong_disagreement")
    return tuple(out)
