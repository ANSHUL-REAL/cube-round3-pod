"""Turn a decided prep inspection into a Round 3 Evidence Record (and a fail-open pending record when it could not run).

Record id: ``PRP-<request_id>`` (sanitised), so the same request always gets the same record_id. A pending record
has its own id (``PRP-PENDING-<request_id>``) so it can never collide with a later judged one.
"""
from __future__ import annotations

import re

from shared.utils.records import build_output, build_record, error_obj, utcnow

from . import rules
from .captures import Capture, Prepared
from .requirements import RULE_SOURCE, RequirementPack
from .vision import PROMPT_VERSION, Observed
from .workorders import WorkOrder

STAGE = "prep"
VERSION = "0.1.0"
AGENT_ID = f"prep-manager@{VERSION}"
ENGINE = "prep-rules/1"  # the deterministic decision core (agents/prep/rules.py)
MEASUREMENTS_NOTE = (
    "Not measured. Weight cannot be read from a photograph, and the photos carry no scale or ruler reference, so "
    "length, width and height cannot be derived either. Left empty rather than estimated: downstream, weight-tier fee "
    "lines stay SILENT (finding F-07). A scale or a calibrated measuring station would be a different source.")
CONFIDENCE_NOTE = (
    "Check confidence is the model's own ordinal band mapped to a fixed number (high 0.9, medium 0.6, low 0.3). It is "
    "not a calibrated probability.")


def _safe(request: dict) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "-", request["request_id"])


def record_id_for(request: dict) -> str:
    """Same request in, same record_id out (the contract's idempotency rule)."""
    return f"PRP-{_safe(request)}"


def pending_record_id(request: dict) -> str:
    return f"PRP-PENDING-{_safe(request)}"


def model_block(observed: Observed) -> dict:
    return {"name": observed.model, "version": observed.model_version, "provider": observed.provider,
            "prompt_version": observed.prompt_version, "calls": observed.attempts, "cost_usd": observed.cost_usd}


def _photo_dict(p: Prepared) -> dict:
    return {"ref": p.ref, "original_sha256": p.sha256, "analysed_sha256": p.analysed_sha256, "width": p.width,
            "height": p.height}


def build(request: dict, order: WorkOrder, pack: RequirementPack, rule_checks: list[rules.RuleCheck],
          observed: Observed, photos: list[Prepared], extra: list[Capture], captured_at: str, captured_source: str,
          upstream: dict) -> dict:
    """The Evidence Record for a unit the model looked at."""
    checks = rules.to_contract_checks(rule_checks)
    verdict, outcome = rules.decide(checks)
    reason = rules.summarise(checks)
    if extra:
        reason += (f" {len(extra)} further photo(s) were not examined (limit {len(photos)}): "
                   + ", ".join(c.ref for c in extra) + ".")
    rec = (upstream or {}).get("receiving")
    if rec and rec["effective_verdict"] != "PASS":
        reason += f" Receiving's effective verdict for this unit is {rec['effective_verdict']} ({rec['record_id']})."
    confs = [c["confidence"] for c in checks if c.get("confidence") is not None]
    resp = observed.response
    payload = {
        "requirements": pack.describe(), "rule_source": RULE_SOURCE,
        "rule_checks": [r.as_dict() for r in rule_checks],
        "failed_checks": [c["check_key"] for c in checks if c["verdict"] == "FAIL"],
        "uncertain_checks": [{"check_key": c["check_key"], "uncertain_reason": c.get("uncertain_reason")}
                             for c in checks if c["verdict"] == "UNCERTAIN"],
        "not_verified": [{"check_id": c.check_id, "requirement": c.requirement,
                          "reason": "a physical property that a photograph cannot establish"}
                         for c in pack.not_verifiable()],
        "measurements": {}, "measurements_note": MEASUREMENTS_NOTE,
        "photos": [_photo_dict(p) for p in photos],
        "photos_not_used": [{"ref": c.ref, "sha256": c.sha256} for c in extra],
        "photo_quality": [q.model_dump() for q in resp.photo_quality],
        "label_text_read": resp.label_text_read.model_dump(),
        "usage": observed.usage, "decision_engine": ENGINE, "confidence_note": CONFIDENCE_NOTE,
        "captured_at_source": captured_source, "upstream": upstream,
    }
    if order.prep_price_usd is not None:
        payload["prep_price_usd"] = order.prep_price_usd
    return build_record(
        request, agent_id=AGENT_ID, record_id=record_id_for(request), captured_at=captured_at,
        operator_id=order.operator_id, unit_scope="unit", refs=order.refs, checks=checks, outcome=outcome,
        verdict=verdict, confidence=round(min(confs), 3) if confs else None, reason=reason,
        model=model_block(observed),
        inputs=[{"ref": p.ref, "sha256": p.sha256, "kind": "image"} for p in photos],
        payload=payload, latency_ms=observed.latency_ms)


def pending(request: dict, order: WorkOrder | None, *, code: str, message: str, retryable: bool,
            photos: list[Prepared] | None = None, extra: list[Capture] | None = None, calls: int = 0,
            model: dict | None = None, upstream: dict | None = None, captured_at: str | None = None) -> dict:
    """Fail open: a record exists, says UNCERTAIN and why, and keeps the photos. Never a made-up verdict."""
    body = {"requirements": order.requirements.describe() if order else None, "rule_source": RULE_SOURCE,
            "measurements": {}, "measurements_note": MEASUREMENTS_NOTE,
            "photos": [_photo_dict(p) for p in photos or []],
            "photos_not_used": [{"ref": c.ref, "sha256": c.sha256} for c in extra or []],
            "upstream": upstream or {}, "decision_engine": ENGINE}
    record = build_record(
        request, agent_id=AGENT_ID, record_id=pending_record_id(request), captured_at=captured_at or utcnow(),
        operator_id=order.operator_id if order else None, unit_scope="unit", refs=order.refs if order else None,
        checks=[], outcome="pending_review", verdict="UNCERTAIN", needs_human=True, reason=f"{code}: {message}",
        model=model or {"name": "none", "version": "0", "provider": None, "prompt_version": PROMPT_VERSION,
                        "calls": calls, "cost_usd": None},
        inputs=[{"ref": p.ref, "sha256": p.sha256, "kind": "image"} for p in photos or []],
        payload=body, status="pending" if retryable else "error",
        error=error_obj(code, message, retryable=retryable, stage=STAGE, agent_id=AGENT_ID))
    return build_output(record, next_step="retry" if retryable else "review", reason=message)
