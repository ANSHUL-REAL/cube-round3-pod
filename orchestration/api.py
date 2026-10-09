"""Optional HTTP front door for the orchestrator (useful for a deployed demo).

  uvicorn orchestration.api:app --port 8100
  POST /workflows                 {"org_id": "org_demo_alpha", "unit_id": "UNIT-0002"}   -> Workflow State (runs it)
  GET  /workflows/{id}            -> Workflow State
  GET  /workflows/{id}/evidence   -> the workflow plus all its evidence records
  POST /workflows/{id}/resume     -> continue after a halt / decision / failure
  POST /workflows/{id}/overrides  {"record_id": "...", "new_verdict": "PASS", "actor": "...", "reason": "..."}
  GET  /workflows?status=BLOCKED   -> the workflows you may see (the review queue: ?needs=review)
  GET  /evidence/{record_id}       -> one evidence record, with its content hash checked
  GET  /health                    -> orchestrator, every agent in the flow, the database and any fault switches
  GET  /whoami                    -> who the server thinks you are and which orgs you may see

Access (orchestration/web/access.py): open on a laptop with no settings. A deployment sets ADMIN_PASSWORD (and
SESSION_SECRET); then every route except /health needs a session (the console) or `Authorization: Bearer <access code>`,
and a code for one org gets 404 for anything of another org.
"""
from __future__ import annotations

import os


def _tidy_key(name: str = "GEMINI_API_KEY") -> None:
    """A key pasted into a hosting dashboard often arrives with quotes, spaces or its own "NAME=" in front. Tidy it
    before any agent reads its settings, so a paste slip is not a 400 from Google on every stage."""
    value = os.environ.get(name)
    if value is None:
        return
    v = value.strip()
    if v.upper().startswith(f"{name}="):
        v = v[len(name) + 1:].strip()
    v = v.strip('"').strip("'").strip()
    if v != value:
        os.environ[name] = v


_tidy_key()

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles

from shared.utils import sample_data

from .clients import HttpClient, client_for, load_manifest
from .orchestrator import WorkflowConflict, apply_override, bundle, default_flow_path, flow_stages, load_flow, resume, run_workflow
from shared.utils import db
from shared.utils.hashing import verify as hash_ok

from . import faults
from .store import EvidenceConflict, FileStore, default_store, is_safe_id  # noqa: F401  (tests swap in a FileStore)
from .web.access import audit, current, guard_on
from .web import HERE as _WEB, router as _ui_router

app = FastAPI(title="CUBE Round 3 orchestrator")
app.mount("/ui/static", StaticFiles(directory=str(_WEB / "static")), name="ui-static")
app.include_router(_ui_router)  # the readable console: /, /ui/w/<workflow>, /ui/capture/<org>/<unit>
from .web.station import lan_guard as _lan_guard, router as _station_router  # noqa: E402

app.include_router(_station_router)  # phone stations: /ui/station, /join (serve.py --lan)
from .web.admin import router as _admin_router  # noqa: E402

app.include_router(_admin_router)  # /admin (admin only) and /ui/review (the review queue)
app.middleware("http")(_lan_guard)  # sessions, access codes and tenancy (web/access.py); open on a laptop with no settings
FLOW = os.environ.get("ORCH_FLOW") or default_flow_path()
STORE = default_store()  # Postgres when DATABASE_URL is set, else JSON files under OUT_DIR


@app.on_event("startup")
def _restore() -> None:
    """A fresh container has an empty disk: put back the photos, reference cards and ledger the agents read."""
    if db.enabled():
        db.restore_files()


@app.api_route("/health", methods=["GET", "HEAD"])  # HEAD: uptime monitors
def health() -> dict:
    agents = {}
    for stage in flow_stages(load_flow(FLOW)):
        client = client_for(stage)
        try:
            agents[stage] = client.health() if isinstance(client, HttpClient) else {"status": "ok", "mode": "inproc"}
        except Exception as exc:
            agents[stage] = {"status": "down", "error": str(exc)[:200], "owner": load_manifest(stage)["owner"]}
    switched = faults.active()
    for stage, f in switched.items():  # an agent an admin switched off reports down, as a stopped agent would
        if stage in agents:
            agents[stage] = {"status": "down", "fault": f["mode"], "set_by": f["set_by"]}
    store = db.ping() if db.enabled() else {"status": "off", "detail": "files on this machine"}
    ok = all(a["status"] == "ok" for a in agents.values()) and store["status"] in ("ok", "off")
    return {"status": "ok" if ok else "degraded", "flow": load_flow(FLOW)["flow_id"], "agents": agents,
            "database": {"status": store["status"]}}


