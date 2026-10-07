"""Canonical JSON (RFC 8785) and SHA-256 helpers for everything the Round 2 engine hashes.

Round 3: the Round 2 modules `canonical/jcs.py` and `canonical/hashing.py` joined into one file, bodies unchanged.
These hashes (the disposition engine's `inputs_sha256`, reference-document hashes) are internal to this agent. The
Evidence Record's own `content_hash` is the pod's (shared/utils/hashing.py), not this one.
"""
from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from typing import Any

import rfc8785


class CanonicalizationError(ValueError):
    """The value cannot be canonicalised for hashing."""


def _reject_floats(value: Any, path: str) -> None:
    if isinstance(value, bool) or value is None or isinstance(value, int | str):
        return
    if isinstance(value, float):
        raise CanonicalizationError(f"float at {path or '$'}: hashed payloads must not contain floats")
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                raise CanonicalizationError(f"non-string key at {path or '$'}")
            _reject_floats(item, f"{path}.{key}")
        return
    if isinstance(value, Sequence):
        for index, item in enumerate(value):
            _reject_floats(item, f"{path}[{index}]")
        return
    raise CanonicalizationError(f"unsupported type {type(value).__name__} at {path or '$'}")


def canonical_bytes(value: Any) -> bytes:
    """Return the RFC 8785 canonical UTF-8 bytes of `value` (no floats allowed)."""
    _reject_floats(value, "")
    try:
        return rfc8785.dumps(value)
    except rfc8785.CanonicalizationError as exc:
        raise CanonicalizationError(str(exc)) from exc


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_jcs(value: Any) -> str:
    """Lowercase hex SHA-256 of the RFC 8785 canonical form of `value`."""
    return sha256_hex(canonical_bytes(value))


def normalize_text(data: bytes) -> bytes:
    """UTF-8 text with CRLF/CR line endings normalised to LF, so hashes match across Windows and Linux."""
    return data.replace(b"\r\n", b"\n").replace(b"\r", b"\n")


def sha256_text(data: bytes) -> str:
    return sha256_hex(normalize_text(data))
