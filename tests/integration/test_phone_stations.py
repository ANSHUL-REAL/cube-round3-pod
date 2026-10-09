"""Phone stations (orchestration/web/station.py): each person clears their own step from their phone, in turn, and only
with the access code when the console is served to the Wi-Fi (serve.py --lan)."""
from __future__ import annotations

import importlib
import io

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from orchestration import api
from orchestration.store import FileStore
from orchestration.web import station

REAL_AGENTS = {"receiving", "prep", "pack", "returns", "recovery"}
ALPHA = "org_demo_alpha"
FBA_RETURNED = "UNIT-0014"  # receiving -> prep -> returns -> recovery
PHONE = ("192.168.1.50", 50000)


def jpg(size=(8, 8), color=(200, 30, 90)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "JPEG")
    return buf.getvalue()


class _Stub:
    def __init__(self, stage):
        self.handle = importlib.import_module(f"tests.stubs.{stage}_stub").handle

    def run(self, request, timeout_s):
        return self.handle(request)


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setattr(api, "STORE", FileStore(tmp_path / "out"))
    monkeypatch.setenv("INPUT_DIR", str(tmp_path / "input"))
    monkeypatch.delenv("POD_UI_STUBS", raising=False)
    monkeypatch.delenv("POD_LAN_CODE", raising=False)
    # the photo stages answer with the organisers' recorded evidence (no key in tests); Recovery stays our real agent
    monkeypatch.setattr(station, "station_clients",
                        lambda: {s: _Stub(s) for s in ("receiving", "prep", "pack", "returns")})
    station._WRONG.clear()
    return tmp_path


@pytest.fixture
def laptop(setup):
    return TestClient(api.app, follow_redirects=False)


def snap(c, stage, unit=FBA_RETURNED, n=1, name="p.jpg"):
    return c.post(f"/ui/station/{stage}/{ALPHA}/{unit}", files=[("files", (name, jpg(), "image/jpeg")) for _ in range(n)])


def state(unit=FBA_RETURNED):
    return api.STORE.load_workflow(f"WF-{ALPHA}-{unit}")


def states(unit=FBA_RETURNED):
    return {s["stage"]: s["state"] for s in state(unit)["stage_results"]}


# ---------------------------------------------------------------- access code
def test_without_lan_the_stations_need_no_code(laptop):
    assert laptop.get("/ui/station").status_code == 200
    assert laptop.get("/ui/station/receiving").status_code == 200


def test_with_lan_a_phone_needs_the_code_and_the_laptop_does_not(setup, monkeypatch):
    monkeypatch.setenv("POD_LAN_CODE", "482913")
    phone = TestClient(api.app, client=PHONE, follow_redirects=False)
    laptop = TestClient(api.app, follow_redirects=False)
    assert laptop.get("/ui/station").status_code == 200
    r = phone.get("/ui/station/prep")
    assert r.status_code == 303 and r.headers["location"].startswith("/join")
    assert snap(phone, "receiving").status_code == 403, "no upload or agent run without the code"
    # the JSON API answers 401 (sign in with a Bearer code), not a redirect to an HTML page
    assert phone.post("/workflows", json={"org_id": ALPHA, "unit_id": FBA_RETURNED}).status_code == 401, "the API too"
    assert phone.get(f"/workflows/WF-{ALPHA}-{FBA_RETURNED}").status_code == 401
    assert phone.get("/join").status_code == 200 and phone.get("/ui/static/ui.css").status_code == 200
    bad = phone.post("/join", data={"code": "000000"})
    assert "bad=1" in bad.headers["location"] and phone.get("/ui/station").status_code == 303
    ok = phone.post("/join", data={"code": "482913", "next": "/ui/station/prep"})
    assert ok.status_code == 303 and ok.headers["location"] == "/ui/station/prep"
    assert phone.get("/ui/station/prep").status_code == 200
    assert phone.post("/join", data={"code": "482913", "next": "https://evil.example/"}).headers["location"] == "/ui/station"


def test_ten_wrong_codes_lock_that_phone_out(setup, monkeypatch):
    monkeypatch.setenv("POD_LAN_CODE", "482913")
    phone = TestClient(api.app, client=PHONE, follow_redirects=False)
    for _ in range(station.MAX_WRONG):
        phone.post("/join", data={"code": "111111"})
    assert phone.post("/join", data={"code": "482913"}).status_code == 429, "even the right code, once locked"


