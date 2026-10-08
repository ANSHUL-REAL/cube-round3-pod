"""Chain and ledger verifier (§13.5).

`verify_unit(conn, org_id, unit_id)` recomputes every payload hash and event hash,
checks seq continuity and the head row, checks each finalized record's
document_sha256 and unit_head_event_hash, and checks anchors.

`verify_org(conn, org_id)` runs verify_unit for every unit then verifies the ledger.

`verify_all(conn, org_id_list)` iterates all orgs.

Exit-code 3 is used by the CLI on failure (CLAUDE.md / §20).
"""

from __future__ import annotations

import dataclasses
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


@dataclasses.dataclass
class VerificationResult:
    org_id: str
    units_checked: int = 0
    events_checked: int = 0
    ledger_entries_checked: int = 0
    records_checked: int = 0
    anchors_checked: int = 0
    first_hash: str | None = None  # earliest event hash seen
    last_hash: str | None = None  # latest event hash seen (ledger or unit)
    valid: bool = True
    failures: list[str] = dataclasses.field(default_factory=list)

    def fail(self, message: str) -> None:
        self.valid = False
        self.failures.append(message)

    def summary_line(self) -> str:
        if self.valid:
            return (
                f"✓ chain valid · units: {self.units_checked} · "
                f"events: {self.events_checked} · "
                f"ledger entries: {self.ledger_entries_checked} · "
                f"first/last hash: {self.first_hash} / {self.last_hash}"
            )
        return "\n".join([f"✗ chain INVALID for org {self.org_id}", *self.failures])


async def verify_unit(
    conn: AsyncConnection[Any],
    org_id: str,
    unit_id: str,
    result: VerificationResult,
) -> None:
    """Verify the event chain for a single unit.

    Recomputes every hash from scratch and checks seq continuity.
    Must run inside a tenant transaction (app.org_id set).
    """
    cur = await conn.execute(
        """
        SELECT event_id, unit_id, return_id, seq,
               schema_version, event_type,
               to_char(
                   occurred_at AT TIME ZONE 'UTC',
                   'YYYY-MM-DD"T"HH24:MI:SS.US"+00:00"'
               ) AS occurred_at_str,
               actor_type, actor_id,
               payload, payload_sha256, prev_event_hash, event_hash
        FROM rm.unit_events
        WHERE org_id = %s AND unit_id = %s
        ORDER BY seq ASC
        """,
        (org_id, unit_id),
    )
    rows = await cur.fetchall()

    if not rows:
        return  # no events yet — fine

    # Initialise prev_hash from genesis
    expected_prev = genesis_hash(org_id, unit_id)

    for i, row in enumerate(rows):
        seq = row["seq"]
        expected_seq = i + 1

        # Seq continuity
        if seq != expected_seq:
            result.fail(f"unit {unit_id} seq gap: expected {expected_seq}, got {seq}")

        # Payload hash
        computed_p = payload_sha256(dict(row["payload"]))
        if computed_p != row["payload_sha256"]:
            result.fail(
                f"unit {unit_id} seq {seq}: payload_sha256 mismatch "
                f"(stored {row['payload_sha256']!r}, computed {computed_p!r})"
            )

        # Prev-hash linkage
        if row["prev_event_hash"] != expected_prev:
            result.fail(
                f"unit {unit_id} seq {seq}: prev_event_hash mismatch "
                f"(stored {row['prev_event_hash']!r}, expected {expected_prev!r})"
            )

        # Event hash
        core = event_core(
            schema_version=row["schema_version"],
            org_id=org_id,
            unit_id=row["unit_id"],
            return_id=row["return_id"],
            seq=seq,
            event_type=row["event_type"],
            occurred_at=row["occurred_at_str"],
            actor_type=row["actor_type"],
            actor_id=row["actor_id"],
            payload_sha256_hex=row["payload_sha256"],
        )
        computed_e = compute_event_hash(expected_prev, core)
        if computed_e != row["event_hash"]:
            result.fail(
                f"unit {unit_id} seq {seq}: event_hash mismatch "
                f"(stored {row['event_hash']!r}, computed {computed_e!r})"
            )

        expected_prev = row["event_hash"]
        result.events_checked += 1
        if result.first_hash is None:
            result.first_hash = row["event_hash"]
        result.last_hash = row["event_hash"]

    # Check chain head
    cur = await conn.execute(
        "SELECT last_seq, last_hash FROM rm.unit_chain_heads WHERE org_id = %s AND unit_id = %s",
        (org_id, unit_id),
    )
    head = await cur.fetchone()
    if head is None:
        result.fail(f"unit {unit_id}: chain head row missing")
    else:
        if head["last_seq"] != len(rows):
            result.fail(f"unit {unit_id}: head last_seq={head['last_seq']} but {len(rows)} events")
        if head["last_hash"] != expected_prev:
            result.fail(
                f"unit {unit_id}: head last_hash mismatch "
                f"(head={head['last_hash']!r}, chain_tip={expected_prev!r})"
            )

    # Check evidence records linked to this unit
    cur = await conn.execute(
        """
        SELECT evidence_id, return_id, record_version, document_sha256, unit_head_event_hash,
               document
        FROM rm.evidence_records
        WHERE org_id = %s AND unit_id = %s
        """,
        (org_id, unit_id),
    )
    rec_rows = await cur.fetchall()
    for rec in rec_rows:
        result.records_checked += 1
        # Recompute document_sha256
        from returns_manager.chain.records import document_sha256_of  # local import to avoid cycle

        computed_doc = document_sha256_of(dict(rec["document"]))
        if computed_doc != rec["document_sha256"]:
            result.fail(f"unit {unit_id} record {rec['evidence_id']}: document_sha256 mismatch")
        # unit_head_event_hash must be a hash that actually appears in this unit's chain
        if rec["unit_head_event_hash"] not in {r["event_hash"] for r in rows}:
            result.fail(
                f"unit {unit_id} record {rec['evidence_id']}: "
                f"unit_head_event_hash {rec['unit_head_event_hash']!r} not in chain"
            )

    result.units_checked += 1


