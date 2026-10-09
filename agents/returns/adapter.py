"""Map a Round 2 Returns Manager result onto a Round 3 Evidence Record.

Round 2's pipeline (core/pipeline.py) produced checks `photo_quality`, `unit_presence`, `identity`, `completeness`,
`component:<id>`, `condition_grade`, `relistable_as_is`, `category_policy`, plus a disposition from the rules engine.
Round 3 wants stable snake_case keys and recommends `identity_match`, `completeness`, `condition`. So:

    identity_match   <- identity (Round 2 identity fusion), then upstream conflicts (UNCERTAIN, never guessed)
    completeness     <- completeness (the per-component results are kept whole in payload.components)
    condition        <- condition_grade + the physical listing blockers (see `condition_check`)
    unit_presence    <- unit_presence          (extra)
    photo_quality    <- photo_quality          (extra; never FAIL, nobody is there to retake the photo)

`identity_match` is deliberately FIRST: the organiser Recovery stub reads checks[0] as "the right item came back".

The disposition is the Round 2 engine's (`restock | refurbish | liquidate | dispose`). It is reported as the outcome only
when nothing needs a person: otherwise the outcome is `pending_review` and the engine's route stays in
`payload.disposition.engine_recommendation`, so a person sees the suggestion but is not handed a decision.

The record never contradicts itself: before it is returned, `_assert_consistent` raises if a route was reported with a
failing or uncertain identity / presence, or `restock` with any check that is not PASS.
"""
from __future__ import annotations

import re
from dataclasses import asdict

from shared.utils.records import build_output, build_record, error_obj, rollup, utcnow

from .core import __version__
from .core.pipeline import PipelineResult
from .core.prompts import get_prompt
from .returned import ReturnedItem

STAGE = "returns"
AGENT_ID = f"returns-manager@{__version__}"
ENGINE = "returns-rules/round2"  # the deterministic decision engine (core/engine.py)
ROUTES = ("restock", "refurbish", "liquidate", "dispose")

# Round 2 uncertainty reasons -> the contract's closed enum.
REASON = {
    "bad_photo": "poor_image", "blur": "poor_image", "missing_angle": "poor_image", "barcode_unreadable": "poor_image",
    "marking_not_visible": "poor_image",
    "occlusion": "occluded", "component_area_not_visible": "occluded",
    "ocr_conflict": "conflicting_evidence", "visual_conflict": "conflicting_evidence",
    "contradictory_evidence": "conflicting_evidence",
    "similar_product": "insufficient_evidence", "component_not_photo_verifiable": "insufficient_evidence",
    "condition_ambiguous": "insufficient_evidence", "packaging_state_unclear": "insufficient_evidence",
    "reference_insufficient": "insufficient_evidence", "insufficient_product_body_evidence": "insufficient_evidence",
    "other": "other",
}
_REASON_RANK = {"poor_image": 0, "occluded": 1, "conflicting_evidence": 2, "insufficient_evidence": 3, "other": 4}
# Listing blockers that are a physical condition of the item. `essential_component_missing` belongs to completeness;
# `functional_test_required` is a process step (the function is never observed), not a defect.
PHYSICAL_BLOCKERS = {"damaged_difficult_to_use", "not_clean", "consumable_used", "category_new_only_opened"}


def _safe(request: dict) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "-", request["request_id"])


def record_id_for(request: dict) -> str:
    """Same request in, same record_id out (the contract's idempotency rule)."""
    return "RTN-" + _safe(request)


def pending_record_id(request: dict) -> str:
    return "RTN-PENDING-" + _safe(request)


def _reason(code: str | None, default: str = "insufficient_evidence") -> str:
    return REASON.get(code or "", default)


def _best_reason(codes: list[str], default: str = "insufficient_evidence") -> str:
    mapped = [_reason(c, default) for c in codes if c]
    return min(mapped, key=_REASON_RANK.__getitem__) if mapped else default


def _conf(bp: int | None, verdict: str) -> float | None:
    """Round 2 keeps confidence as integer basis points. A verdict of UNCERTAIN carries no confidence."""
    return None if verdict == "UNCERTAIN" or bp is None else round(bp / 10000, 4)


