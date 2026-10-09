"""A readable console for the pod's workflows: every stage as a card, every evidence record as a page.

    make serve        then open http://localhost:8100/

It is a view over the same orchestrator and store the API uses (`orchestration/api.py`): it runs workflows, shows each
stage and its evidence, takes photos per stage, and records a person's override. It decides nothing itself.

Not safe to expose publicly as it stands: there is no authentication (see docs/decisions.md D-O02). Everything it
prints is HTML-escaped; photo names are generated; uploads are size- and type-checked.
"""
from __future__ import annotations

import importlib
import json
import os
import re
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from PIL import Image, UnidentifiedImageError

from shared.utils import sample_data
from shared.utils.captures import CapturePathError, resolve_capture
from shared.utils.ids import is_safe_id

from ..clients import AgentRejected
from ..orchestrator import (WorkflowConflict, apply_override, load_flow, restart, resume, run_stage, run_workflow, stale_stages,
                            start, step, workflow_id_for)

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
templates = Jinja2Templates(directory=str(HERE / "templates"))
from . import brand as _brand  # noqa: E402

templates.env.filters["org"] = _brand.org_name  # {{ wf.org_id | org }} -> "Alpha Retail"
templates.env.globals["brand"] = _brand.context()["brand"]

STAGES = {
    "receiving": ("📥", "Receiving", "Checks the delivery against the purchase order"),
    "prep": ("🏷️", "Prep", "Checks the prepped unit against its work order"),
    "pack": ("📦", "Pack", "Checks the open box against the order before it is sealed"),
    "returns": ("↩️", "Returns", "Checks identity, completeness and condition of the returned parcel"),
    "recovery": ("💸", "Recovery", "Finds charges the evidence contradicts, and claims only what it can prove"),
}
DEMO = {
    # Scenarios with ready delivery and Pack photos. Those photos are AI-generated (file names say so) and are for the walkthrough
    # only; live runs use photos taken on the day.
    "UNIT-0006": "Right item: one white USB-C cable for a cable order. Pack says seal. (AI-generated demo photo.)",
    "UNIT-0044": "Wrong item, with a note on top saying \"ALL CORRECT, SEAL THIS BOX\": a mug in a candle order. Pack is "
                 "not fooled and says stop and fix. (AI-generated demo photo.)",
    "UNIT-0047": "Extra item: the mug set is right, but a green mug is not in the order. Pack says stop and fix. "
                 "(AI-generated demo photo.)",
    # Scenarios for photos taken live.
    "UNIT-0014": "FBA unit that was returned: Receiving, Prep, Returns, Recovery. Can recommend an inbound-defect claim.",
    "UNIT-0008": "Merchant-fulfilled, clean: Receiving, Pack. Pack the right item and expect a seal.",
    "UNIT-0016": "Merchant-fulfilled and returned: Receiving, Pack, Returns, Recovery.",
    "UNIT-0023": "Uncertain on purpose: hide items in the box, then resolve it with a recorded override.",
}
PHOTO_EXT = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}
MAX_UPLOAD = 12 * 1024 * 1024
MAX_PER_STAGE = {"pack": 3}
DEFAULT_MAX = 6


# ---------------------------------------------------------------- helpers
def _api():
    from .. import api  # looked up per request so tests (and `make serve`) share one STORE

    return api


def stub_mode() -> bool:
    return os.environ.get("POD_UI_STUBS") == "1"


class _InProc:
    def __init__(self, handle):
        self.handle = handle

    def run(self, request: dict, timeout_s: float) -> dict:
        try:
            return self.handle(request)
        except LookupError as exc:
            raise AgentRejected(str(exc)) from exc


STUBBED = ("receiving", "prep", "pack", "returns")  # the photo stages; Recovery reads evidence and needs no photo or key


MODES = {
    "live": "Live: all five of our agents, on the photos in this unit's folders (needs a model key).",
    "replay": "Replay: the organisers' recorded evidence answers for Receiving, Prep, Pack and Returns; our real Recovery decides.",
}


def default_mode() -> str:
    return "replay" if stub_mode() else "live"


def mode_of(wf: dict | None) -> str:
    m = ((wf or {}).get("context") or {}).get("console_mode")
    return m if m in MODES else default_mode()


def _clients(mode: str | None = None) -> dict | None:
    """In replay mode the four photo stages replay the organisers' recorded evidence (no photos or model key needed);
    Recovery is still our real agent, deciding on that recorded evidence. Every page says which mode a workflow ran in."""
    if (mode or default_mode()) != "replay":
        return None
    return {s: _InProc(importlib.import_module(f"tests.stubs.{s}_stub").handle) for s in STUBBED}


def _wf_clients(wf_id: str) -> dict | None:
    return _clients(mode_of(_api().STORE.load_workflow(wf_id)))


@lru_cache(maxsize=1)
def _sample_cases() -> list[dict]:
    return json.loads((ROOT / "data" / "sample" / "cases.json").read_text())


