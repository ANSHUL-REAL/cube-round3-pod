"""Persistence for blind P9 escalation/audit outcomes.

The caller supplies independently produced, validated inspection values.  This layer
does not invoke a model and therefore cannot accidentally expose the primary verdict
to an escalation prompt or let an audit mutate the evidence record.
"""

from __future__ import annotations

import json
from typing import Any

from returns_manager.chain import event_types as ET
from returns_manager.chain.append import append_event
from returns_manager.db.pool import Database
from returns_manager.errors import NotFound
from returns_manager.ids import new_id
from returns_manager.jobs.statemachine import ReturnStatus, transition_return_status
from returns_manager.review.merge import audit_disagreements, audit_sampled, merge_area


class EscalationAuditService:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def merge(
        self,
        *,
        org_id: str,
        return_id: str,
        primary_inspection_id: str,
        escalation_inspection_id: str,
        primary: dict[str, Any],
        escalation: dict[str, Any],
    ) -> dict[str, Any]:
        """Persist each field's exact §11.12 merge-table result, never overwrite primary output."""
        if set(primary) != set(escalation):
            raise ValueError("primary and escalation areas must have identical keys")
        async with self.db.transaction(org_id) as conn:
            cur = await conn.execute(
                "SELECT unit_id FROM rm.returns WHERE org_id = %s AND return_id = %s FOR UPDATE",
                (org_id, return_id),
            )
            ret = await cur.fetchone()
            if ret is None:
                raise NotFound(f"Return '{return_id}' not found")
            outcomes: dict[str, Any] = {}
            for area in sorted(primary):
                outcome = merge_area(primary[area], escalation[area])
                outcomes[area] = {"value": outcome.value, "outcome": outcome.outcome}
                await conn.execute(
                    """INSERT INTO rm.escalation_merges (
                         merge_id, org_id, return_id, primary_inspection_id, escalation_inspection_id,
                         area, primary_value, escalation_value, merged_value, outcome
                       ) VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s::jsonb, %s)""",
                    (
                        new_id(),
                        org_id,
                        return_id,
                        primary_inspection_id,
                        escalation_inspection_id,
                        area,
                        json.dumps(primary[area]),
                        json.dumps(escalation[area]),
                        json.dumps(outcome.value),
                        outcome.outcome,
                    ),
                )
            await append_event(
                conn,
                org_id=org_id,
                unit_id=ret["unit_id"],
                return_id=return_id,
                event_type=ET.ESCALATION_COMPLETED,
                actor_type="system",
                actor_id="escalation-merge",
                payload={
                    "primary_inspection_id": primary_inspection_id,
                    "escalation_inspection_id": escalation_inspection_id,
                    "areas": outcomes,
                },
            )
            return outcomes

    async def record_audit(
        self,
        *,
        org_id: str,
        return_id: str,
        primary_inspection_id: str,
        audit_inspection_id: str,
        primary: dict[str, Any],
        audit: dict[str, Any],
        eval_run_id: str | None = None,
    ) -> tuple[str, ...]:
        """Record advisory disagreement. The audit never changes model/human evidence values."""
        disagreements = audit_disagreements(primary, audit)
        async with self.db.transaction(org_id) as conn:
            cur = await conn.execute(
                "SELECT unit_id, status FROM rm.returns WHERE org_id = %s AND return_id = %s FOR UPDATE",
                (org_id, return_id),
            )
            ret = await cur.fetchone()
            if ret is None:
                raise NotFound(f"Return '{return_id}' not found")
            await conn.execute(
                """INSERT INTO rm.audit_findings (
                     finding_id, org_id, return_id, primary_inspection_id, audit_inspection_id,
                     disagreements, routes_to_review, eval_run_id
                   ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
                (
                    new_id(),
                    org_id,
                    return_id,
                    primary_inspection_id,
                    audit_inspection_id,
                    list(disagreements),
                    bool(disagreements),
                    eval_run_id,
                ),
            )
            await append_event(
                conn,
                org_id=org_id,
                unit_id=ret["unit_id"],
                return_id=return_id,
                event_type=ET.AUDIT_COMPLETED,
                actor_type="system",
                actor_id="audit-reviewer",
                payload={
                    "primary_inspection_id": primary_inspection_id,
                    "audit_inspection_id": audit_inspection_id,
                    "disagreements": list(disagreements),
                },
            )
            if disagreements and ret["status"] != ReturnStatus.FINALIZED:
                target = ReturnStatus.AWAITING_REVIEW
                if ret["status"] != str(target):
                    transition_return_status(ret["status"], target, "audit_disagreement")
                    await conn.execute(
                        "UPDATE rm.returns SET status = %s WHERE org_id = %s AND return_id = %s",
                        (str(target), org_id, return_id),
                    )
        return disagreements

    @staticmethod
    def should_audit(return_id: str, sample_rate: float, *, eval_run: bool = False) -> bool:
        return audit_sampled(return_id, sample_rate, eval_run=eval_run)
