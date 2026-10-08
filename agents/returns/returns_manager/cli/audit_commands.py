"""P9 CLI commands: `audit run --eval-run X` (§20, §11.13)."""

from __future__ import annotations

import logging
from typing import Any

import typer

from returns_manager.config import get_settings
from returns_manager.db.pool import Database, run_async
from returns_manager.db.tenant import transaction as tenant_txn
from returns_manager.errors import SpendGuardRefused
from returns_manager.inspection.runtime import build_runtime
from returns_manager.jobs.budget import get_budget_status
from returns_manager.jobs.queue import JobQueue
from returns_manager.jobs.worker import Worker
from returns_manager.review.merge import audit_sampled

logger = logging.getLogger(__name__)

audit_app = typer.Typer(help="Blind audit reviewer.", no_args_is_help=True)


def _app_db() -> Database:
    settings = get_settings()
    settings.require("database_url")
    assert settings.database_url is not None
    return Database(settings.database_url.get_secret_value())


async def find_audit_candidates(
    db: Database,
    eval_run: str,
    org_id: str | None = None,
    limit: int | None = None,
    sample_rate: float = 0.0,
) -> list[dict[str, Any]]:
    """Discover candidate returns that have completed primary judgment and need audit."""
    if org_id:
        orgs = [org_id]
    else:
        async with tenant_txn(db.pool, None) as conn:
            cur = await conn.execute("SELECT DISTINCT org_id FROM rm.inspection_jobs ORDER BY org_id")
            rows = await cur.fetchall()
            orgs = [r["org_id"] for r in rows]

    candidates: list[dict[str, Any]] = []
    for org in orgs:
        async with db.transaction(org) as conn:
            cur = await conn.execute(
                """
                SELECT r.org_id, r.return_id, r.unit_id, r.status
                FROM rm.returns r
                WHERE EXISTS (
                    SELECT 1 FROM rm.inspection_runs ir
                    WHERE ir.org_id = r.org_id AND ir.return_id = r.return_id
                      AND ir.kind = 'judgment' AND ir.status = 'completed'
                )
                AND NOT EXISTS (
                    SELECT 1 FROM rm.audit_findings af
                    WHERE af.org_id = r.org_id AND af.return_id = r.return_id
                      AND af.eval_run_id = %s
                )
                ORDER BY r.created_at, r.return_id
                """,
                (eval_run,),
            )
            ret_rows = await cur.fetchall()
            for row in ret_rows:
                if audit_sampled(row["return_id"], sample_rate, eval_run=True):
                    candidates.append(dict(row))
                    if limit is not None and len(candidates) >= limit:
                        return candidates
    return candidates


@audit_app.command("run")
def audit_run(
    eval_run: str = typer.Option(..., "--eval-run", help="Evaluation run identifier."),
    org: str | None = typer.Option(
        None, "--org", help="Organisation ID (audits this org, or discovers across orgs)."
    ),
    limit: int | None = typer.Option(None, "--limit", help="Maximum number of returns to audit."),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Display sampled count, model, quota status without running."
    ),
    confirm_spend: bool = typer.Option(
        False, "--confirm-spend", help="Explicit human approval to spend model quota/calls."
    ),
) -> None:
    """Run blind audit re-judgment on the audit model (eval runs)."""
    settings = get_settings()

    async def go() -> None:
        async with _app_db() as db:
            candidates = await find_audit_candidates(
                db,
                eval_run=eval_run,
                org_id=org,
                limit=limit,
                sample_rate=settings.rm_audit_sample_rate,
            )

            async with db.transaction(None) as conn:
                budget = await get_budget_status(
                    conn,
                    settings.rm_audit_model,
                    settings.rm_daily_request_budget_audit,
                    settings.rm_quota_reset_tz,
                )

            count = len(candidates)
            if count == 0:
                typer.echo(f"No eligible returns to audit for eval-run '{eval_run}'.")
                return

            if (count > budget.requests_remaining or not budget.allowed) and not confirm_spend:
                raise SpendGuardRefused(
                    f"Audit run requires {count} requests, but daily quota for "
                    f"{settings.rm_audit_model} has {budget.requests_remaining} remaining. "
                    f"Pass --confirm-spend or wait for quota reset."
                )

            if dry_run:
                typer.echo(f"Audit Run Dry-Run (eval-run: {eval_run}):")
                typer.echo(f"  Audit Model: {settings.rm_audit_model}")
                typer.echo(f"  Sampled Returns: {count}")
                rem = budget.requests_remaining
                quota_info = f"{budget.requests_used}/{budget.daily_budget} (rem: {rem})"
                typer.echo(f"  Daily Quota: {quota_info}")
                typer.echo(f"  Spend Confirmed: {confirm_spend}")
                return

            # Live model calls require explicit approval (§20 spend guard)
            if settings.rm_model_provider == "gemini" and not confirm_spend:
                raise SpendGuardRefused(
                    "Live model calls require explicit human approval. Pass --confirm-spend to proceed."
                )

            runtime = build_runtime(db, settings, eval_run_id=eval_run)
            worker = Worker(
                db,
                settings,
                concurrency=1,
                kinds=["audit"],
                handler=runtime.handler,
                worker_id=f"cli-audit-{eval_run}",
            )

            audited = 0
            for item in candidates:
                org_id = item["org_id"]
                return_id = item["return_id"]
                async with db.transaction(org_id) as conn:
                    await JobQueue.enqueue_job(
                        conn,
                        org_id,
                        return_id,
                        kind="audit",
                        priority=-10,
                    )
                    claimed = await JobQueue.claim_specific(
                        conn,
                        org_id,
                        return_id,
                        worker_id=worker.worker_id,
                        lease_s=settings.rm_job_lease_s,
                        kinds="audit",
                    )
                if claimed is not None:
                    ok = await worker._process_job(claimed)
                    if ok:
                        audited += 1
                        typer.echo(f"Audited return {return_id} ({org_id})")
                    else:
                        typer.echo(f"Audit job held or failed for return {return_id} ({org_id})", err=True)

            typer.echo(f"Audit run '{eval_run}' complete: {audited}/{count} returns audited.")

    run_async(go)
