"""Identifiers: ULIDs for records, events and jobs; uuid4 for storage keys (§4.1)."""

from __future__ import annotations

import uuid

from ulid import ULID


def new_id() -> str:
    """A new ULID (26 characters, lexicographically time-ordered)."""
    return str(ULID())


def new_storage_uuid() -> str:
    """An unguessable uuid4 for storage object keys."""
    return str(uuid.uuid4())
