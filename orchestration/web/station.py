"""Phone stations: each person clears their own step from their own phone.

    python scripts/serve.py --lan --data D:/pod12-demo

The laptop shows a QR code and a 6-digit access code. A teammate scans it on the same Wi-Fi, enters the code once, picks
their station (Receiving, Prep, Pack or Returns) and sees the units waiting for them. Tapping the camera button opens the
phone's own camera app (an <input capture> works over plain http on a phone; the in-page camera needs https), the photos
are uploaded, and that stage's real agent runs at once. When the last photo stage is done, Recovery runs on its own.

Rules the stations keep:
  * your turn only: a step runs when every earlier step of that unit has finished without an error, so a phone never
    triggers someone else's step with no photo;
  * live mode only: a unit that the laptop started in replay mode stays on the laptop;
  * a retake replaces that step's photos, and the later steps run again on the new evidence at their turn
    (orchestrator.run_stage); nothing already recorded is deleted from the evidence store.

Access: when serve.py starts with --lan, every request from another device needs the access code (a cookie set on
/join). Requests from the laptop itself (loopback) do not. Ten wrong codes from one address locks it out until the
server restarts. This is a demo guard on a trusted Wi-Fi, not an account system (decision D-O08).
"""
from __future__ import annotations

import hmac
import io
import os
import socket
import time
from collections import defaultdict
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from . import (STAGES, WorkflowConflict, _api, _case, _cases, _photos, _reference, _render, _same_origin, _stages_for,
               _what_to_shoot, _workflows, DEMO, load_flow, mode_of, run_stage, save_photos, start, workflow_id_for)

PHOTO_STAGES = ("receiving", "prep", "pack", "returns")
STATIONS = PHOTO_STAGES + ("recovery",)  # every agent has its own station; Recovery needs no photo, only a press
COOKIE = "pod12_code"
LOOPBACK = {"127.0.0.1", "::1", "localhost", "testclient"}
_WRONG: dict[str, int] = defaultdict(int)
MAX_WRONG = 10
MAX_WRONG_TOTAL = 50  # from anywhere: a forwarded address can be faked, so the total is capped too

router = APIRouter(dependencies=[Depends(_same_origin)])


# ---------------------------------------------------------------- access code (only with --lan)
def lan_code() -> str | None:
    return os.environ.get("POD_LAN_CODE") or None


def lan_url() -> str | None:
    """The join page phones open: the public link when served through a tunnel (POD_PUBLIC_URL, set by
    serve.py --tunnel), else http://<this laptop's Wi-Fi address>:<port>/join. None when not serving to phones."""
    if not lan_code():
        return None
    public = os.environ.get("POD_PUBLIC_URL", "").rstrip("/")
    return f"{public}/join" if public else f"http://{lan_ip()}:{os.environ.get('POD_LAN_PORT', '8100')}/join"


def lan_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))  # no packet is sent: this only asks the OS which interface would be used
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


PROXY_HEADERS = ("cf-connecting-ip", "cf-ray", "x-forwarded-for", "x-real-ip", "forwarded")


def is_local(request: Request) -> bool:
    """The laptop itself: a loopback connection that no proxy forwarded. A Cloudflare tunnel (or any reverse proxy)
    also connects from 127.0.0.1, so without the header check every visitor to the public link would count as local
    and skip the access code."""
    if any(h in request.headers for h in PROXY_HEADERS):
        return False
    return (request.client.host if request.client else "") in LOOPBACK


def visitor(request: Request) -> str:
    """Who is asking, for the wrong-code lockout: the address the tunnel or proxy reports, else the socket's."""
    for h in ("cf-connecting-ip", "x-real-ip"):
        if request.headers.get(h):
            return request.headers[h].strip()
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else ""


def _has_code(request: Request) -> bool:
    code = lan_code()
    return bool(code) and hmac.compare_digest(request.cookies.get(COOKIE, ""), code)


async def lan_guard(request: Request, call_next):
    """With --lan, everything except the join page and the stylesheet needs the code, unless it comes from the laptop."""
    if not lan_code() or is_local(request) or _has_code(request):
        return await call_next(request)
    path = request.url.path
    if path == "/join" or path.startswith("/ui/static/"):
        return await call_next(request)
    if request.method == "GET":
        return RedirectResponse(f"/join?next={quote(path)}", status_code=303)
    return Response("access code required", status_code=403)


@router.get("/join", response_class=HTMLResponse)
def join_page(request: Request, next: str = "/ui/station"):
    return _render(request, "join.html", phone=True, next=next if next.startswith("/ui/station") else "/ui/station",
                   locked=_WRONG[visitor(request)] >= MAX_WRONG)


