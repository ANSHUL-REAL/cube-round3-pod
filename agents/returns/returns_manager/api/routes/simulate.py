"""What-if simulation endpoint (§12.6, §15): reviewer only; writes nothing; labelled SIMULATION."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field

from returns_manager.api.deps import PrincipalDep, ServicesDep
from returns_manager.disposition.simulate import simulate_for_return
from returns_manager.security.roles import Permission, require

router = APIRouter(prefix="/api/v1")


class SimulateRequest(BaseModel):
    return_id: str = Field(min_length=1)
    changes: dict[str, Any] = Field(default_factory=dict)


class SimulateResponse(BaseModel):
    mode: str
    changes: dict[str, Any]
    before: dict[str, Any]
    after: dict[str, Any]


@router.post("/simulate/disposition", response_model=SimulateResponse)
async def simulate_disposition(
    body: SimulateRequest, principal: PrincipalDep, svc: ServicesDep
) -> SimulateResponse:
    require(principal, Permission.REVIEW_WRITE)
    async with svc.db.transaction(principal.org_id) as conn:
        out = await simulate_for_return(conn, body.return_id, body.changes)
    return SimulateResponse(**out)
