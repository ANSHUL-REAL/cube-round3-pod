"""Atomic event-append and org-ledger-append (§13.3, §13.4).

Both operations run inside a SINGLE transaction that the caller owns.
The connection is obtained via `db.tenant.transaction(org_id)`.

Append algorithm for a unit event (§13.3):
  1. SELECT … FROM rm.unit_chain_heads WHERE org_id=$1 AND unit_id=$2 FOR UPDATE
     (or insert the genesis head if the row does not exist yet).
  2. seq = last_seq + 1. Compute payload_sha256 and event_hash.
  3. INSERT into rm.unit_events.  UNIQUE(org_id, unit_id, seq) prevents forks.
  4. UPDATE rm.unit_chain_heads.

This function is intentionally low-level: it takes a pre-validated payload dict and
a pre-built actor dict; callers are responsible for building meaningful payloads.
"""

from __future__ import annotations

import dataclasses
import json
from datetime import UTC, datetime
from typing import Any

from psycopg import AsyncConnection

from returns_manager.chain.crypto import (
    compute_event_hash,
    compute_ledger_hash,
    event_core,
    genesis_hash,
    ledger_entry_core,
    ledger_genesis_hash,
    payload_sha256,
)
from returns_manager.chain.event_types import (
    validate_ledger_entry_type,
    validate_unit_event_type,
)
from returns_manager.ids import new_id

SCHEMA_VERSION = "rm/evt/v1"


@dataclasses.dataclass(frozen=True)
class AppendedEvent:
    event_id: str
    seq: int
    event_hash: str
    occurred_at: str  # ISO-8601 UTC string as stored


@dataclasses.dataclass(frozen=True)
class AppendedLedgerEntry:
    entry_id: str
    seq: int
    ledger_hash: str
    occurred_at: str


# ── Unit event append ─────────────────────────────────────────────────────────


async def append_event(
    conn: AsyncConnection[Any],
    *,
    org_id: str,
    unit_id: str,
    return_id: str,
    event_type: str,
    actor_type: str,
    actor_id: str,
    payload: dict[str, Any],
    occurred_at: datetime | None = None,
) -> AppendedEvent:
    """Append one event to the unit chain inside `conn`'s transaction.

    The caller must be inside `db.tenant.transaction(org_id)` (conn has
    app.org_id set for the transaction).

    `payload` must contain no floats (CanonicalizationError is raised otherwise).
    `occurred_at` defaults to now(UTC); pass an explicit value for replays.
    """
    validate_unit_event_type(event_type)

    ts = occurred_at or datetime.now(UTC)
    # Normalise to UTC ISO-8601 with fixed-width offset +00:00 for stable hashing
    occurred_at_str = ts.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f+00:00")

    p_sha256 = payload_sha256(payload)

    # 1. Lock/upsert the head row
    await conn.execute(
        """
        INSERT INTO rm.unit_chain_heads (org_id, unit_id, last_seq, last_hash)
        VALUES (%s, %s, 0, %s)
        ON CONFLICT (org_id, unit_id) DO UPDATE SET updated_at = now()
        """,
        (org_id, unit_id, genesis_hash(org_id, unit_id)),
    )
    # Re-lock with FOR UPDATE to serialise concurrent appends for this unit
    cur = await conn.execute(
        "SELECT last_seq, last_hash FROM rm.unit_chain_heads WHERE org_id = %s AND unit_id = %s FOR UPDATE",
        (org_id, unit_id),
    )
    row = await cur.fetchone()
    assert row is not None  # just inserted above

    last_seq: int = row["last_seq"]
    prev_hash: str = row["last_hash"]
    seq = last_seq + 1

    # 2. Compute hashes
    core = event_core(
        schema_version=SCHEMA_VERSION,
        org_id=org_id,
        unit_id=unit_id,
        return_id=return_id,
        seq=seq,
        event_type=event_type,
        occurred_at=occurred_at_str,
        actor_type=actor_type,
        actor_id=actor_id,
        payload_sha256_hex=p_sha256,
    )
    e_hash = compute_event_hash(prev_hash, core)
    event_id = new_id()

    # 3. Insert the event (UNIQUE on (org_id, unit_id, seq) prevents forks)
    await conn.execute(
        """
        INSERT INTO rm.unit_events (
            event_id, org_id, unit_id, return_id, seq,
            schema_version, event_type, occurred_at,
            actor_type, actor_id,
            payload, payload_sha256,
            prev_event_hash, event_hash
        ) VALUES (
            %s, %s, %s, %s, %s,
            %s, %s, %s,
            %s, %s,
            %s, %s,
            %s, %s
        )
        """,
        (
            event_id,
            org_id,
            unit_id,
            return_id,
            seq,
            SCHEMA_VERSION,
            event_type,
            ts,
            actor_type,
            actor_id,
            json.dumps(payload),
            p_sha256,
            prev_hash,
            e_hash,
        ),
    )

    # 4. Update the head
    await conn.execute(
        """
        UPDATE rm.unit_chain_heads
        SET last_seq = %s, last_event_id = %s, last_hash = %s, updated_at = now()
        WHERE org_id = %s AND unit_id = %s
        """,
        (seq, event_id, e_hash, org_id, unit_id),
    )

    return AppendedEvent(
        event_id=event_id,
        seq=seq,
        event_hash=e_hash,
        occurred_at=occurred_at_str,
    )