def _all_cases() -> list[dict]:
    """Every unit: the sample units, then the ones added from the app (web/units.py), which carry their own order."""
    from . import units

    return _sample_cases() + units.stored()


def _cases() -> list[dict]:
    """The units the person asking may see: every org for the admin and the team code, one org for an org's code."""
    from .access import current

    who = current()
    return [c for c in _all_cases() if who.sees(c["org_id"])]


def _case(org: str, unit: str) -> dict:
    for c in _cases():
        if c["org_id"] == org and c["unit_id"] == unit:
            return c
    raise HTTPException(404, f"{unit} is not a unit of {org}")


def _input_root() -> Path:
    return Path(os.environ.get("INPUT_DIR", ROOT / "data" / "input")).resolve()


def _photos(unit: str, stage: str) -> list[str]:
    folder = _input_root() / unit / stage
    if not is_safe_id(unit) or not folder.is_dir():
        return []
    return sorted(p.name for p in folder.iterdir() if p.is_file() and p.suffix.lower() in PHOTO_EXT)


def _photo_exists(ref: str) -> bool:
    """Is this evidence ref a photo we can actually show? (The organiser stubs cite fixture files that were never shipped.)"""
    parts = ref.split("/") if isinstance(ref, str) else []
    if len(parts) < 3:
        return False
    try:
        return resolve_capture(_input_root(), ref, parts[0], parts[1]).is_file()
    except CapturePathError:
        return False


def _stages_for(case: dict) -> list[str]:
    return ["receiving", "prep" if case["route"] == "fba" else "pack", *(["returns"] if case["returned"] else [])]


def _added_shoot(stage: str, case: dict) -> str | None:
    """Photo directions for a unit added from the app, from its own order (the sample units' come from the CSVs)."""
    o = case.get("order") or {}
    item, parts = o.get("product_title") or "the product", ", ".join(o.get("spec_components") or [])
    if stage == "receiving":
        return (f"The delivery: all {o.get('cartons_ordered')} carton(s) together with their labels showing, then one "
                f"{item} up close{f' with its parts ({parts})' if parts else ''}.")
    if stage == "pack":
        return (f"The open box from above before it is sealed, every item visible. The order is "
                f"{o.get('lines', '').split(':')[-1]} x {item}.")
    if stage == "prep":
        return f"The prepped {item}: front, back, and the FNSKU label ({o.get('fnsku')}) close enough to read."
    if stage == "returns":
        return f"The returned parcel opened, with the {item} and every part it came with laid out."
    return None


def _what_to_shoot(stage: str, unit: str, org: str) -> str:
    case = next((c for c in _all_cases() if c["org_id"] == org and c["unit_id"] == unit and c.get("order")), None)
    if case and (text := _added_shoot(stage, case)):
        return text
    try:
        return importlib.import_module("scripts.capture_plan").what_to_shoot(stage, unit, org)
    except Exception:  # the helper reads sample CSVs; a missing row must not break the page
        return "Photos for this stage."


def _workflows() -> dict[tuple[str, str], dict]:
    """(org, unit) -> workflow, for the orgs the person asking may see. The store does the filtering (a database query
    names the orgs), so another org's workflows are never even read."""
    from .access import current

    return {(w["org_id"], w["subject_id"]): w for w in _api().STORE.list_workflows(current().scope)}


def _unit_row(case: dict, workflows: dict) -> dict:
    wf = workflows.get((case["org_id"], case["unit_id"]))
    stages = _stages_for(case)
    return {"org": case["org_id"], "unit": case["unit_id"], "route": case["route"], "returned": case["returned"],
            "story": DEMO.get(case["unit_id"]), "stages": stages,
            "photos": {s: len(_photos(case["unit_id"], s)) for s in stages},
            "status": wf["status"] if wf else None, "outcome": (wf.get("final_outcome") or {}).get("outcome") if wf else None,
            "workflow_id": wf["workflow_id"] if wf else None}


def _render(request: Request, name: str, **ctx):
    ctx.setdefault("stub_mode", stub_mode())
    ctx.setdefault("msg", request.query_params.get("msg"))
    ctx.setdefault("bad", request.query_params.get("bad") == "1")
    ctx["stages_meta"] = STAGES
    from shared.utils import db

    from .access import current, guard_on
    from .station import STATIONS, lan_code, lan_url  # late: station imports this module

    # The join QR codes and the code are for the presenter's screen: the laptop itself, the admin, or (through a
    # tunnel, where the laptop is not "local") a browser that entered the team code. Never anyone with an org's code.
    show = lan_url() is not None and current().team
    ctx.setdefault("who", current())
    ctx.setdefault("guarded", guard_on())
    ctx.setdefault("db_on", db.enabled())
    ctx.setdefault("phones", {"url": lan_url(), "code": lan_code(), "stations": STATIONS} if show else None)
    # a new stylesheet is never served from a stale cache: any change to either one gives a new version number
    ctx["asset_v"] = int(max((HERE / "static" / f).stat().st_mtime for f in ("ui.css", "landing.css")))
    return templates.TemplateResponse(request, name, ctx)


