"""Tenant context (§6.3): transaction-local and fail-closed.

`transaction(pool, org_id)` is the only way application code opens a database transaction. It starts an
explicit transaction and, when `org_id` is given, sets `app.org_id` with `set_config(..., true)` so the
setting dies with the transaction and can never leak to the next user of a pooled connection.

`org_id=None` opens a transaction WITHOUT tenant context: every tenant table then returns zero rows and
rejects writes (the RLS policies' NULLIF turns the missing setting into NULL). It exists only to call the
four allowlisted cross-tenant functions (§6.4) and to write global controls from the CLI.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

ORG_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_]{1,62}$")


class InvalidOrgId(ValueError):
    pass


def validate_org_id(org_id: str) -> str:
    if not isinstance(org_id, str) or not ORG_ID_RE.match(org_id) or org_id == "global":
        raise InvalidOrgId(f"invalid org_id {org_id!r}")
    return org_id


@asynccontextmanager
async def transaction(
    pool: AsyncConnectionPool[AsyncConnection[Any]], org_id: str | None
) -> AsyncIterator[AsyncConnection[Any]]:
    if org_id is not None:
        validate_org_id(org_id)
    async with pool.connection() as conn, conn.transaction():
        if org_id is not None:
            await conn.execute("SELECT set_config('app.org_id', %s, true)", (org_id,))
        yield conn