def _refs(aliases, alias_ref: dict[str, str], fallback: list[str]) -> list[str]:
    out = list(dict.fromkeys(alias_ref[a] for a in aliases if a in alias_ref))
    return out or list(fallback)


def _by_key(result: PipelineResult) -> dict:
    return {c.check_key: c for c in result.checks}


# ---------------------------------------------------------------- the five checks
def identity_check(item: ReturnedItem, result: PipelineResult, alias_ref, all_refs, up: dict) -> dict:
    base = _by_key(result)["identity"]
    fused = result.identity
    verdict, detail, reason = base.verdict, base.detail, None
    refs = _refs(fused.evidence_photos, alias_ref, all_refs)
    if verdict == "UNCERTAIN":
        reason = "conflicting_evidence" if fused.strength == "conflict" else _reason(
            result.judgment.identity.uncertainty_reason, "insufficient_evidence")
    notes = [c["detail"] for c in up["conflicts"]]
    if (pack := up["pack"]) and (pack["order_id"] or pack["order_lines"]):
        refs.append(pack["record_id"])  # the order was compared with Pack's: cite it whether or not they agree
    if notes:
        refs += [r for c in up["conflicts"] for r in c["record_ids"] if r not in refs]
        if verdict == "PASS":  # records disagree about what was sold or sent: UNCERTAIN, never a guess
            verdict, reason = "UNCERTAIN", "conflicting_evidence"
        detail += " Upstream evidence disagrees: " + "; ".join(notes) + "."
    for cause in up["supplier_side"]["identity"]:
        detail += f" {cause['text']}."
        refs.append(cause["record_id"])
    if pack and pack["effective_verdict"] == "FAIL" and fused.actual_sku:
        sent = pack.get("sent") or {}
        if fused.actual_sku in sent:
            detail += (f" The returned item looks like {fused.actual_sku}, which Pack's record counted in the box:"
                       " it may be what was packed, not a swap by the customer.")
            refs.append(pack["record_id"])
    return {"check_key": "identity_match", "verdict": verdict, "confidence": _conf(base.confidence_bp, verdict),
            "expected": item.ordered_sku,
            "observed": {"identity": fused.identity_match, "strength": fused.strength, "barcode": fused.barcode_status,
                         "likely_actual_sku": fused.actual_sku, "risk_flags": list(fused.risk_flags)},
            "detail": detail, "evidence_refs": list(dict.fromkeys(refs)),
            **({"uncertain_reason": reason} if verdict == "UNCERTAIN" else {})}


def completeness_check(result: PipelineResult, alias_ref, all_refs, up: dict) -> dict:
    base, comp = _by_key(result)["completeness"], result.completeness
    detail, refs = base.detail, _refs([a for c in comp.components for a in c.photos], alias_ref, all_refs)
    reason = None
    if base.verdict == "UNCERTAIN":
        reason = _best_reason([c.reason for c in comp.components if c.status == "uncertain"])
    if base.verdict == "FAIL":
        if up["receiving"]:
            refs.append(up["receiving"]["record_id"])  # a missing part was weighed against what Receiving found
        for cause in up["supplier_side"]["completeness"]:
            detail += f" {cause['text']}."
            refs.append(cause["record_id"])
    return {"check_key": "completeness", "verdict": base.verdict, "confidence": _conf(base.confidence_bp, base.verdict),
            "expected": [f"{c.name} x{c.expected}" if c.expected > 1 else c.name for c in comp.components],
            "observed": {"missing": [p for p in comp.parts_missing.split(";") if p],
                         "uncertain": [p for p in comp.parts_uncertain.split(";") if p]},
            "detail": detail, "evidence_refs": list(dict.fromkeys(refs)),
            **({"uncertain_reason": reason} if base.verdict == "UNCERTAIN" else {})}