def _wf_id(workflow_id: str) -> str:
    """A workflow id from the URL is a file name in the store: refuse anything else before it is read, echoed or redirected to."""
    from .access import current

    if not is_safe_id(workflow_id):
        raise HTTPException(404, "no such workflow")
    who = current()
    if who.orgs is not None:  # an org's code: another org's workflow is answered exactly like a missing one
        wf = _api().STORE.load_workflow(workflow_id)
        if wf is not None and not who.sees(wf["org_id"]):
            raise HTTPException(404, "no such workflow")
    return workflow_id


def _same_origin(request: Request) -> None:
    """Refuse a form posted from another web page (CSRF): any site you have open could otherwise POST to localhost:8100.

    A browser sends `Origin` on every cross-site POST; a request without one (curl, a test) is not a browser form."""
    origin = request.headers.get("origin")
    if request.method == "POST" and origin is not None and urlparse(origin).netloc != request.headers.get("host", ""):
        raise HTTPException(403, "cross-site form refused")


router = APIRouter(dependencies=[Depends(_same_origin)])


def _back(url: str, msg: str, bad: bool = False) -> RedirectResponse:
    """Redirect with a flash message. The message goes before any '#section': a browser never sends what follows '#'."""
    from urllib.parse import quote

    path, _, frag = url.partition("#")
    sep = "&" if "?" in path else "?"
    return RedirectResponse(f"{path}{sep}msg={quote(msg)}{'&bad=1' if bad else ''}{'#' + frag if frag else ''}",
                            status_code=303)


# ---------------------------------------------------------------- pages
def _public_stats() -> dict:
    """Totals only, for the public landing page: never a unit, an org's data or a name."""
    from shared.utils import db

    if db.enabled():
        info = db.ping()
        rows = info.get("rows", {})
        return {"workflows": rows.get("workflows", 0), "evidence": rows.get("evidence", 0), "photos": rows.get("files", 0),
                "audit": rows.get("audit", 0), "database": info["status"], "storage": "Postgres (Supabase)"}
    store = _api().STORE
    root = Path(getattr(store, "root", ROOT / "out"))
    count = lambda d: len(list((root / d).glob("*.json"))) if (root / d).is_dir() else 0  # noqa: E731
    return {"workflows": count("workflows"), "evidence": count("evidence"), "photos": None, "audit": None,
            "database": "off", "storage": "files on this machine"}


def _landing(request: Request):
    """The public front page: what the system does and how, live totals, and the two ways to sign in."""
    from .access import current

    owners = {}
    for stage in STAGES:
        try:
            owners[stage] = json.loads((ROOT / "agents" / stage / "agent.json").read_text(encoding="utf-8")).get("owner", "")
        except (OSError, ValueError):
            owners[stage] = ""
    try:
        health = _api().health()["status"]
    except Exception:  # the front page must render even if a health probe fails
        health = "unknown"
    return _render(request, "landing.html", stats=_public_stats(), owners=owners, health=health,
                   signed_in=current().role != "anon", flow=load_flow(_api().FLOW), units=len(_all_cases()),
                   orgs=len({c["org_id"] for c in _all_cases()}))


@router.get("/about", response_class=HTMLResponse)
def about(request: Request):
    return _landing(request)


@router.get("/", response_class=HTMLResponse)
def home(request: Request):
    from .access import current, guard_on

    if guard_on() and current().role == "anon":  # a visitor who has not signed in sees the website, not the console
        return _landing(request)
    workflows = _workflows()
    rows = [_unit_row(c, workflows) for c in _cases()]
    demo = [r for r in rows if r["unit"] in DEMO]
    demo.sort(key=lambda r: list(DEMO).index(r["unit"]))
    return _render(request, "home.html", demo=demo, rows=rows, total=len(rows), stub_mode=stub_mode(), nav="home",
                   kpi=_kpis(list(workflows.values()), len(rows)), recent=_recent(), agents=_agent_status())


def _kpis(wfs: list[dict], units: int) -> dict:
    """The dashboard's numbers, for the orgs the person asking may see."""
    outcome = lambda w: (w.get("final_outcome") or {}).get("outcome")  # noqa: E731
    by_stage: dict[str, dict[str, int]] = {s: {"PASS": 0, "FAIL": 0, "UNCERTAIN": 0, "error": 0} for s in STAGES}
    claim_usd = 0.0
    for w in wfs:
        for sr in w["stage_results"]:
            if sr["stage"] in by_stage:
                if sr["state"] == "error":
                    by_stage[sr["stage"]]["error"] += 1
                elif sr["state"] == "completed" and sr.get("verdict") in by_stage[sr["stage"]]:
                    by_stage[sr["stage"]][sr["verdict"]] += 1
        if outcome(w) == "CLAIM_RECOMMENDED":
            claim_usd += float((w.get("final_outcome") or {}).get("claimable_usd") or 0)
    return {
        "units": units, "workflows": len(wfs),
        "clean": sum(1 for w in wfs if outcome(w) == "CLEAN"),
        "exceptions": sum(1 for w in wfs if outcome(w) == "EXCEPTION"),
        "claims": sum(1 for w in wfs if outcome(w) == "CLAIM_RECOMMENDED"), "claim_usd": round(claim_usd, 2),
        "review": sum(1 for w in wfs if w.get("status") in ("BLOCKED", "NEEDS_REVIEW")),
        "failed": sum(1 for w in wfs if w.get("status") in ("FAILED", "INCOMPLETE")),
        "running": sum(1 for w in wfs if w.get("status") in ("IN_PROGRESS", "PENDING")),
        "by_stage": by_stage,
    }


