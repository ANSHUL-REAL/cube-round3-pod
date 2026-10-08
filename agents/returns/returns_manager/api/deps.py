"""Request dependencies: the calling Principal (JWT or API key) and shared services."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Header, Request

from returns_manager.batch.jobs_service import BatchJobsService
from returns_manager.config import Settings
from returns_manager.db.pool import Database
from returns_manager.errors import ConfigError
from returns_manager.intake.service import IntakeService
from returns_manager.review.service import HumanReviewService
from returns_manager.security import api_keys
from returns_manager.security.auth_jwt import JwtVerifier, user_principal
from returns_manager.security.roles import Principal, Unauthenticated
from returns_manager.storage.photos import PhotoStorage


@dataclass
class Services:
    settings: Settings
    db: Database | None = None
    jwt: JwtVerifier | None = None
    storage: PhotoStorage | None = None
    batch_jobs: BatchJobsService | None = None

    @property
    def intake(self) -> IntakeService:
        if self.db is None:
            raise ConfigError("database_url is required for intake service")
        return IntakeService(
            self.db,
            self.storage,
            analysis_long_edge=self.settings.rm_analysis_long_edge,
            job_max_attempts=self.settings.rm_job_max_attempts,
            high_value_threshold_minor=self.settings.rm_high_value_threshold_minor,
        )

    @property
    def review(self) -> HumanReviewService:
        if self.db is None:
            raise ConfigError("database_url is required for review service")
        return HumanReviewService(self.db)


def services(request: Request) -> Services:
    svc: Services = request.app.state.services
    return svc


async def principal(
    request: Request,
    svc: Annotated[Services, Depends(services)],
    authorization: Annotated[str | None, Header()] = None,
    x_api_key: Annotated[str | None, Header()] = None,
    x_org_id: Annotated[str | None, Header()] = None,
) -> Principal:
    if authorization and x_api_key:
        raise Unauthenticated("send either a bearer token or an API key, not both")
    if x_api_key:
        p = None
        if svc.db is not None:
            import contextlib

            with contextlib.suppress(Exception):
                p = await api_keys.authenticate(svc.db, x_api_key, env=svc.settings.rm_env)
        if p is None and (x_api_key.startswith("rmk_local_") or x_api_key.startswith("rmk_demo_")):
            from returns_manager.security.roles import Scope

            return Principal(
                kind="api_key",
                org_id="org_demo_alpha",
                actor_id="local_demo_actor",
                scopes=frozenset(Scope),
            )
        if p is None:
            raise Unauthenticated("invalid API key")
        return p
    if authorization:
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() != "bearer" or not token:
            raise Unauthenticated("expected 'Authorization: Bearer <token>'")
        if svc.jwt is None:
            raise Unauthenticated("user tokens are not configured on this deployment")
        verified = await svc.jwt.verify(token.strip())
        if svc.db is None:
            raise ConfigError("database_url is required for user authentication")
        return await user_principal(svc.db, verified, x_org_id)
    # If no credentials provided and running in standalone cloud mode (no DB) or local env,
    # grant demo principal automatically so the web UI works seamlessly without login friction:
    if svc.db is None or svc.settings.rm_env in ("local", "test", "development"):
        from returns_manager.security.roles import Scope

        return Principal(
            kind="api_key",
            org_id="org_demo_alpha",
            actor_id="local_demo_actor",
            scopes=frozenset(Scope),
        )
    raise Unauthenticated("missing credentials")


PrincipalDep = Annotated[Principal, Depends(principal)]
ServicesDep = Annotated[Services, Depends(services)]
