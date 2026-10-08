"""P5/P6 CLI: `inspect`, `quota status|set-budget`, `simulate disposition` (§20).

Commands only call services."""

from __future__ import annotations

import json
import re

import typer

from returns_manager.config import REPO_ROOT, get_settings
from returns_manager.db.pool import Database, run_async
from returns_manager.disposition.simulate import parse_changes, simulate_for_return
from returns_manager.errors import BadRequest, NotFound
from returns_manager.inspection.dryrun import dry_run
from returns_manager.inspection.runtime import build_runtime
from returns_manager.jobs.queue import JobQueue
from returns_manager.jobs.worker import Worker
from returns_manager.llm.quota import QuotaGuard, Role
from returns_manager.storage.photos import PhotoStorage

quota_app = typer.Typer(help="Gemini free-tier daily request budgets.", no_args_is_help=True)
simulate_app = typer.Typer(help="What-if simulation.", no_args_is_help=True)


def _db() -> Database:
    settings = get_settings()
    settings.require("database_url")
    assert settings.database_url is not None
    return Database(settings.database_url.get_secret_value())


async def _resolve_return(db: Database, org: str, return_id: str | None, unit: str | None) -> str:
    if not return_id and not unit:
        raise BadRequest("pass --return or --unit")
    async with db.transaction(org) as conn:
        if return_id:
            cur = await conn.execute("SELECT return_id FROM rm.returns WHERE return_id = %s", (return_id,))
        else:
            cur = await conn.execute(
                "SELECT return_id FROM rm.returns WHERE unit_id = %s ORDER BY created_at DESC LIMIT 1",
                (unit,),
            )
        row = await cur.fetchone()
    if row is None:
        raise NotFound("return not found in this org")
    return str(row["return_id"])


def inspect_command(
    org: str = typer.Option(..., "--org"),
    return_id: str | None = typer.Option(None, "--return"),
    unit: str | None = typer.Option(None, "--unit"),
    dry: bool = typer.Option(
        False, "--dry-run", help="context summary, token/cost estimate, quota; no model call"
    ),
) -> None:
    """Inspect one return now (same path as the worker), or --dry-run it."""
    settings = get_settings()

    async def go() -> dict[str, object]:
        async with _db() as db:
            rid = await _resolve_return(db, org, return_id, unit)
            if dry:
                settings.require("supabase_url")
                d = await dry_run(db, settings, PhotoStorage(settings), org, rid)
                return {
                    "would_call_model": d.would_call_model,
                    "skip_reasons": d.skip_reasons,
                    "missing": d.missing,
                    **d.summary,
                }
            rt = build_runtime(db, settings)
            worker = Worker(db, settings, concurrency=1, handler=rt.handler, worker_id="cli-inspect")
            async with db.transaction(org) as conn:
                job = await JobQueue.claim_specific(conn, org, rid, worker.worker_id, settings.rm_job_lease_s)
            if job is None:
                raise NotFound(
                    "no due judgment job for this return (submit it first, or it is already processed)"
                )
            await worker._process_job(job)
            async with db.transaction(org) as conn:
                cur = await conn.execute("SELECT status FROM rm.returns WHERE return_id = %s", (rid,))
                status = await cur.fetchone()
                done = await JobQueue.get_job(conn, org, job.job_id)
            return {
                "return_id": rid,
                "return_status": status["status"] if status else None,
                "job_status": str(done.status) if done else None,
                "job_error": done.last_error_class if done else None,
            }

    typer.echo(json.dumps(run_async(go), indent=2, default=str))


_ROLE_OF_MODEL_VAR = {
    "judgment": "RM_DAILY_REQUEST_BUDGET_JUDGMENT",
    "audit": "RM_DAILY_REQUEST_BUDGET_AUDIT",
    "explainer": "RM_DAILY_REQUEST_BUDGET_EXPLAINER",
}


@quota_app.command("status")
def quota_status() -> None:
    """Today's request budget used/remaining per configured model (Pacific day)."""
    settings = get_settings()
    models: list[tuple[Role, str]] = [
        ("judgment", settings.rm_judgment_model),
        ("audit", settings.rm_audit_model),
        ("explainer", settings.rm_explainer_model),
    ]

    async def go() -> list[str]:
        async with _db() as db:
            guard = QuotaGuard(db, settings)
            lines = []
            for role, model in models:
                s = await guard.status(model, role)
                lines.append(
                    f"{role:<10} {model:<24} used {s.requests_used:>3}/{s.daily_budget:<3} "
                    f"remaining {s.requests_remaining:>3}  day {s.quota_day}  "
                    f"resets {s.next_reset_utc.isoformat()}"
                )
            return lines

    for line in run_async(go):
        typer.echo(line)


@quota_app.command("set-budget")
def quota_set_budget(
    role: str = typer.Option(..., "--role", help="judgment | audit | explainer"),
    rpd: int = typer.Option(
        ..., "--rpd", min=0, help="requests per day; keep ~10% below the AI Studio number"
    ),
) -> None:
    """Write RM_DAILY_REQUEST_BUDGET_<ROLE> into the repository-root .env (only that line changes)."""
    var = _ROLE_OF_MODEL_VAR.get(role)
    if var is None:
        raise BadRequest("role must be judgment, audit or explainer")
    env = REPO_ROOT / ".env"
    text = env.read_text(encoding="utf-8") if env.exists() else ""
    line = f"{var}={rpd}"
    pattern = re.compile(rf"^{var}=.*$", re.MULTILINE)
    text = pattern.sub(line, text) if pattern.search(text) else text.rstrip("\n") + f"\n{line}\n"
    env.write_text(text, encoding="utf-8", newline="\n")
    typer.echo(f"{line} written to .env (restart workers to apply)")


@simulate_app.command("disposition")
def simulate_disposition(
    org: str = typer.Option(..., "--org"),
    return_id: str = typer.Option(..., "--return"),
    change: list[str] = typer.Option([], "--change", help="key=value on the stored inputs; repeatable"),
) -> None:
    """What-if: re-run the disposition engine on a stored result with changed inputs. Writes nothing."""
    changes = parse_changes(change)

    async def go() -> dict[str, object]:
        async with _db() as db, db.transaction(org) as conn:
            return await simulate_for_return(conn, return_id, changes)

    typer.echo(json.dumps(run_async(go), indent=2, default=str))