@router.post("/join")
def join(request: Request, code: str = Form(...), next: str = Form("/ui/station")):
    ip = visitor(request)
    want = lan_code()
    if not want:
        return RedirectResponse("/ui/station", status_code=303)
    if _WRONG[ip] >= MAX_WRONG or sum(_WRONG.values()) >= MAX_WRONG_TOTAL:
        raise HTTPException(429, "too many wrong codes; restart the server to reset")
    if not hmac.compare_digest(code.strip(), want):
        _WRONG[ip] += 1
        return RedirectResponse(f"/join?msg={quote('Wrong code. It is on the laptop screen.')}&bad=1", status_code=303)
    _WRONG.pop(ip, None)
    resp = RedirectResponse(next if next.startswith("/ui/station") else "/ui/station", status_code=303)
    resp.set_cookie(COOKIE, want, httponly=True, samesite="lax", max_age=12 * 3600)
    return resp


@router.get("/ui/phones.svg")
def phones_qr(request: Request, stage: str = ""):
    """The QR code for the join page (or straight to one agent's station), shown to the presenter: the laptop
    itself, or anyone who already entered the code (a tunnelled laptop is not local)."""
    url = lan_url()
    if not url or not (is_local(request) or _has_code(request)):
        raise HTTPException(404, "not serving to phones (start serve.py with --lan)")
    if stage in STATIONS:
        url += f"?next=/ui/station/{stage}"
    import segno

    buf = io.BytesIO()
    segno.make(url, error="m").save(buf, kind="svg", scale=6, border=2, dark="#17131a", light="#fffdf8")
    return Response(buf.getvalue(), media_type="image/svg+xml", headers={"Cache-Control": "no-store"})


def station_clients() -> dict | None:
    """Our real agents (None = the flow's own clients). Stations only ever run live workflows; tests replace this."""
    return None


# ---------------------------------------------------------------- whose turn is it
def turn(wf: dict | None, stage: str) -> tuple[bool, str]:
    """(is it this stage's turn, why not). Earlier stages must have finished, and not with an error."""
    if wf is None:
        return stage == "receiving", "not started yet: Receiving starts it"
    if mode_of(wf) != "live":
        return False, "this unit runs in replay mode on the laptop"
    for sr in wf["stage_results"]:
        if sr["stage"] == stage:
            if sr["state"] == "skipped":
                return False, f"{stage} is not a step of this unit"
            return True, ""
        if sr["state"] == "skipped":
            continue
        if sr["state"] == "pending":
            return False, f"waiting for {STAGES[sr['stage']][1]}"
        if sr["state"] == "error":
            return False, f"{STAGES[sr['stage']][1]} must retake its photos first"
    return False, f"{stage} is not a step of this unit"


def _stage_result(wf: dict | None, stage: str) -> dict | None:
    return next((sr for sr in (wf or {}).get("stage_results", []) if sr["stage"] == stage), None)


def _station_stages(case: dict) -> list[str]:
    """The steps of this unit that a person clears at a station: its photo steps, then Recovery."""
    return [*_stages_for(case), "recovery"]


# ---------------------------------------------------------------- pages
@router.get("/ui/station", response_class=HTMLResponse)
def stations(request: Request):
    wfs = _workflows()
    waiting = {s: 0 for s in STATIONS}
    for wf in wfs.values():
        for s in STATIONS:
            sr = _stage_result(wf, s)
            if sr and sr["state"] == "pending" and turn(wf, s)[0]:
                waiting[s] += 1
    return _render(request, "station_home.html", phone=True, waiting=waiting, photo_stages=STATIONS)


@router.get("/ui/station/{stage}", response_class=HTMLResponse)
def station(request: Request, stage: str):
    if stage not in STATIONS:
        raise HTTPException(404, "no such station")
    wfs = _workflows()
    mine, later, done = [], [], []
    for c in _cases():
        if stage not in _station_stages(c):
            continue
        wf = wfs.get((c["org_id"], c["unit_id"]))
        if wf is None and stage != "receiving":
            continue
        sr = _stage_result(wf, stage)
        ok, why = turn(wf, stage)
        row = {"org": c["org_id"], "unit": c["unit_id"], "story": DEMO.get(c["unit_id"]), "why": why,
               "state": sr["state"] if sr else None, "verdict": sr.get("verdict") if sr else None,
               "outcome": sr.get("outcome") if sr else None, "demo": c["unit_id"] in DEMO}
        if ok and (sr is None or sr["state"] == "pending"):
            mine.append(row)
        elif sr and sr["state"] in ("completed", "error"):
            done.append(row)
        elif wf is not None:
            later.append(row)
    mine.sort(key=lambda r: (not r["demo"], r["unit"]))
    if stage == "receiving":  # anyone can start a unit at Receiving; the demo stories come first, the rest stay one search away
        mine = [r for r in mine if r["demo"] or r["state"] == "pending"] + [r for r in mine if not r["demo"] and r["state"] != "pending"]
    return _render(request, "station_stage.html", phone=True, stage=stage, mine=mine, later=later, done=done)


