"""Identities, roles, scopes and the authorization matrix (§6.1, §6.6).

A `Principal` is who is calling: a Supabase-authenticated user with one role in one org, or an API key
with scopes in one org. Every endpoint states one `Permission`; `require()` is the single check.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from returns_manager.errors import ReturnsManagerError


class Role(StrEnum):
    OPERATOR = "operator"
    REVIEWER = "reviewer"
    ADMIN = "admin"


class Scope(StrEnum):
    RETURNS_WRITE = "returns:write"
    RETURNS_READ = "returns:read"
    REVIEW_WRITE = "review:write"
    EVIDENCE_READ = "evidence:read"
    METRICS_READ = "metrics:read"
    ADMIN = "admin"


class Permission(StrEnum):
    RETURNS_WRITE = "returns_write"  # create returns, upload, submit, accept/override own org's decisions
    RETURNS_READ = "returns_read"  # read returns and jobs of the org
    PHOTO_URL = "photo_url"  # obtain a short-TTL signed URL for one of the org's photos
    REVIEW_WRITE = "review_write"  # resolve reviews, override dispose, reinspect, simulate
    SIGNOFF = "signoff"  # second-person sign-off (four-eyes checked in the service layer)
    EVIDENCE_READ = "evidence_read"  # evidence records, chain verification, export
    METRICS_READ = "metrics_read"
    ADMIN = "admin"  # kill-switch controls, API keys, reference data


_OPERATOR = frozenset(
    {
        Permission.RETURNS_WRITE,
        Permission.RETURNS_READ,
        Permission.PHOTO_URL,
        Permission.EVIDENCE_READ,
        Permission.METRICS_READ,
    }
)
_REVIEWER = _OPERATOR | {Permission.REVIEW_WRITE, Permission.SIGNOFF}
_ADMIN = frozenset(Permission)

ROLE_PERMISSIONS: dict[Role, frozenset[Permission]] = {
    Role.OPERATOR: _OPERATOR,
    Role.REVIEWER: frozenset(_REVIEWER),
    Role.ADMIN: _ADMIN,
}

SCOPE_PERMISSIONS: dict[Scope, frozenset[Permission]] = {
    Scope.RETURNS_WRITE: frozenset({Permission.RETURNS_WRITE}),
    Scope.RETURNS_READ: frozenset({Permission.RETURNS_READ, Permission.PHOTO_URL}),
    Scope.REVIEW_WRITE: frozenset({Permission.REVIEW_WRITE, Permission.SIGNOFF}),
    # Recovery's key: evidence records may embed short-TTL photo URLs when asked (include_photo_urls).
    Scope.EVIDENCE_READ: frozenset({Permission.EVIDENCE_READ, Permission.PHOTO_URL}),
    Scope.METRICS_READ: frozenset({Permission.METRICS_READ}),
    Scope.ADMIN: _ADMIN,
}


class Forbidden(ReturnsManagerError):
    """Authenticated, but not allowed to do this (HTTP 403)."""


class Unauthenticated(ReturnsManagerError):
    """No valid credentials (HTTP 401)."""


@dataclass(frozen=True)
class Principal:
    kind: str  # "user" | "api_key" | "cli"
    org_id: str
    actor_id: str  # user uuid, api key id, or cli:<name>
    role: Role | None = None
    scopes: frozenset[Scope] = field(default_factory=frozenset)
    operator_label: str | None = None

    @property
    def permissions(self) -> frozenset[Permission]:
        if self.kind == "user":
            return ROLE_PERMISSIONS[self.role] if self.role else frozenset()
        perms: set[Permission] = set()
        for scope in self.scopes:
            perms |= SCOPE_PERMISSIONS[scope]
        return frozenset(perms)

    @property
    def actor_label(self) -> str:
        """Human-readable actor for audit fields (never a secret)."""
        if self.kind == "user":
            return f"user:{self.operator_label or self.actor_id}"
        if self.kind == "api_key":
            return f"key:{self.actor_id}"
        return self.actor_id


def require(principal: Principal, permission: Permission) -> None:
    if permission not in principal.permissions:
        raise Forbidden(f"missing permission '{permission}'")