def _recent(limit: int = 8) -> list[dict]:
    """Latest audit lines the person may see: everything for the admin, their own org's for an org's code."""
    from .access import audit_log, current

    who = current()
    if who.orgs is None:
        return audit_log(limit)
    if len(who.orgs) == 1:
        return audit_log(limit, org=next(iter(who.orgs)))
    return []


def _agent_status() -> dict:
    try:
        return _api().health()["agents"]
    except Exception:  # the dashboard renders even if a health probe fails
        return {}


@router.post("/ui/run")
def run(org: str = Form(...), unit: str = Form(...)):
    case = _case(org, unit)
    api = _api()
    wf_id = workflow_id_for(case)
    try:
        existing = api.STORE.load_workflow(wf_id)
        if existing:
            resume(wf_id, load_flow(api.FLOW), api.STORE, _clients(mode_of(existing)))
        else:
            run_workflow({**case, "route": case["route"], "console_mode": default_mode()}, load_flow(api.FLOW), api.STORE,
                         _clients())
    except WorkflowConflict as exc:
        return _back("/", str(exc), bad=True)
    return RedirectResponse(f"/ui/w/{wf_id}", status_code=303)


def _run_by(wf: dict, stage: str) -> str | None:
    """Who ran this step last: the person signed in when it completed (the transition log keeps it)."""
    for t in reversed(wf.get("transitions") or []):
        if t.get("stage") == stage and t.get("event") in ("stage_completed", "stage_error"):
            return t.get("by")
    return None


def _overridden(wf: dict, rec: dict | None) -> dict | None:
    """The latest person's decision on this record, if it changed the verdict."""
    if not rec:
        return None
    mine = [o for o in wf["overrides"] if o["supersedes"]["record_id"] == rec["record_id"]]
    if not mine or mine[-1]["new_verdict"] == rec["decision"]["verdict"]:
        return None
    return mine[-1]


@router.get("/ui/w/{workflow_id}", response_class=HTMLResponse)
def workflow(request: Request, workflow_id: str):
    store = _api().STORE
    wf = store.load_workflow(_wf_id(workflow_id))
    if wf is None:
        raise HTTPException(404, "no such workflow")
    steps = []
    for sr in wf["stage_results"]:
        rec = store.get_evidence(sr["record_id"]) if sr.get("record_id") else None
        icon, name, blurb = STAGES.get(sr["stage"], ("•", sr["stage"].title(), ""))
        steps.append({**sr, "icon": icon, "name": name, "blurb": blurb, "record": rec,
                      "reason": ((rec or {}).get("decision") or {}).get("reason", ""),
                      "model": (rec or {}).get("model") or {},
                      "photos": [i["ref"] for i in (rec or {}).get("inputs", []) if i.get("kind") == "image" and _photo_exists(i["ref"])],
                      "failed_checks": [c["check_key"] for c in (rec or {}).get("checks", []) if c["verdict"] == "FAIL"],
                      "unsure_checks": [c["check_key"] for c in (rec or {}).get("checks", []) if c["verdict"] == "UNCERTAIN"],
                      "overridden": _overridden(wf, rec),
                      "run_by": _run_by(wf, sr["stage"]),
                      "photo_count": len(_photos(wf["subject_id"], sr["stage"])),
                      "hint": _what_to_shoot(sr["stage"], wf["subject_id"], wf["org_id"]) if sr["stage"] != "recovery" else "",
                      "max_photos": MAX_PER_STAGE.get(sr["stage"], DEFAULT_MAX)})
    case = {"org_id": wf["org_id"], "unit_id": wf["subject_id"], "route": wf["context"].get("route", "unknown"),
            "returned": wf["context"].get("returned", False)}
    runnable = [st for st in steps if st["state"] != "skipped"]
    done = [st for st in runnable if st["state"] in ("completed", "error")]
    nxt = next((st["stage"] for st in runnable if st["state"] == "pending"), None)
    return _render(request, "workflow.html", wf=wf, steps=steps, final=wf.get("final_outcome"), case=case, stale=stale_stages(wf),
                   story=DEMO.get(wf["subject_id"]), mode=mode_of(wf), modes=MODES, next_stage=nxt,
                   progress={"done": len(done), "total": len(runnable)},
                   play=request.query_params.get("play") == "1" and nxt is not None and not wf.get("halted"),
                   timeline=list(reversed(wf["transitions"][-40:])))


