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
from ..orchestrator import WorkflowConflict, apply_override, load_flow, resume, run_workflow, stale_stages, workflow_id_for

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
templates = Jinja2Templates(directory=str(HERE / "templates"))

STAGES = {
    "receiving": ("📥", "Receiving", "Checks the delivery against the purchase order"),
    "prep": ("🏷️", "Prep", "Checks the prepped unit against its work order"),
    "pack": ("📦", "Pack", "Checks the open box against the order before it is sealed"),
    "returns": ("↩️", "Returns", "Checks identity, completeness and condition of the returned parcel"),
    "recovery": ("💸", "Recovery", "Finds charges the evidence contradicts, and claims only what it can prove"),
}
DEMO = {
    "UNIT-0014": "FBA unit that was returned: Receiving, Prep, Returns, Recovery. Can recommend an inbound-defect claim.",
    "UNIT-0008": "Merchant-fulfilled, clean: Receiving, Pack. Pack the right item and expect a seal.",
    "UNIT-0044": "Merchant-fulfilled, wrong box: put something else in the candle order and expect stop and fix.",
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


def _clients() -> dict | None:
    """With POD_UI_STUBS=1 the four photo stages replay the organisers' recorded evidence (no photos or model key needed);
    Recovery is still our real agent, deciding on that recorded evidence. Every page says so."""
    if not stub_mode():
        return None
    return {s: _InProc(importlib.import_module(f"tests.stubs.{s}_stub").handle) for s in STUBBED}


@lru_cache(maxsize=1)
def _cases() -> list[dict]:
    return json.loads((ROOT / "data" / "sample" / "cases.json").read_text())


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


def _what_to_shoot(stage: str, unit: str, org: str) -> str:
    try:
        return importlib.import_module("scripts.capture_plan").what_to_shoot(stage, unit, org)
    except Exception:  # the helper reads sample CSVs; a missing row must not break the page
        return "Photos for this stage."


def _workflows() -> dict[tuple[str, str], dict]:
    store = _api().STORE
    if not hasattr(store, "root"):  # an in-memory store keeps its workflows in a dict
        return {(w["org_id"], w["subject_id"]): w for w in store.workflows.values()}
    root, out = Path(store.root) / "workflows", {}
    for p in sorted(root.glob("*.json")) if root.is_dir() else []:
        try:
            wf = json.loads(p.read_text(encoding="utf-8"))
            out[(wf["org_id"], wf["subject_id"])] = wf
        except (OSError, ValueError, KeyError):
            continue
    return out


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
    return templates.TemplateResponse(request, name, ctx)


def _wf_id(workflow_id: str) -> str:
    """A workflow id from the URL is a file name in the store: refuse anything else before it is read, echoed or redirected to."""
    if not is_safe_id(workflow_id):
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
    from urllib.parse import quote

    return RedirectResponse(f"{url}?msg={quote(msg)}{'&bad=1' if bad else ''}", status_code=303)


# ---------------------------------------------------------------- pages
@router.get("/", response_class=HTMLResponse)
def home(request: Request):
    workflows = _workflows()
    rows = [_unit_row(c, workflows) for c in _cases()]
    demo = [r for r in rows if r["unit"] in DEMO]
    demo.sort(key=lambda r: list(DEMO).index(r["unit"]))
    return _render(request, "home.html", demo=demo, rows=rows, total=len(rows), stub_mode=stub_mode())


@router.post("/ui/run")
def run(org: str = Form(...), unit: str = Form(...)):
    case = _case(org, unit)
    api = _api()
    wf_id = workflow_id_for(case)
    try:
        existing = api.STORE.load_workflow(wf_id)
        if existing:
            resume(wf_id, load_flow(api.FLOW), api.STORE, _clients())
        else:
            run_workflow({**case, "route": case["route"]}, load_flow(api.FLOW), api.STORE, _clients())
    except WorkflowConflict as exc:
        return _back("/", str(exc), bad=True)
    return RedirectResponse(f"/ui/w/{wf_id}", status_code=303)


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
                      "unsure_checks": [c["check_key"] for c in (rec or {}).get("checks", []) if c["verdict"] == "UNCERTAIN"]})
    case = {"org_id": wf["org_id"], "unit_id": wf["subject_id"], "route": wf["context"].get("route", "unknown"),
            "returned": wf["context"].get("returned", False)}
    return _render(request, "workflow.html", wf=wf, steps=steps, final=wf.get("final_outcome"), case=case, stale=stale_stages(wf),
                   story=DEMO.get(wf["subject_id"]))


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
        wf = apply_override(workflow_id, api.STORE, record_id=record_id, new_verdict=new_verdict, actor=actor, reason=reason)
    except (ValueError, KeyError) as exc:
        return _back(url, str(exc), bad=True)
    stale = stale_stages(wf)  # later stages that decided on the verdict that was just overridden
    if not stale:
        return _back(url, "Override recorded. The original record is unchanged.")
    resume(workflow_id, load_flow(api.FLOW), api.STORE, _clients())
    return _back(url, f"Override recorded. The original record is unchanged. Ran {', '.join(stale)} again on the new verdict.")


