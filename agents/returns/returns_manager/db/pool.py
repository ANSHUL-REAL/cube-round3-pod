"""Connection pool for the application role and the fail-safe boot check (§6.2).

The API, worker, MCP server and CLI connect as `rm_app_login`. Before serving anything they run
`assert_safe_role`: if the connected role is a superuser, has BYPASSRLS, or can become (is a member of)
any role that is, or can act as the schema owner or the definer role, the process refuses to start.
A green isolation test under a bypass role would prove nothing.
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Callable, Coroutine
from contextlib import AbstractAsyncContextManager
from typing import Any

from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from returns_manager.db import tenant
from returns_manager.errors import ConfigError


class UnsafeDatabaseRole(ConfigError):
    """The application is connected with a role that can bypass row-level security."""


_BOOT_CHECK_SQL = """
SELECT r.rolname,
       r.rolsuper,
       r.rolbypassrls,
       EXISTS (
         SELECT 1 FROM pg_roles AS b
         WHERE (b.rolsuper OR b.rolbypassrls)
           AND b.oid <> r.oid
           AND pg_has_role(r.oid, b.oid, 'MEMBER')
       ) AS can_become_bypass_role,
       EXISTS (
         SELECT 1 FROM pg_roles AS p
         WHERE p.rolname IN ('rm_owner', 'rm_definer')
           AND pg_has_role(r.oid, p.oid, 'MEMBER')
       ) AS can_act_as_owner_or_definer
FROM pg_roles AS r
WHERE r.rolname = current_user
"""


async def check_role(conn: AsyncConnection[Any]) -> dict[str, Any]:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(_BOOT_CHECK_SQL)
        row = await cur.fetchone()
    if row is None:
        raise UnsafeDatabaseRole("could not read the connected role from pg_roles")
    return row


async def assert_safe_role(conn: AsyncConnection[Any]) -> str:
    """Raise UnsafeDatabaseRole unless the connected role is subject to RLS. Returns the role name."""
    info = await check_role(conn)
    problems = [
        label
        for key, label in (
            ("rolsuper", "is a superuser"),
            ("rolbypassrls", "has BYPASSRLS"),
            ("can_become_bypass_role", "is a member of a superuser/BYPASSRLS role"),
            ("can_act_as_owner_or_definer", "is a member of rm_owner or rm_definer"),
        )
        if info[key]
    ]
    if problems:
        raise UnsafeDatabaseRole(
            f"refusing to start: database role '{info['rolname']}' "
            + ", ".join(problems)
            + ". The application must connect as rm_app_login (NOSUPERUSER NOBYPASSRLS); see DATABASE_URL."
        )
    return str(info["rolname"])


def clean_conninfo(uri: str) -> str:
    """Normalize and URL-encode password if it contains '@' which breaks URI parsing."""
    if not (uri.startswith("postgres://") or uri.startswith("postgresql://")):
        return uri
    try:
        import urllib.parse

        scheme, rest = uri.split("://", 1)
        if "@" not in rest:
            return uri
        userinfo, hostdb = rest.rsplit("@", 1)
        if ":" in userinfo:
            user, pwd = userinfo.split(":", 1)
            pwd_quoted = urllib.parse.quote(urllib.parse.unquote(pwd), safe="")
            return f"{scheme}://{user}:{pwd_quoted}@{hostdb}"
    except Exception:
        pass
    return uri


class Database:
    """Owns the pool. All transactions go through `transaction(org_id)` (see db.tenant)."""

    def __init__(self, conninfo: str, *, min_size: int = 1, max_size: int = 10) -> None:
        conninfo = clean_conninfo(conninfo)
        self._pool: AsyncConnectionPool[AsyncConnection[Any]] = AsyncConnectionPool(
            conninfo,
            min_size=min_size,
            max_size=max_size,
            open=False,
            # autocommit: every unit of work is an explicit `conn.transaction()` block.
            # prepare_threshold=None: safe behind a transaction-mode pooler (§6.2).
            kwargs={"autocommit": True, "prepare_threshold": None, "row_factory": dict_row},
        )
        self.role_name: str | None = None

    @property
    def pool(self) -> AsyncConnectionPool[AsyncConnection[Any]]:
        return self._pool

    async def open(self, *, check_role: bool = True) -> None:
        await self._pool.open(wait=True, timeout=15)
        if check_role:
            async with self._pool.connection() as conn:
                self.role_name = await assert_safe_role(conn)

    async def close(self) -> None:
        await self._pool.close()

    def transaction(self, org_id: str | None) -> AbstractAsyncContextManager[AsyncConnection[Any]]:
        return tenant.transaction(self._pool, org_id)

    async def __aenter__(self) -> Database:
        await self.open()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()


async def open_database(conninfo: str, **kwargs: Any) -> Database:
    db = Database(conninfo, **kwargs)
    await db.open()
    return db


def run_async[T](factory: Callable[[], Coroutine[Any, Any, T]]) -> T:
    """asyncio.run with the selector event loop on Windows (psycopg's async mode cannot use Proactor)."""
    if sys.platform == "win32":
        return asyncio.run(factory(), loop_factory=asyncio.SelectorEventLoop)
    return asyncio.run(factory())
