"""What-if simulation (§12.6): re-run `decide()` on a stored result's inputs with changes. Writes nothing.

The response is always labelled `mode: SIMULATION`. Changes are `key=value` pairs on the canonical
DispositionInputs; values are parsed as JSON when possible (`cosmetic_grade=null`, `flags=["x"]`),
else kept as strings. Unknown keys are refused, so a typo can never silently simulate the unchanged case.
"""

from __future__ import annotations

import dataclasses
import json
from typing import Any

from returns_manager.disposition.engine import DispositionDecision, DispositionInputs, decide
from returns_manager.disposition.params import load_params, rules_version
from returns_manager.errors import BadRequest, NotFound

FIELDS = {f.name for f in dataclasses.fields(DispositionInputs)}


def parse_changes(pairs: list[str]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for pair in pairs:
        key, sep, raw = pair.partition("=")
        if not sep:
            raise BadRequest(f"change must be key=value: {pair!r}")
        key = key.strip()
        if key not in FIELDS:
            raise BadRequest(f"unknown input field {key!r}; known: {', '.join(sorted(FIELDS))}")
        try:
            out[key] = json.loads(raw)
        except json.JSONDecodeError:
            out[key] = raw
    return out


def simulate(stored_inputs: dict[str, Any], changes: dict[str, Any]) -> dict[str, Any]:
    unknown = set(changes) - FIELDS
    if unknown:
        raise BadRequest(f"unknown input field(s): {', '.join(sorted(unknown))}")
    base = DispositionInputs.from_canonical(stored_inputs)
    try:
        changed = DispositionInputs.from_canonical({**base.canonical(), **changes})
    except (TypeError, KeyError, ValueError) as exc:
        raise BadRequest(f"invalid change: {exc}") from None
    rv = rules_version(load_params())
    before: DispositionDecision = decide(base, rv)
    after: DispositionDecision = decide(changed, rv)
    return {
        "mode": "SIMULATION",
        "changes": changes,
        "before": dataclasses.asdict(before),
        "after": dataclasses.asdict(after),
    }


async def simulate_for_return(conn: Any, return_id: str, changes: dict[str, Any]) -> dict[str, Any]:
    """Latest stored inputs of this return (caller's tenant transaction; other orgs' returns are NotFound)."""
    cur = await conn.execute(
        "SELECT disposition_inputs FROM rm.inspection_results WHERE return_id = %s "
        "ORDER BY created_at DESC LIMIT 1",
        (return_id,),
    )
    row = await cur.fetchone()
    if row is None:
        raise NotFound("no inspection result for this return")
    return simulate(row["disposition_inputs"], changes)
