"""Map a Round 2 Pack Manager result onto a Round 3 Evidence Record.

Round 2 produced per-line checks (``line_present:SKU``, ``line_quantity:SKU``, ``wrong_item``, ``extra_item``)
plus ``image_quality``, ``photo_reuse`` and ``scene_coverage``. The Round 3 contract wants stable lowercase
check keys, so the per-line checks are rolled up (worst verdict wins) into the three recommended keys, and
the per-line detail is kept whole in ``payload.line_checks``. Nothing is dropped:

    items_present        <- every line_present:*
    quantities_correct   <- every line_quantity:*
    no_extra_items       <- wrong_item + extra_item (anything in the box that is not in the order)
    image_quality, photo_reuse, scene_coverage  <- carried over one to one (NOT_CHECKED becomes UNCERTAIN)

Box decision: STOP_AND_FIX -> FAIL / stop_and_fix, SEAL -> PASS / seal, UNCERTAIN -> UNCERTAIN / pending_review.
"""
from __future__ import annotations

import re

from shared.utils.records import build_output, build_record, error_obj, rollup, utcnow

from .core import __version__
from .core.models import EvidenceRecord, Order

STAGE = "pack"
AGENT_ID = f"pack-manager@{__version__}"
ENGINE = "pack-rules/1"  # the deterministic decision engine (core/decision.py)
OUTCOME = {"SEAL": "seal", "STOP_AND_FIX": "stop_and_fix", "UNCERTAIN": "pending_review"}
EXPECTED_VERDICT = {"SEAL": "PASS", "STOP_AND_FIX": "FAIL", "UNCERTAIN": "UNCERTAIN"}
_RANK = {"PASS": 0, "UNCERTAIN": 1, "FAIL": 2}
_VERDICT = {"PASS": "PASS", "FAIL": "FAIL", "UNCERTAIN": "UNCERTAIN", "NOT_CHECKED": "UNCERTAIN"}


def record_id_for(request: dict) -> str:
    """Same request in, same record_id out (the contract's idempotency rule)."""
    return "PCK-" + re.sub(r"[^A-Za-z0-9._-]", "-", request["request_id"])


def pending_record_id(request: dict) -> str:
    return "PCK-PENDING-" + re.sub(r"[^A-Za-z0-9._-]", "-", request["request_id"])


def _worst(verdicts: list[str]) -> str:
    return max(verdicts, key=_RANK.__getitem__) if verdicts else "UNCERTAIN"


def _conf(checks) -> float | None:
    values = [c.confidence for c in checks if c.confidence is not None]
    return round(max(0.0, min(1.0, min(values))), 3) if values else None


def _roll(key: str, group, *, expected, observed, refs: list[str], summary: str, reason: str) -> dict:
    """One Round 3 check from a group of Round 2 checks."""
    verdict = _worst([_VERDICT[c.verdict.value] for c in group])
    unclear = [c.detail for c in group if _VERDICT[c.verdict.value] != "PASS"]
    out = {"check_key": key, "verdict": verdict, "confidence": _conf(group),
           "detail": " ".join(unclear) if unclear else summary}
    if expected is not None:
        out["expected"] = expected
    if observed is not None:
        out["observed"] = observed
    if refs:
        out["evidence_refs"] = refs
    if verdict == "UNCERTAIN":
        out["uncertain_reason"] = reason
    return out


def _rows(rec: EvidenceRecord) -> list[dict]:
    return [r for r in rec.observations.get("expected_vs_observed", []) if r.get("sku")]


def map_checks(rec: EvidenceRecord, refs: list[str]) -> list[dict]:
    by = rec.checks
    obs = rec.observations
    rows = _rows(rec)
    expected = {r["sku"]: r["ordered"] for r in rows}
    found = {r["sku"]: r["found"] for r in rows}
    scene_unclear = any(c.check_key == "scene_coverage" and c.verdict.value != "PASS" for c in by)
    soft = "occluded" if scene_unclear else "insufficient_evidence"
    n = len(expected)
    extras = [e.get("sku") or e.get("title") for e in obs.get("extra", [])]
    extras += [w.get("found_sku") or w.get("found_title") for w in obs.get("wrong", [])]
    groups = (
        ("image_quality", [c for c in by if c.check_key == "image_quality"], None, None, "poor_image",
         "The photo passed the quality checks."),
        ("photo_reuse", [c for c in by if c.check_key == "photo_reuse"], None, None, "conflicting_evidence",
         "The photo has not been used for another order."),
        ("scene_coverage", [c for c in by if c.check_key == "scene_coverage"], None, None, "occluded",
         "The whole inside of the box is visible."),
        ("items_present", [c for c in by if c.check_key.startswith("line_present:")], sorted(expected),
         sorted(s for s, k in found.items() if k > 0), soft, f"All {n} ordered line(s) are in the box."),
        ("quantities_correct", [c for c in by if c.check_key.startswith("line_quantity:")], expected,
         {s: found.get(s, 0) for s in expected}, soft, f"Quantities match on all {n} line(s)."),
        ("no_extra_items", [c for c in by if c.check_key in ("wrong_item", "extra_item")], [],
         sorted({e for e in extras if e}), soft, "Nothing outside the order is in the box."),
    )
    # A check that did not run (e.g. no reuse lookup) is left out, never invented.
    return [_roll(key, group, expected=exp, observed=got, refs=refs, summary=summary, reason=reason)
            for key, group, exp, got, reason, summary in groups if group]


