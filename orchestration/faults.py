"""Admin fault switches: make one agent fail on a live deployment, show what the orchestrator does, then restore it and
resume. A judge cannot "stop one agent or set its URL wrong" on a hosted URL (all five agents run inside the server),
so the admin page offers the same test as a switch. Every switch is in the audit log.

  down      the agent does not answer (AgentUnavailable): retried, then recorded as an error; the workflow ends
            FAILED or INCOMPLETE, never COMPLETED/CLEAN
  timeout   the agent answers too late (AgentTimeout): same path, a different error code
  garbage   the agent answers with output that breaks the contract: the orchestrator refuses it (invalid_output) and
            records a degraded record instead of using it

The switches live in the database when there is one (so every worker sees them), else in this process.
"""
from __future__ import annotations

import threading

from .clients import AgentTimeout, AgentUnavailable

MODES = {
    "down": "Agent does not answer",
    "timeout": "Agent answers too late",
    "garbage": "Agent returns output that breaks the contract",
}
_MEM: dict[str, dict] = {}
_LOCK = threading.Lock()


def active() -> dict[str, dict]:
    """stage -> {"mode", "set_by", "set_at"} for every agent that is switched off or misbehaving."""
    from shared.utils import db

    if db.enabled():
        rows = db.fetchall(f"select stage, mode, set_by, set_at from {db.SCHEMA}.faults")
        return {s: {"mode": m, "set_by": by, "set_at": at.strftime("%Y-%m-%dT%H:%M:%SZ")} for s, m, by, at in rows}
    with _LOCK:
        return {k: dict(v) for k, v in _MEM.items()}


def set_fault(stage: str, mode: str, actor: str) -> None:
    from shared.utils import db

    if mode not in MODES:
        raise ValueError(f"unknown fault mode {mode!r}")
    if db.enabled():
        db.execute(f"insert into {db.SCHEMA}.faults (stage, mode, set_by) values (%s,%s,%s) on conflict (stage) "
                   f"do update set mode=excluded.mode, set_by=excluded.set_by, set_at=now()", (stage, mode, actor))
    else:
        with _LOCK:
            _MEM[stage] = {"mode": mode, "set_by": actor, "set_at": db.now()}


def clear(stage: str) -> None:
    from shared.utils import db

    if db.enabled():
        db.execute(f"delete from {db.SCHEMA}.faults where stage=%s", (stage,))
    else:
        with _LOCK:
            _MEM.pop(stage, None)


class FaultyClient:
    def __init__(self, stage: str, mode: str):
        self.stage, self.mode = stage, mode

    def run(self, request: dict, timeout_s: float) -> dict:
        why = f"{self.stage} agent switched to '{self.mode}' by an admin (fault test)"
        if self.mode == "timeout":
            raise AgentTimeout(f"no answer within {timeout_s:g}s: {why}")
        if self.mode == "garbage":  # looks like an answer, breaks the contract: no hash, an unknown verdict
            return {"schema_version": "1.0", "evidence": {"record_id": f"BAD-{request['request_id'][:40]}",
                                                         "decision": {"verdict": "PROBABLY_FINE"}}}
        raise AgentUnavailable(f"connection refused: {why}")

    def health(self) -> dict:
        return {"status": "down", "fault": self.mode}


def wrap(stage: str, client, faults: dict[str, dict] | None = None):
    """The client to use for `stage`: the real one, or a faulty stand-in while an admin has switched it."""
    f = (faults if faults is not None else active()).get(stage)
    return FaultyClient(stage, f["mode"]) if f else client
