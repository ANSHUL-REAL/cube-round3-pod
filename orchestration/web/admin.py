"""The admin page (/admin) and the review queue (/ui/review).

Admin only (web/access.py refuses everyone else): the state of the deployment (database, model key, every agent),
fault switches to show failure handling live, access codes for each org (issued, listed, revoked), every workflow of
every org, and the audit log. The review queue is for anyone signed in, scoped to the orgs they may see.
"""
from __future__ import annotations

import hashlib
import os
import time

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse

from shared.utils import db

from .. import faults
from . import STAGES, _all_cases, _api, _back, _render, _same_origin, _wf_id, restart
from . import brand
from .access import audit, current, issue_code, list_codes, revoke_code

router = APIRouter(dependencies=[Depends(_same_origin)])
REVIEW = ("BLOCKED", "NEEDS_REVIEW", "FAILED", "INCOMPLETE")


def _orgs() -> list[str]:
    """Every seller: those with sample units, and those an admin added (who start with none)."""
    return sorted({c["org_id"] for c in _all_cases()} | set(brand.org_names()))


def _sellers(rows: list[dict], codes: list[dict]) -> list[dict]:
    units: dict[str, int] = {}
    for c in _all_cases():
        units[c["org_id"]] = units.get(c["org_id"], 0) + 1
    flows: dict[str, int] = {}
    for r in rows:
        flows[r["org"]] = flows.get(r["org"], 0) + 1
    people: dict[str, int] = {}
    for c in codes:
        if c.get("org_id") and not c.get("revoked_at"):
            people[c["org_id"]] = people.get(c["org_id"], 0) + 1
    out = [{"id": o, "name": brand.org_name(o), "units": units.get(o, 0), "workflows": flows.get(o, 0),
            "people": people.get(o, 0)} for o in _orgs()]
    return sorted(out, key=lambda x: x["name"].lower())


def _rows(scope: set[str] | None) -> list[dict]:
    rows = _api().STORE.list_workflows(scope)
    rows.sort(key=lambda w: w.get("updated_at") or w.get("created_at") or "", reverse=True)
    return [{"id": w["workflow_id"], "org": w["org_id"], "unit": w["subject_id"], "status": w.get("status"),
             "outcome": (w.get("final_outcome") or {}).get("outcome"), "reason": w.get("status_reason"),
             "updated": (w.get("updated_at") or w.get("created_at") or "").replace("T", " ").rstrip("Z"),
             "stages": [(s["stage"], s["state"], s.get("verdict")) for s in w["stage_results"]]} for w in rows]


_KEY_CHECK: dict[str, tuple[float, str]] = {}


def _key_status() -> str:
    """Does Google accept the model key? "valid", "rejected", "missing" or "unchecked" (no answer). One small call,
    remembered for 5 minutes; the key itself is never shown or logged."""
    key = os.environ.get("GEMINI_API_KEY") or ""
    if not key:
        return "missing"
    tag = hashlib.sha256(key.encode()).hexdigest()[:12]
    hit = _KEY_CHECK.get(tag)
    if hit and time.monotonic() - hit[0] < 300:
        return hit[1]
    try:
        from google import genai

        client = genai.Client(api_key=key)
        next(iter(client.models.list(config={"page_size": 1})), None)
        status = "valid"
    except Exception as exc:  # the page must say what Google said, not crash
        msg = str(exc)
        status = "rejected" if ("API_KEY_INVALID" in msg or "API key not valid" in msg or "PERMISSION_DENIED" in msg) \
            else "unchecked"
    _KEY_CHECK[tag] = (time.monotonic(), status)
    return status


def _system() -> dict:
    health = _api().health()
    return {"database": db.ping(), "agents": health["agents"], "status": health["status"],
            "model_key": bool(os.environ.get("GEMINI_API_KEY")), "key_status": _key_status(),
            "model": os.environ.get("GEMINI_MODEL", "(default)"),
            "public_url": os.environ.get("RENDER_EXTERNAL_URL") or os.environ.get("POD_PUBLIC_URL") or "",
            "commit": (os.environ.get("RENDER_GIT_COMMIT") or "")[:7]}


def _page(request: Request, **extra):
    codes, rows = list_codes(), _rows(None)
    return _render(request, "admin.html", nav="admin", system=_system(), faults=faults.active(), modes=faults.MODES,
                   stages=list(STAGES), codes=codes, orgs=_orgs(), sellers=_sellers(rows, codes), rows=rows,
                   review=REVIEW, audit=_audit(), **extra)