def _observed_in_box(rec: EvidenceRecord, threshold: float) -> tuple[dict, int]:
    counts: dict[str, int] = {}
    unsure = 0
    for item in rec.observations.get("detected_items", []):
        if item.get("classification") == "NON_PRODUCT":
            continue
        if item.get("sku") and item.get("confidence", 0) >= threshold:
            counts[item["sku"]] = counts.get(item["sku"], 0) + 1
        else:
            unsure += 1
    return counts, unsure


def model_block(rec: EvidenceRecord, provider: str | None) -> dict:
    version = rec.agent.get("model_version", "unknown")
    return {"name": version.split("/")[0], "version": version, "provider": provider,
            "prompt_version": rec.agent.get("prompt_version"),
            "calls": 0 if rec.observations.get("cached_response") else 1, "cost_usd": rec.observations.get("cost_usd")}


def build(request: dict, order: Order, meta: dict, rec: EvidenceRecord, photos: list[dict], not_used: list[str],
          settings, *, provider: str | None, upstream: dict) -> dict:
    """The Round 3 Evidence Record for a box the model answered."""
    refs = [p["ref"] for p in photos]
    checks = map_checks(rec, refs)
    verdict = rollup(checks)
    decision = rec.outcome.decision.value
    if verdict != EXPECTED_VERDICT[decision]:  # the roll-up must agree with the engine; never ship a contradiction
        raise RuntimeError(f"roll-up {verdict} disagrees with the engine's decision {decision}")
    outcome = OUTCOME[decision]
    observed, unsure = _observed_in_box(rec, settings.match_threshold)
    obs = rec.observations
    payload = {
        "channel": meta.get("channel"), "order_id": order.order_id,
        "order_lines": order.expected(), "observed_in_box": observed, "unclear_objects": unsure,
        "operator_verdict": meta.get("operator_verdict"),
        "fix_instructions": rec.outcome.fix_instructions, "uncertainty": obs.get("uncertainty"),
        "expected_vs_observed": obs.get("expected_vs_observed"), "scene": obs.get("scene"),
        "line_checks": [c.model_dump(mode="json") for c in rec.checks],
        "photos": photos, "photos_not_used": not_used,
        "usage": obs.get("usage"), "cached_response": obs.get("cached_response"), "decision_engine": ENGINE,
        "round2_decision": decision, "upstream": upstream,
    }
    if meta.get("operator_verdict"):
        payload["agent_agrees_with_operator"] = meta["operator_verdict"] == outcome
    return build_record(
        request, agent_id=AGENT_ID, record_id=record_id_for(request), captured_at=meta.get("captured_at") or utcnow(),
        operator_id=meta.get("operator_id"), unit_scope="order",
        refs={"order_id": order.order_id, "shipment_id": order.shipment_id},
        checks=checks, outcome=outcome, verdict=verdict, confidence=_conf(rec.checks),
        reason=" ".join(rec.outcome.reasons[:3])[:600], model=model_block(rec, provider),
        inputs=[{"ref": p["ref"], "sha256": p["original_sha256"], "kind": "image"} for p in photos],
        payload=payload, latency_ms=max((c.latency_ms for c in rec.checks if c.latency_ms), default=None))


def pending(request: dict, order: Order | None, meta: dict | None, *, code: str, message: str, retryable: bool,
            photos: list[dict] | None = None, checks: list[dict] | None = None, payload: dict | None = None) -> dict:
    """Fail open: a record exists, says UNCERTAIN and why, and keeps the photos. Never a made-up verdict."""
    meta = meta or {}
    body = {"order_id": order.order_id if order else None, "order_lines": order.expected() if order else None,
            "channel": meta.get("channel"), **(payload or {})}
    record = build_record(
        request, agent_id=AGENT_ID, record_id=pending_record_id(request),
        captured_at=meta.get("captured_at") or utcnow(), operator_id=meta.get("operator_id"), unit_scope="order",
        refs={"order_id": order.order_id if order else None}, checks=checks or [], outcome="pending_review",
        verdict="UNCERTAIN", needs_human=True, reason=f"{code}: {message}",
        model={"name": "none", "version": "0", "calls": 0, "cost_usd": None},
        inputs=[{"ref": p["ref"], "sha256": p["original_sha256"], "kind": "image"} for p in (photos or [])],
        payload=body, status="pending" if retryable else "error",
        error=error_obj(code, message, retryable=retryable, stage=STAGE, agent_id=AGENT_ID))
    return build_output(record, next_step="retry" if retryable else "review", reason=message)
