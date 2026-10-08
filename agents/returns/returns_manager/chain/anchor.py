"""Ledger anchoring (§13.6 — optional, cheap external witness).

`anchor_org(pool, org_id, anchors_file)` appends the current ledger head to
`anchors/ledger-anchors.jsonl` in JSONL format.  After writing, the file should
be committed and pushed to GitHub.

Once pushed, anyone who saw that commit can detect a later silent rewrite of
ledger history before that anchor point.

Honest wording (§13.6, ADR-007 — use verbatim in docs):
  "Tamper-evident within this database: modification, reordering, insertion or
   deletion of events or records is detected unless an attacker rewrites the
   entire chain consistently. Not immutable. Weakly anchored: ledger heads are
   published in public git commits; history before an anchor cannot be silently
   rewritten without breaking the anchor."

Never claim "blockchain", "immutable" or "tamper-proof".
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from returns_manager.db.tenant import transaction

DEFAULT_ANCHORS_FILE = "anchors/ledger-anchors.jsonl"


async def anchor_org(
    pool: AsyncConnectionPool[AsyncConnection[Any]],
    org_id: str,
    anchors_file: str = DEFAULT_ANCHORS_FILE,
) -> dict[str, Any]:
    """Append the current ledger head for `org_id` to the anchors JSONL file.

    Returns the anchor dict that was written.
    Raises RuntimeError if the org has no ledger yet.
    """
    actual_pool = pool.pool if hasattr(pool, "pool") else pool
    async with transaction(actual_pool, org_id) as conn:
        cur = await conn.execute(
            "SELECT last_seq, last_hash FROM rm.org_ledger_heads WHERE org_id = %s",
            (org_id,),
        )
        row = await cur.fetchone()

    if row is None or row["last_seq"] == 0:
        raise RuntimeError(f"No ledger entries yet for org {org_id!r}. Nothing to anchor.")

    anchor: dict[str, Any] = {
        "org_id": org_id,
        "ledger_seq": row["last_seq"],
        "ledger_hash": row["last_hash"],
        "anchored_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f+00:00"),
    }

    def _write_anchor() -> None:
        p = Path(anchors_file)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(anchor, sort_keys=True) + "\n")

    await asyncio.to_thread(_write_anchor)

    return anchor
