"""Human decision workflow (§7.5, §12.4, P8).

Inspection results are immutable observations.  This service records every human action
separately, calculates an effective decision view, and only then finalizes (or
supersedes) an evidence record.  It is the only write path for P8 endpoints.
"""

from __future__ import annotations

import dataclasses
import json
import logging
from datetime import date, datetime
from typing import Any, Literal

from returns_manager.chain import event_types as ET
from returns_manager.chain.append import append_event
from returns_manager.chain.records import finalize_record, supersede_record
from returns_manager.contract.service import build_evidence_record
from returns_manager.db.pool import Database
from returns_manager.errors import BadRequest, Conflict, NotFound
from returns_manager.ids import new_id
from returns_manager.jobs.statemachine import ReturnStatus, transition_return_status

logger = logging.getLogger(__name__)

Action = Literal["accept", "override"]
_PATHS = {
    "identity_match",
    "unit_presence",
    "cosmetic_grade",
    "amazon_condition",
    "disposition",
}
_REASONS = {
    "missed_defect",
    "false_defect",
    "occluded_component",
    "similar_sku",
    "packaging_state_misread",
    "barcode_misread",
    "policy_exception",
    "photo_quality",
    "other",
}


@dataclasses.dataclass(frozen=True)
class OverrideInput:
    field_path: str
    new_value: Any
    reason_code: str
    reason_text: str


@dataclasses.dataclass(frozen=True)
class HumanDecision:
    return_id: str
    status: str
    snapshot_id: str
    evidence_id: str | None
    record_version: int | None
    requires_signoff: bool
    unresolved_review_reasons: tuple[str, ...]
    unit_id: str | None = None
    record_id: str | None = None
    document_sha256: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


def _json(value: Any) -> str:
    return json.dumps(value, allow_nan=False, separators=(",", ":"), sort_keys=True)


def _validate_override(item: OverrideInput) -> None:
    is_component = item.field_path.startswith("component:") and item.field_path.endswith(".status")
    if item.field_path not in _PATHS and not is_component:
        raise BadRequest("Unsupported override field_path")
    if item.reason_code not in _REASONS:
        raise BadRequest("Unsupported override reason_code")
    if not isinstance(item.reason_text, str) or not item.reason_text.strip() or len(item.reason_text) > 1000:
        raise BadRequest("reason_text must contain 1 to 1000 characters")
    if item.field_path in {"identity_match", "unit_presence"} and item.new_value not in {
        "yes",
        "no",
        "uncertain",
        "product_present",
        "empty_packaging",
        "non_product_contents",
    }:
        raise BadRequest(f"Invalid value for {item.field_path}")
    if item.field_path == "disposition" and item.new_value not in {
        "restock",
        "refurbish",
        "liquidate",
        "dispose",
        None,
    }:
        raise BadRequest("disposition must be restock, refurbish, liquidate, dispose, or null")


def _apply_path(values: dict[str, Any], path: str, value: Any) -> None:
    if path in _PATHS:
        values[path] = value
        return
    component_id = path.removeprefix("component:").removesuffix(".status")
    components = values.setdefault("components", {})
    if not isinstance(components, dict):
        components = {}
        values["components"] = components
    components[component_id] = value


def _normalise_for_jcs(value: Any) -> Any:
    """Keep evidence canonicalizable without losing a numeric value silently.

    JCS intentionally rejects floats. Decimal model values are not copied into the
    P8 evidence document; if one appears in a nested legacy result, represent it as
    an explicit decimal string rather than rounding it.
    """
    if isinstance(value, float):
        return {"decimal_string": repr(value)}
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): _normalise_for_jcs(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_normalise_for_jcs(v) for v in value]
    return value


