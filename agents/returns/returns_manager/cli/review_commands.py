"""P8 CLI commands. Parsing stays thin; HumanReviewService owns the workflow."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import typer

from returns_manager.cli.job_commands import _app_db
from returns_manager.db.pool import run_async
from returns_manager.review.service import HumanReviewService, OverrideInput

review_app = typer.Typer(help="Human review and independent sign-off.", no_args_is_help=True)


def _load_resolution(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise typer.BadParameter(f"cannot read resolution JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise typer.BadParameter("resolution JSON must be an object")
    return data


@review_app.command("list")
def review_list(
    org: str = typer.Option(..., "--org"), reason: str | None = typer.Option(None, "--reason")
) -> None:
    """List returns awaiting review or sign-off."""

    async def go() -> list[dict[str, Any]]:
        async with _app_db() as db:
            return await HumanReviewService(db).review_queue(org_id=org, reason=reason)

    for item in run_async(go):
        typer.echo(json.dumps(item, default=str, sort_keys=True))


@review_app.command("resolve")
def review_resolve(
    return_id: str,
    file: Path = typer.Option(..., "--file", exists=True, dir_okay=False),
    org: str = typer.Option(..., "--org"),
    reviewer: str = typer.Option(..., "--reviewer"),
) -> None:
    """Resolve named review reasons and optionally apply documented field overrides."""
    payload = _load_resolution(file)
    raw = payload.get("overrides", [])
    if not isinstance(raw, list):
        raise typer.BadParameter("overrides must be a list")
    overrides = [OverrideInput(**item) for item in raw]

    async def go() -> dict[str, Any]:
        async with _app_db() as db:
            decision = await HumanReviewService(db).resolve_review(
                org_id=org,
                reviewer_id=reviewer,
                reviewer_role="reviewer",
                return_id=return_id,
                overrides=overrides,
                resolved_review_reasons=list(payload.get("resolved_review_reasons", [])),
                note=payload.get("note"),
            )
            return decision.to_dict()

    typer.echo(json.dumps(run_async(go), sort_keys=True))


@review_app.command("signoff")
def review_signoff(
    return_id: str,
    approve: bool = typer.Option(False, "--approve"),
    reject: bool = typer.Option(False, "--reject"),
    reason: str = typer.Option(..., "--reason"),
    org: str = typer.Option(..., "--org"),
    reviewer: str = typer.Option(..., "--reviewer"),
) -> None:
    """Approve or reject an independent sign-off; capturers are rejected by the service."""
    if approve == reject:
        raise typer.BadParameter("pass exactly one of --approve or --reject")

    async def go() -> dict[str, Any]:
        async with _app_db() as db:
            decision = await HumanReviewService(db).signoff(
                org_id=org, reviewer_id=reviewer, return_id=return_id, approved=approve, reason=reason
            )
            return decision.to_dict()

    typer.echo(json.dumps(run_async(go), sort_keys=True))
