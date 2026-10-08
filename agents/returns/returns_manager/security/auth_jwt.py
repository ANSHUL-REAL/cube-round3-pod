"""Supabase Auth JWT verification (§6.1): signature (project JWKS, or the legacy HS256 secret), iss, aud, exp.

After verification the user's org membership is resolved through `rm.user_memberships()` (allowlist
item 3). A user in several orgs must name the org with the `X-Org-Id` header; the membership is checked.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from typing import Any

import jwt

from returns_manager.config import Settings
from returns_manager.db.pool import Database
from returns_manager.errors import ConfigError
from returns_manager.security.roles import Principal, Role, Unauthenticated

_ASYMMETRIC = ["ES256", "RS256", "EdDSA"]


@dataclass(frozen=True)
class VerifiedToken:
    user_id: uuid.UUID
    claims: dict[str, Any]


class JwtVerifier:
    def __init__(self, settings: Settings) -> None:
        issuer = settings.supabase_jwt_issuer
        if not issuer and settings.supabase_url:
            issuer = f"{settings.supabase_url.rstrip('/')}/auth/v1"
        if not issuer:
            raise ConfigError("Missing required configuration: SUPABASE_JWT_ISSUER")
        self._issuer = issuer
        self._audience = settings.supabase_jwt_audience
        self._jwks: jwt.PyJWKClient | None = None
        self._secret: str | None = None
        if settings.supabase_jwks_url:
            self._jwks = jwt.PyJWKClient(
                settings.supabase_jwks_url, cache_keys=True, lifespan=300, timeout=10
            )
        elif settings.supabase_url and (
            settings.supabase_jwt_secret is None or not settings.supabase_jwt_secret.get_secret_value()
        ):
            jwks_url = f"{settings.supabase_url.rstrip('/')}/auth/v1/.well-known/jwks.json"
            self._jwks = jwt.PyJWKClient(jwks_url, cache_keys=True, lifespan=300, timeout=10)
        if settings.supabase_jwt_secret is not None and settings.supabase_jwt_secret.get_secret_value():
            self._secret = settings.supabase_jwt_secret.get_secret_value()
        if self._jwks is None and self._secret is None:
            raise ConfigError(
                "Missing required configuration: SUPABASE_JWKS_URL (or legacy SUPABASE_JWT_SECRET)"
            )

    def _key_for(self, token: str) -> tuple[Any, list[str]]:
        header = jwt.get_unverified_header(token)
        alg = header.get("alg")
        if alg == "HS256":
            if self._secret is None:
                raise Unauthenticated("HS256 tokens are not accepted by this deployment")
            return self._secret, ["HS256"]
        if alg not in _ASYMMETRIC or self._jwks is None:
            raise Unauthenticated("unsupported token algorithm")
        signing_key = self._jwks.get_signing_key_from_jwt(token)
        return signing_key.key, [alg]

    def verify_sync(self, token: str) -> VerifiedToken:
        try:
            key, algorithms = self._key_for(token)
            claims = jwt.decode(
                token,
                key,
                algorithms=algorithms,
                audience=self._audience,
                issuer=self._issuer,
                options={"require": ["exp", "iat", "sub", "aud", "iss"]},
                leeway=5,
            )
            return VerifiedToken(uuid.UUID(str(claims["sub"])), claims)
        except Unauthenticated:
            raise
        except (jwt.PyJWTError, ValueError, KeyError) as exc:
            raise Unauthenticated(f"invalid token: {type(exc).__name__}") from None

    async def verify(self, token: str) -> VerifiedToken:
        # PyJWKClient fetches the JWKS with blocking I/O (cached for 300 s): keep it off the event loop.
        return await asyncio.to_thread(self.verify_sync, token)


async def user_principal(db: Database, verified: VerifiedToken, requested_org: str | None) -> Principal:
    async with db.transaction(None) as conn:
        cur = await conn.execute("SELECT org_id, role FROM rm.user_memberships(%s)", (verified.user_id,))
        memberships = {r["org_id"]: r["role"] for r in await cur.fetchall()}
    if not memberships:
        raise Unauthenticated("user has no organization membership")
    if requested_org is None:
        if len(memberships) > 1:
            raise Unauthenticated("user belongs to several organizations: send X-Org-Id")
        org_id = next(iter(memberships))
    elif requested_org in memberships:
        org_id = requested_org
    else:
        # Same answer as "no membership": never reveal whether another org exists.
        raise Unauthenticated("no membership for the requested organization")
    async with db.transaction(org_id) as conn:
        cur = await conn.execute(
            "SELECT operator_label FROM rm.memberships WHERE user_id = %s", (verified.user_id,)
        )
        row = await cur.fetchone()
    return Principal(
        kind="user",
        org_id=org_id,
        actor_id=str(verified.user_id),
        role=Role(memberships[org_id]),
        operator_label=row["operator_label"] if row else None,
    )
