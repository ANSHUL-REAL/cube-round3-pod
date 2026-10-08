"""P11 CLI command: `economics report --org --window --volume [--out]` (§20, §18.3)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Any

import typer

economics_app = typer.Typer(help="Unit economics.", no_args_is_help=True)


@economics_app.command("report")
def economics_report(
    org_id: Annotated[str, typer.Option("--org", help="Org ID (required).")] = "",
    window: Annotated[str, typer.Option("--window", help="'24h', '7d', '30d', or 'all'.")] = "7d",
    volume: Annotated[int, typer.Option("--volume", help="Units/month for the projection.")] = 1000,
    out: Annotated[str, typer.Option("--out", help="Output file; stdout if omitted.")] = "-",
) -> None:
    """Unit-economics report from real inspection_runs/inspection_results rows (§18.3).

    Every figure carries its own {value, n, window, method}; recovery uplift is labelled
    synthetic. This never spends model quota — it only reads what already happened.
    """
    from returns_manager.config import get_settings
    from returns_manager.db.pool import Database, run_async
    from returns_manager.observability.economics import EconomicsService
    from returns_manager.observability.logging import configure_logging
    from returns_manager.observability.metrics import parse_window

    if not org_id:
        typer.echo("error: --org is required", err=True)
        raise typer.Exit(2)

    try:
        parse_window(window)
    except ValueError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(2) from exc

    settings = get_settings()
    configure_logging(settings.log_level)
    settings.require("database_url")
    assert settings.database_url is not None

    async def _run() -> dict[str, Any]:
        db = Database(settings.database_url.get_secret_value())  # type: ignore[union-attr]
        await db.open()
        try:
            async with db.transaction(org_id) as conn:
                service = EconomicsService(conn, org_id, settings)
                return await service.report(window, volume)
        finally:
            await db.close()

    report = run_async(_run)
    content = json.dumps(report, indent=2, ensure_ascii=False)
    if out == "-":
        typer.echo(content)
    else:
        Path(out).write_text(content, encoding="utf-8")
        typer.echo(f"Economics report written to {out}")