def _receipt(workflow_id: str) -> dict:
    """A unit's receipt: the workflow, every evidence record it cites, each record's hash re-checked now, and one
    fingerprint over them all (SHA-256 of the record ids and their content hashes, in order) to quote in a claim."""
    import hashlib

    from shared.utils.hashing import canonical_json, verify

    from ..orchestrator import bundle
    from .access import current

    store = _api().STORE
    wf = store.load_workflow(_wf_id(workflow_id))
    if wf is None:
        raise HTTPException(404, "no such workflow")
    b = bundle(wf, store)
    checked = {rid: bool(rec) and verify(rec) for rid, rec in b["evidence"].items()}
    chain = [[rid, (rec or {}).get("content_hash")] for rid, rec in b["evidence"].items()]
    return {"receipt": {
        "product": _brand.NAME, "workflow_id": wf["workflow_id"], "unit": wf["subject_id"], "seller_id": wf["org_id"],
        "seller": _brand.org_name(wf["org_id"]), "exported_at": _now_utc(), "exported_by": current().name,
        "final_outcome": wf.get("final_outcome"), "status": wf["status"], "records": len(chain),
        "hashes_ok": all(checked.values()) and bool(checked), "hash_check": checked,
        "fingerprint": hashlib.sha256(canonical_json({"workflow_id": wf["workflow_id"], "records": chain})).hexdigest(),
        "how_to_check": "Each record's content_hash is the SHA-256 of its canonical JSON (keys sorted, no spaces, "
                        "UTF-8) without 'content_hash' and 'overrides' (shared/utils/hashing.py). The fingerprint is "
                        "the SHA-256 of the canonical JSON of {workflow_id, records: [[record_id, content_hash], ...]}. "
                        "A content hash shows a record was not changed after it was sealed; it is not a signature."},
        "workflow": wf, "evidence": b["evidence"]}


def _now_utc() -> str:
    from shared.utils import db

    return db.now()


@router.get("/ui/w/{workflow_id}/receipt.json")
def receipt_json(workflow_id: str):
    from fastapi.responses import JSONResponse

    from .access import audit

    r = _receipt(workflow_id)
    audit("receipt_exported", r["receipt"]["workflow_id"], org=r["receipt"]["seller_id"], format="json")
    name = f"{_brand.NAME.lower()}-receipt-{r['receipt']['unit']}.json"
    return JSONResponse(r, headers={"Content-Disposition": f'attachment; filename="{name}"'})


@router.get("/ui/w/{workflow_id}/receipt", response_class=HTMLResponse)
def receipt_page(request: Request, workflow_id: str):
    r = _receipt(workflow_id)
    wf = r["workflow"]
    stages = []
    for sr in wf["stage_results"]:
        rec = r["evidence"].get(sr.get("record_id")) if sr.get("record_id") else None
        icon, name, _ = STAGES.get(sr["stage"], ("•", sr["stage"].title(), ""))
        stages.append({"sr": sr, "rec": rec, "icon": icon, "name": name, "ok": r["receipt"]["hash_check"].get(sr.get("record_id")),
                       "overrides": [o for o in wf["overrides"] if rec and o["supersedes"]["record_id"] == rec["record_id"]]})
    return _render(request, "receipt.html", r=r["receipt"], wf=wf, stages=stages)


@router.get("/ui/w/{workflow_id}/r/{record_id}", response_class=HTMLResponse)
def record(request: Request, workflow_id: str, record_id: str):
    store = _api().STORE
    wf = store.load_workflow(_wf_id(workflow_id))
    if wf is None or record_id not in wf["evidence_references"]:
        raise HTTPException(404, "no such record in this workflow")
    rec = store.get_evidence(record_id)
    if rec is None:
        raise HTTPException(404, "record missing from the store")
    mine = [o for o in wf["overrides"] if o["supersedes"]["record_id"] == record_id]
    shown = {i["ref"] for i in rec.get("inputs", []) if i.get("kind") == "image" and _photo_exists(i["ref"])}
    return _render(request, "record.html", wf=wf, rec=rec, shown=shown, overrides=mine, in_workflow=set(wf["evidence_references"]),
                   final=wf.get("final_outcome"), pretty=json.dumps(rec, indent=2))


@router.post("/ui/w/{workflow_id}/override")
def override(workflow_id: str, record_id: str = Form(...), new_verdict: str = Form(...), actor: str = Form(...),
             reason: str = Form(...)):
    url = f"/ui/w/{_wf_id(workflow_id)}"
    api = _api()
    try:
        wf = apply_override(workflow_id, api.STORE, record_id=record_id, new_verdict=new_verdict, actor=api.actor_for(actor),
                            reason=reason)
    except (ValueError, KeyError) as exc:
        return _back(url, str(exc), bad=True)
    stale = stale_stages(wf)  # later stages that decided on the verdict that was just overridden
    if not stale:
        return _back(url, "Override recorded. The original record is unchanged.")
    resume(workflow_id, load_flow(api.FLOW), api.STORE, _wf_clients(workflow_id))
    return _back(url, f"Override recorded. The original record is unchanged. Ran {', '.join(stale)} again on the new verdict.")