@router.post("/ui/w/{workflow_id}/resume")
def resume_view(workflow_id: str):
    api = _api()
    if api.STORE.load_workflow(_wf_id(workflow_id)) is None:
        raise HTTPException(404, "no such workflow")
    resume(workflow_id, load_flow(api.FLOW), api.STORE, _clients())
    return RedirectResponse(f"/ui/w/{workflow_id}", status_code=303)


# ---------------------------------------------------------------- photos
def _reference(org: str, unit: str) -> dict | None:
    """Returns judges only a product that has a reference photo on its card (agents/returns/onboard.py). For a returned
    unit: which product was ordered, and how many reference photos its card holds."""
    try:
        sku = sample_data.row("returns", unit, org)["ordered_sku"]
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
    folder = _input_root() / unit / stage
    have, limit, saved = len(_photos(unit, stage)), MAX_PER_STAGE.get(stage, DEFAULT_MAX), 0
    for f in files:
        if not f.filename:
            continue
        ext = Path(f.filename).suffix.lower()
        if ext not in PHOTO_EXT:
            return _back(url, f"{f.filename}: only JPG, PNG or WEBP photos.", bad=True)
        if have + saved >= limit:
            return _back(url, f"{stage} takes at most {limit} photos. Remove one first.", bad=True)
        data = await f.read(MAX_UPLOAD + 1)
        if len(data) > MAX_UPLOAD:
            return _back(url, f"{f.filename}: larger than {MAX_UPLOAD // (1024 * 1024)} MB.", bad=True)
        try:
            import io

            with Image.open(io.BytesIO(data)) as img:
                img.verify()
        except (UnidentifiedImageError, OSError, SyntaxError, Image.DecompressionBombError):
            return _back(url, f"{f.filename} is not a readable image.", bad=True)
        folder.mkdir(parents=True, exist_ok=True)
        stem = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(f.filename).stem).strip("._-")[:40] or "photo"
        n = have + saved + 1
        (folder / f"{n:02d}-{stem}{ext}").write_bytes(data)
        saved += 1
    return _back(url, f"Saved {saved} photo(s) for {stage}." if saved else "No photo was chosen.", bad=not saved)


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
    return _back(f"/ui/capture/{org}/{unit}", "Photo removed.")


@router.get("/ui/photo/{unit}/{stage}/{name}")
def photo(unit: str, stage: str, name: str):
    try:
        path = resolve_capture(_input_root(), f"{unit}/{stage}/{name}", unit, stage)
    except CapturePathError:
        raise HTTPException(404, "no such photo") from None
    if path.suffix.lower() not in PHOTO_EXT or not path.is_file():
        raise HTTPException(404, "no such photo")
    return FileResponse(path, media_type=PHOTO_EXT[path.suffix.lower()], headers={"Cache-Control": "no-store"})