@router.get("/ui/station/{stage}/{org}/{unit}", response_class=HTMLResponse)
def station_unit(request: Request, stage: str, org: str, unit: str):
    case = _case(org, unit)
    if stage not in STATIONS or stage not in _station_stages(case):
        raise HTTPException(404, f"{stage} is not a step of {unit}")
    api = _api()
    wf = api.STORE.load_workflow(workflow_id_for(case))
    ok, why = turn(wf, stage)
    sr = _stage_result(wf, stage)
    rec = api.STORE.get_evidence(sr["record_id"]) if sr and sr.get("record_id") else None
    nxt = None
    if wf:
        nxt = next((STAGES[s["stage"]][1] for s in wf["stage_results"] if s["state"] == "pending"), None)
    ref = _reference(org, unit) if stage == "returns" else None
    return _render(request, "station_unit.html", phone=True, stage=stage, org=org, unit=unit, story=DEMO.get(unit),
                   ok=ok, why=why, sr=sr, rec=rec, wf=wf, nxt=nxt, photos=_photos(unit, stage), ref=ref,
                   what=_what_to_shoot(stage, unit, org), done=request.query_params.get("done") == "1")


async def _files(request: Request) -> list[UploadFile]:
    """The photos in a form. "Check now" with no new photo sends an empty file field; that is "use what is here",
    not an error (a declared UploadFile parameter would answer it with a 422 page on the phone)."""
    form = await request.form()
    return [f for f in form.getlist("files") if getattr(f, "filename", None)]


@router.post("/ui/station/{stage}/{org}/{unit}")
async def station_run(request: Request, stage: str, org: str, unit: str):
    """Save the photos just taken (a retake replaces the old ones), then run this step's real agent now."""
    files = await _files(request)
    case = _case(org, unit)
    if stage not in STATIONS or stage not in _station_stages(case):
        raise HTTPException(404, f"{stage} is not a step of {unit}")
    here = f"/ui/station/{stage}/{org}/{unit}"
    api, flow = _api(), None
    wf_id = workflow_id_for(case)
    wf = api.STORE.load_workflow(wf_id)
    ok, why = turn(wf, stage)
    if not ok:
        return RedirectResponse(f"{here}?msg={quote('Not your turn yet: ' + why)}&bad=1", status_code=303)
    if stage in PHOTO_STAGES:
        saved, err = await save_photos(unit, stage, files, replace=True)
        if err:
            return RedirectResponse(f"{here}?msg={quote(err)}&bad=1", status_code=303)
        if not saved and not _photos(unit, stage):
            return RedirectResponse(f"{here}?msg={quote('Take a photo first.')}&bad=1", status_code=303)
    flow = load_flow(api.FLOW)
    started = time.perf_counter()
    try:
        if wf is None:
            start({**case, "route": case["route"]}, flow, api.STORE, extra_context={"console_mode": "live"})
        why = "run at the Recovery station" if stage == "recovery" else f"photos taken at the {stage} station"
        run_stage(wf_id, stage, flow, api.STORE, station_clients(), why=why)
    except WorkflowConflict as exc:
        return RedirectResponse(f"{here}?msg={quote(str(exc))}&bad=1", status_code=303)
    secs = time.perf_counter() - started
    return RedirectResponse(f"{here}?done=1&msg={quote(f'Checked in {secs:.1f} s.')}", status_code=303)


@router.post("/ui/station/returns/{org}/{unit}/reference")
async def station_reference(request: Request, org: str, unit: str):
    """Onboard the product as sold (once per product), from the Returns phone."""
    files = await _files(request)
    from agents.returns.onboard import OnboardError, onboard

    here = f"/ui/station/returns/{org}/{unit}"
    ref = _reference(org, unit) if _case(org, unit)["returned"] else None
    if ref is None or not ref["card"]:
        raise HTTPException(404, f"{unit} has no returned product to onboard")
    added = 0
    for f in files:
        if not f.filename:
            continue
        try:
            added += onboard(org, ref["sku"], await f.read(12 * 1024 * 1024 + 1), view="contents_layout", actor="returns-station")["added"]
        except OnboardError as exc:
            return RedirectResponse(f"{here}?msg={quote(str(exc))}&bad=1", status_code=303)
    msg = "Product photo saved. Now photograph the returned item." if added else "No new product photo."
    return RedirectResponse(f"{here}?msg={quote(msg)}{'' if added else '&bad=1'}", status_code=303)