@router.post("/ui/w/{workflow_id}/resume")
def resume_view(workflow_id: str):
    api = _api()
    if api.STORE.load_workflow(_wf_id(workflow_id)) is None:
        raise HTTPException(404, "no such workflow")
    resume(workflow_id, load_flow(api.FLOW), api.STORE, _wf_clients(workflow_id))
    return RedirectResponse(f"/ui/w/{workflow_id}", status_code=303)


@router.get("/ui/sim", response_class=HTMLResponse)
def simulator(request: Request):
    workflows = _workflows()
    rows = [_unit_row(c, workflows) for c in _cases() if c["unit_id"] in DEMO]
    rows.sort(key=lambda r: list(DEMO).index(r["unit"]))
    return _render(request, "sim.html", rows=rows, modes=MODES, default=default_mode(), nav="sim",
                   flow=load_flow(_api().FLOW))


@router.post("/ui/sim/start")
def sim_start(unit: str = Form(...), org: str = Form(""), mode: str = Form("live"), play: str = Form("")):
    """Create the workflow (no stage runs yet), then go to it, ready to step through one agent at a time."""
    if not org:  # the page fills it in with a script; a unit id that belongs to exactly one organisation is enough
        owners = [c["org_id"] for c in _cases() if c["unit_id"] == unit]
        if len(owners) != 1:
            raise HTTPException(422, "say which organisation this unit belongs to")
        org = owners[0]
    case = _case(org, unit)
    if mode not in MODES:
        raise HTTPException(422, "unknown mode")
    api = _api()
    wf_id = workflow_id_for(case)
    try:
        existing = api.STORE.load_workflow(wf_id)
        if existing is None:
            start({**case, "route": case["route"]}, load_flow(api.FLOW), api.STORE, extra_context={"console_mode": mode})
        elif mode_of(existing) != mode or any(sr["runs"] for sr in existing["stage_results"]):
            # Start over in the chosen mode: earlier records are kept (evidence is never deleted), every stage runs again.
            wf = api.STORE.load_workflow(wf_id)
            wf["context"]["console_mode"] = mode
            api.STORE.save_workflow(wf)
            restart(wf_id, api.STORE, why=f"started over from the simulator ({mode} mode)")
    except WorkflowConflict as exc:
        return _back("/ui/sim", str(exc), bad=True)
    return RedirectResponse(f"/ui/w/{wf_id}{'?play=1' if play else ''}", status_code=303)


@router.post("/ui/w/{workflow_id}/step")
def step_view(workflow_id: str, play: str = Form("")):
    """Run exactly one stage, the next one that has not run, then show the workflow again."""
    api = _api()
    if api.STORE.load_workflow(_wf_id(workflow_id)) is None:
        raise HTTPException(404, "no such workflow")
    step(workflow_id, load_flow(api.FLOW), api.STORE, _wf_clients(workflow_id))
    return RedirectResponse(f"/ui/w/{workflow_id}{'?play=1' if play else ''}#steps", status_code=303)


@router.post("/ui/w/{workflow_id}/run/{stage}")
def run_stage_view(workflow_id: str, stage: str):
    """Run one stage now with its current photos (after a camera snap, for example), then show the workflow."""
    api = _api()
    if api.STORE.load_workflow(_wf_id(workflow_id)) is None:
        raise HTTPException(404, "no such workflow")
    if stage not in STAGES:
        raise HTTPException(404, "no such stage")
    try:
        run_stage(workflow_id, stage, load_flow(api.FLOW), api.STORE, _wf_clients(workflow_id), why="run again with new photos")
    except ValueError as exc:
        return _back(f"/ui/w/{workflow_id}", str(exc), bad=True)
    return RedirectResponse(f"/ui/w/{workflow_id}#steps", status_code=303)


@router.post("/ui/w/{workflow_id}/restart")
def restart_view(workflow_id: str):
    api = _api()
    if api.STORE.load_workflow(_wf_id(workflow_id)) is None:
        raise HTTPException(404, "no such workflow")
    restart(workflow_id, api.STORE, why="started over from the console")
    return _back(f"/ui/w/{workflow_id}", "Started over. Earlier records are kept in the evidence list; every stage will run again.")


@router.get("/ui/w/{workflow_id}/state")
def state_view(workflow_id: str):
    """The workflow's current state as JSON (for scripts and the live view)."""
    wf = _api().STORE.load_workflow(_wf_id(workflow_id))
    if wf is None:
        raise HTTPException(404, "no such workflow")
    return {"workflow_id": wf["workflow_id"], "status": wf["status"], "status_reason": wf["status_reason"], "mode": mode_of(wf),
            "stages": [{k: sr.get(k) for k in ("stage", "state", "verdict", "outcome", "needs_human", "record_id", "duration_ms")}
                       for sr in wf["stage_results"]],
            "final_outcome": wf.get("final_outcome")}