# ── Org ledger append ─────────────────────────────────────────────────────────


async def append_ledger_entry(
    conn: AsyncConnection[Any],
    *,
    org_id: str,
    entry_type: str,
    payload: dict[str, Any],
    record_id: str | None = None,
    record_version: int | None = None,
    document_sha256: str | None = None,
    unit_head_event_hash: str | None = None,
    occurred_at: datetime | None = None,
) -> AppendedLedgerEntry:
    """Append one entry to the org ledger inside `conn`'s transaction.

    Called for record_finalized, record_superseded and system/control events.
    """
    validate_ledger_entry_type(entry_type)

    ts = occurred_at or datetime.now(UTC)
    occurred_at_str = ts.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f+00:00")
    p_sha256 = payload_sha256(payload)

    # Lock/upsert the org ledger head
    await conn.execute(
        """
        INSERT INTO rm.org_ledger_heads (org_id, last_seq, last_hash)
        VALUES (%s, 0, %s)
        ON CONFLICT (org_id) DO NOTHING
        """,
        (org_id, ledger_genesis_hash(org_id)),
    )
    cur = await conn.execute(
        "SELECT last_seq, last_hash FROM rm.org_ledger_heads WHERE org_id = %s FOR UPDATE",
        (org_id,),
    )
    row = await cur.fetchone()
    assert row is not None

    last_seq: int = row["last_seq"]
    prev_hash: str = row["last_hash"]
    seq = last_seq + 1

    core = ledger_entry_core(
        org_id=org_id,
        seq=seq,
        entry_type=entry_type,
        record_id=record_id,
        record_version=record_version,
        document_sha256=document_sha256,
        unit_head_event_hash=unit_head_event_hash,
        payload_sha256_hex=p_sha256,
        occurred_at=occurred_at_str,
    )
    l_hash = compute_ledger_hash(prev_hash, core)
    entry_id = new_id()

    await conn.execute(
        """
        INSERT INTO rm.org_ledger (
            entry_id, org_id, seq, entry_type,
            record_id, record_version, document_sha256, unit_head_event_hash,
            payload, prev_ledger_hash, ledger_hash, occurred_at
        ) VALUES (
            %s, %s, %s, %s,
            %s, %s, %s, %s,
            %s, %s, %s, %s
        )
        """,
        (
            entry_id,
            org_id,
            seq,
            entry_type,
            record_id,
            record_version,
            document_sha256,
            unit_head_event_hash,
            json.dumps(payload),
            prev_hash,
            l_hash,
            ts,
        ),
    )

    await conn.execute(
        """
        UPDATE rm.org_ledger_heads
        SET last_seq = %s, last_hash = %s, updated_at = now()
        WHERE org_id = %s
        """,
        (seq, l_hash, org_id),
    )

    return AppendedLedgerEntry(
        entry_id=entry_id,
        seq=seq,
        ledger_hash=l_hash,
        occurred_at=occurred_at_str,
    )
