"""Webhook HMAC-SHA256 signing and verification (§17 / P14).

Header format:
  RM-Signature: t=<unix>,v1=<hex HMAC-SHA256(secret, t + "." + raw_body)>
"""

from __future__ import annotations

import hashlib
import hmac
import time


def compute_signature(secret: str, body: bytes, timestamp: int | None = None) -> str:
    """Compute RM-Signature header value for body using secret.

    Format: t=<unix>,v1=<hex HMAC-SHA256(secret, t + "." + raw_body)>
    """
    ts = timestamp if timestamp is not None else int(time.time())
    payload = f"{ts}.".encode() + body
    sig = hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()
    return f"t={ts},v1={sig}"


def verify_signature(
    secret: str,
    body: bytes,
    header: str,
    *,
    max_age_seconds: int = 300,
    current_time: int | None = None,
) -> bool:
    """Verify an incoming RM-Signature header.

    Rejects:
    - Malformed headers
    - Signatures older than max_age_seconds (replay defense)
    - Future timestamps beyond clock skew tolerance (30 seconds)
    - Invalid HMAC digests
    """
    now = current_time if current_time is not None else int(time.time())

    # Parse components: t=<unix>,v1=<hex>
    parts = {}
    for item in header.split(","):
        if "=" in item:
            k, v = item.split("=", 1)
            parts[k.strip()] = v.strip()

    if "t" not in parts or "v1" not in parts:
        return False

    try:
        ts = int(parts["t"])
    except ValueError:
        return False

    # Check clock drift: reject if older than max_age_seconds or > 30s in the future
    if (now - ts) > max_age_seconds:
        return False
    if (ts - now) > 30:
        return False

    # Compute expected signature
    payload = f"{ts}.".encode() + body
    expected = hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()

    return hmac.compare_digest(expected, parts["v1"])
