"""Migration runner (§7.7): numbered, checksummed, one transaction per file.

- Files: `agent/migrations/NNNN_slug.sql`, versions contiguous from 0001.
- Checksum: SHA-256 of the LF-normalised UTF-8 text (identical on Windows and Linux).
- The runner refuses to run if a previously applied file's checksum changed, or if an applied version is
  missing from disk: history is never rewritten; a change needs a new migration.
- Runs only through `DATABASE_MIGRATOR_URL` (admin/owner). After migrating it sets the password of
  `rm_app_login` from `DATABASE_URL`, so no password is ever written into a migration file.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from psycopg import AsyncConnection, sql
from psycopg.conninfo import conninfo_to_dict

from returns_manager.canonical.hashing import normalize_text, sha256_hex
from returns_manager.config import AGENT_ROOT
from returns_manager.errors import ConfigError, VerificationFailed

MIGRATIONS_DIR = AGENT_ROOT / "migrations"
_FILE_RE = re.compile(r"^(\d{4})_[a-z0-9_]+\.sql$")
_LOCK_KEY = 7_452_019_311  # arbitrary constant for pg_advisory_lock
APP_LOGIN_ROLE = "rm_app_login"


@dataclass(frozen=True)
class Migration:
    version: str
    path: Path
    checksum: str
    sql_text: str


@dataclass(frozen=True)
class MigrationState:
    version: str
    name: str
    state: str  # applied | pending | CHANGED | MISSING_FILE


class MigrationError(VerificationFailed):
    pass


def discover(directory: Path = MIGRATIONS_DIR) -> list[Migration]:
    migrations: list[Migration] = []
    for path in sorted(directory.glob("*.sql")):
        m = _FILE_RE.match(path.name)
        if not m:
            raise MigrationError(f"migration file name must be NNNN_slug.sql: {path.name}")
        raw = normalize_text(path.read_bytes())
        migrations.append(Migration(m.group(1), path, sha256_hex(raw), raw.decode("utf-8")))
    for expected, mig in enumerate(migrations, start=1):
        if mig.version != f"{expected:04d}":
            raise MigrationError(f"migration versions must be contiguous from 0001; found {mig.version}")
    return migrations


async def _applied(conn: AsyncConnection[Any]) -> dict[str, str]:
    cur = await conn.execute("SELECT to_regclass('rm.schema_migrations') IS NOT NULL")
    row = await cur.fetchone()
    if not row or not row[0]:
        return {}
    cur = await conn.execute("SELECT version, checksum FROM rm.schema_migrations ORDER BY version")
    return {str(v): str(c) for v, c in await cur.fetchall()}


def compare(files: list[Migration], applied: dict[str, str]) -> list[MigrationState]:
    states: list[MigrationState] = []
    on_disk = {m.version: m for m in files}
    for mig in files:
        if mig.version not in applied:
            state = "pending"
        elif applied[mig.version] != mig.checksum:
            state = "CHANGED"
        else:
            state = "applied"
        states.append(MigrationState(mig.version, mig.path.name, state))
    for version in sorted(set(applied) - set(on_disk)):
        states.append(MigrationState(version, "?", "MISSING_FILE"))
    return states


async def status(migrator_dsn: str) -> list[MigrationState]:
    files = discover()
    async with await AsyncConnection.connect(migrator_dsn, autocommit=True) as conn:
        return compare(files, await _applied(conn))


def _app_login_password(app_dsn: str) -> str:
    params = conninfo_to_dict(app_dsn)
    if params.get("user") != APP_LOGIN_ROLE:
        raise ConfigError(
            f"DATABASE_URL must connect as {APP_LOGIN_ROLE} (found user {params.get('user')!r})"
        )
    password = params.get("password")
    if not password or len(str(password)) < 16:
        raise ConfigError("DATABASE_URL must carry a password of at least 16 characters for rm_app_login")
    return str(password)


async def migrate(migrator_dsn: str, app_dsn: str) -> list[str]:
    """Apply pending migrations; return the versions applied. Refuses on any checksum drift."""
    files = discover()
    password = _app_login_password(app_dsn)
    applied_now: list[str] = []
    async with await AsyncConnection.connect(migrator_dsn, autocommit=True) as conn:
        await conn.execute("SELECT pg_advisory_lock(%s)", (_LOCK_KEY,))
        try:
            states = compare(files, await _applied(conn))
            bad = [s for s in states if s.state in ("CHANGED", "MISSING_FILE")]
            if bad:
                detail = ", ".join(f"{s.version} ({s.state})" for s in bad)
                raise MigrationError(
                    f"refusing to migrate: applied migrations differ from files on disk: {detail}. "
                    "Never edit an applied migration; add a new one."
                )
            pending = {s.version for s in states if s.state == "pending"}
            for mig in files:
                if mig.version not in pending:
                    continue
                async with conn.transaction():
                    await conn.execute(mig.sql_text.encode("utf-8"))
                    await conn.execute(
                        "INSERT INTO rm.schema_migrations (version, checksum) VALUES (%s, %s)",
                        (mig.version, mig.checksum),
                    )
                applied_now.append(mig.version)
            await conn.execute(
                sql.SQL("ALTER ROLE {} WITH LOGIN PASSWORD {}").format(
                    sql.Identifier(APP_LOGIN_ROLE), sql.Literal(password)
                )
            )
        finally:
            await conn.execute("SELECT pg_advisory_unlock(%s)", (_LOCK_KEY,))
    return applied_now