class HumanReviewService:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def _dispatch_finalized_webhook(self, org_id: str, decision: HumanDecision) -> None:
        """Fire evidence.finalized/superseded AFTER the caller's transaction has
        committed - never from inside it. Firing from inside a still-open transaction
        (the original P14 implementation did this in chain/records.py) cannot guarantee
        the record it describes was ever actually persisted: if something later in the
        same transaction rolled back, a webhook would still have gone out for a record
        that no longer exists. Best-effort: a webhook failure must never surface to the
        human whose accept/override/signoff already succeeded and committed.
        """
        if decision.evidence_id is None or decision.unit_id is None or decision.record_id is None:
            return
        try:
            from returns_manager.webhooks.service import get_webhook_service

            service = get_webhook_service(self.db)
            event: Any = "evidence.finalized" if decision.record_version == 1 else "evidence.superseded"
            await service.dispatch(
                event=event,
                org_id=org_id,
                unit_id=decision.unit_id,
                record_id=decision.record_id,
                record_version=decision.record_version or 1,
                document_sha256=decision.document_sha256 or "",
            )
        except Exception:
            logger.warning("Webhook dispatch failed for %s (committed regardless)", decision.return_id)

    async def decide(
        self,
        *,
        org_id: str,
        actor_id: str,
        actor_role: str,
        return_id: str,
        action: Action,
        overrides: list[OverrideInput] | None = None,
        note: str | None = None,
    ) -> HumanDecision:
        if action == "accept" and overrides:
            raise BadRequest("accept must not include overrides")
        if action == "override" and not overrides:
            raise BadRequest("override requires at least one override")
        for item in overrides or []:
            _validate_override(item)
        async with self.db.transaction(org_id) as conn:
            ret, result = await self._locked_return_and_result(conn, org_id, return_id)
            if ret["status"] not in {
                ReturnStatus.AWAITING_OPERATOR,
                ReturnStatus.AWAITING_REVIEW,
                ReturnStatus.FINALIZED,
            }:
                raise Conflict("A human decision is not available in the return's current state")
            decision_id = new_id()
            await conn.execute(
                """INSERT INTO rm.operator_decisions (
                     decision_id, org_id, return_id, action, operator_id, note
                   )
                   VALUES (%s, %s, %s, %s, %s, %s)""",
                (decision_id, org_id, return_id, action, actor_id, note),
            )
            await append_event(
                conn,
                org_id=org_id,
                unit_id=ret["unit_id"],
                return_id=return_id,
                event_type=ET.OPERATOR_DECISION_RECORDED,
                actor_type="operator",
                actor_id=actor_id,
                payload={"decision_id": decision_id, "action": action},
            )
            if overrides:
                await self._insert_overrides(
                    conn, org_id, return_id, ret["unit_id"], actor_id, actor_role, result, overrides
                )
            decision = await self._snapshot_and_route(
                conn, org_id, ret, result, actor_id, "override" if overrides else "accept", set()
            )
        await self._dispatch_finalized_webhook(org_id, decision)
        return decision

    async def resolve_review(
        self,
        *,
        org_id: str,
        reviewer_id: str,
        reviewer_role: str,
        return_id: str,
        overrides: list[OverrideInput],
        resolved_review_reasons: list[str],
        note: str | None,
    ) -> HumanDecision:
        for item in overrides:
            _validate_override(item)
        async with self.db.transaction(org_id) as conn:
            ret, result = await self._locked_return_and_result(conn, org_id, return_id)
            if ret["status"] != ReturnStatus.AWAITING_REVIEW:
                raise Conflict("This return is not awaiting review")
            known = set(result["review_reasons"] or [])
            resolved = set(resolved_review_reasons)
            if not resolved <= known:
                raise BadRequest("resolved_review_reasons contains a reason not on this return")
            resolution_id = new_id()
            resolution = {
                "resolved_review_reasons": sorted(resolved),
                "override_count": len(overrides),
            }
            await conn.execute(
                """INSERT INTO rm.review_resolutions (
                     resolution_id, org_id, return_id, reviewer_id, resolution, note
                   )
                   VALUES (%s, %s, %s, %s, %s::jsonb, %s)""",
                (resolution_id, org_id, return_id, reviewer_id, _json(resolution), note),
            )
            await append_event(
                conn,
                org_id=org_id,
                unit_id=ret["unit_id"],
                return_id=return_id,
                event_type=ET.REVIEW_RESOLUTION_RECORDED,
                actor_type="operator",
                actor_id=reviewer_id,
                payload={"resolution_id": resolution_id, **resolution},
            )
            if overrides:
                await self._insert_overrides(
                    conn, org_id, return_id, ret["unit_id"], reviewer_id, reviewer_role, result, overrides
                )
            decision = await self._snapshot_and_route(
                conn, org_id, ret, result, reviewer_id, "review_resolution", resolved
            )
        await self._dispatch_finalized_webhook(org_id, decision)
        return decision

    async def signoff(
        self, *, org_id: str, reviewer_id: str, return_id: str, approved: bool, reason: str
    ) -> HumanDecision:
        if not reason.strip() or len(reason) > 1000:
            raise BadRequest("reason must contain 1 to 1000 characters")
        async with self.db.transaction(org_id) as conn:
            ret, result = await self._locked_return_and_result(conn, org_id, return_id)
            if ret["status"] != ReturnStatus.AWAITING_SIGNOFF:
                raise Conflict("This return is not awaiting sign-off")
            if reviewer_id == ret["created_by"]:
                raise Conflict("Four-eyes rule: the return capturer cannot sign off")
            signoff_id = new_id()
            signoff_decision = "approved" if approved else "rejected"
            await conn.execute(
                """INSERT INTO rm.signoffs (signoff_id, org_id, return_id, decision, reason, actor_id)
                   VALUES (%s, %s, %s, %s, %s, %s)""",
                (signoff_id, org_id, return_id, signoff_decision, reason, reviewer_id),
            )
            await append_event(
                conn,
                org_id=org_id,
                unit_id=ret["unit_id"],
                return_id=return_id,
                event_type=ET.SIGNOFF_RECORDED,
                actor_type="operator",
                actor_id=reviewer_id,
                payload={"signoff_id": signoff_id, "decision": signoff_decision, "reason": reason},
            )
            if not approved:
                await self._set_status(
                    conn, org_id, return_id, ret["status"], ReturnStatus.AWAITING_REVIEW, "signoff_rejected"
                )
                return HumanDecision(
                    return_id,
                    str(ReturnStatus.AWAITING_REVIEW),
                    signoff_id,
                    None,
                    None,
                    True,
                    ("signoff_rejected",),
                )
            decision = await self._snapshot_and_route(
                conn, org_id, ret, result, reviewer_id, "signoff", set(), signoff_approved=True
            )
        await self._dispatch_finalized_webhook(org_id, decision)
        return decision

    async def review_queue(
        self, *, org_id: str, reason: str | None = None, limit: int = 50
    ) -> list[dict[str, Any]]:
        async with self.db.transaction(org_id) as conn:
            cur = await conn.execute(
                """SELECT r.return_id, r.record_id, r.unit_id, r.status,
                          ir.review_reasons, ir.requires_signoff,
                          ir.recommended_disposition, ir.created_at
                   FROM rm.returns r
                   LEFT JOIN LATERAL (
                     SELECT * FROM rm.inspection_results x
                     WHERE x.org_id = r.org_id AND x.return_id = r.return_id
                     ORDER BY x.created_at DESC LIMIT 1
                   ) ir ON true
                   WHERE r.org_id = %s AND r.status IN ('awaiting_review', 'awaiting_signoff')
                   ORDER BY ir.created_at NULLS LAST, r.created_at LIMIT %s""",
                (org_id, limit),
            )
            rows = await cur.fetchall()
        items = [dict(row) for row in rows]
        if reason:
            items = [item for item in items if reason in (item.get("review_reasons") or [])]
        return items

    async def _locked_return_and_result(self, conn: Any, org_id: str, return_id: str) -> tuple[Any, Any]:
        cur = await conn.execute(
            "SELECT * FROM rm.returns WHERE org_id = %s AND return_id = %s FOR UPDATE", (org_id, return_id)
        )
        ret = await cur.fetchone()
        if ret is None:
            raise NotFound(f"Return '{return_id}' not found")
        cur = await conn.execute(
            """SELECT * FROM rm.inspection_results WHERE org_id = %s AND return_id = %s
               ORDER BY created_at DESC LIMIT 1""",
            (org_id, return_id),
        )
        result = await cur.fetchone()
        if result is None:
            raise Conflict("No completed inspection is available for this return")
        return ret, result

    async def _insert_overrides(
        self,
        conn: Any,
        org_id: str,
        return_id: str,
        unit_id: str,
        actor_id: str,
        actor_role: str,
        result: Any,
        overrides: list[OverrideInput],
    ) -> None:
        values = await self._effective_values(conn, org_id, return_id, result)
        for item in overrides:
            original = values.get(item.field_path)
            if item.field_path.startswith("component:"):
                cid = item.field_path.removeprefix("component:").removesuffix(".status")
                original = (values.get("components") or {}).get(cid)
            override_id = new_id()
            await conn.execute(
                """INSERT INTO rm.overrides (
                     override_id, org_id, return_id, field_path, original_value,
                     new_value, reason_code, reason_text, actor_id, actor_role
                   )
                   VALUES (%s, %s, %s, %s, %s::jsonb, %s::jsonb, %s, %s, %s, %s)""",
                (
                    override_id,
                    org_id,
                    return_id,
                    item.field_path,
                    _json(original),
                    _json(item.new_value),
                    item.reason_code,
                    item.reason_text.strip(),
                    actor_id,
                    actor_role,
                ),
            )
            _apply_path(values, item.field_path, item.new_value)
            await append_event(
                conn,
                org_id=org_id,
                unit_id=unit_id,
                return_id=return_id,
                event_type=ET.OVERRIDE_RECORDED,
                actor_type="operator",
                actor_id=actor_id,
                payload={
                    "override_id": override_id,
                    "field_path": item.field_path,
                    "reason_code": item.reason_code,
                },
            )

    async def _effective_values(self, conn: Any, org_id: str, return_id: str, result: Any) -> dict[str, Any]:
        values: dict[str, Any] = {
            "identity_match": result["identity_match"],
            "unit_presence": result["unit_presence"],
            "cosmetic_grade": result["cosmetic_grade"],
            "amazon_condition": result["amazon_condition"],
            "disposition": result["recommended_disposition"],
            "components": {},
        }
        components = result["components"] or []
        if isinstance(components, list):
            values["components"] = {
                str(c.get("component_id") or c.get("id")): c.get("status")
                for c in components
                if isinstance(c, dict)
            }
        cur = await conn.execute(
            """SELECT field_path, new_value FROM rm.overrides
               WHERE org_id = %s AND return_id = %s ORDER BY created_at, override_id""",
            (org_id, return_id),
        )
        for row in await cur.fetchall():
            _apply_path(values, row["field_path"], row["new_value"])
        return values

    async def _snapshot_and_route(
        self,
        conn: Any,
        org_id: str,
        ret: Any,
        result: Any,
        actor_id: str,
        action: str,
        resolved: set[str],
        signoff_approved: bool = False,
    ) -> HumanDecision:
        values = await self._effective_values(conn, org_id, ret["return_id"], result)
        remaining = tuple(reason for reason in (result["review_reasons"] or []) if reason not in resolved)
        disposition = values["disposition"]
        requires_signoff = bool(result["requires_signoff"]) or disposition == "dispose"
        snapshot_id = new_id()
        effective_disposition = dict(result["disposition"] or {})
        effective_disposition["recommended_disposition"] = disposition
        effective_disposition["decided_by"] = (
            "human_override" if action != "accept" else "deterministic_engine"
        )
        await conn.execute(
            """INSERT INTO rm.human_decision_snapshots (
                 snapshot_id, org_id, return_id, source_inspection_id, action,
                 effective_values, effective_disposition, unresolved_review_reasons,
                 requires_signoff, created_by
               )
               VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s, %s, %s)""",
            (
                snapshot_id,
                org_id,
                ret["return_id"],
                result["inspection_id"],
                action,
                _json(values),
                _json(effective_disposition),
                list(remaining),
                requires_signoff,
                actor_id,
            ),
        )
        if remaining:
            target = ReturnStatus.AWAITING_REVIEW
        elif requires_signoff and not signoff_approved:
            target = ReturnStatus.AWAITING_SIGNOFF
        else:
            target = ReturnStatus.FINALIZED
        await self._set_status(conn, org_id, ret["return_id"], ret["status"], target, action)
        if target != ReturnStatus.FINALIZED:
            return HumanDecision(
                ret["return_id"], str(target), snapshot_id, None, None, requires_signoff, remaining
            )
        finalized = await self._finalize_document(
            conn, org_id, ret, result, snapshot_id, values, effective_disposition, actor_id, action
        )
        return HumanDecision(
            ret["return_id"],
            str(target),
            snapshot_id,
            finalized.evidence_id,
            finalized.record_version,
            requires_signoff,
            remaining,
            unit_id=ret["unit_id"],
            record_id=ret["record_id"],
            document_sha256=finalized.document_sha256,
        )

    async def _set_status(
        self, conn: Any, org_id: str, return_id: str, current: str, target: ReturnStatus, trigger: str
    ) -> None:
        if current != str(target):
            transition_return_status(current, target, trigger)
        await conn.execute(
            "UPDATE rm.returns SET status = %s WHERE org_id = %s AND return_id = %s",
            (str(target), org_id, return_id),
        )

    async def _finalize_document(
        self,
        conn: Any,
        org_id: str,
        ret: Any,
        result: Any,
        snapshot_id: str,
        values: dict[str, Any],
        effective_disposition: dict[str, Any],
        actor_id: str,
        action: str,
    ) -> Any:
        cur = await conn.execute(
            """SELECT * FROM rm.overrides WHERE org_id = %s AND return_id = %s
               ORDER BY created_at, override_id""",
            (org_id, ret["return_id"]),
        )
        overrides = [dict(r) for r in await cur.fetchall()]

        cur = await conn.execute(
            """SELECT * FROM rm.return_photos WHERE org_id = %s AND return_id = %s
               AND NOT superseded ORDER BY slot""",
            (org_id, ret["return_id"]),
        )
        photos = [dict(r) for r in await cur.fetchall()]

        cur = await conn.execute(
            """SELECT evidence_id, record_version FROM rm.evidence_records
               WHERE org_id = %s AND return_id = %s AND status = 'finalized' FOR UPDATE""",
            (org_id, ret["return_id"]),
        )
        previous = await cur.fetchone()
        record_version = 1 if previous is None else previous["record_version"] + 1

        rules_version = str(effective_disposition.get("rules_version") or "")
        if action == "accept":
            decided_by = f"rules_engine@{rules_version}"
        elif action == "review_resolution" or action == "signoff":
            decided_by = f"reviewer:{actor_id}"
        else:  # "override"
            decided_by = f"operator:{actor_id}"

        document = _normalise_for_jcs(
            build_evidence_record(
                org_id=org_id,
                ret=dict(ret),
                result=dict(result),
                values=values,
                overrides=overrides,
                photos=photos,
                record_version=record_version,
                actor_id=actor_id,
                decided_by=decided_by,
            )
        )
        event = await append_event(
            conn,
            org_id=org_id,
            unit_id=ret["unit_id"],
            return_id=ret["return_id"],
            event_type=ET.RECORD_FINALIZED if ret["status"] != "finalized" else ET.RECORD_SUPERSEDED,
            actor_type="system",
            actor_id="human-loop",
            payload={"snapshot_id": snapshot_id},
        )
        if previous is None:
            written = await finalize_record(
                conn,
                org_id=org_id,
                return_id=ret["return_id"],
                unit_id=ret["unit_id"],
                document=document,
                unit_head_event_hash=event.event_hash,
            )
        else:
            written = await supersede_record(
                conn,
                org_id=org_id,
                return_id=ret["return_id"],
                unit_id=ret["unit_id"],
                previous_evidence_id=previous["evidence_id"],
                previous_version=previous["record_version"],
                new_document=document,
                unit_head_event_hash=event.event_hash,
            )
        await conn.execute(
            """UPDATE rm.returns SET finalized_at = now(), current_record_version = %s
               WHERE org_id = %s AND return_id = %s""",
            (written.record_version, org_id, ret["return_id"]),
        )
        return written