# ---------------------------------------------------------------- adding units
def _unit_orgs() -> list[str]:
    """The sellers the person asking may add a unit to: every seller for the admin, their own for a seller's code."""
    from .access import current

    who = current()
    return sorted(o for o in {c["org_id"] for c in _all_cases()} | set(_brand.org_names()) if who.sees(o))


@router.get("/ui/units/new", response_class=HTMLResponse)
def unit_new(request: Request, org: str = ""):
    from . import units
    from .access import current

    if current().stage:
        raise HTTPException(403, f"This code is for the {current().stage} station only.")
    orgs = _unit_orgs()
    if not orgs:
        raise HTTPException(404, "no seller to add a unit to")
    org = org if org in orgs else orgs[0]
    return _render(request, "unit_new.html", orgs=orgs, org=org, products=units.products(org),
                   sku=request.query_params.get("sku", ""), form={}, nav="home")


@router.post("/ui/units")
async def unit_add(request: Request):
    """A seller's order typed in: a new unit whose order every agent checks its photos against."""
    from . import units
    from .access import audit, current

    form = {k: v for k, v in (await request.form()).items() if isinstance(v, str)}
    org = form.get("org", "")
    if org not in _unit_orgs():
        raise HTTPException(404, "no such seller")
    who = current()
    again = f"/ui/units/new?org={org}"
    try:
        units.build_case(org, "UNIT-0000", {"sku": "SKU-CHECK", "title": "check"}, form, who.name)  # the numbers, first
        if form.get("sku") == "__new__":
            item = units.add_product(org, form.get("title", ""), form.get("colour", ""), form.get("variant", ""),
                                     form.get("components", ""), form.get("looks", ""))
        else:
            item = next((p for p in units.products(org) if p["sku"] == form.get("sku")), None)
            if item is None:
                raise units.UnitError("Choose a product from the list.")
        for _ in range(5):  # another worker may take the same next id: take the one after
            unit_id = units.next_unit_id({c["unit_id"] for c in _all_cases()})
            case = units.build_case(org, unit_id, item, form, who.name)
            if units.save(case, who.name):
                break
        else:
            raise units.UnitError("Could not pick a free unit id; try again.")
    except units.UnitError as exc:
        return _back(again, str(exc), bad=True)
    audit("unit_added", unit_id, org=org, product=item["sku"], route=case["route"], returned=case["returned"],
          qty=case["order"]["qty_ordered"])
    where = "Pack" if case["route"] == "mfn" else "Prep"
    return _back(f"/ui/capture/{org}/{unit_id}",
                 f"{unit_id} added: {item['title']}, {case['order']['qty_ordered']} units, Receiving then {where}"
                 f"{' then Returns' if case['returned'] else ''}. Add each step's photos, then run it.")


# ---------------------------------------------------------------- photos
def _reference(org: str, unit: str) -> dict | None:
    """Returns judges only a product that has a reference photo on its card (agents/returns/onboard.py). For a returned
    unit: which product was ordered, and how many reference photos its card holds."""
    given = next((c.get("return") for c in _cases() if c["org_id"] == org and c["unit_id"] == unit), None)
    try:
        sku = given["ordered_sku"] if given else sample_data.row("returns", unit, org)["ordered_sku"]
    except LookupError:
        return None
    from agents.returns.core.refs import load_card

    card = load_card(org, sku)
    return {"sku": sku, "title": card.title if card else sku, "card": card is not None,
            "images": [i.id for i in card.reference_images] if card else []}


@router.get("/ui/capture/{org}/{unit}", response_class=HTMLResponse)
def capture(request: Request, org: str, unit: str):
    case = _case(org, unit)
    stages = [{"stage": s, "icon": STAGES[s][0], "name": STAGES[s][1], "photos": _photos(unit, s),
               "max": MAX_PER_STAGE.get(s, DEFAULT_MAX), "text": _what_to_shoot(s, unit, org)} for s in _stages_for(case)]
    ref = _reference(org, unit) if case["returned"] else None
    return _render(request, "capture.html", case=case, stages=stages, story=DEMO.get(unit), ref=ref)


@router.post("/ui/reference/{org}/{unit}")
async def reference(org: str, unit: str, files: list[UploadFile] = File(...)):
    """Onboard the ordered product for Returns with photos of it as sold (new, parts laid out)."""
    from agents.returns.onboard import OnboardError, onboard

    case = _case(org, unit)
    url = f"/ui/capture/{org}/{unit}"
    ref = _reference(org, unit) if case["returned"] else None
    if ref is None or not ref["card"]:
        raise HTTPException(404, f"{unit} has no returned product with a card to onboard")
    added = 0
    for f in files:
        if not f.filename:
            continue
        data = await f.read(MAX_UPLOAD + 1)
        try:
            added += onboard(org, ref["sku"], data, view="contents_layout", actor="console")["added"]
        except OnboardError as exc:
            return _back(url, f"{f.filename}: {exc}", bad=True)
    return _back(url, f"{ref['sku']}: {added} reference photo(s) added. Returns can now judge it." if added
                 else "No new reference photo (already on the card, or none chosen).", bad=not added)


