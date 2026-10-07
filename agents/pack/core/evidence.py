"""Evidence records: building, hashing, verifying and overriding.

content_hash is SHA-256 over the record's canonical JSON (sorted keys, no whitespace,
content_hash itself excluded). It covers the hashes of the stored photos. It makes any
edit to a record detectable when the record is compared with its hash. It is NOT an
append-only log or an externally anchored proof, and we don't claim it is.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone

from .models import Decision, EvidenceRecord, Override, RecordStatus


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def new_record_id() -> str:
    return str(uuid.uuid4())


def canonical_json(data: dict) -> str:
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def compute_hash(record: EvidenceRecord) -> str:
    data = record.model_dump(mode="json")
    data.pop("content_hash", None)
    return "sha256:" + hashlib.sha256(canonical_json(data).encode("utf-8")).hexdigest()


def seal(record: EvidenceRecord) -> EvidenceRecord:
    return record.model_copy(update={"content_hash": compute_hash(record)})


def verify(record: EvidenceRecord) -> bool:
    return bool(record.content_hash) and record.content_hash == compute_hash(record)


def verify_history(record: EvidenceRecord) -> bool | None:
    """Check the record, then rebuild it as it was before each override and check that against
    the override's prior_content_hash, back to the agent's original record.

    True: every version matches. False: something doesn't. None: an override was made before
    overrides stored what they replaced, so the earlier versions can't be rebuilt.
    """
    if not verify(record):
        return False
    current = record
    while current.overrides:
        last = current.overrides[-1]
        if last.prior_outcome is None or last.prior_status is None:
            return None
        current = current.model_copy(update={
            "overrides": current.overrides[:-1], "outcome": last.prior_outcome,
            "status": last.prior_status, "content_hash": last.prior_content_hash,
        })
        if not verify(current):
            return False
    return True


def apply_override(
    record: EvidenceRecord,
    new_decision: Decision,
    reason_code: str,
    operator_label: str,
    note: str = "",
    at: datetime | None = None,
) -> EvidenceRecord:
    """Record a human decision. The agent's checks and the prior outcome are kept.

    Refuses a record that doesn't match its content hash: sealing a new hash over an edited
    record would hide the edit.
    """
    if not verify(record):
        raise ValueError("This record doesn't match its content hash, so it can't be changed.")
    at = at or utcnow()
    entry = Override(
        original_decision=record.outcome.decision,
        new_decision=new_decision,
        reason_code=reason_code,
        note=note,
        operator_label=operator_label,
        at=at,
        prior_content_hash=record.content_hash,
        prior_outcome=record.outcome,
        prior_status=record.status,
    )
    outcome = record.outcome.model_copy(
        update={"decision": new_decision, "decided_by": f"operator:{operator_label}", "decided_at": at}
    )
    updated = record.model_copy(
        update={
            "overrides": [*record.overrides, entry],
            "outcome": outcome,
            "status": RecordStatus.OVERRIDDEN,
        }
    )
    return seal(updated)


def apply_check_override(
    record: EvidenceRecord,
    check_key: str,
    to_verdict: str,
    reason_code: str,
    operator_label: str,
    note: str = "",
    at: datetime | None = None,
) -> EvidenceRecord:
    """Record a person's correction of one check (Evidence Contract 1.1 key), e.g. "quantities
    correct: counted by hand". The agent's checks stay as they were, and the box decision doesn't
    change: deciding the box is apply_override. Refuses a record that doesn't match its hash."""
    from .contract import CHECK_KEYS, current_verdicts

    if check_key not in CHECK_KEYS or check_key == "order_matches_manifest":
        raise ValueError("That check can't be changed here. Decide the box with Seal or Stop instead.")
    if to_verdict not in ("pass", "fail", "uncertain"):
        raise ValueError("Choose pass, fail or unclear.")
    if not verify(record):
        raise ValueError("This record doesn't match its content hash, so it can't be changed.")
    from_verdict = current_verdicts(record)[check_key]
    if to_verdict == from_verdict:
        raise ValueError("That check already has that result.")
    at = at or utcnow()
    entry = Override(
        original_decision=record.outcome.decision,
        new_decision=record.outcome.decision,
        reason_code=reason_code,
        note=note,
        operator_label=operator_label,
        at=at,
        prior_content_hash=record.content_hash,
        prior_outcome=record.outcome,
        prior_status=record.status,
        check_key=check_key,
        from_verdict=from_verdict,
        to_verdict=to_verdict,
    )
    return seal(record.model_copy(update={"overrides": [*record.overrides, entry]}))
