"""Which orders has each photo already been used for? (the photo_reuse check)

An in-process ledger keyed by (org_id, sha256 of the prepared photo). It is per-process and not persisted:
a restart forgets earlier uses, so this check can miss reuse across restarts. Documented in the README.
The same photo for the same order is a re-check, not reuse (see core.decision.reuse_check).
"""
from __future__ import annotations

import threading

_LOCK = threading.Lock()
_USES: dict[tuple[str, str], list[dict]] = {}


def earlier_uses(org_id: str, hashes: list[str]) -> list[dict]:
    with _LOCK:
        return [u for h in hashes for u in _USES.get((org_id, h), [])]


def remember(org_id: str, hashes: list[str], order_id: str, record_id: str) -> None:
    with _LOCK:
        for h in hashes:
            uses = _USES.setdefault((org_id, h), [])
            if not any(u["order_id"] == order_id and u["record_id"] == record_id for u in uses):
                uses.append({"order_id": order_id, "record_id": record_id})


def clear() -> None:  # tests
    with _LOCK:
        _USES.clear()