async def verify_ledger(
    conn: AsyncConnection[Any],
    org_id: str,
    result: VerificationResult,
) -> None:
    """Verify the org ledger for `org_id`.

    Must run inside a tenant transaction (app.org_id set).
    """
    cur = await conn.execute(
        """
        SELECT entry_id, org_id, seq, entry_type,
               record_id, record_version, document_sha256, unit_head_event_hash,
               payload,
               prev_ledger_hash, ledger_hash,
               to_char(
                   occurred_at AT TIME ZONE 'UTC',
                   'YYYY-MM-DD"T"HH24:MI:SS.US"+00:00"'
               ) AS occurred_at_str
        FROM rm.org_ledger
        WHERE org_id = %s
        ORDER BY seq ASC
        """,
        (org_id,),
    )
    rows = await cur.fetchall()

    if not rows:
        return

    expected_prev = ledger_genesis_hash(org_id)

    for i, row in enumerate(rows):
        seq = row["seq"]
        expected_seq = i + 1
        if seq != expected_seq:
            result.fail(f"ledger seq gap for org {org_id}: expected {expected_seq}, got {seq}")

        computed_p = payload_sha256(dict(row["payload"]))

        if row["prev_ledger_hash"] != expected_prev:
            result.fail(f"ledger org {org_id} seq {seq}: prev_ledger_hash mismatch")

        core = ledger_entry_core(
            org_id=org_id,
            seq=seq,
            entry_type=row["entry_type"],
            record_id=row["record_id"],
            record_version=row["record_version"],
            document_sha256=row["document_sha256"],
            unit_head_event_hash=row["unit_head_event_hash"],
            payload_sha256_hex=computed_p,
            occurred_at=row["occurred_at_str"],
        )
        computed_l = compute_ledger_hash(expected_prev, core)
        if computed_l != row["ledger_hash"]:
            result.fail(
                f"ledger org {org_id} seq {seq}: ledger_hash mismatch "
                f"(stored {row['ledger_hash']!r}, computed {computed_l!r})"
            )

        expected_prev = row["ledger_hash"]
        result.ledger_entries_checked += 1
        result.last_hash = row["ledger_hash"]

    # Check ledger head
    cur = await conn.execute(
        "SELECT last_seq, last_hash FROM rm.org_ledger_heads WHERE org_id = %s",
        (org_id,),
    )
    head = await cur.fetchone()
    if head is None:
        # If there are ledger rows but no head, that's an error
        if rows:
            result.fail(f"org {org_id}: ledger head row missing")
    else:
        if head["last_seq"] != len(rows):
            result.fail(f"org {org_id}: ledger head last_seq={head['last_seq']} but {len(rows)} entries")
        if head["last_hash"] != expected_prev:
            result.fail(f"org {org_id}: ledger head last_hash mismatch")


async def verify_anchors(
    conn: AsyncConnection[Any],
    org_id: str,
    result: VerificationResult,
    anchors_file: str = "anchors/ledger-anchors.jsonl",
) -> None:
    """Check every anchored (ledger_seq, ledger_hash) still matches the live ledger.

    Only anchors for `org_id` are checked.  Missing anchors file is not an error
    (the feature is optional per §13.6).
    """
    import asyncio
    import json
    from pathlib import Path

    p = Path(anchors_file)
    exists = await asyncio.to_thread(p.exists)
    if not exists:
        return

    # Build lookup: seq → stored ledger_hash from the live DB
    cur = await conn.execute(
        "SELECT seq, ledger_hash FROM rm.org_ledger WHERE org_id = %s",
        (org_id,),
    )
    db_rows = await cur.fetchall()
    db_map: dict[int, str] = {r["seq"]: r["ledger_hash"] for r in db_rows}

    content = await asyncio.to_thread(p.read_text, encoding="utf-8")
    for line in content.splitlines():
        line = line.strip()
        if not line:
            continue
        anchor = json.loads(line)
        if anchor.get("org_id") != org_id:
            continue
        a_seq: int = anchor["ledger_seq"]
        a_hash: str = anchor["ledger_hash"]
        live_hash = db_map.get(a_seq)
        if live_hash is None:
            result.fail(
                f"anchor org {org_id} seq {a_seq}: seq not found in live ledger (anchored hash {a_hash!r})"
            )
        elif live_hash != a_hash:
            result.fail(
                f"anchor org {org_id} seq {a_seq}: ledger_hash mismatch "
                f"(anchored {a_hash!r}, live {live_hash!r})"
            )
        else:
            result.anchors_checked += 1
