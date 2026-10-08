"""Condition: cosmetic grade versus listing eligibility (§11.11).

- `amazon_condition` is always the rubric label of the physical grade (`Used - Good`, ...) or
  `uncertain`. Missing parts never change it; completeness is its own check.
- Listing blockers come from the rubric's unacceptable list plus the category policy. They drive the
  disposition but never overwrite the condition label. A blocker that depends on something the photos did not
  show (e.g. whether the packaging is sealed) is `undetermined`, never assumed absent.
- Function is never observed: `functional_check` is always `not_performed`.
"""

from __future__ import annotations

from returns_manager.judgment.types import (
    Blocker,
    CompletenessResult,
    ConditionResult,
    JudgmentContext,
    to_bp,
)
from returns_manager.llm.schemas import JudgmentV1

_SEVERITY_ORDER = {"none": 0, "minor": 1, "moderate": 2, "severe": 3}


def grade_condition(j: JudgmentV1, ctx: JudgmentContext, completeness: CompletenessResult) -> ConditionResult:
    cond = j.condition
    grade = cond.proposed_grade.grade_code
    packaging = cond.packaging_state
    sealed: bool | None = None if packaging == "not_visible" else packaging == "factory_sealed_intact"
    defects = cond.observations
    max_sev = "none"
    for d in defects:
        if _SEVERITY_ORDER[d.severity] > _SEVERITY_ORDER[max_sev]:
            max_sev = d.severity

    blockers: list[Blocker] = []
    undetermined: list[Blocker] = []

    def gate(name: Blocker, value: bool | None) -> None:
        if value is None:
            undetermined.append(name)
        elif value:
            blockers.append(name)

    gate("essential_component_missing", bool(completeness.essential_missing))
    gate(
        "damaged_difficult_to_use",
        any(d.severity == "severe" and d.defect_type != "label_damage" for d in defects),
    )
    gate(
        "not_clean",
        cond.cleanliness == "dirty" and any(d.defect_type in ("stain", "residue_or_dirt") for d in defects),
    )
    if ctx.policy.functional_verification_required_for_used:
        gate("functional_test_required", None if sealed is None else not sealed)
    if ctx.policy.new_only:
        gate("category_new_only_opened", None if sealed is None else not sealed)
    consumable = ctx.policy.consumable_ingestible_or_topical or (
        ctx.policy.consumable_items_where_any_part_used_prohibited and ctx.card.consumable
    )
    if consumable:
        used_signal = cond.signs_of_use in ("light", "moderate", "heavy") or any(
            d.defect_type in ("signs_of_use", "residue_or_dirt") for d in defects
        )
        gate(
            "consumable_used",
            None if (cond.signs_of_use == "not_determinable" and not used_signal) else used_signal,
        )

    labels = {g.code: g.label for g in ctx.rubric.grades}
    relistable: bool | None = False if blockers else (None if undetermined else True)
    return ConditionResult(
        cosmetic_grade=grade,
        amazon_condition=labels[grade] if grade is not None else "uncertain",
        listing_blockers=tuple(blockers),
        blockers_undetermined=tuple(undetermined),
        relistable_as_is=relistable,
        packaging_state=packaging,
        signs_of_use=cond.signs_of_use,
        cleanliness=cond.cleanliness,
        max_severity=max_sev,  # type: ignore[arg-type]
        phrases_matched=tuple(cond.proposed_grade.rubric_phrases_matched),
        confidence_bp=to_bp(cond.proposed_grade.confidence),
        uncertainty_reason=cond.proposed_grade.uncertainty_reason if grade is None else None,
    )
