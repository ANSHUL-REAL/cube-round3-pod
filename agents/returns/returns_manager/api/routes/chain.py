"""GET /api/v1/units/{unit_id}/chain/verification — read-only chain verifier (§13.5).

Scoped to the caller's own org via the authenticated `Principal`, exactly like every other
evidence-bearing route (§15: "org member / evidence:read"; cross-org resources return 404,
not 403, per §6.5). `org_id` is never accepted from the caller — it used to be a plain query
parameter here, which meant any caller (with or without a valid credential — the route never
checked one) could read another organization's chain by naming its `org_id`. Found and fixed
during the P11 pass; see build-log.md.
"""

from __future__ import annotations

from fastapi import APIRouter, Query
from pydantic import BaseModel

from returns_manager.api.deps import PrincipalDep, ServicesDep
from returns_manager.chain.service import run_verify_unit
from returns_manager.security.roles import Permission, require

router = APIRouter(prefix="/api/v1/units", tags=["chain"])


class ChainVerificationResponse(BaseModel):
    org_id: str
    unit_id: str
    valid: bool
    units_checked: int
    events_checked: int
    ledger_entries_checked: int
    records_checked: int
    anchors_checked: int
    first_hash: str | None = None
    last_hash: str | None = None
    failures: list[str] = []
    summary: str


@router.get(
    "/{unit_id}/chain/verification",
    response_model=ChainVerificationResponse,
    summary="Verify the event chain for a unit (§13.5)",
)
async def verify_unit_chain(
    unit_id: str,
    principal: PrincipalDep,
    svc: ServicesDep,
    anchors_file: str = Query("anchors/ledger-anchors.jsonl", description="Anchors JSONL path."),
) -> ChainVerificationResponse:
    """Recompute every hash and check seq continuity for `unit_id` in the caller's org.

    This is a read-only operation. It does not modify any data.
    """
    require(principal, Permission.EVIDENCE_READ)
    result = await run_verify_unit(svc.db.pool, principal.org_id, unit_id, anchors_file)
    return ChainVerificationResponse(
        org_id=principal.org_id,
        unit_id=unit_id,
        valid=result.valid,
        units_checked=result.units_checked,
        events_checked=result.events_checked,
        ledger_entries_checked=result.ledger_entries_checked,
        records_checked=result.records_checked,
        anchors_checked=result.anchors_checked,
        first_hash=result.first_hash,
        last_hash=result.last_hash,
        failures=result.failures,
        summary=result.summary_line(),
    )
