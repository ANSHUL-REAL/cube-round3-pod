"""P1 endpoints: photo signed URLs, kill-switch controls, API keys (§15). Handlers only call services."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Query, Response
from pydantic import BaseModel, Field

from returns_manager.api.deps import PrincipalDep, ServicesDep
from returns_manager.api.problems import Problem
from returns_manager.errors import NotFound
from returns_manager.security import api_keys, controls
from returns_manager.security.roles import Permission, Scope, require
from returns_manager.storage.signed_urls import signed_url_for_photo

router = APIRouter(prefix="/api/v1")


class PhotoUrlResponse(BaseModel):
    photo_id: str
    variant: str
    url: str
    expires_at: datetime
    ttl_s: int


@router.get("/photos/{photo_id}/url", response_model=PhotoUrlResponse)
async def photo_url(
    photo_id: str,
    principal: PrincipalDep,
    svc: ServicesDep,
    variant: Literal["original", "analysis"] = Query("analysis"),
) -> PhotoUrlResponse:
    if svc.storage is None:
        raise Problem(503, "storage_unavailable", "Storage is not configured")
    ttl = svc.settings.rm_signed_url_ttl_s
    signed = await signed_url_for_photo(svc.db, svc.storage, principal, photo_id, variant, ttl)
    return PhotoUrlResponse(
        photo_id=signed.photo_id,
        variant=signed.variant,
        url=signed.url,
        expires_at=signed.expires_at,
        ttl_s=ttl,
    )


class ControlOut(BaseModel):
    control: str
    enabled: bool
    global_enabled: bool | None
    org_enabled: bool | None
    reason: str | None
    updated_by: str | None
    updated_at: datetime | None


class ControlsResponse(BaseModel):
    org_id: str
    controls: list[ControlOut]


class ControlUpdate(BaseModel):
    control: controls.Control
    enabled: bool
    reason: str = Field(min_length=1, max_length=500)


def _controls_out(org_id: str, eff: dict[controls.Control, controls.EffectiveControl]) -> ControlsResponse:
    out = []
    for c in controls.Control:
        e = eff[c]
        latest = max((r for r in (e.global_row, e.org_row) if r), key=lambda r: r.updated_at, default=None)
        out.append(
            ControlOut(
                control=c.value,
                enabled=e.enabled,
                global_enabled=e.global_row.enabled if e.global_row else None,
                org_enabled=e.org_row.enabled if e.org_row else None,
                reason=latest.reason if latest else None,
                updated_by=latest.updated_by if latest else None,
                updated_at=latest.updated_at if latest else None,
            )
        )
    return ControlsResponse(org_id=org_id, controls=out)


@router.get("/system/controls", response_model=ControlsResponse)
async def get_controls(principal: PrincipalDep, svc: ServicesDep) -> ControlsResponse:
    require(principal, Permission.ADMIN)
    return _controls_out(principal.org_id, await controls.effective(svc.db, principal.org_id))


@router.put("/system/controls", response_model=ControlsResponse)
async def put_control(body: ControlUpdate, principal: PrincipalDep, svc: ServicesDep) -> ControlsResponse:
    await controls.set_org_control(svc.db, principal, body.control, body.enabled, body.reason)
    return _controls_out(principal.org_id, await controls.effective(svc.db, principal.org_id))


class KeyCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    scopes: list[Scope] = Field(min_length=1)
    expires_at: datetime | None = None


class KeyCreated(BaseModel):
    key_id: str
    api_key: str = Field(description="Shown once. Store it now; it cannot be retrieved again.")
    key_prefix: str
    org_id: str
    scopes: list[str]


@router.post("/keys", response_model=KeyCreated, status_code=201)
async def create_key(body: KeyCreate, principal: PrincipalDep, svc: ServicesDep) -> KeyCreated:
    require(principal, Permission.ADMIN)
    new = await api_keys.create_key(
        svc.db,
        org_id=principal.org_id,
        name=body.name,
        scopes=[s.value for s in body.scopes],
        created_by=principal.actor_label,
        env=svc.settings.rm_env,
        expires_at=body.expires_at,
    )
    return KeyCreated(
        key_id=new.key_id,
        api_key=new.plaintext,
        key_prefix=new.prefix,
        org_id=new.org_id,
        scopes=list(new.scopes),
    )


@router.delete("/keys/{key_id}", status_code=204)
async def revoke_key(key_id: str, principal: PrincipalDep, svc: ServicesDep) -> Response:
    require(principal, Permission.ADMIN)
    if not await api_keys.revoke_key(svc.db, principal.org_id, key_id):
        raise NotFound("key not found")
    return Response(status_code=204)
