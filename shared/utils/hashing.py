"""Content hashing for Evidence Records.

The hash covers the canonical JSON of the record, excluding `content_hash` and the optional
agent-level `overrides` (appended after the fact).

Be honest in your docs: this is a content hash. It is not tamper-evident or immutable unless you also
anchor it somewhere you can show.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

EXCLUDED_KEYS = ("content_hash", "overrides")


def canonical_json(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def content_hash(record: dict) -> str:
    body = {k: v for k, v in record.items() if k not in EXCLUDED_KEYS}
    return hashlib.sha256(canonical_json(body)).hexdigest()


def seal(record: dict) -> dict:
    """Return the record with `content_hash` (re)computed."""
    record = dict(record)
    record["content_hash"] = content_hash(record)
    return record


def verify(record: dict) -> bool:
    return record.get("content_hash") == content_hash(record)
