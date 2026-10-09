"""The admin page (/admin) and the review queue (/ui/review).

Admin only (web/access.py refuses everyone else): the state of the deployment (database, model key, every agent),
fault switches to show failure handling live, access codes for each org (issued, listed, revoked), every workflow of
every org, and the audit log. The review queue is for anyone signed in, scoped to the orgs they may see.
"""
from __future__ import annotations

import os

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse

from shared.utils import db

from .. import faults
from . import STAGES, _all_cases, _api, _back, _render, _same_origin, _wf_id, restart
from .access import audit, current, issue_code, list_codes, revoke_code

router = APIRouter(dependencies=[Depends(_same_origin)])
REVIEW = ("BLOCKED", "NEEDS_REVIEW", "FAILED", "INCOMPLETE")


def _orgs() -> list[str]:
    return sorted({c["org_id"] for c in _all_cases()})


def _rows(scope: set[str] | None) -> list[dict]:
    rows = _api().STORE.list_workflows(scope)
    rows.sort(key=lambda w: w.get("updated_at") or w.get("created_at") or "", reverse=True)
    return [{"id": w["workflow_id"], "org": w["org_id"], "unit": w["subject_id"], "status": w.get("status"),
             "outcome": (w.get("final_outcome") or {}).get("outcome"), "reason": w.get("status_reason"),
             "updated": (w.get("updated_at") or w.get("created_at") or "").replace("T", " ").rstrip("Z"),
             "stages": [(s["stage"], s["state"], s.get("verdict")) for s in w["stage_results"]]} for w in rows]


def _system() -> dict:
    health = _api().health()
    return {"database": db.ping(), "agents": health["agents"], "status": health["status"],
            "model_key": bool(os.environ.get("GEMINI_API_KEY")), "model": os.environ.get("GEMINI_MODEL", "(default)"),
            "public_url": os.environ.get("RENDER_EXTERNAL_URL") or os.environ.get("POD_PUBLIC_URL") or "",
            "commit": (os.environ.get("RENDER_GIT_COMMIT") or "")[:7]}


def _page(request: Request, **extra):
    return _render(request, "admin.html", nav="admin", system=_system(), faults=faults.active(), modes=faults.MODES,
                   stages=list(STAGES), codes=list_codes(), orgs=_orgs(), rows=_rows(None), review=REVIEW,
                   audit=_audit(), **extra)


def _audit() -> list[dict]:
    from .access import audit_log

    return audit_log(150)


@router.get("/admin", response_class=HTMLResponse)
def admin(request: Request):
    return _page(request)


@router.post("/admin/codes", response_class=HTMLResponse)
def new_code(request: Request, label: str = Form(...), org: str = Form("*"), role: str = Form("operator")):
    """Issue a code. It is shown once, on this page (never in a URL); only its SHA-256 is kept."""
    org_id = None if org == "*" else org
    if org_id is not None and org_id not in _orgs():
        raise HTTPException(422, "unknown org")
    code = issue_code(label, org_id, role)
    audit("code_issued", label, org=org_id, role=role)
    return _page(request, new_code={"code": code, "label": label, "org": org_id or "every org", "role": role})


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
