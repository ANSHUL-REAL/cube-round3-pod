"""Recovery Manager: the pod's Round 3 agent entry point.

Recovery has no camera. For one unit it reads the organisers' fee-report lines (tenant-scoped) and ALL the evidence the
earlier stages left, and decides charge by charge whether the evidence contradicts the charge (a claim), supports it,
or is silent (never a claim). Deterministic rules decide; no model is called.

The condition behind every check is "this charge is supported by evidence":
    FAIL = contradicted = a claim, with the evidence attached     PASS = supported or settled
    UNCERTAIN = SILENT = never a claim, listed with the reason

Fail open: an unexpected error returns a *pending* record (UNCERTAIN, with the error), never an exception and never
an invented verdict. A subject that is not under `subject.org_id` raises LookupError (HTTP 404).

Run:  uvicorn agents.recovery.app:app --port 8105
"""
from __future__ import annotations

import hashlib
import os
import time

from shared.utils.hashing import canonical_json
from shared.utils.log import get_logger
from shared.utils.records import build_output, pending_output, utcnow
from shared.utils.server import make_app

from . import __version__, adapter, fees, policy as policy_module
from .rules import Ctx, assess, find_duplicates, load_tier_table
from .upstream import Evidence

STAGE = adapter.STAGE
AGENT_ID = adapter.AGENT_ID
log = get_logger("recovery")

# Same request over the same inputs gives the same record, byte for byte. produced_at and latency are measured once
# and reused, so a retry after a timeout cannot collide with (and be refused as) a different record of the same id.
_PINNED: dict[tuple[str, str], tuple[str, int]] = {}
_PINNED_MAX = 5000


def _flow() -> list[str] | None:
    """The stages of this pod's flow (pod.json), so "no Prep record" can say whether Prep was ever going to run."""
    try:
        from orchestration.orchestrator import flow_stages, load_flow

        return flow_stages(load_flow(os.environ.get("ORCH_FLOW") or None))
    except Exception:  # an agent served on its own has no flow file; the reason text just gets less specific
        return None


def _decide(request: dict) -> dict:
    t0 = time.monotonic()
    subject = request["subject"]
    org_id, unit_id = subject["org_id"], subject["subject_id"]
    if not fees.known_subject(unit_id, org_id):
        raise LookupError(f"unknown subject {unit_id} in {org_id}")  # tenancy: refuse, never answer "no claim"
    evidence = Evidence(request)  # LookupError if it carries another organisation's evidence
    lines = fees.lines_for(unit_id, org_id)
    tiers, tiers_problem = load_tier_table()
    ctx = Ctx(request=request, ev=evidence, lines=lines, flow=_flow(), tiers=tiers, tiers_problem=tiers_problem,
              duplicate_of=find_duplicates(lines), policy=policy_module.POLICY)
    positions = [assess(ln, ctx) for ln in lines]

    inputs_digest = canonical_json({
        "lines": [ln.raw for ln in lines], "previous": [(r["record_id"], r["content_hash"]) for r in
                                                        request.get("previous_evidence") or []],
        "overrides": (request.get("context") or {}).get("overrides") or [], "route": ctx.route,
        "policy": ctx.policy.snapshot(), "flow": ctx.flow, "tiers": tiers})
    key = (request["request_id"], hashlib.sha256(inputs_digest).hexdigest())
    if key not in _PINNED:
        if len(_PINNED) >= _PINNED_MAX:
            _PINNED.clear()
        _PINNED[key] = (utcnow(), int((time.monotonic() - t0) * 1000))
    stamp, latency = _PINNED[key]
    record = adapter.build(request, lines, positions, ctx, stamp=stamp, latency_ms=latency)
    d = record["decision"]
    log.info("recovery_decided", extra={"ctx": {"workflow_id": request["workflow_id"], "stage": STAGE, "org_id": org_id,
                                                "subject_id": unit_id, "outcome": d["outcome"],
                                                "claimable_usd": record["payload"]["claimable_usd"]}})
    return build_output(record, next_step="review" if d.get("needs_human") else "complete")


def handle(request: dict) -> dict:
    try:
        return _decide(request)
    except LookupError:
        raise
    except Exception as exc:  # fail open: a record exists, it says why, and it claims nothing
        log.error("recovery_failed", extra={"ctx": {"workflow_id": request.get("workflow_id"), "stage": STAGE,
                                                    "detail": f"{type(exc).__name__}: {exc}"}})
        return pending_output(request, code="agent_exception", message=f"{type(exc).__name__}: {exc}", retryable=True,
                              agent_id=AGENT_ID)


app = make_app(STAGE, handle, version=__version__)
