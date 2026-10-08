"""Closed set of event types for the unit event chain and org ledger (§13.2).

This is the authoritative list.  Any code that emits an event must use one of
these constants — never a bare string — so that a typo is caught at import time.
"""

from __future__ import annotations

# ── Unit event chain ─────────────────────────────────────────────────────────
# Each value is the exact string stored in rm.unit_events.event_type.

RETURN_CREATED = "return_created"
PHOTO_RECEIVED = "photo_received"
PHOTO_QUALITY_ASSESSED = "photo_quality_assessed"
PHOTO_SUPERSEDED = "photo_superseded"
OPERATOR_OBSERVATION_RECORDED = "operator_observation_recorded"
RETURN_SUBMITTED = "return_submitted"
JOB_ENQUEUED = "job_enqueued"
INSPECTION_STARTED = "inspection_started"
CONTEXT_ASSEMBLED = "context_assembled"
MODEL_REQUEST_COMPLETED = "model_request_completed"
MODEL_TOOL_EXECUTED = "model_tool_executed"
MODEL_REFUSAL = "model_refusal"
INSPECTION_FAILED = "inspection_failed"
INSPECTION_SKIPPED = "inspection_skipped"
INSPECTION_COMPLETED = "inspection_completed"
VALIDATOR_APPLIED = "validator_applied"
IDENTITY_FUSED = "identity_fused"
ESCALATION_REQUESTED = "escalation_requested"
ESCALATION_COMPLETED = "escalation_completed"
AUDIT_COMPLETED = "audit_completed"
RETAKE_REQUESTED = "retake_requested"
DISPOSITION_COMPUTED = "disposition_computed"
OPERATOR_DECISION_RECORDED = "operator_decision_recorded"
OVERRIDE_RECORDED = "override_recorded"
REVIEW_RESOLUTION_RECORDED = "review_resolution_recorded"
SIGNOFF_RECORDED = "signoff_recorded"
RECORD_FINALIZED = "record_finalized"
RECORD_SUPERSEDED = "record_superseded"

# ── Org-level system/control chain (as ledger entries) ───────────────────────
CONTROL_CHANGED = "control_changed"
KEY_ISSUED = "key_issued"
KEY_REVOKED = "key_revoked"
CIRCUIT_OPENED = "circuit_opened"
CIRCUIT_CLOSED = "circuit_closed"

# ── Validation helpers ────────────────────────────────────────────────────────
_UNIT_EVENT_TYPES: frozenset[str] = frozenset(
    {
        RETURN_CREATED,
        PHOTO_RECEIVED,
        PHOTO_QUALITY_ASSESSED,
        PHOTO_SUPERSEDED,
        OPERATOR_OBSERVATION_RECORDED,
        RETURN_SUBMITTED,
        JOB_ENQUEUED,
        INSPECTION_STARTED,
        CONTEXT_ASSEMBLED,
        MODEL_REQUEST_COMPLETED,
        MODEL_TOOL_EXECUTED,
        MODEL_REFUSAL,
        INSPECTION_FAILED,
        INSPECTION_SKIPPED,
        INSPECTION_COMPLETED,
        VALIDATOR_APPLIED,
        IDENTITY_FUSED,
        ESCALATION_REQUESTED,
        ESCALATION_COMPLETED,
        AUDIT_COMPLETED,
        RETAKE_REQUESTED,
        DISPOSITION_COMPUTED,
        OPERATOR_DECISION_RECORDED,
        OVERRIDE_RECORDED,
        REVIEW_RESOLUTION_RECORDED,
        SIGNOFF_RECORDED,
        RECORD_FINALIZED,
        RECORD_SUPERSEDED,
    }
)

_LEDGER_ENTRY_TYPES: frozenset[str] = frozenset(
    {
        RECORD_FINALIZED,
        RECORD_SUPERSEDED,
        CONTROL_CHANGED,
        KEY_ISSUED,
        KEY_REVOKED,
        CIRCUIT_OPENED,
        CIRCUIT_CLOSED,
    }
)


def validate_unit_event_type(event_type: str) -> str:
    if event_type not in _UNIT_EVENT_TYPES:
        raise ValueError(f"Unknown unit event type: {event_type!r}")
    return event_type


def validate_ledger_entry_type(entry_type: str) -> str:
    if entry_type not in _LEDGER_ENTRY_TYPES:
        raise ValueError(f"Unknown ledger entry type: {entry_type!r}")
    return entry_type