def condition_check(result: PipelineResult, alias_ref, all_refs, up: dict) -> dict:
    """The Amazon condition grade, against the rubric snapshot the engine was given.

    PASS       a rubric grade was assigned and nothing physically unacceptable was seen.
    FAIL       something physically unacceptable (severe damage, not clean, a used consumable, an opened item in a
               New-only category). The grade, if any, is still reported.
    UNCERTAIN  no grade could be assigned. Missing parts never change the grade; completeness is its own check.
    The function of the item is never observed (`functional_check` is always `not_performed`).
    """
    cond = result.condition
    physical = [b for b in cond.listing_blockers if b in PHYSICAL_BLOCKERS]
    if physical:
        verdict = "FAIL"
    elif cond.cosmetic_grade is not None:
        verdict = "PASS"
    else:
        verdict = "UNCERTAIN"
    parts = [f"Amazon condition: {cond.amazon_condition}."]
    if cond.phrases_matched:
        parts.append(f'Rubric: "{cond.phrases_matched[0]}"')
    if physical:
        parts.append("Not acceptable as-is: " + ", ".join(b.replace("_", " ") for b in physical) + ".")
    if verdict == "UNCERTAIN":
        parts.append(f"Grade not determinable: {(cond.uncertainty_reason or 'condition_ambiguous').replace('_', ' ')}.")
    if cond.blockers_undetermined:
        parts.append("Could not determine: " + ", ".join(b.replace("_", " ") for b in cond.blockers_undetermined) + ".")
    parts.append("Function was not tested.")
    refs = _refs([d.photo for d in result.judgment.condition.observations], alias_ref, all_refs)
    for cause in up["supplier_side"]["condition"]:
        parts.append(cause["text"] + ".")
        refs.append(cause["record_id"])
    return {"check_key": "condition", "verdict": verdict, "confidence": _conf(cond.confidence_bp, verdict),
            "expected": "an Amazon condition grade and nothing physically unacceptable",
            "observed": {"amazon_condition": cond.cosmetic_grade and cond.amazon_condition, "grade": cond.cosmetic_grade,
                         "packaging_state": cond.packaging_state, "signs_of_use": cond.signs_of_use,
                         "cleanliness": cond.cleanliness, "max_severity": cond.max_severity,
                         "physical_blockers": physical, "functional_check": cond.functional_check},
            "detail": " ".join(parts), "evidence_refs": list(dict.fromkeys(refs)),
            **({"uncertain_reason": _reason(cond.uncertainty_reason)} if verdict == "UNCERTAIN" else {})}


def presence_check(result: PipelineResult, alias_ref, all_refs) -> dict:
    base, pres = _by_key(result)["unit_presence"], result.presence
    return {"check_key": "unit_presence", "verdict": base.verdict, "confidence": _conf(base.confidence_bp, base.verdict),
            "expected": "product_present", "observed": pres.status, "detail": base.detail,
            "evidence_refs": _refs(pres.evidence_photos, alias_ref, all_refs),
            **({"uncertain_reason": "insufficient_evidence"} if base.verdict == "UNCERTAIN" else {})}


def photo_quality_check(result: PipelineResult, photos: list[dict], all_refs) -> dict:
    n_ok = sum(1 for p in photos if p["quality_status"] != "fail")
    verdict = "PASS" if n_ok >= 2 else "UNCERTAIN"
    detail = (f"{n_ok} of {len(photos)} photos passed the quality gate."
              + ("" if verdict == "PASS" else " Fewer than 2 usable photos; a retake is needed for a reliable judgment."))
    return {"check_key": "photo_quality", "verdict": verdict, "confidence": None, "expected": ">= 2 usable photos",
            "observed": {"usable": n_ok, "photos": len(photos)}, "detail": detail, "evidence_refs": list(all_refs),
            **({"uncertain_reason": "poor_image"} if verdict == "UNCERTAIN" else {})}


# ---------------------------------------------------------------- decision
def _assert_consistent(outcome: str, verdict: str, checks: dict) -> None:
    """Never ship a contradiction: a route needs the right item to be present, and restock needs everything PASS."""
    if outcome in ROUTES:
        if checks["identity_match"]["verdict"] != "PASS" or checks["unit_presence"]["verdict"] != "PASS":
            raise RuntimeError(f"outcome {outcome} reported with identity/presence that are not PASS")
        if verdict == "UNCERTAIN":
            raise RuntimeError(f"outcome {outcome} reported with an UNCERTAIN verdict")
    if outcome == "restock" and any(c["verdict"] != "PASS" for c in checks.values()):
        raise RuntimeError("restock reported while a check is not PASS")


