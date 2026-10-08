"""P4 CLI commands: `worker` and `jobs list|retry|cancel` (§20). Each command only calls a service."""

from __future__ import annotations

import typer

from returns_manager.config import get_settings
from returns_manager.db.pool import Database, run_async
from returns_manager.inspection.runtime import build_runtime
from returns_manager.jobs import service as jobs_svc
from returns_manager.jobs.queue import JobRecord
from returns_manager.jobs.statemachine import JobStatus
from returns_manager.jobs.worker import Worker

jobs_app = typer.Typer(help="Durable job queue.", no_args_is_help=True)


def _app_db() -> Database:
    settings = get_settings()
    settings.require("database_url")
    assert settings.database_url is not None
    return Database(settings.database_url.get_secret_value())


def _line(j: JobRecord) -> str:
    err = f"  {j.last_error_class}" if j.last_error_class else ""
    return (
        f"{j.job_id}  {j.kind:<12} {j.status!s:<16} attempts={j.attempts}/{j.max_attempts} "
        f"prio={j.priority} return={j.return_id}{err}"
    )


def worker_command(
    concurrency: int | None = typer.Option(
        None, "--concurrency", help="slots (default RM_WORKER_CONCURRENCY)"
    ),
    kinds: str = typer.Option("judgment,escalation,reinspection", "--kinds"),
    max_jobs: int | None = typer.Option(None, "--max-jobs", help="exit after this many finished jobs"),
) -> None:
    """Run the job worker (Ctrl+C stops it gracefully)."""
    settings = get_settings()

    from returns_manager.observability.logging import configure_logging

    configure_logging(settings.log_level)

    async def go() -> int:
        async with _app_db() as db:
            runtime = build_runtime(db, settings)
            worker = Worker(
                db,
                settings,
                concurrency=concurrency,
                kinds=[k.strip() for k in kinds.split(",") if k.strip()],
                handler=runtime.handler,
            )
            return await worker.run(max_jobs=max_jobs)

    try:
        n = run_async(go)
    except KeyboardInterrupt:
        typer.echo("worker stopped")
        return
    typer.echo(f"worker finished {n} job(s)")


@jobs_app.command("list")
def jobs_list(
    org: str = typer.Option(..., "--org"),
    status: JobStatus | None = typer.Option(None, "--status"),
    limit: int = typer.Option(50, "--limit", min=1, max=500),
) -> None:
    """List an org's jobs, newest first."""

    async def go() -> list[JobRecord]:
        async with _app_db() as db:
            return await jobs_svc.list_jobs(db, org, status, limit)

    for j in run_async(go):
        typer.echo(_line(j))


@jobs_app.command("retry")
def jobs_retry(job_id: str, org: str = typer.Option(..., "--org")) -> None:
    """Requeue a needs_attention job (fresh attempts), or make a retryable job due now."""

    async def go() -> JobRecord:
        async with _app_db() as db:
            return await jobs_svc.retry_job(db, org, job_id)

    typer.echo(_line(run_async(go)))


@jobs_app.command("cancel")
def jobs_cancel(job_id: str, org: str = typer.Option(..., "--org")) -> None:
    """Cancel a pending, in-progress or retryable job."""

    async def go() -> JobRecord:
        async with _app_db() as db:
            return await jobs_svc.cancel_job(db, org, job_id)

    typer.echo(_line(run_async(go)))