def _audit() -> list[dict]:
    from .access import audit_log

    return audit_log(150)


@router.get("/admin", response_class=HTMLResponse)
def admin(request: Request):
    return _page(request)


@router.post("/admin/codes", response_class=HTMLResponse)
def new_code(request: Request, label: str = Form(...), org: str = Form("*"), role: str = Form("operator"),
             stage: str = Form("")):
    """Issue a code for one person. It is shown once, on this page (never in a URL); only its SHA-256 is kept.
    With a station, the code can run only that agent's step: the person at the Pack bench packs, nothing else."""
    org_id = None if org == "*" else org
    if org_id is not None and org_id not in _orgs():
        raise HTTPException(422, "unknown org")
    station = stage or None
    try:
        code = issue_code(label, org_id, role, station)
    except ValueError as exc:
        return _back("/admin#codes", str(exc), bad=True)
    audit("code_issued", label, org=org_id, role=role, station=station or "any")
    return _page(request, new_code={"code": code, "label": label, "org": org_id or "every org", "role": role,
                                    "stage": station})


@router.post("/admin/sellers")
def add_seller(name: str = Form("")):  # an empty name gets the same message on the page as any other bad name
    """A new seller starts with no units: its units arrive with its orders. Codes can be issued for it at once."""
    try:
        name = brand.clean_name(name)
    except ValueError as exc:
        return _back("/admin#sellers", str(exc), bad=True)
    if any(n.lower() == name.lower() for n in brand.org_names().values()):
        return _back("/admin#sellers", f"There is already a seller called {name}.", bad=True)
    org_id = brand.new_org_id(name, set(_orgs()))
    brand.save_seller(org_id, name, current().actor)
    audit("seller_added", name, org=org_id)
    return _back("/admin#sellers", f"Seller added: {name}. Issue access codes for its people under Access codes.")


@router.post("/admin/sellers/{org_id}/rename")
def rename_seller(org_id: str, name: str = Form("")):
    if org_id not in _orgs():
        raise HTTPException(404, "no such seller")
    try:
        name = brand.clean_name(name)
    except ValueError as exc:
        return _back("/admin#sellers", str(exc), bad=True)
    if any(n.lower() == name.lower() for o, n in brand.org_names().items() if o != org_id):
        return _back("/admin#sellers", f"There is already a seller called {name}.", bad=True)
    old = brand.org_name(org_id)
    brand.save_seller(org_id, name, current().actor)
    audit("seller_renamed", name, org=org_id, before=old)
    return _back("/admin#sellers", f"{old} is now called {name}. Its data, codes and audit history are unchanged.")


@router.post("/admin/codes/{cid}/revoke")
def revoke(cid: int):
    ok = revoke_code(cid)
    audit("code_revoked", str(cid))
    return _back("/admin", "Code revoked: anyone signed in with it is signed out." if ok else "Already revoked.", bad=not ok)


@router.post("/admin/faults/{stage}")
def fault(stage: str, mode: str = Form(...)):
    if stage not in STAGES:
        raise HTTPException(404, "no such agent")
    if mode == "ok":
        faults.clear(stage)
        audit("agent_restored", stage)
        return _back("/admin#faults", f"{stage} restored. Open a stopped workflow and press Resume to finish it.")
    if mode not in faults.MODES:
        raise HTTPException(422, "unknown fault")
    faults.set_fault(stage, mode, current().actor)
    audit("agent_faulted", stage, mode=mode)
    return _back("/admin#faults", f"{stage}: {faults.MODES[mode].lower()}. Run a unit to see the orchestrator handle it.")


@router.post("/admin/w/{workflow_id}/restart")
def admin_restart(workflow_id: str):
    try:
        restart(_wf_id(workflow_id), _api().STORE, why=f"started over by {current().actor} from the admin page")
    except KeyError:
        raise HTTPException(404, "no such workflow") from None
    return _back("/admin#workflows", f"{workflow_id} will run every stage again. Earlier evidence is kept.")


@router.get("/ui/review", response_class=HTMLResponse)
def review(request: Request):
    """Everything waiting for a person, in the orgs you may see: blocked for review, failed, or incomplete."""
    rows = [r for r in _rows(current().scope) if r["status"] in REVIEW]
    return _render(request, "review.html", nav="review", rows=rows)