def build(request: dict, item: ReturnedItem, result: PipelineResult, bundle, photos: list[dict], not_used: list[str],
          judge, *, rule_source: dict, card, up: dict, provider: str | None, manifest: dict) -> dict:
    """The Round 3 Evidence Record for a return the model answered."""
    alias_ref = {p["alias"]: p["ref"] for p in photos}
    all_refs = [p["ref"] for p in photos]
    keyed = {
        "identity_match": identity_check(item, result, alias_ref, all_refs, up),
        "completeness": completeness_check(result, alias_ref, all_refs, up),
        "condition": condition_check(result, alias_ref, all_refs, up),
        "unit_presence": presence_check(result, alias_ref, all_refs),
        "photo_quality": photo_quality_check(result, photos, all_refs),
    }
    checks = list(keyed.values())
    verdict = rollup(checks)
    d = result.decision
    route = d.recommended_disposition
    conflicts = up["conflicts"]
    review = list(result.review_reasons) + (["upstream_conflict"] if conflicts else [])
    if route is not None and verdict != "UNCERTAIN" and not review:
        outcome = route
    else:
        outcome = "pending_review"
        if verdict == "PASS":
            verdict = "UNCERTAIN"  # a pending review never carries PASS
    _assert_consistent(outcome, verdict, keyed)
    needs_human = verdict == "UNCERTAIN" or outcome == "pending_review" or d.requires_signoff

    cond, comp = result.condition, result.completeness
    reason_bits = list(d.reasons) + ([f"upstream: {c['detail']}" for c in conflicts])
    if outcome == "pending_review":
        why = d.no_recommendation_reason or ", ".join(review) or "a check is uncertain"
        reason = f"pending review ({why}); " + "; ".join(reason_bits)
    else:
        reason = f"{outcome} by rule {d.rule_id}: " + "; ".join(reason_bits)
    if d.requires_signoff:
        reason += f" Sign-off needed: {', '.join(d.signoff_reasons)}."

    j = result.judgment
    payload = {
        "order_id": item.order_id, "ordered_sku": item.ordered_sku, "route": request["subject"].get("route"),
        "observed_state": j.model_observed_state, "amazon_condition": cond.cosmetic_grade and cond.amazon_condition,
        "condition_grade": cond.cosmetic_grade, "condition_graded": cond.cosmetic_grade is not None,
        "functional_check": cond.functional_check, "parts_missing": [p for p in comp.parts_missing.split(";") if p],
        "parts_uncertain": [p for p in comp.parts_uncertain.split(";") if p],
        "rule_source": rule_source,
        "components": [{"component_id": c.component_id, "name": c.name, "expected": c.expected, "observed": c.observed,
                        "status": c.status, "essential": c.essential, "replaceable": c.replaceable,
                        "missing_quantity": c.missing_quantity, "reason": c.reason,
                        "evidence_refs": _refs(c.photos, alias_ref, [])} for c in comp.components],
        "disposition": {
            "outcome": outcome, "engine_recommendation": route, "rule_id": d.rule_id, "rules_version": d.rules_version,
            "decided_by": d.decided_by, "reasons": list(d.reasons), "no_recommendation_reason": d.no_recommendation_reason,
            "listing_condition": d.listing_condition, "provisional": d.provisional, "assumptions": list(d.assumptions),
            "requires_review": bool(review), "review_reasons": review, "requires_signoff": d.requires_signoff,
            "signoff_reasons": list(d.signoff_reasons), "inputs_sha256": d.inputs_sha256,
            "expected_recovery_minor": dict(d.expected_recovery_minor), "currency": d.currency,
            "synthetic_values": d.synthetic_values,
        },
        "claim_signals": {
            name: {"value": s.value, "basis": list(s.basis), "evidence_refs": _refs(s.evidence, alias_ref, [])}
            for name, s in (("item_not_returned", result.claims.item_not_returned),
                            ("wrong_item_returned", result.claims.wrong_item_returned),
                            ("returned_damaged", result.claims.returned_damaged))},
        "escalation_triggers": list(result.escalation_triggers),
        "retake_requests": [r.model_dump() for r in j.retake_requests],
        "uncertainties": [u.model_dump() for u in j.uncertainties],
        "untrusted_text_observed": [t.model_dump() for t in j.untrusted_text_observed],
        "validator_actions": [asdict(a) for a in result.report.actions],
        "invented_reference_count": result.report.invented_reference_count,
        "invented_quote_count": result.report.invented_quote_count,
        "operator": {"state": item.operator_state, "disposition": item.operator_disposition,
                     "agrees_with_operator": (item.operator_disposition == outcome) if item.operator_disposition else None},
        "sent_vs_returned": up["sent_vs_returned"],
        "upstream": {"pack": up["pack"], "receiving": up["receiving"], "conflicts": conflicts,
                     "supplier_side": up["supplier_side"], "notes": up["notes"]},
        "catalogue": {"sku": card.sku, "card_version": card.version, "card_sha256": card.content_sha256,
                      "placeholder_card": bool(card.provenance_notes and card.provenance_notes.startswith("SYNTHETIC")),
                      "value_data_synthetic": card.value.synthetic, "reference_images": len(card.reference_images)},
        "photos": photos, "photos_not_used": not_used, "photo_aliases": alias_ref,
        "model_manifest": manifest, "usage": judge.usage,
        "round2_model_role": "observes only; the schema has no disposition field",
        "decision_engine": ENGINE,
    }
    confs = [c["confidence"] for c in checks if c["confidence"] is not None]
    return build_record(
        request, agent_id=AGENT_ID, record_id=record_id_for(request), captured_at=item.captured_at or utcnow(),
        operator_id=item.operator_id, unit_scope="unit",
        refs={"order_id": item.order_id, "sku": item.ordered_sku, "asin": item.ordered_asin},
        checks=checks, outcome=outcome, verdict=verdict, confidence=round(min(confs), 4) if confs else None,
        reason=reason[:900], needs_human=needs_human, model=model_block(judge, provider),
        inputs=[{"ref": p["ref"], "sha256": p["sha256_original"], "kind": "image"} for p in photos],
        payload=payload, latency_ms=judge.latency_ms)


