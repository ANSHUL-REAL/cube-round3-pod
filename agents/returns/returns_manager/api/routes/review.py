"""P8 human-loop API routes. Routes translate HTTP only; service owns all decisions."""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field, model_validator

from returns_manager.api.deps import PrincipalDep, ServicesDep
from returns_manager.review.service import OverrideInput
from returns_manager.security.roles import Permission, require

router = APIRouter(prefix="/api/v1")


class OverrideRequest(BaseModel):
    field_path: str = Field(..., min_length=1, max_length=160)
    new_value: Any
    reason_code: str = Field(..., min_length=1)
    reason_text: str = Field(..., min_length=1, max_length=1000)

    def as_input(self) -> OverrideInput:
        return OverrideInput(**self.model_dump())


class DecisionRequest(BaseModel):
    action: Literal["accept", "override"]
    overrides: list[OverrideRequest] = Field(default_factory=list, max_length=50)
    note: str | None = Field(None, max_length=1000)

    @model_validator(mode="after")
    def validate_action(self) -> DecisionRequest:
        if self.action == "accept" and self.overrides:
            raise ValueError("accept must not include overrides")
        if self.action == "override" and not self.overrides:
            raise ValueError("override requires at least one override")
        return self


class ReviewResolutionRequest(BaseModel):
    overrides: list[OverrideRequest] = Field(default_factory=list, max_length=50)
    resolved_review_reasons: list[str] = Field(default_factory=list, max_length=50)
    note: str | None = Field(None, max_length=1000)


class SignoffRequest(BaseModel):
    decision: Literal["approved", "rejected"]
    reason: str = Field(..., min_length=1, max_length=1000)


@router.post("/returns/{return_id}/decision")
async def record_decision(
    return_id: str, body: DecisionRequest, principal: PrincipalDep, svc: ServicesDep
) -> dict[str, Any]:
    require(principal, Permission.RETURNS_WRITE)
    return (
        await svc.review.decide(
            org_id=principal.org_id,
            actor_id=principal.actor_id,
            actor_role=principal.role.value if principal.role else "operator",
            return_id=return_id,
            action=body.action,
            overrides=[item.as_input() for item in body.overrides],
            note=body.note,
        )
    ).to_dict()


@router.post("/returns/{return_id}/review-resolution")
async def resolve_review(
    return_id: str, body: ReviewResolutionRequest, principal: PrincipalDep, svc: ServicesDep
) -> dict[str, Any]:
    require(principal, Permission.REVIEW_WRITE)
    return (
        await svc.review.resolve_review(
            org_id=principal.org_id,
            reviewer_id=principal.actor_id,
            reviewer_role=principal.role.value if principal.role else "reviewer",
            return_id=return_id,
            overrides=[item.as_input() for item in body.overrides],
            resolved_review_reasons=body.resolved_review_reasons,
            note=body.note,
        )
    ).to_dict()


@router.post("/returns/{return_id}/signoffs")
async def signoff(
    return_id: str, body: SignoffRequest, principal: PrincipalDep, svc: ServicesDep
) -> dict[str, Any]:
    require(principal, Permission.SIGNOFF)
    return (
        await svc.review.signoff(
            org_id=principal.org_id,
            reviewer_id=principal.actor_id,
            return_id=return_id,
            approved=body.decision == "approved",
            reason=body.reason,
        )
    ).to_dict()


@router.get("/review-queue")
async def review_queue(
    principal: PrincipalDep,
    svc: ServicesDep,
    reason: str | None = None,
    limit: int = Query(50, ge=1, le=100),
) -> list[dict[str, Any]]:
    require(principal, Permission.REVIEW_WRITE)
    return await svc.review.review_queue(org_id=principal.org_id, reason=reason, limit=limit)