@app.get("/whoami")
def whoami() -> dict:
    who = current()
    return {"role": who.role, "actor": who.actor, "orgs": "all" if who.orgs is None else sorted(who.orgs),
            "access_control": guard_on()}


REVIEW = {"BLOCKED", "NEEDS_REVIEW", "FAILED", "INCOMPLETE"}


@app.get("/workflows")
def list_workflows(status: str | None = None, needs: str | None = None, org: str | None = None) -> dict:
    """The workflows you may see, newest first. `needs=review`: the human-review queue (blocked, failed, incomplete)."""
    who = current()
    scope = who.scope if org is None else ({org} if who.sees(org) else set())
    rows = STORE.list_workflows(scope)
    if status:
        rows = [w for w in rows if w.get("status") == status]
    if needs == "review":
        rows = [w for w in rows if w.get("status") in REVIEW]
    rows.sort(key=lambda w: w.get("updated_at") or w.get("created_at") or "", reverse=True)
    return {"count": len(rows), "workflows": [{
        "workflow_id": w["workflow_id"], "org_id": w["org_id"], "subject_id": w["subject_id"], "status": w.get("status"),
        "outcome": (w.get("final_outcome") or {}).get("outcome"), "status_reason": w.get("status_reason"),
        "current_stage": w.get("current_stage"), "updated_at": w.get("updated_at")} for w in rows]}


@app.get("/evidence/{record_id}")
def get_record(record_id: str) -> dict:
    """One evidence record, as stored, with `content_hash_ok`: is the record exactly what was sealed?"""
    rec = STORE.get_evidence(record_id) if is_safe_id(record_id) else None
    if rec is None or not current().sees((rec.get("subject") or {}).get("org_id")):
        raise HTTPException(404, f"no evidence record {record_id}")
    return {"content_hash_ok": hash_ok(rec), "record": rec}



@app.post("/workflows")
def create(body: dict) -> dict:
    org, subject = body.get("org_id"), body.get("subject_id") or body.get("unit_id")
    if not org or not subject:
        raise HTTPException(422, "org_id and unit_id (or subject_id) are required")
    if not (is_safe_id(org) and is_safe_id(subject)):
        raise HTTPException(422, "org_id and unit_id must be text made of letters, digits, '.', '_' and '-'")
    if not current().sees(org):  # a code for one org cannot start (or learn about) work for another
        raise HTTPException(404, f"no org {org} for this access code")
    from .web import units  # a unit added from the app carries its own order

    added = next((c for c in units.stored() if c["org_id"] == org and c["unit_id"] == subject), None)
    case = added or {"org_id": org, "unit_id": subject, "route": body.get("route") or sample_data.route(subject, org),
                     "returned": body.get("returned", sample_data.has("returns", subject, org))}
    try:
        return run_workflow(case, load_flow(FLOW), STORE)
    except WorkflowConflict as exc:
        raise HTTPException(409, str(exc)) from exc


def _get(workflow_id: str) -> dict:
    wf = STORE.load_workflow(workflow_id)
    if wf is None or not current().sees(wf["org_id"]):  # another org's workflow is answered like a missing one
        raise HTTPException(404, f"no workflow {workflow_id}")
    return wf


@app.get("/workflows/{workflow_id}")
def get(workflow_id: str) -> dict:
    return _get(workflow_id)


@app.get("/workflows/{workflow_id}/evidence")
def evidence(workflow_id: str) -> dict:
    return bundle(_get(workflow_id), STORE)


@app.post("/workflows/{workflow_id}/resume")
def resume_workflow(workflow_id: str) -> dict:
    _get(workflow_id)
    return resume(workflow_id, load_flow(FLOW), STORE)


@app.post("/workflows/{workflow_id}/overrides")
def override(workflow_id: str, body: dict) -> dict:
    _get(workflow_id)
    try:
        return apply_override(workflow_id, STORE, record_id=body.get("record_id", ""), new_verdict=body.get("new_verdict", ""),
                              actor=actor_for(body.get("actor", "")), reason=body.get("reason", ""),
                              new_outcome=body.get("new_outcome"))
    except (ValueError, KeyError, EvidenceConflict) as exc:
        raise HTTPException(422, str(exc)) from exc


def actor_for(claimed: str) -> str:
    """Who an override is recorded against. With access control on, the signed-in identity, never only a typed name:
    "code:Judges alpha (Priya)". Without it (a laptop), the name as typed."""
    claimed = " ".join((claimed or "").split())[:80]
    if not guard_on():
        return claimed
    who = current().actor
    return f"{who} ({claimed})" if claimed and claimed != who else who