def model_block(judge, provider: str | None) -> dict:
    prompt = f"{get_prompt('judgment').ref}+{get_prompt('judgment_task').ref}"
    return {"name": judge.model.split("/")[0], "version": judge.model, "provider": provider, "prompt_version": prompt,
            "calls": judge.calls, "cost_usd": judge.cost_usd}


def pending(request: dict, item: ReturnedItem | None, *, code: str, message: str, retryable: bool,
            photos: list[dict] | None = None, payload: dict | None = None, refs: dict | None = None,
            model_calls: int = 0, model_name: str | None = None) -> dict:
    """Fail open: a record exists, says UNCERTAIN and why, and keeps the captures. Never a made-up verdict."""
    body = {"order_id": item.order_id if item else None, "ordered_sku": item.ordered_sku if item else None,
            **(payload or {})}
    record = build_record(
        request, agent_id=AGENT_ID, record_id=pending_record_id(request),
        captured_at=(item.captured_at if item else None) or utcnow(), operator_id=item.operator_id if item else None,
        unit_scope="unit", refs={"order_id": item.order_id if item else None, "sku": item.ordered_sku if item else None,
                                 **(refs or {})},
        checks=[], outcome="pending_review", verdict="UNCERTAIN", needs_human=True, reason=f"{code}: {message}",
        model={"name": model_name or "none", "version": "0", "calls": model_calls, "cost_usd": None},
        inputs=[{"ref": p["ref"], "sha256": p["sha256_original"], "kind": "image"} for p in (photos or [])],
        payload=body, status="pending" if retryable else "error",
        error=error_obj(code, message, retryable=retryable, stage=STAGE, agent_id=AGENT_ID))
    return build_output(record, next_step="retry" if retryable else "review", reason=message)

