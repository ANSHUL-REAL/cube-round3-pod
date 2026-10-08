"""CLI commands for chain verify and ledger anchor (§13.5, §13.6, §20, P7)."""

from __future__ import annotations

from typing import Annotated

import typer

from returns_manager.chain.anchor import anchor_org
from returns_manager.chain.service import run_verify_all, run_verify_org, run_verify_unit
from returns_manager.config import get_settings
from returns_manager.db.pool import Database, run_async
from returns_manager.errors import ExitCode

chain_app = typer.Typer(help="Per-unit event chains.", no_args_is_help=True)
ledger_app = typer.Typer(help="Per-org ledger and anchors.", no_args_is_help=True)

_ANCHORS_FILE = "anchors/ledger-anchors.jsonl"


def _db() -> Database:
    settings = get_settings()
    settings.require("database_url")
    assert settings.database_url is not None
    return Database(settings.database_url.get_secret_value())


# ─────────────────────────────────────────────────────────────────────────────
# chain verify
# ─────────────────────────────────────────────────────────────────────────────


@chain_app.command("verify")
def chain_verify(
    unit: Annotated[str | None, typer.Option("--unit", help="Verify a single unit_id.")] = None,
    org: Annotated[str | None, typer.Option("--org", help="Verify all units and ledger for an org.")] = None,
    all_orgs: Annotated[bool, typer.Option("--all", help="Verify every org with chain data.")] = False,
    anchors_file: Annotated[str, typer.Option(help="Path to ledger-anchors.jsonl.")] = _ANCHORS_FILE,
) -> None:
    """Verify per-unit event chains, the org ledger and anchors (§13.5).

    Exit code 3 on any failure (CLAUDE.md exit codes).
    """
    db = _db()

    async def _run() -> int:
        async with db:
            if unit is not None:
                if org is None:
                    typer.echo("error: --unit requires --org", err=True)
                    return int(ExitCode.USAGE)
                result = await run_verify_unit(db.pool, org, unit, anchors_file)
                typer.echo(result.summary_line())
                return 0 if result.valid else 3

            if org is not None:
                result = await run_verify_org(db.pool, org, anchors_file)
                typer.echo(result.summary_line())
                return 0 if result.valid else 3

            if all_orgs:
                # Fetch all org_ids that have chain data (no-tenant query)
                from returns_manager.db.tenant import transaction as tenant_txn

                async with tenant_txn(db.pool, None) as conn:
                    cur = await conn.execute(
                        "SELECT DISTINCT org_id FROM rm.unit_chain_heads ORDER BY org_id"
                    )
                    rows = await cur.fetchall()
                org_ids = [r["org_id"] for r in rows]
                if not org_ids:
                    typer.echo("No org chain data found.")
                    return 0
                results = await run_verify_all(db.pool, org_ids, anchors_file)
                overall_valid = True
                for r in results:
                    typer.echo(r.summary_line())
                    if not r.valid:
                        overall_valid = False
                return 0 if overall_valid else 3

            typer.echo("error: one of --unit --org, --org, or --all is required", err=True)
            return int(ExitCode.USAGE)

    code = run_async(_run)
    if code != 0:
        raise typer.Exit(code)


# ─────────────────────────────────────────────────────────────────────────────
# ledger anchor
# ─────────────────────────────────────────────────────────────────────────────


@ledger_app.command("anchor")
def ledger_anchor(
    org: Annotated[str, typer.Option("--org", help="Org to anchor.")] = "",
    anchors_file: Annotated[str, typer.Option(help="Path to ledger-anchors.jsonl.")] = _ANCHORS_FILE,
) -> None:
    """Append the current ledger head for --org to anchors/ledger-anchors.jsonl (§13.6).

    After anchoring, commit and push the file so the anchor is externally witnessed.

    Honest wording (ADR-007 — never say 'immutable' or 'tamper-proof'):
    Tamper-evident within this database: modification, reordering, insertion or
    deletion of events or records is detected unless an attacker rewrites the
    entire chain consistently. Not immutable. Weakly anchored: ledger heads are
    published in public git commits; history before an anchor cannot be silently
    rewritten without breaking the anchor.
    """
    if not org:
        typer.echo("error: --org is required", err=True)
        raise typer.Exit(int(ExitCode.USAGE))

    db = _db()

    async def _run() -> None:
        async with db:
            anchor = await anchor_org(db.pool, org, anchors_file)
        typer.echo(
            f"Anchored org={org!r} ledger_seq={anchor['ledger_seq']} "
            f"hash={anchor['ledger_hash'][:16]}… → {anchors_file}"
        )
        typer.echo("Commit and push anchors/ledger-anchors.jsonl to make this anchor externally witnessed.")

    run_async(_run)


@ledger_app.command("verify-anchors")
def ledger_verify_anchors(
    org: Annotated[str, typer.Option("--org", help="Org to check.")] = "",
    anchors_file: Annotated[str, typer.Option(help="Path to ledger-anchors.jsonl.")] = _ANCHORS_FILE,
) -> None:
    """Check every published anchor still matches the live ledger (§13.6).

    Exit code 3 on anchor mismatch.
    """
    if not org:
        typer.echo("error: --org is required", err=True)
        raise typer.Exit(int(ExitCode.USAGE))

    db = _db()

    from returns_manager.chain.verify import VerificationResult, verify_anchors
    from returns_manager.db.tenant import transaction as tenant_txn

    async def _run() -> int:
        async with db:
            result = VerificationResult(org_id=org)
            async with tenant_txn(db.pool, org) as conn:
                await verify_anchors(conn, org, result, anchors_file)
        if result.valid:
            typer.echo(f"✓ {result.anchors_checked} anchor(s) verified for org {org!r}.")
            return 0
        typer.echo(result.summary_line())
        return 3

    code = run_async(_run)
    if code != 0:
        raise typer.Exit(code)
