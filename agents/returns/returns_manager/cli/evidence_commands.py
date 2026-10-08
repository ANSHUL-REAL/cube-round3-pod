"""P10 CLI commands: evidence, openapi, contract, mcp (§20)."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Annotated

import typer

# ── evidence ──────────────────────────────────────────────────────────────────

evidence_app = typer.Typer(help="Evidence records.", no_args_is_help=True)


@evidence_app.command("show")
def evidence_show(
    unit_id: Annotated[str, typer.Argument(help="Unit ID to fetch the evidence record for.")],
    org_id: Annotated[str, typer.Option("--org", help="Org ID (required).")] = "",
    version: Annotated[int | None, typer.Option("--version", help="Version; latest if omitted.")] = None,
    include_pending: Annotated[bool, typer.Option("--include-pending")] = False,
    output: Annotated[str, typer.Option("--output", help="json | flat")] = "json",
) -> None:
    """Show the evidence record for a unit."""
    from returns_manager.config import get_settings
    from returns_manager.contract.flat import build_flat_row
    from returns_manager.contract.service import get_evidence_document
    from returns_manager.db.pool import Database, run_async

    if not org_id:
        typer.echo("error: --org is required", err=True)
        raise typer.Exit(2)

    settings = get_settings()
    settings.require("database_url")
    assert settings.database_url is not None

    async def _run() -> None:
        db = Database(settings.database_url.get_secret_value())  # type: ignore[union-attr]
        await db.open()
        try:
            doc = await get_evidence_document(
                db.pool,
                org_id=org_id,
                unit_id=unit_id,
                version=version,
                include_pending=include_pending,
            )
        finally:
            await db.close()
        if doc is None:
            typer.echo(f"No evidence record found for unit {unit_id!r} in org {org_id!r}", err=True)
            raise typer.Exit(1)
        if output == "flat":
            row = build_flat_row(doc)
            for k, v in row.items():
                typer.echo(f"{k}: {v}")
        else:
            typer.echo(json.dumps(doc, indent=2, ensure_ascii=False))

    run_async(_run)


@evidence_app.command("export")
def evidence_export(
    org_id: Annotated[str, typer.Option("--org", help="Org ID (required).")] = "",
    since: Annotated[str | None, typer.Option("--since", help="ISO 8601 UTC timestamp.")] = None,
    format: Annotated[str, typer.Option("--format", help="jsonl | csv")] = "jsonl",
    out: Annotated[str, typer.Option("--out", help="Output file path; stdout if omitted.")] = "-",
) -> None:
    """Export finalized evidence records as JSONL or flat CSV."""
    from datetime import datetime

    from returns_manager.config import get_settings
    from returns_manager.contract.flat import build_flat_row, flat_rows_to_csv
    from returns_manager.contract.service import export_evidence_stream
    from returns_manager.db.pool import Database, run_async

    if not org_id:
        typer.echo("error: --org is required", err=True)
        raise typer.Exit(2)

    since_dt: datetime | None = None
    if since:
        try:
            since_dt = datetime.fromisoformat(since.replace("Z", "+00:00"))
        except ValueError:
            typer.echo(f"error: invalid --since value {since!r}", err=True)
            raise typer.Exit(2)  # noqa: B904

    settings = get_settings()
    settings.require("database_url")
    assert settings.database_url is not None

    async def _run() -> str:
        db = Database(settings.database_url.get_secret_value())  # type: ignore[union-attr]
        await db.open()
        try:
            records = await export_evidence_stream(db.pool, org_id=org_id, since=since_dt)
        finally:
            await db.close()
        if format == "csv":
            rows = [build_flat_row(doc) for doc in records]
            return flat_rows_to_csv(rows)
        else:
            return "\n".join(json.dumps(doc, ensure_ascii=False) for doc in records) + "\n"

    content = run_async(_run)
    if out == "-":
        sys.stdout.write(content)
    else:
        Path(out).write_text(content, encoding="utf-8")
        typer.echo(f"Wrote {len(content.splitlines())} records to {out}")


# ── openapi ───────────────────────────────────────────────────────────────────

openapi_app = typer.Typer(help="OpenAPI export.", no_args_is_help=True)


@openapi_app.command("export")
def openapi_export(
    out: Annotated[str, typer.Option("--out", help="Output file; stdout if omitted.")] = "-",
) -> None:
    """Export the OpenAPI JSON schema for the Returns Manager API."""
    from returns_manager.api.app import create_app

    app = create_app()
    schema = app.openapi()
    content = json.dumps(schema, indent=2, ensure_ascii=False)
    if out == "-":
        typer.echo(content)
    else:
        Path(out).write_text(content, encoding="utf-8")
        typer.echo(f"OpenAPI schema written to {out}")


# ── contract ──────────────────────────────────────────────────────────────────

contract_app = typer.Typer(help="Cross-pod evidence contract.", no_args_is_help=True)

_DEFAULT_CONTRACT_DIR = "contract"


@contract_app.command("build")
def contract_build(
    output_dir: Annotated[
        str,
        typer.Option("--output-dir", help="Output directory for contract artifacts."),
    ] = _DEFAULT_CONTRACT_DIR,
) -> None:
    """Generate contract artifacts: JSON Schema, flat schema, column list."""
    from returns_manager.contract.schema import write_contract_artifacts

    written = write_contract_artifacts(Path(output_dir))
    for p in written:
        typer.echo(f"wrote {p}")
    typer.echo(f"Contract artifacts written to {output_dir}/")


@contract_app.command("check")
def contract_check(
    schema_path: Annotated[
        str,
        typer.Option("--schema", help="Path to evidence-record.v1.schema.json."),
    ] = f"{_DEFAULT_CONTRACT_DIR}/evidence-record.v1.schema.json",
    example_dir: Annotated[
        str,
        typer.Option("--examples", help="Directory of example JSON files to validate."),
    ] = f"{_DEFAULT_CONTRACT_DIR}/examples",
) -> None:
    """Check contract compatibility: validate examples against the schema.

    Exit code 0 = all examples valid.
    Exit code 1 = one or more examples failed validation.
    """
    import jsonschema  # type: ignore[import-untyped]

    schema_file = Path(schema_path)
    if not schema_file.exists():
        typer.echo(f"Schema file not found: {schema_path}. Run 'contract build' first.", err=True)
        raise typer.Exit(1)

    schema = json.loads(schema_file.read_text(encoding="utf-8"))

    examples_dir = Path(example_dir)
    if not examples_dir.is_dir():
        typer.echo(f"No examples directory found at {example_dir}", err=True)
        raise typer.Exit(0)  # Not an error if no examples yet

    failures: list[str] = []
    ok_count = 0
    for f in sorted(examples_dir.glob("*.json")):
        doc = json.loads(f.read_text(encoding="utf-8"))
        try:
            jsonschema.validate(doc, schema)
            typer.echo(f"  ok  {f.name}")
            ok_count += 1
        except jsonschema.ValidationError as exc:
            typer.echo(f"FAIL  {f.name}: {exc.message}", err=True)
            failures.append(f.name)

    typer.echo(f"{ok_count} example(s) valid, {len(failures)} invalid.")
    if failures:
        raise typer.Exit(1)


# ── mcp ───────────────────────────────────────────────────────────────────────

mcp_app = typer.Typer(help="MCP server.", no_args_is_help=True)


@mcp_app.command("serve")
def mcp_serve(
    host: Annotated[str, typer.Option("--host")] = "0.0.0.0",  # noqa: S104
    port: Annotated[int, typer.Option("--port")] = 8001,
) -> None:
    """Serve the read-only MCP server (§16).

    Authenticated by API key with scope evidence:read.
    """
    from functools import partial

    from returns_manager.db.pool import run_async
    from returns_manager.mcp_server import run_mcp_server

    run_async(partial(run_mcp_server, host=host, port=port))
