"""Cryptographic helpers for the event chain and org ledger (§13.1).

All functions produce lowercase hex SHA-256 strings.

Domain-separated hashes prevent cross-context collisions:
  - Genesis head:    b"rm/genesis/v1" || 0x00 || org_id_bytes || 0x00 || unit_id_bytes
  - Event hash:      b"rm/evt/v1"     || 0x00 || prev_hash_bytes || JCS(event_core)
  - Ledger hash:     b"rm/ledger/v1"  || 0x00 || prev_hash_bytes || JCS(entry_core)

Golden test vectors are in tests/unit/test_chain.py.  The inputs are fixed; the
expected outputs are committed so any accidental change to this module is caught.

No floats are allowed in hashed payloads (§13.1): canonical_bytes() from
canonical.jcs enforces this at serialisation time.
"""

from __future__ import annotations

import hashlib
from typing import Any

from returns_manager.canonical.hashing import sha256_hex
from returns_manager.canonical.jcs import canonical_bytes

# ── Payload hash (§13.1) ─────────────────────────────────────────────────────


def payload_sha256(payload: dict[str, Any]) -> str:
    """SHA-256 of the JCS canonical bytes of `payload`.

    Raises CanonicalizationError if `payload` contains floats.
    """
    return sha256_hex(canonical_bytes(payload))


# ── Genesis head (§13.1) ─────────────────────────────────────────────────────


def genesis_hash(org_id: str, unit_id: str) -> str:
    """Deterministic genesis prev_event_hash for a (org_id, unit_id) pair.

    SHA-256(b"rm/genesis/v1" || 0x00 || org_id.encode() || 0x00 || unit_id.encode())
    """
    data = b"rm/genesis/v1" + b"\x00" + org_id.encode("utf-8") + b"\x00" + unit_id.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


# ── Event hash (§13.1) ───────────────────────────────────────────────────────


def event_core(
    *,
    schema_version: str,
    org_id: str,
    unit_id: str,
    return_id: str,
    seq: int,
    event_type: str,
    occurred_at: str,  # ISO-8601 string; no floats allowed
    actor_type: str,
    actor_id: str,
    payload_sha256_hex: str,
) -> dict[str, Any]:
    """Build the event_core dict for hashing (§13.1).

    All values are strings or integers — no floats.
    `occurred_at` must be the ISO-8601 string stored in the DB.
    """
    return {
        "schema_version": schema_version,
        "org_id": org_id,
        "unit_id": unit_id,
        "return_id": return_id,
        "seq": seq,
        "event_type": event_type,
        "occurred_at": occurred_at,
        "actor": {"type": actor_type, "id": actor_id},
        "payload_sha256": payload_sha256_hex,
    }


def compute_event_hash(prev_event_hash: str, core: dict[str, Any]) -> str:
    """Domain-separated event hash.

    SHA-256(b"rm/evt/v1" || 0x00 || bytes.fromhex(prev_event_hash) || JCS(event_core))
    """
    data = b"rm/evt/v1" + b"\x00" + bytes.fromhex(prev_event_hash) + canonical_bytes(core)
    return hashlib.sha256(data).hexdigest()


# ── Ledger hash (§13.4) ──────────────────────────────────────────────────────


def ledger_genesis_hash(org_id: str) -> str:
    """Deterministic genesis prev_ledger_hash for an org.

    SHA-256(b"rm/ledger-genesis/v1" || 0x00 || org_id.encode())
    """
    data = b"rm/ledger-genesis/v1" + b"\x00" + org_id.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def ledger_entry_core(
    *,
    org_id: str,
    seq: int,
    entry_type: str,
    record_id: str | None,
    record_version: int | None,
    document_sha256: str | None,
    unit_head_event_hash: str | None,
    payload_sha256_hex: str,
    occurred_at: str,
) -> dict[str, Any]:
    """Build the entry_core dict for ledger hashing (§13.4).

    Absent optional fields are omitted so that the hash is stable whether or not
    the values are present — an absent record_id is different from a None record_id.
    """
    core: dict[str, Any] = {
        "org_id": org_id,
        "seq": seq,
        "entry_type": entry_type,
        "occurred_at": occurred_at,
        "payload_sha256": payload_sha256_hex,
    }
    if record_id is not None:
        core["record_id"] = record_id
    if record_version is not None:
        core["record_version"] = record_version
    if document_sha256 is not None:
        core["document_sha256"] = document_sha256
    if unit_head_event_hash is not None:
        core["unit_head_event_hash"] = unit_head_event_hash
    return core


def compute_ledger_hash(prev_ledger_hash: str, core: dict[str, Any]) -> str:
    """Domain-separated ledger hash.

    SHA-256(b"rm/ledger/v1" || 0x00 || bytes.fromhex(prev_ledger_hash) || JCS(entry_core))
    """
    data = b"rm/ledger/v1" + b"\x00" + bytes.fromhex(prev_ledger_hash) + canonical_bytes(core)
    return hashlib.sha256(data).hexdigest()