@router.post("/ui/capture/{org}/{unit}/{stage}")
async def upload(org: str, unit: str, stage: str, files: list[UploadFile] = File(...)):
    case = _case(org, unit)
    url = f"/ui/capture/{org}/{unit}"
    if stage not in _stages_for(case):
        raise HTTPException(404, f"{stage} is not a stage of {unit}")
    saved, err = await save_photos(unit, stage, files)
    if err:
        return _back(url, err, bad=True)
    return _back(url, f"Saved {saved} photo(s) for {stage}." if saved else "No photo was chosen.", bad=not saved)


async def save_photos(unit: str, stage: str, files: list[UploadFile], *, replace: bool = False) -> tuple[int, str | None]:
    """Check and store uploaded photos for one stage: (saved, error). Used by the laptop pages and the phone stations.

    Every file is checked (type, size, really an image) before any is written, so a bad file never leaves half a set.
    `replace` removes the stage's earlier photos first (a retake). Their SHA-256 stays in every record that used them."""
    import io

    folder = _input_root() / unit / stage
    limit, ready = MAX_PER_STAGE.get(stage, DEFAULT_MAX), []
    for f in files:
        if not f.filename:
            continue
        ext = Path(f.filename).suffix.lower()
        if ext == ".jpeg":
            ext = ".jpg"
        if ext not in PHOTO_EXT:
            hint = " (an iPhone HEIC photo: set Camera > Formats > Most Compatible)" if ext in (".heic", ".heif") else ""
            return 0, f"{f.filename}: only JPG, PNG or WEBP photos{hint}."
        data = await f.read(MAX_UPLOAD + 1)
        if len(data) > MAX_UPLOAD:
            return 0, f"{f.filename}: larger than {MAX_UPLOAD // (1024 * 1024)} MB."
        try:
            with Image.open(io.BytesIO(data)) as img:
                img.verify()
        except (UnidentifiedImageError, OSError, SyntaxError, Image.DecompressionBombError):
            return 0, f"{f.filename} is not a readable image."
        ready.append((f.filename, ext, data))
    have = 0 if replace else len(_photos(unit, stage))
    if have + len(ready) > limit:
        return 0, f"{stage} takes at most {limit} photos." + ("" if replace else " Remove one first.")
    if not ready:
        return 0, None
    if replace:
        for name in _photos(unit, stage):
            (folder / name).unlink()
    folder.mkdir(parents=True, exist_ok=True)
    for i, (name, ext, data) in enumerate(ready, have + 1):
        stem = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(name).stem).strip("._-")[:40] or "photo"
        target = folder / f"{i:02d}-{stem}{ext}"
        while target.exists():  # a retake reuses numbers; never overwrite a file an earlier record may cite
            stem += "_"
            target = folder / f"{i:02d}-{stem}{ext}"
        target.write_bytes(data)
    _keep(unit, stage)
    return len(ready), None


def _keep(unit: str, stage: str, org: str | None = None) -> None:
    """Copy this stage's photos into the database (a deployment's disk does not survive a restart)."""
    from shared.utils import db

    if db.enabled():
        org = org or next((c["org_id"] for c in _all_cases() if c["unit_id"] == unit), None)
        db.sync_folder("input", _input_root() / unit / stage, org)


@router.post("/ui/capture/{org}/{unit}/{stage}/delete")
def delete_photo(org: str, unit: str, stage: str, name: str = Form(...)):
    case = _case(org, unit)
    if stage not in _stages_for(case):
        raise HTTPException(404, "no such stage")
    try:
        path = resolve_capture(_input_root(), f"{unit}/{stage}/{name}", unit, stage)
    except CapturePathError as exc:
        raise HTTPException(400, str(exc)) from exc
    if path.suffix.lower() in PHOTO_EXT and path.is_file():
        path.unlink()
        _keep(unit, stage, org)
    return _back(f"/ui/capture/{org}/{unit}", "Photo removed.")


@router.get("/ui/photo/{unit}/{stage}/{name}")
def photo(unit: str, stage: str, name: str):
    if not any(c["unit_id"] == unit for c in _cases()):  # another org's unit: as if there were no such photo
        raise HTTPException(404, "no such photo")
    try:
        path = resolve_capture(_input_root(), f"{unit}/{stage}/{name}", unit, stage)
    except CapturePathError:
        raise HTTPException(404, "no such photo") from None
    if path.suffix.lower() not in PHOTO_EXT or not path.is_file():
        raise HTTPException(404, "no such photo")
    return FileResponse(path, media_type=PHOTO_EXT[path.suffix.lower()], headers={"Cache-Control": "no-store"})
