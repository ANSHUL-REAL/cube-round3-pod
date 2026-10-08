"""Kill-switch controls (§6.7).

A control is on unless switched off. Effective value for an org = global row (default on) AND the org's row
(default on): switching a control off globally stops that class of action everywhere.

- Org scope: changed by that org's admins (API or CLI), in the org's tenant context.
- Global scope: changed only from the CLI, in a transaction WITHOUT tenant context. The RLS policy on
  rm.system_controls rejects global writes from any transaction that carries an org context, so no API
  request can ever change a global control.
- A reason is always required. The change is also appended to the org's system chain once the evidence
  chain exists (phase P7); until then updated_by / reason / updated_at are the record.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from returns_manager.db.pool import Database
from returns_manager.security.roles import Permission, Principal, require

GLOBAL = "global"


class Control(StrEnum):
    AUTO_DISPOSITION = "auto_disposition_enabled"
    MODEL_CALLS = "model_calls_enabled"
    AUDIT = "audit_enabled"
    ESCALATION = "escalation_enabled"


@dataclass(frozen=True)
class ControlRow:
    scope: str
    control: Control
    enabled: bool
    reason: str
    updated_by: str
    updated_at: datetime


@dataclass(frozen=True)
class EffectiveControl:
    control: Control
    enabled: bool
    global_row: ControlRow | None
    org_row: ControlRow | None


class ReasonRequired(ValueError):
    pass


async def _rows(db: Database, org_id: str | None) -> list[ControlRow]:
    async with db.transaction(org_id) as conn:
        cur = await conn.execute(
            "SELECT scope, control, enabled, reason, updated_by, updated_at FROM rm.system_controls"
        )
        rows = await cur.fetchall()
    return [ControlRow(**{**r, "control": Control(r["control"])}) for r in rows]


async def effective(db: Database, org_id: str) -> dict[Control, EffectiveControl]:
    rows = await _rows(db, org_id)
    out: dict[Control, EffectiveControl] = {}
    for control in Control:
        g = next((r for r in rows if r.scope == GLOBAL and r.control == control), None)
        o = next((r for r in rows if r.scope == org_id and r.control == control), None)
        enabled = (g.enabled if g else True) and (o.enabled if o else True)
        out[control] = EffectiveControl(control, enabled, g, o)
    return out


async def global_rows(db: Database) -> list[ControlRow]:
    return [r for r in await _rows(db, None) if r.scope == GLOBAL]


async def _upsert(
    db: Database, tx_org: str | None, scope: str, control: Control, enabled: bool, reason: str, actor: str
) -> ControlRow:
    if not reason or not reason.strip():
        raise ReasonRequired("a reason is required to change a control")
    async with db.transaction(tx_org) as conn:
        cur = await conn.execute(
            """INSERT INTO rm.system_controls (scope, control, enabled, reason, updated_by, updated_at)
               VALUES (%s, %s, %s, %s, %s, now())
               ON CONFLICT (scope, control) DO UPDATE
                 SET enabled = EXCLUDED.enabled, reason = EXCLUDED.reason,
                     updated_by = EXCLUDED.updated_by, updated_at = EXCLUDED.updated_at
               RETURNING scope, control, enabled, reason, updated_by, updated_at""",
            (scope, control.value, enabled, reason.strip(), actor),
        )
        row = await cur.fetchone()
    assert row is not None
    return ControlRow(**{**row, "control": Control(row["control"])})


async def set_org_control(
    db: Database, principal: Principal, control: Control, enabled: bool, reason: str
) -> ControlRow:
    """An org admin changes a control for their own org only."""
    require(principal, Permission.ADMIN)
    return await _upsert(
        db, principal.org_id, principal.org_id, control, enabled, reason, principal.actor_label
    )


async def set_global_control(
    db: Database, control: Control, enabled: bool, reason: str, actor: str
) -> ControlRow:
    """CLI only: a transaction without tenant context (the only context in which RLS allows global writes)."""
    return await _upsert(db, None, GLOBAL, control, enabled, reason, actor)
