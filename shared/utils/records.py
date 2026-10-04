"""Helpers to build contract-compliant Evidence Records and Agent Outputs.

Use these in your agent so the envelope is always right, then spend your time on the checks.
You may replace them, but your output must still validate.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from .hashing import seal

PREFIX = {"receiving": "RCV", "prep": "PRP", "pack": "PCK", "returns": "RTN", "recovery": "RCY"}
# What a stage's verdict suggests to the orchestrator. Advice only; the orchestrator decides.
RECOMMEND = {"PASS": "continue", "UNCERTAIN": "review", "FAIL": "route_to_recovery"}


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def check(check_key: str, verdict: str, confidence: float | None, *, expected: Any = None, observed: Any = None,
          detail: str = "", evidence_refs: list[str] | None = None, uncertain_reason: str | None = None) -> dict:
    c: dict[str, Any] = {"check_key": check_key, "verdict": verdict, "confidence": confidence}
    if expected is not None:
        c["expected"] = expected
    if observed is not None:
        c["observed"] = observed
    if detail:
        c["detail"] = detail
    if evidence_refs:
        c["evidence_refs"] = evidence_refs
    if verdict == "UNCERTAIN":
        c["uncertain_reason"] = uncertain_reason or "insufficient_evidence"
    return c


def rollup(checks: list[dict]) -> str:
    """Default roll-up: any FAIL -> FAIL; else any UNCERTAIN -> UNCERTAIN; else PASS.

    An empty check list is UNCERTAIN: nothing was checked, so nothing can be claimed.
    """
    verdicts = {c["verdict"] for c in checks}
    if "FAIL" in verdicts:
        return "FAIL"
    if "UNCERTAIN" in verdicts or not verdicts:
        return "UNCERTAIN"
    return "PASS"


def build_record(request: dict, *, agent_id: str, record_id: str, captured_at: str, checks: list[dict], outcome: str,
                 reason: str, model: dict, unit_scope: str = "unit", refs: dict | None = None,
                 status: str = "completed", operator_id: str | None = None, inputs: list[dict] | None = None,
                 payload: dict | None = None, upstream_refs: list[str] | None = None, verdict: str | None = None,
                 confidence: float | None = None, needs_human: bool | None = None, error: dict | None = None,
                 latency_ms: int | None = None, client_id: str | None = None) -> dict:
    """An Evidence Record for the stage/workflow/subject named in `request` (an Agent Input)."""
    verdict = verdict or rollup(checks)
    s = request["subject"]
    record: dict[str, Any] = {
        "schema_version": "1.0",
        "record_id": record_id,
        "workflow_id": request["workflow_id"],
        "stage": request["stage"],
        "agent_id": agent_id,
        "subject": {"org_id": s["org_id"], "subject_id": s["subject_id"], "unit_id": s["subject_id"],
                    "unit_scope": unit_scope, "refs": {k: v for k, v in (refs or {}).items() if v not in (None, "")}},
        "client_id": client_id,
        "status": status,
        "captured_at": captured_at,
        "produced_at": utcnow(),
        "latency_ms": latency_ms,
        "operator_id": operator_id,
        "model": model,
        "inputs": inputs or [],
        "checks": checks,
        "decision": {"verdict": verdict, "outcome": outcome, "confidence": confidence, "reason": reason,
                     "needs_human": verdict == "UNCERTAIN" if needs_human is None else needs_human},
        "payload": payload or {},
        "upstream_refs": upstream_refs or [r["record_id"] for r in request.get("previous_evidence", [])],
        "overrides": [],
        "error": error,
    }
    return seal(record)


def build_output(record: dict, *, next_step: str | None = None, reason: str = "") -> dict:
    """Wrap an Evidence Record in the Agent Output the orchestrator expects."""
    d = record["decision"]
    action = next_step or RECOMMEND[d["verdict"]]
    return {
        "schema_version": "1.0",
        "workflow_id": record["workflow_id"],
        "stage": record["stage"],
        "agent_id": record["agent_id"],
        "status": record["status"],
        "verdict": d["verdict"],
        "confidence": d.get("confidence"),
        "timestamp": record["produced_at"],
        "model": record["model"],
        "error": record.get("error"),
        "next_step_recommendation": {"action": action, "reason": reason or d["reason"]},
        "evidence": record,
    }


def error_obj(code: str, message: str, *, retryable: bool, stage: str | None = None, agent_id: str | None = None) -> dict:
    return {"code": code, "message": message, "retryable": retryable, "stage": stage, "agent_id": agent_id,
            "at": utcnow(), "detail": None}


def pending_output(request: dict, *, code: str, message: str, retryable: bool = True,
                   agent_id: str | None = None) -> dict:
    """Fail-open output: the agent could not judge, but a record still exists and nothing is hidden.

    Engineering rule 3: a model error or timeout must never block the line. This is NOT a judgment: it has
    no checks, an UNCERTAIN verdict, status "pending"/"error", and says why.
    """
    stage = request["stage"]
    agent_id = agent_id or f"{stage}-agent"
    safe_id = re.sub(r"[^A-Za-z0-9._-]", "-", request["request_id"])
    record = build_record(
        request, agent_id=agent_id, record_id=f"{PREFIX[stage]}-PENDING-{safe_id}", captured_at=utcnow(), checks=[],
        outcome="pending_review", reason=f"{code}: {message}", model={"name": "none", "version": "0", "calls": 0},
        status="pending" if retryable else "error", verdict="UNCERTAIN", needs_human=True,
        error=error_obj(code, message, retryable=retryable, stage=stage, agent_id=agent_id))
    return build_output(record, next_step="retry" if retryable else "review", reason=message)


def add_agent_override(record: dict, *, by: str, target: str, new_verdict: str, reason: str) -> dict:
    """Agent-level override (an operator disagreeing with this agent). Appends; never edits the original."""
    original = record["decision"] if target == "decision" else next(c for c in record["checks"] if c["check_key"] == target)
    updated = dict(record)
    updated["overrides"] = [*record.get("overrides", []), {
        "overridden_at": utcnow(), "overridden_by": by, "target": target,
        "original_verdict": original["verdict"], "new_verdict": new_verdict, "reason": reason}]
    return updated
