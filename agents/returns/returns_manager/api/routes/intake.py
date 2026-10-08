"""Intake API routes (§9, §15): returns, photos, observations, and submit."""

from __future__ import annotations

import json
from typing import Annotated, Any

from fastapi import APIRouter, File, Form, Header, Response, UploadFile
from pydantic import BaseModel, Field

from returns_manager.api.deps import PrincipalDep, ServicesDep
from returns_manager.errors import BadRequest
from returns_manager.security.roles import Permission, require

router = APIRouter(prefix="/api/v1")


class CreateReturnRequest(BaseModel):
    order_id: str = Field(..., min_length=1)
    unit_id: str = Field(..., min_length=1)
    ordered_sku: str | None = None
    ordered_asin: str | None = None
    return_seq: int = Field(1, ge=1)
    record_id: str | None = None


class RecordObservationRequest(BaseModel):
    observed_state: str = Field(..., min_length=1)
    note: str | None = None


class SubmitReturnRequest(BaseModel):
    acknowledge_quality_warnings: bool = False
    note: str | None = None


@router.post("/returns", status_code=201)
async def create_return(
    body: CreateReturnRequest,
    principal: PrincipalDep,
    svc: ServicesDep,
) -> dict[str, Any]:
    require(principal, Permission.RETURNS_WRITE)
    rec = await svc.intake.create_return(
        org_id=principal.org_id,
        actor_id=principal.actor_id,
        order_id=body.order_id,
        unit_id=body.unit_id,
        ordered_sku=body.ordered_sku,
        ordered_asin=body.ordered_asin,
        return_seq=body.return_seq,
        record_id=body.record_id,
    )
    return rec.to_dict()


@router.get("/returns/{return_id}")
async def get_return(
    return_id: str,
    principal: PrincipalDep,
    svc: ServicesDep,
) -> dict[str, Any]:
    require(principal, Permission.RETURNS_READ)
    details = await svc.intake.get_return(
        org_id=principal.org_id,
        return_id=return_id,
    )
    return details.to_dict()


@router.post("/returns/{return_id}/photos", status_code=200)
async def upload_photo(
    return_id: str,
    principal: PrincipalDep,
    svc: ServicesDep,
    response: Response,
    file: Annotated[UploadFile, File()],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    role_hint: Annotated[str | None, Form()] = None,
    retake_of: Annotated[str | None, Form()] = None,
    client_transform: Annotated[str | None, Form()] = None,
) -> dict[str, Any]:
    require(principal, Permission.RETURNS_WRITE)

    parsed_transform = None
    if client_transform:
        try:
            parsed_transform = json.loads(client_transform)
        except Exception as exc:
            raise BadRequest(f"Invalid client_transform JSON: {exc}") from exc

    photo_bytes = await file.read()
    result = await svc.intake.upload_photo(
        org_id=principal.org_id,
        actor_id=principal.actor_id,
        return_id=return_id,
        photo_bytes=photo_bytes,
        idempotency_key=idempotency_key,
        client_transform=parsed_transform,
        role_hint=role_hint,
        retake_of=retake_of,
    )

    if result.idempotent_replay:
        response.headers["Idempotent-Replay"] = "true"

    return result.to_dict()


@router.post("/returns/{return_id}/observation", status_code=200)
async def record_observation(
    return_id: str,
    body: RecordObservationRequest,
    principal: PrincipalDep,
    svc: ServicesDep,
) -> dict[str, Any]:
    require(principal, Permission.RETURNS_WRITE)
    rec = await svc.intake.record_observation(
        org_id=principal.org_id,
        actor_id=principal.actor_id,
        return_id=return_id,
        observed_state=body.observed_state,
        note=body.note,
    )
    return rec.to_dict()


@router.post("/returns/{return_id}/submit", status_code=202)
async def submit_return(
    return_id: str,
    body: SubmitReturnRequest,
    principal: PrincipalDep,
    svc: ServicesDep,
) -> dict[str, Any]:
    require(principal, Permission.RETURNS_WRITE)
    res = await svc.intake.submit_return(
        org_id=principal.org_id,
        actor_id=principal.actor_id,
        return_id=return_id,
        acknowledge_quality_warnings=body.acknowledge_quality_warnings,
        note=body.note,
    )
    return res.to_dict()