def test_the_join_qr_and_code_are_shown_on_the_laptop_only(setup, monkeypatch):
    monkeypatch.setenv("POD_LAN_CODE", "482913")
    laptop = TestClient(api.app, follow_redirects=False)
    svg = laptop.get("/ui/phones.svg")
    assert svg.status_code == 200 and svg.text.lstrip().startswith(("<?xml", "<svg"))
    assert "482913" in laptop.get("/").text and "482913" in laptop.get("/ui/sim").text
    stranger = TestClient(api.app, client=PHONE, follow_redirects=False)
    assert stranger.get("/ui/phones.svg").status_code == 303, "no QR without the code"
    assert "482913" not in stranger.get("/join").text
    for s in ("receiving", "prep", "pack", "returns", "recovery"):  # one QR per agent, straight to its station
        assert laptop.get(f"/ui/phones.svg?stage={s}").status_code == 200
    stranger.post("/join", data={"code": "482913"})
    assert "482913" not in stranger.get("/ui/station").text, "a station page never prints the code"


def test_through_a_cloudflare_tunnel_nobody_counts_as_the_laptop(setup, monkeypatch):
    """cloudflared connects from 127.0.0.1: without the header check every visitor to the public link would skip the
    code. With it, a tunnelled request needs the code like any phone, and the lockout counts the real visitor."""
    monkeypatch.setenv("POD_LAN_CODE", "482913")
    tunnel = TestClient(api.app, follow_redirects=False)  # a loopback connection, like cloudflared's
    via = {"cf-connecting-ip": "203.0.113.7", "cf-ray": "abc"}
    r = tunnel.get("/ui/station", headers=via)
    assert r.status_code == 303 and r.headers["location"].startswith("/join")
    assert tunnel.post("/workflows", json={"org_id": ALPHA, "unit_id": FBA_RETURNED}, headers=via).status_code == 401
    assert tunnel.get("/ui/phones.svg", headers=via).status_code == 303
    for _ in range(station.MAX_WRONG):
        tunnel.post("/join", data={"code": "000000"}, headers=via)
    assert station._WRONG["203.0.113.7"] == station.MAX_WRONG, "counted against the visitor, not 127.0.0.1"
    other = {"cf-connecting-ip": "198.51.100.9"}
    ok = tunnel.post("/join", data={"code": "482913"}, headers=other)
    assert ok.status_code == 303 and "pod12_code" in ok.headers.get("set-cookie", "")


def test_fifty_wrong_codes_from_anywhere_close_joining(setup, monkeypatch):
    monkeypatch.setenv("POD_LAN_CODE", "482913")
    c = TestClient(api.app, follow_redirects=False)
    for i in range(station.MAX_WRONG_TOTAL):  # spread over many faked addresses, under the per-visitor limit
        c.post("/join", data={"code": "000000"}, headers={"x-forwarded-for": f"10.0.{i // 200}.{i % 200}"})
    assert c.post("/join", data={"code": "482913"}, headers={"x-forwarded-for": "10.9.9.9"}).status_code == 429


# ---------------------------------------------------------------- turns
def test_each_person_clears_their_step_in_turn_and_recovery_runs_by_itself(laptop):
    first = snap(laptop, "prep")
    assert "bad=1" in first.headers["location"] and "Not%20your%20turn" in first.headers["location"]
    assert state() is None, "Prep cannot start a unit; nothing was created"
    r = snap(laptop, "receiving")
    assert r.status_code == 303 and "done=1" in r.headers["location"]
    assert states()["receiving"] == "completed" and states()["prep"] == "pending"
    assert state()["context"]["console_mode"] == "live"
    assert FBA_RETURNED in laptop.get("/ui/station/prep").text.split("Your turn")[1].split("Coming to you")[0]
    early = snap(laptop, "returns")
    assert "bad=1" in early.headers["location"] and "Prep" in early.headers["location"]
    assert states()["returns"] == "pending", "Returns did not run before Prep"
    assert "done=1" in snap(laptop, "prep").headers["location"]
    assert "done=1" in snap(laptop, "returns").headers["location"]
    assert states()["recovery"] == "pending", "Recovery is its own person's step: it waits for their press"
    assert FBA_RETURNED in laptop.get("/ui/station/recovery").text.split("Your turn")[1].split("Coming to you")[0]
    assert "done=1" in laptop.post(f"/ui/station/recovery/{ALPHA}/{FBA_RETURNED}").headers["location"]
    st = state()
    assert {s: v for s, v in states().items() if v != "skipped"} == {
        "receiving": "completed", "prep": "completed", "returns": "completed", "recovery": "completed"}
    assert st["final_outcome"] and st["status"] != "IN_PROGRESS"
    page = laptop.get(f"/ui/station/recovery/{ALPHA}/{FBA_RETURNED}").text
    assert "$2.00" in page and "claim" in page, "on the organisers' recorded evidence, our Recovery claims $2.00"
    page = laptop.get(f"/ui/station/returns/{ALPHA}/{FBA_RETURNED}").text
    assert "flow is finished" in page


