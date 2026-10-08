"""P14 Explainer Agent REST route (§15, §11.14).

Endpoint:
  POST /api/v1/units/{unit_id}/explain

Requires EVIDENCE_READ permission (scoped to caller's org).
"""

from __future__ import annotations

from fastapi import APIRouter

from returns_manager.api.deps import PrincipalDep, ServicesDep
from returns_manager.explainer.service import (
    ExplainerService,
    ExplainRequest,
    ExplainResponse,
)
from returns_manager.security.roles import Permission, require

router = APIRouter(prefix="/api/v1/units", tags=["explainer"])


@router.post(
    "/{unit_id}/explain",
    response_model=ExplainResponse,
    summary="Explain a return decision grounded in evidence (§15, §11.14)",
)
async def explain_decision(
    unit_id: str,
    body: ExplainRequest,
    principal: PrincipalDep,
    svc: ServicesDep,
) -> ExplainResponse:
    """Answer question about unit_id using immutable evidence and events.

    Read-only; citations are strictly validated against stored records.
    """
    require(principal, Permission.EVIDENCE_READ)
    service = ExplainerService(svc.db.pool)
    return await service.explain(
        org_id=principal.org_id,
        unit_id=unit_id,
        question=body.question,
    )
