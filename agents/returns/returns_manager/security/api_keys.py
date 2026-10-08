"""Machine API keys (§6.1): `rmk_<env>_<40 random base62>`, stored as SHA-256 plus an 8-character prefix.

The plaintext key is returned exactly once, at creation. Lookup goes through `rm.resolve_api_key()` (a
cross-tenant SECURITY DEFINER function, allowlist item 2) because the org is not known before the key is.
"""

from __future__ import annotations

import hashlib
import re
import secrets
import string
from dataclasses import dataclass
from datetime import UTC, datetime

from returns_manager.db.pool import Database
from returns_manager.ids import new_id
from returns_manager.security.roles import Principal, Scope

_ALPHABET = string.ascii_letters + string.digits  # base62
_RANDOM_LEN = 40
_KEY_RE = re.compile(r"^rmk_(local|test|demo)_([A-Za-z0-9]{32,})$")


@dataclass(frozen=True)
class NewApiKey:
    key_id: str
    plaintext: str  # shown once; never stored or logged
    prefix: str
    org_id: str
    scopes: tuple[str, ...]


@dataclass(frozen=True)
class ApiKeyInfo:
    key_id: str
    org_id: str
    name: str
    key_prefix: str
    scopes: tuple[str, ...]
    created_by: str
    created_at: datetime
    expires_at: datetime | None
    revoked_at: datetime | None
    last_used_at: datetime | None


def hash_key(plaintext: str) -> bytes:
    return hashlib.sha256(plaintext.encode("utf-8")).digest()


def generate(env: str) -> tuple[str, str]:
    """Return (plaintext, prefix). The prefix is the first 8 random characters, for identification."""
    random_part = "".join(secrets.choice(_ALPHABET) for _ in range(_RANDOM_LEN))
    return f"rmk_{env}_{random_part}", random_part[:8]


def parse_scopes(values: list[str] | tuple[str, ...]) -> tuple[str, ...]:
    scopes = tuple(sorted({Scope(v).value for v in values}))
    if not scopes:
        raise ValueError("at least one scope is required")
    return scopes


async def create_key(
    db: Database,
    *,
    org_id: str,
    name: str,
    scopes: list[str] | tuple[str, ...],
    created_by: str,
    env: str,
    expires_at: datetime | None = None,
) -> NewApiKey:
    parsed = parse_scopes(scopes)
    plaintext, prefix = generate(env)
    key_id = new_id()
    async with db.transaction(org_id) as conn:
        await conn.execute(
            """INSERT INTO rm.api_keys
                 (key_id, org_id, name, key_prefix, key_hash, scopes, created_by, expires_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
            (key_id, org_id, name, prefix, hash_key(plaintext), list(parsed), created_by, expires_at),
        )
    return NewApiKey(key_id, plaintext, prefix, org_id, parsed)


async def list_keys(db: Database, org_id: str) -> list[ApiKeyInfo]:
    async with db.transaction(org_id) as conn:
        cur = await conn.execute(
            """SELECT key_id, org_id, name, key_prefix, scopes, created_by, created_at, expires_at,
                      revoked_at, last_used_at
               FROM rm.api_keys ORDER BY created_at"""
        )
        rows = await cur.fetchall()
    return [ApiKeyInfo(**{**r, "scopes": tuple(r["scopes"])}) for r in rows]


async def revoke_key(db: Database, org_id: str, key_id: str) -> bool:
    """Revoke a key of this org. Returns False if no such key exists in this org (→ 404)."""
    async with db.transaction(org_id) as conn:
        cur = await conn.execute(
            "UPDATE rm.api_keys SET revoked_at = coalesce(revoked_at, now()) WHERE key_id = %s", (key_id,)
        )
        return cur.rowcount == 1


async def authenticate(db: Database, presented: str, *, env: str) -> Principal | None:
    """Resolve a presented key to a Principal, or None (unknown, malformed, revoked, expired, wrong env)."""
    m = _KEY_RE.match(presented or "")
    if not m or m.group(1) != env:
        return None
    digest = hash_key(presented)
    async with db.transaction(None) as conn:
        cur = await conn.execute("SELECT * FROM rm.resolve_api_key(%s)", (digest,))
        row = await cur.fetchone()
    if row is None or row["revoked_at"] is not None:
        return None
    if row["expires_at"] is not None and row["expires_at"] <= datetime.now(UTC):
        return None
    async with db.transaction(row["org_id"]) as conn:
        await conn.execute("UPDATE rm.api_keys SET last_used_at = now() WHERE key_id = %s", (row["key_id"],))
    return Principal(
        kind="api_key",
        org_id=row["org_id"],
        actor_id=row["key_id"],
        scopes=frozenset(Scope(s) for s in row["scopes"]),
    )
