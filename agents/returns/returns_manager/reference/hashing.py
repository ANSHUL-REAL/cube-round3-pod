"""Reference document content hashing (§8.1).

content_sha256 = SHA-256( RFC 8785 JCS( parsed_document without the content_sha256 field ) )
Because it hashes the parsed document (via JCS), whitespace and line endings do not affect the hash.
"""

from __future__ import annotations

from typing import Any

from returns_manager.canonical.hashing import sha256_jcs


def compute_reference_content_sha256(data: dict[str, Any]) -> str:
    """Compute the RFC 8785 JCS SHA-256 of `data` without the `content_sha256` key."""
    payload = {k: v for k, v in data.items() if k != "content_sha256"}
    return sha256_jcs(payload)


def verify_reference_content_sha256(data: dict[str, Any]) -> bool:
    """Return True if `data['content_sha256']` matches the computed hash."""
    expected = data.get("content_sha256")
    if not expected:
        return False
    return bool(compute_reference_content_sha256(data) == expected)
