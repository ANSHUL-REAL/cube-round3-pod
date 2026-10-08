"""P13 CLI command: `load-test --mode replay|live --units N --concurrency C` (§20, §23)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Any

import typer

from returns_manager.config import get_settings
from returns_manager.db.pool import Database, run_async
from returns_manager.errors import ExitCode, SpendGuardRefused
from returns_manager.load.drills import run_all_drills
from returns_manager.load.models import LoadMode
from returns_manager.load.runner import run_load_test
from returns_manager.observability.logging import configure_logging


def load_test_command(
    mode: Annotated[
        str,
        typer.Option("--mode", help="Execution mode: 'replay' (default) or 'live'."),
    ] = "replay",
    units: Annotated[
        int,
        typer.Option("--units", min=1, help="Number of units/returns to process."),
    ] = 10,
    concurrency: Annotated[
        int,
        typer.Option("--concurrency", min=1, help="Number of concurrent worker slots."),
    ] = 4,
    latency_profile: Annotated[
        str,
        typer.Option(
            "--latency-profile",
            help="Simulated latency: 'instant', 'fixed:<time>', or 'uniform:<min>:<max>'.",
        ),
    ] = "instant",
    confirm_spend: Annotated[
        bool,
        typer.Option("--confirm-spend", help="Confirm spend in live mode (§18.4)."),
    ] = False,
    allow_multi_day: Annotated[
        bool,
        typer.Option("--allow-multi-day", help="Allow run to span multiple Pacific days in live mode."),
    ] = False,
    inject_outage: Annotated[
        str | None,
        typer.Option(
            "--inject-outage",
            help="Inject an outage for resilience testing: 'provider_500' or 'timeout'.",
        ),
    ] = None,
    run_drills: Annotated[
        bool,
        typer.Option(
            "--run-drills",
            help="Run resilience drills (burst, outage, kill-switch, circuit, budget).",
        ),
    ] = False,
    out: Annotated[
        str,
        typer.Option("--out", help="Output file path (JSON or Markdown); prints to stdout if omitted."),
    ] = "-",
) -> None:
    """Load test in replay or live mode (§20, §23).

    Measures system throughput, p50/p95/p99 latency, zero duplicates, zero drops,
    and fail-open behavior under an injected outage.
    """
    settings = get_settings()
    configure_logging(settings.log_level)
    settings.require("database_url")
    assert settings.database_url is not None

    mode_lower = mode.strip().lower()
    if mode_lower not in (LoadMode.REPLAY, LoadMode.LIVE):
        typer.echo(f"error: --mode must be 'replay' or 'live', got {mode!r}", err=True)
        raise typer.Exit(int(ExitCode.USAGE))

    async def _run() -> tuple[Any, list[Any] | None]:
        db = Database(settings.database_url.get_secret_value())  # type: ignore[union-attr]
        await db.open()
        try:
            drill_reports = None
            if run_drills:
                typer.echo("Running resilience drills (burst, outage, kill-switch, circuit, budget)...")
                drill_reports = await run_all_drills(db, settings)

            report = await run_load_test(
                db,
                settings,
                mode=mode_lower,
                units=units,
                concurrency=concurrency,
                latency_profile=latency_profile,
                confirm_spend=confirm_spend,
                allow_multi_day=allow_multi_day,
                injected_outage=inject_outage,
            )
            return report, drill_reports
        finally:
            await db.close()

    try:
        report, drill_reports = run_async(_run)
    except SpendGuardRefused as exc:
        typer.echo(f"spend guard refused: {exc}", err=True)
        raise typer.Exit(int(ExitCode.SPEND_GUARD_REFUSED)) from exc

    md_content = report.to_markdown()

    if drill_reports:
        md_content += "\n\n## Resilience Drills\n\n"
        for dr in drill_reports:
            md_content += dr.to_markdown() + "\n\n"

    if out == "-":
        typer.echo(md_content)
    else:
        out_path = Path(out)
        if out_path.suffix == ".json":
            out_dict = report.to_dict()
            if drill_reports:
                out_dict["drills"] = [dr.to_dict() for dr in drill_reports]
            out_path.write_text(json.dumps(out_dict, indent=2), encoding="utf-8")
        else:
            out_path.write_text(md_content, encoding="utf-8")
        typer.echo(f"Load test report written to {out}")
