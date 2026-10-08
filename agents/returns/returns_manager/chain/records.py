"""Evidence records: finalize and supersede (§13.7).

A record is written at finalization (version 1) and on post-finalization overrides
(version N+1); the previous version is marked `superseded`.  A ledger entry is
appended for each write.

`document_sha256 = SHA-256(JCS(document))` is computed here before INSERT.

Never stored: thinking content, raw prompts, signed URLs, secrets.

Webhook dispatch (`evidence.finalized` / `evidence.superseded`, §17) does NOT happen
here. It used to: fired via `loop.create_task(...)` from inside this module, which runs
inside the caller's still-open transaction (see the docstrings below - these functions
must run inside `db.tenant.transaction(org_id)`). A fire-and-forget task scheduled there
has no guarantee the surrounding transaction ever commits, so a webhook could go out for
a record a later rollback undoes. The dispatch now happens in the one real caller,
`review/service.py`, after its transaction has actually committed - see
`HumanReviewService._dispatch_finalized_webhook`.
"""

from __future__ import annotations

import dataclasses
import json
from datetime import UTC, datetime
from typing import Any

from psycopg import AsyncConnection

from returns_manager.canonical.hashing import sha256_hex
from returns_manager.canonical.jcs import canonical_bytes
from returns_manager.chain.append import append_ledger_entry
from returns_manager.chain.event_types import RECORD_FINALIZED, RECORD_SUPERSEDED
from returns_manager.ids import new_id


def document_sha256_of(document: dict[str, Any]) -> str:
    """SHA-256(JCS(document)) — the canonical hash of an evidence document.

    No floats allowed (canonical_bytes raises CanonicalizationError).
    """
    return sha256_hex(canonical_bytes(document))


@dataclasses.dataclass(frozen=True)
class FinalizedRecord:
    evidence_id: str
    record_version: int
    document_sha256: str
    ledger_seq: int
    ledger_hash: str


async def finalize_record(
    conn: AsyncConnection[Any],
    *,
    org_id: str,
    return_id: str,
    unit_id: str,
    document: dict[str, Any],
    unit_head_event_hash: str,
    finalized_at: datetime | None = None,
) -> FinalizedRecord:
    """Write the initial evidence record (version 1) and a ledger entry.

    Must run inside `db.tenant.transaction(org_id)`.
    `document` must not contain floats (CanonicalizationError).
    """
    ts = finalized_at or datetime.now(UTC)
    doc_sha256 = document_sha256_of(document)
    evidence_id = new_id()
    record_version = 1

    await conn.execute(
        """
        INSERT INTO rm.evidence_records (
            evidence_id, org_id, return_id, unit_id,
            record_version, document, document_sha256,
            unit_head_event_hash, status, finalized_at
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'finalized', %s)
        """,
        (
            evidence_id,
            org_id,
            return_id,
            unit_id,
            record_version,
            json.dumps(document),
            doc_sha256,
            unit_head_event_hash,
            ts,
        ),
    )

    # Fetch the record_id from the returns table for the ledger payload
    cur = await conn.execute(
        "SELECT record_id FROM rm.returns WHERE org_id = %s AND return_id = %s",
        (org_id, return_id),
    )
    row = await cur.fetchone()
    record_id = row["record_id"] if row else return_id

    ledger = await append_ledger_entry(
        conn,
        org_id=org_id,
        entry_type=RECORD_FINALIZED,
        payload={
            "evidence_id": evidence_id,
            "unit_id": unit_id,
            "record_version": record_version,
        },
        record_id=record_id,
        record_version=record_version,
        document_sha256=doc_sha256,
        unit_head_event_hash=unit_head_event_hash,
        occurred_at=ts,
    )

    return FinalizedRecord(
        evidence_id=evidence_id,
        record_version=record_version,
        document_sha256=doc_sha256,
        ledger_seq=ledger.seq,
        ledger_hash=ledger.ledger_hash,
    )


async def supersede_record(
    conn: AsyncConnection[Any],
    *,
    org_id: str,
    return_id: str,
    unit_id: str,
    previous_evidence_id: str,
    previous_version: int,
    new_document: dict[str, Any],
    unit_head_event_hash: str,
    superseded_at: datetime | None = None,
) -> FinalizedRecord:
    """Write a superseding record (version N+1) and mark the previous as superseded.

    Must run inside `db.tenant.transaction(org_id)`.
    """
    ts = superseded_at or datetime.now(UTC)
    new_version = previous_version + 1
    doc_sha256 = document_sha256_of(new_document)
    evidence_id = new_id()

    # Insert new version
    await conn.execute(
        """
        INSERT INTO rm.evidence_records (
            evidence_id, org_id, return_id, unit_id,
            record_version, document, document_sha256,
            unit_head_event_hash, status, finalized_at
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'finalized', %s)
        """,
        (
            evidence_id,
            org_id,
            return_id,
            unit_id,
            new_version,
            json.dumps(new_document),
            doc_sha256,
            unit_head_event_hash,
            ts,
        ),
    )

    # Mark previous as superseded (narrow UPDATE — only status + superseded_* columns)
    await conn.execute(
        """
        UPDATE rm.evidence_records
        SET status = 'superseded', superseded_at = %s, superseded_by = %s
        WHERE org_id = %s AND evidence_id = %s
        """,
        (ts, evidence_id, org_id, previous_evidence_id),
    )

    cur = await conn.execute(
        "SELECT record_id FROM rm.returns WHERE org_id = %s AND return_id = %s",
        (org_id, return_id),
    )
    row = await cur.fetchone()
    record_id = row["record_id"] if row else return_id

    ledger = await append_ledger_entry(
        conn,
        org_id=org_id,
        entry_type=RECORD_SUPERSEDED,
        payload={
            "previous_evidence_id": previous_evidence_id,
            "evidence_id": evidence_id,
            "unit_id": unit_id,
            "record_version": new_version,
        },
        record_id=record_id,
        record_version=new_version,
        document_sha256=doc_sha256,
        unit_head_event_hash=unit_head_event_hash,
        occurred_at=ts,
    )

    return FinalizedRecord(
        evidence_id=evidence_id,
        record_version=new_version,
        document_sha256=doc_sha256,
        ledger_seq=ledger.seq,
        ledger_hash=ledger.ledger_hash,
    )
