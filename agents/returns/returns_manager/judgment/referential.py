"""Referential validation (§11.7 step 2): the model may only reference what it was given.

Unknown photo/reference/crop aliases, component ids, feature ids, grade codes and sibling SKUs are removed and
counted in `invented_reference_count`. Every `rubric_phrases_matched` item must be an exact substring of the
rubric text that was sent; others are removed and counted in `invented_quote_count`. A component of the parts
list that the model did not report at all is added back as `uncertain` (never assumed present or missing).
"""

from __future__ import annotations

from returns_manager.judgment.types import JudgmentContext, ValidationReport
from returns_manager.llm.schemas import ComponentObservation, EvidenceRef, JudgmentV1


def _rubric_text(ctx: JudgmentContext) -> str:
    if ctx.rubric_text_sent:
        return ctx.rubric_text_sent
    parts = [g.text for g in ctx.rubric.grades] + [u.text for u in ctx.rubric.unacceptable_conditions]
    return "\n".join(parts)


def validate_references(judgment: JudgmentV1, ctx: JudgmentContext) -> tuple[JudgmentV1, ValidationReport]:
    j = judgment.model_copy(deep=True)
    rep = ValidationReport()
    aliases = ctx.all_aliases
    photos = set(ctx.photo_aliases)

    def keep_refs(refs: list[EvidenceRef], where: str) -> list[EvidenceRef]:
        kept = [r for r in refs if r.photo in aliases]
        dropped = len(refs) - len(kept)
        if dropped:
            rep.invented_reference_count += dropped
            rep.act("REF-ALIAS", where, dropped, 0, "evidence cited an image alias that was not provided")
        return kept

    def keep_aliases(values: list[str], where: str) -> list[str]:
        kept = [v for v in values if v in aliases]
        if len(kept) != len(values):
            rep.invented_reference_count += len(values) - len(kept)
            rep.act("REF-ALIAS", where, values, kept, "unknown image alias removed")
        return kept

    # photo reports: one per provided return photo alias
    seen: set[str] = set()
    reports = []
    for r in j.photo_reports:
        if r.photo not in photos:
            rep.invented_reference_count += 1
            rep.act(
                "REF-ALIAS", "photo_reports", r.photo, "removed", "report for a photo that was not provided"
            )
        elif r.photo in seen:
            rep.act("REF-DUP", "photo_reports", r.photo, "removed", "duplicate photo report")
        else:
            seen.add(r.photo)
            reports.append(r)
    j.photo_reports = reports

    j.unit_presence.evidence = keep_refs(j.unit_presence.evidence, "unit_presence.evidence")

    # identity
    ident = j.identity
    ident.evidence = keep_refs(ident.evidence, "identity.evidence")
    idents = [o for o in ident.observed_identifiers if o.photo in aliases]
    if len(idents) != len(ident.observed_identifiers):
        rep.invented_reference_count += len(ident.observed_identifiers) - len(idents)
        rep.act(
            "REF-ALIAS", "identity.observed_identifiers", "", "", "identifier on an unknown image removed"
        )
    ident.observed_identifiers = idents
    feature_ids = {f.id for f in ctx.card.distinguishing_features}
    checks = []
    for fc in ident.feature_checks:
        if fc.feature_id not in feature_ids:
            rep.invented_reference_count += 1
            rep.act(
                "REF-FEATURE", "identity.feature_checks", fc.feature_id, "removed", "feature id not on card"
            )
            continue
        if fc.photo is not None and fc.photo not in aliases:
            rep.invented_reference_count += 1
            rep.act("REF-ALIAS", f"identity.feature_checks.{fc.feature_id}", fc.photo, None, "unknown alias")
            fc.photo = None
        checks.append(fc)
    ident.feature_checks = checks
    siblings = {s.sku for s in ctx.card.similar_skus}
    if ident.likely_actual_sku is not None and ident.likely_actual_sku not in siblings:
        rep.invented_reference_count += 1
        rep.act("REF-SKU", "identity.likely_actual_sku", ident.likely_actual_sku, None, "not in similar_skus")
        ident.likely_actual_sku = None

    # completeness: exactly one observation per parts-list component
    card_components = {c.id: c for c in ctx.card.components}
    reported: dict[str, ComponentObservation] = {}
    for obs in j.completeness.components:
        if obs.component_id not in card_components:
            rep.invented_reference_count += 1
            rep.act(
                "REF-COMPONENT", "completeness.components", obs.component_id, "removed", "not in parts list"
            )
            continue
        if obs.component_id in reported:
            rep.act("REF-DUP", f"component:{obs.component_id}", "duplicate", "first kept", "reported twice")
            continue
        obs.photos = keep_aliases(obs.photos, f"component:{obs.component_id}.photos")
        reported[obs.component_id] = obs
    for cid in card_components:
        if cid not in reported:
            reported[cid] = ComponentObservation(
                component_id=cid,
                observed_quantity=None,
                visibility="not_visible",
                status="uncertain",
                photos=[],
                confidence=0.0,
            )
            rep.act(
                "REF-OMITTED", f"component:{cid}", "not reported", "uncertain", "model omitted a component"
            )
    j.completeness.components = [reported[cid] for cid in card_components]  # card order
    items = [u for u in j.completeness.unexpected_items if u.photo in aliases]
    if len(items) != len(j.completeness.unexpected_items):
        rep.invented_reference_count += len(j.completeness.unexpected_items) - len(items)
    j.completeness.unexpected_items = items

    # condition
    cond = j.condition
    defects = [d for d in cond.observations if d.photo in aliases]
    if len(defects) != len(cond.observations):
        rep.invented_reference_count += len(cond.observations) - len(defects)
        rep.act("REF-ALIAS", "condition.observations", "", "", "defect on an unknown image removed")
    cond.observations = defects
    grade = cond.proposed_grade
    rubric_codes = {g.code for g in ctx.rubric.grades}
    if grade.grade_code is not None and grade.grade_code not in rubric_codes:
        rep.invented_reference_count += 1
        rep.act("REF-GRADE", "condition.proposed_grade", grade.grade_code, None, "grade not in this rubric")
        grade.grade_code = None
        grade.uncertainty_reason = grade.uncertainty_reason or "condition_ambiguous"
    text = _rubric_text(ctx)
    quotes = [q for q in grade.rubric_phrases_matched if q and q in text]
    if len(quotes) != len(grade.rubric_phrases_matched):
        rep.invented_quote_count += len(grade.rubric_phrases_matched) - len(quotes)
        rep.act(
            "REF-QUOTE",
            "condition.proposed_grade.rubric_phrases_matched",
            "",
            "",
            "not an exact rubric quote",
        )
    grade.rubric_phrases_matched = quotes

    return j, rep
