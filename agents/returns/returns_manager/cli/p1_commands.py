"""P1 CLI commands: db, keys, controls, seed, api. Each command only calls a service function."""

from __future__ import annotations

import getpass
from datetime import datetime

import typer

from returns_manager.config import get_settings
from returns_manager.db import migrate as migrate_svc
from returns_manager.db.pool import Database, run_async
from returns_manager.errors import ExitCode, NotFound
from returns_manager.security import api_keys, controls
from returns_manager.security.roles import Principal, Role

db_app = typer.Typer(help="Database migrations.", no_args_is_help=True)
keys_app = typer.Typer(help="Scoped API keys.", no_args_is_help=True)
controls_app = typer.Typer(help="Kill-switch controls.", no_args_is_help=True)
seed_app = typer.Typer(help="Demo data.", no_args_is_help=True)
api_app = typer.Typer(help="REST API server.", no_args_is_help=True)


def _app_db() -> Database:
    settings = get_settings()
    settings.require("database_url")
    assert settings.database_url is not None
    return Database(settings.database_url.get_secret_value())


def _cli_actor() -> str:
    try:
        return f"cli:{getpass.getuser()}"
    except Exception:
        return "cli:unknown"


# ── db ─────────────────────────────────────────────────────────────────
@db_app.command("migrate")
def db_migrate() -> None:
    """Apply numbered, checksummed migrations (owner role only), then sync the app role's password."""
    settings = get_settings()
    settings.require("database_migrator_url", "database_url")
    assert settings.database_migrator_url is not None
    assert settings.database_url is not None
    migrator_dsn = settings.database_migrator_url.get_secret_value()
    app_dsn = settings.database_url.get_secret_value()
    applied = run_async(lambda: migrate_svc.migrate(migrator_dsn, app_dsn))
    typer.echo(f"applied: {', '.join(applied) if applied else 'nothing (up to date)'}")


@db_app.command("status")
def db_status() -> None:
    """Show each migration's state: applied, pending, CHANGED (checksum drift) or MISSING_FILE."""
    settings = get_settings()
    settings.require("database_migrator_url")
    assert settings.database_migrator_url is not None
    migrator_dsn = settings.database_migrator_url.get_secret_value()
    states = run_async(lambda: migrate_svc.status(migrator_dsn))
    for s in states:
        typer.echo(f"{s.version}  {s.state:<12} {s.name}")
    if any(s.state in ("CHANGED", "MISSING_FILE") for s in states):
        raise typer.Exit(int(ExitCode.VERIFICATION_FAILED))


# ── keys ───────────────────────────────────────────────────────────────
@keys_app.command("create")
def keys_create(
    org: str = typer.Option(..., "--org", help="org_id the key is limited to"),
    scope: list[str] = typer.Option(..., "--scope", help="repeatable, e.g. --scope evidence:read"),
    name: str = typer.Option(..., "--name"),
    expires_at: datetime | None = typer.Option(None, "--expires-at", help="ISO 8601 UTC"),
) -> None:
    """Create a scoped API key. The key is printed ONCE; store it now."""
    settings = get_settings()

    async def go() -> api_keys.NewApiKey:
        async with _app_db() as db:
            return await api_keys.create_key(
                db,
                org_id=org,
                name=name,
                scopes=scope,
                created_by=_cli_actor(),
                env=settings.rm_env,
                expires_at=expires_at,
            )

    new = run_async(go)
    typer.echo(f"key_id:  {new.key_id}\norg:     {new.org_id}\nscopes:  {', '.join(new.scopes)}")
    typer.echo(f"api key (shown once): {new.plaintext}")


@keys_app.command("list")
def keys_list(org: str = typer.Option(..., "--org")) -> None:
    """List an org's keys (prefix only, never the key)."""

    async def go() -> list[api_keys.ApiKeyInfo]:
        async with _app_db() as db:
            return await api_keys.list_keys(db, org)

    for k in run_async(go):
        state = "revoked" if k.revoked_at else "active"
        typer.echo(f"{k.key_id}  {k.key_prefix}…  {state:<7} {','.join(k.scopes):<30} {k.name}")


