"""P3 CLI commands: capture (§9, §20).

Headless intake: creates a return, uploads photos, records operator observation, and optionally submits.
"""

from __future__ import annotations

from pathlib import Path

import typer

from returns_manager.config import get_settings
from returns_manager.db.pool import Database, run_async
from returns_manager.errors import ExitCode, ReturnsManagerError
from returns_manager.intake.service import IntakeService
from returns_manager.storage.photos import PhotoStorage


def _services() -> tuple[Database, PhotoStorage | None]:
    settings = get_settings()
    settings.require("database_url")
    assert settings.database_url is not None
    db = Database(settings.database_url.get_secret_value())
    storage = PhotoStorage(settings) if settings.supabase_url else None
    return db, storage


def capture_command(
    order_id: str = typer.Option(..., "--order-id", "-o", help="Order ID"),
    unit_id: str = typer.Option(..., "--unit-id", "-u", help="Unit ID"),
    org_id: str = typer.Option("org_demo_alpha", "--org-id", help="Organization ID"),
    actor_id: str = typer.Option("cli:operator", "--actor-id", help="Actor ID"),
    sku: str | None = typer.Option(None, "--sku", help="Ordered SKU"),
    asin: str | None = typer.Option(None, "--asin", help="Ordered ASIN"),
    return_seq: int = typer.Option(1, "--return-seq", help="Return sequence for unit"),
    photo: list[Path] = typer.Option([], "--photo", "-p", help="Paths to photos to upload (2-3)"),
    observed_state: str | None = typer.Option(None, "--observed-state", help="Operator observation"),
    submit: bool = typer.Option(False, "--submit", help="Submit return upon completion"),
    acknowledge_quality_warnings: bool = typer.Option(
        False, "--acknowledge-quality-warnings", help="Acknowledge quality gate warnings"
    ),
    note: str | None = typer.Option(None, "--note", help="Observation or submit note"),
) -> None:
    """Headless capture: create a return, upload photos, record observation, and optionally submit."""
    db, storage = _services()

    async def _run() -> None:
        async with db:
            settings = get_settings()
            svc = IntakeService(
                db,
                storage,
                analysis_long_edge=settings.rm_analysis_long_edge,
                job_max_attempts=settings.rm_job_max_attempts,
                high_value_threshold_minor=settings.rm_high_value_threshold_minor,
            )
            typer.echo(f"Creating return for unit {unit_id} (order {order_id})...")
            ret = await svc.create_return(
                org_id=org_id,
                actor_id=actor_id,
                order_id=order_id,
                unit_id=unit_id,
                ordered_sku=sku,
                ordered_asin=asin,
                return_seq=return_seq,
            )
            typer.echo(f"Return created: return_id={ret.return_id}, record_id={ret.record_id}")

            for p_path in photo:
                if not p_path.exists():
                    typer.echo(f"Error: photo path '{p_path}' does not exist", err=True)
                    raise typer.Exit(int(ExitCode.USAGE))
                typer.echo(f"Uploading photo '{p_path.name}'...")
                photo_bytes = p_path.read_bytes()
                res = await svc.upload_photo(
                    org_id=org_id,
                    actor_id=actor_id,
                    return_id=ret.return_id,
                    photo_bytes=photo_bytes,
                )
                typer.echo(f"  Photo uploaded: {res.alias} (slot {res.slot}) -> quality={res.quality_status}")
                if res.retake_guidance:
                    for g in res.retake_guidance:
                        typer.echo(f"  [Guidance] {g['reason_code']}: {g['instruction']}")

            if observed_state:
                typer.echo(f"Recording observation '{observed_state}'...")
                obs = await svc.record_observation(
                    org_id=org_id,
                    actor_id=actor_id,
                    return_id=ret.return_id,
                    observed_state=observed_state,
                    note=note,
                )
                typer.echo(f"Observation recorded: obs_id={obs.obs_id}")

            if submit:
                typer.echo("Submitting return for inspection...")
                sub_res = await svc.submit_return(
                    org_id=org_id,
                    actor_id=actor_id,
                    return_id=ret.return_id,
                    acknowledge_quality_warnings=acknowledge_quality_warnings,
                    note=note,
                )
                typer.echo(f"Return submitted! Status={sub_res.status}, job_id={sub_res.job_id}")

    try:
        run_async(_run)
    except ReturnsManagerError as exc:
        typer.echo(f"Capture error: {exc}", err=True)
        raise typer.Exit(int(exc.exit_code)) from exc
