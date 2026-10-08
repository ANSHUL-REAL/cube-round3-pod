"""RFC 8785 (JSON Canonicalization Scheme) serialisation for everything that is hashed (§8.1, §13.1).

Wraps the `rfc8785` package (API verified 2026-09-25: `rfc8785.dumps(obj) -> bytes`; it raises
`IntegerDomainError` outside ±(2**53 - 1) and `CanonicalizationError` for non-string keys or unsupported
types). On top of JCS this module refuses floats anywhere in the value: hashed payloads carry basis
points, minor units and strings only, so a float in a hashed payload is a bug, not a rounding question.
Strings are hashed exactly as given; JCS performs no Unicode normalisation.
"""

from __future__ import annotations

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