@keys_app.command("revoke")
def keys_revoke(key_id: str, org: str = typer.Option(..., "--org")) -> None:
    """Revoke a key (immediately rejected on next use)."""

    async def go() -> bool:
        async with _app_db() as db:
            return await api_keys.revoke_key(db, org, key_id)

    if not run_async(go):
        raise NotFound(f"no key {key_id} in {org}")
    typer.echo(f"revoked {key_id}")


# ── controls ───────────────────────────────────────────────────────────
@controls_app.command("get")
def controls_get(org: str = typer.Option(..., "--org")) -> None:
    """Show the effective controls for an org (global AND org)."""

    async def go() -> dict[controls.Control, controls.EffectiveControl]:
        async with _app_db() as db:
            return await controls.effective(db, org)

    for c, e in run_async(go).items():
        g = "-" if e.global_row is None else ("on" if e.global_row.enabled else "OFF")
        o = "-" if e.org_row is None else ("on" if e.org_row.enabled else "OFF")
        typer.echo(f"{c.value:<26} effective={'on' if e.enabled else 'OFF':<3}  global={g:<3}  org={o}")


@controls_app.command("set")
def controls_set(
    control: controls.Control,
    state: str = typer.Argument(..., help="on | off"),
    scope: str = typer.Option(..., "--scope", help="global or an org_id"),
    reason: str = typer.Option(..., "--reason"),
) -> None:
    """Switch a control on/off. Global scope is CLI-only; org scope acts as that org's admin."""
    if state not in ("on", "off"):
        raise typer.BadParameter("state must be 'on' or 'off'")
    enabled = state == "on"
    actor = _cli_actor()

    async def go() -> controls.ControlRow:
        async with _app_db() as db:
            if scope == controls.GLOBAL:
                return await controls.set_global_control(db, control, enabled, reason, actor)
            # The deployment's CLI operator acts as an admin of the named org.
            admin = Principal(
                kind="user", org_id=scope, actor_id=actor, role=Role.ADMIN, operator_label=actor
            )
            return await controls.set_org_control(db, admin, control, enabled, reason)

    row = run_async(go)
    typer.echo(f"{row.scope}/{row.control.value} = {'on' if row.enabled else 'OFF'} (by {row.updated_by})")


# ── seed ───────────────────────────────────────────────────────────────
@seed_app.command("demo")
def seed_demo() -> None:
    """Private buckets, demo orgs, users and memberships. Passwords go to .env.demo-users (git-ignored)."""
    from returns_manager.seed.demo import DEMO_USERS_FILE, seed

    settings = get_settings()

    async def go() -> int:
        async with _app_db() as db:
            return len(await seed(db, settings))

    n = run_async(go)
    typer.echo(f"seeded 2 orgs, {n} users; credentials written to {DEMO_USERS_FILE.name} (git-ignored)")


# ── api ────────────────────────────────────────────────────────────────
@api_app.command("serve")
def api_serve(
    host: str = typer.Option("127.0.0.1", "--host"),
    port: int = typer.Option(8000, "--port"),
) -> None:
    """Serve the REST API (refuses to start if the database role can bypass RLS)."""
    import uvicorn

    from returns_manager.api.app import create_app

    # log_config=None: keep the app's own structured JSON logging (§18.1, configured inside
    # create_app) instead of letting uvicorn's default dictConfig overwrite the root logger's
    # handlers with its plain-text formatter.
    config = uvicorn.Config(
        create_app(),
        host=host,
        port=port,
        log_level=get_settings().log_level,
        loop="none",
        log_config=None,
    )
    server = uvicorn.Server(config)
    run_async(server.serve)
