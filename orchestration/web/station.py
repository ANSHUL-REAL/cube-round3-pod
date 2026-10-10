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
from .access import (COOKIE, LOOPBACK, MAX_WRONG, MAX_WRONG_TOTAL, PROXY_HEADERS, _WRONG, check_code, current,  # noqa: F401
                     admin_password, guard as lan_guard, is_local, lan_code, locked, set_session, visitor, wrong)

router = APIRouter(dependencies=[Depends(_same_origin)])


# ---------------------------------------------------------------- access code (only with --lan)
def lan_url() -> str | None:
    """The join page phones open: the public link when served through a tunnel (POD_PUBLIC_URL, set by
    serve.py --tunnel) or deployed (RENDER_EXTERNAL_URL, set by Render), else http://<this laptop's Wi-Fi
    address>:<port>/join. None when not serving to phones."""
    public = (os.environ.get("POD_PUBLIC_URL") or os.environ.get("RENDER_EXTERNAL_URL") or "").rstrip("/")
    if not lan_code() and not (public and admin_password()):
        return None
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


def _safe_next(next: str, default: str = "/ui/station") -> str:
    """Only a path on this site: never another host ("//evil", "https://...", a backslash trick)."""
    ok = next.startswith("/") and not next.startswith(("//", "/\\")) and not any(c in next for c in "\r\n")
    return next if ok and next not in ("/join", "/login") else default


@router.get("/join", response_class=HTMLResponse)
def join_page(request: Request, next: str = "/ui/station"):
    return _render(request, "join.html", phone=True, next=_safe_next(next), locked=locked(request))


@router.post("/join")
def join(request: Request, code: str = Form(...), next: str = Form("/ui/station")):
    """The team code (POD_LAN_CODE) or a code the admin issued for one org. A session cookie follows; the code itself
    is never stored in the browser."""
    from .access import audit, guard_on

    if not guard_on():
        return RedirectResponse(_safe_next(next), status_code=303)
    if locked(request):
        raise HTTPException(429, "too many wrong codes; restart the server to reset")
    acc = check_code(code)
    if acc is None:
        wrong(request)
        audit("sign_in_failed", "access code", status=303, ip=visitor(request))
        return RedirectResponse(f"/join?msg={quote('Wrong code. Ask whoever runs the demo.')}&bad=1", status_code=303)
    _WRONG.pop(visitor(request), None)
    audit("signed_in", "access code", acc=acc, org=None if acc.orgs is None else ",".join(sorted(acc.orgs)))
    land = f"/ui/station/{acc.stage}" if acc.stage else "/ui/station"  # a station code goes straight to its station
    resp = RedirectResponse(_safe_next(next, land) if next != "/ui/station" else land, status_code=303)
    set_session(resp, acc, request)
    return resp


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request, next: str = "/admin"):
    return _render(request, "login.html", next=_safe_next(next, "/admin"), locked=locked(request))


@router.post("/login")
def login(request: Request, password: str = Form(...), next: str = Form("/admin")):
    from .access import audit, check_admin

    if locked(request):
        raise HTTPException(429, "too many wrong attempts; restart the server to reset")
    acc = check_admin(password)
    if acc is None:
        wrong(request)
        audit("admin_sign_in_failed", "admin", status=303, ip=visitor(request))
        return RedirectResponse(f"/login?msg={quote('Wrong password.')}&bad=1", status_code=303)
    _WRONG.pop(visitor(request), None)
    audit("admin_signed_in", "admin", acc=acc)
    resp = RedirectResponse(_safe_next(next, "/admin"), status_code=303)
    set_session(resp, acc, request)
    return resp


@router.post("/logout")
def logout():
    resp = RedirectResponse("/join", status_code=303)
    resp.delete_cookie(COOKIE)
    return resp


@router.get("/ui/phones.svg")
def phones_qr(request: Request, stage: str = ""):
    """The QR code for the join page (or straight to one agent's station), shown to the presenter: the laptop
    itself, or anyone who already entered the code (a tunnelled laptop is not local)."""
    url = lan_url()
    if not url or not current().team:
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


def waiting_on(wf: dict | None, stage: str) -> str | None:
    """The earlier step this one waits for (its station is where the unit is now), or None."""
    if wf is None:
        return "receiving" if stage != "receiving" else None
    for sr in wf["stage_results"]:
        if sr["stage"] == stage:
            return None
        if sr["state"] in ("pending", "error"):
            return sr["stage"]
    return None


def plain(sr: dict | None) -> dict | None:
    """A step's result in a station person's words: one label and its colour. The record keeps the exact verdict and
    outcome; this never turns an UNCERTAIN into anything better."""
    if not sr or sr["state"] not in ("completed", "error"):
        return None
    if sr["state"] == "error":
        return {"tone": "error", "label": "did not run"}
    verdict, outcome = sr.get("verdict"), (sr.get("outcome") or "").replace("_", " ").lower()
    if verdict == "PASS":
        return {"tone": "pass", "label": outcome or "passed"}
    if verdict == "FAIL":
        return {"tone": "fail", "label": outcome or "problem found"}
    return {"tone": "uncertain", "label": outcome if outcome and outcome != "pending review" else "a person checks"}


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
               "outcome": sr.get("outcome") if sr else None, "demo": c["unit_id"] in DEMO, "plain": plain(sr),
               "retake": bool(sr) and sr["state"] == "error",
               # Receiving can start any unit: the demo stories, the units someone added and the ones already started
               # are listed; the other sample units stay one search away
               "more": stage == "receiving" and not (c["unit_id"] in DEMO or c.get("added") or sr is not None)}
        if ok and (sr is None or sr["state"] in ("pending", "error")):
            mine.append(row)  # a step that did not run is still this station's to do: it is not "done"
        elif sr and sr["state"] == "completed":
            done.append(row)
        elif wf is not None:
            later.append(row)
    mine.sort(key=lambda r: (r["more"], not r["retake"], r["state"] is None, not r["demo"], r["unit"]))
    return _render(request, "station_stage.html", phone=True, stage=stage, mine=mine, later=later, done=done,
                   hidden=sum(r["more"] for r in mine))


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
                   blocker=None if ok or (wf and mode_of(wf) != "live") else waiting_on(wf, stage),
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
