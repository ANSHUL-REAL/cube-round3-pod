"""SHA-256 helpers over canonical JSON and LF-normalised text (§8.1, §13.1)."""

from __future__ import annotations

import hashlib
from typing import Any

from returns_manager.canonical.jcs import canonical_bytes


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
