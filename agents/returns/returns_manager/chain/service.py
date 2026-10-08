"""High-level chain service: orchestrate verify_unit / verify_org / verify_all (§13.5).

All verification is read-only and runs inside tenant transactions.
"""

from __future__ import annotations

from typing import Any

from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from returns_manager.chain.verify import (
    VerificationResult,
    verify_anchors,
    verify_ledger,
    verify_unit,
)
from returns_manager.db.tenant import transaction


async def run_verify_unit(
    pool: Any,
    org_id: str,
    unit_id: str,
    anchors_file: str = "anchors/ledger-anchors.jsonl",
) -> VerificationResult:
    """Verify the chain for a single unit and return a VerificationResult."""
    actual_pool = pool.pool if hasattr(pool, "pool") else pool
    result = VerificationResult(org_id=org_id)
    async with transaction(actual_pool, org_id) as conn:
        await verify_unit(conn, org_id, unit_id, result)
        await verify_anchors(conn, org_id, result, anchors_file)
    return result


async def run_verify_org(
    pool: Any,
    org_id: str,
    anchors_file: str = "anchors/ledger-anchors.jsonl",
) -> VerificationResult:
    """Verify all unit chains and the ledger for one org."""
    actual_pool = pool.pool if hasattr(pool, "pool") else pool
    result = VerificationResult(org_id=org_id)
    async with transaction(actual_pool, org_id) as conn:
        # Fetch all unit_ids for this org
        cur = await conn.execute(
            "SELECT DISTINCT unit_id FROM rm.unit_events WHERE org_id = %s ORDER BY unit_id",
            (org_id,),
        )
        rows = await cur.fetchall()
        for row in rows:
            await verify_unit(conn, org_id, row["unit_id"], result)
        await verify_ledger(conn, org_id, result)
        await verify_anchors(conn, org_id, result, anchors_file)
    return result


async def run_verify_all(
    pool: AsyncConnectionPool[AsyncConnection[Any]],
    org_ids: list[str],
    anchors_file: str = "anchors/ledger-anchors.jsonl",
) -> list[VerificationResult]:
    """Verify all orgs in `org_ids`."""
    results = []
    for org_id in org_ids:
        results.append(await run_verify_org(pool, org_id, anchors_file))
    return results