def test_a_retake_replaces_the_photos_and_sends_later_steps_back(laptop, setup):
    snap(laptop, "receiving", n=2)
    snap(laptop, "prep")
    folder = setup / "input" / FBA_RETURNED / "receiving"
    assert len(list(folder.iterdir())) == 2
    r = snap(laptop, "receiving", n=1, name="retake.jpg")
    assert "done=1" in r.headers["location"]
    assert [p.name for p in folder.iterdir()] == ["01-retake.jpg"]
    assert states()["receiving"] == "completed" and states()["prep"] == "pending", "Prep must look again"


def test_a_replay_workflow_stays_on_the_laptop(laptop):
    laptop.post("/ui/sim/start", data={"unit": FBA_RETURNED, "org": ALPHA, "mode": "replay"})
    r = snap(laptop, "receiving")
    assert "bad=1" in r.headers["location"] and "replay" in r.headers["location"]
    assert states()["receiving"] == "pending"


def test_no_photo_and_iphone_heic_are_explained(laptop):
    r = laptop.post(f"/ui/station/receiving/{ALPHA}/{FBA_RETURNED}", files=[("files", ("", b"", "application/octet-stream"))])
    assert "Take+a+photo+first" in r.headers["location"] or "Take%20a%20photo%20first" in r.headers["location"]
    r = laptop.post(f"/ui/station/receiving/{ALPHA}/{FBA_RETURNED}", files=[("files", ("IMG_1.HEIC", b"x", "image/heic"))])
    assert "bad=1" in r.headers["location"] and "Most%20Compatible" in r.headers["location"]
    assert state() is None


def test_a_stage_that_is_not_this_units_is_refused(laptop):
    assert snap(laptop, "pack").status_code == 404, "UNIT-0014 is FBA: it has Prep, not Pack"
    assert laptop.get("/ui/station/recovery").status_code == 200, "Recovery has its own station too"
    assert laptop.get("/ui/station/billing").status_code == 404
    early = laptop.post(f"/ui/station/recovery/{ALPHA}/{FBA_RETURNED}")
    assert "bad=1" in early.headers["location"] and state() is None, "Recovery cannot run before the photo steps"


# ---------------------------------------------------------------- Returns' product photo
@pytest.fixture
def refdir(tmp_path, monkeypatch):
    import shutil

    import agents.returns.core.refs as refs
    import agents.returns.onboard as ob

    dst = tmp_path / "reference"
    shutil.copytree(refs.REFERENCE_DIR, dst)
    monkeypatch.setattr(refs, "REFERENCE_DIR", dst)
    monkeypatch.setattr(ob, "REFERENCE_DIR", dst)
    return dst


def test_a_phone_sized_product_photo_is_stored_small_and_hashed_as_stored(refdir):
    import hashlib

    import yaml

    from agents.returns.onboard import REF_MAX_SIDE, onboard

    out = onboard(ALPHA, "SKU-TOWEL-BLU", jpg(size=(4032, 3024)))
    assert out["added"]
    card = yaml.safe_load((refdir / "products" / ALPHA / "SKU-TOWEL-BLU.yaml").read_text(encoding="utf-8"))
    img = card["reference_images"][-1]
    stored = (refdir / "products" / ALPHA / img["path"]).read_bytes()
    assert max(Image.open(io.BytesIO(stored)).size) == REF_MAX_SIDE
    assert img["sha256"] == hashlib.sha256(stored).hexdigest()
    assert onboard(ALPHA, "SKU-TOWEL-BLU", jpg(size=(4032, 3024)))["added"] is False, "the same photo twice is one photo"


def test_the_returns_phone_asks_for_the_product_photo_first(laptop, refdir):
    for stage in ("receiving", "pack"):
        snap(laptop, stage, unit="UNIT-0016")
    page = laptop.get(f"/ui/station/returns/{ALPHA}/UNIT-0016").text
    assert "First, the product as sold" in page
    r = laptop.post(f"/ui/station/returns/{ALPHA}/UNIT-0016/reference", files=[("files", ("new.jpg", jpg(), "image/jpeg"))])
    assert "bad=1" not in r.headers["location"]
    assert "First, the product as sold" not in laptop.get(f"/ui/station/returns/{ALPHA}/UNIT-0016").text
