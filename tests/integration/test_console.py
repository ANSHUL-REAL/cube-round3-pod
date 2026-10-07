"""The readable console (orchestration/web): pages, photo upload, overrides, and what it must refuse.

The console decides nothing; it shows what the orchestrator and store hold. These tests pin the parts that touch the disk or
change state: a photo can only land in (and be read from, or deleted from) its own unit and stage folder; a unit belongs to
one organisation; an override goes through the orchestrator's checks; everything printed is escaped; a form posted from
another web page is refused. The agents run on the organiser stubs (POD_UI_STUBS=1), so no photo or model key is needed.
"""
from __future__ import annotations

import io
import json

import pytest
from fastapi.testclient import TestClient
from PIL import Image

import orchestration.api as api
from orchestration.store import FileStore

ALPHA = "org_demo_alpha"
BRAVO = "org_demo_bravo"
FBA_RETURNED = "UNIT-0014"  # alpha, fba, returned: receiving, prep, returns, recovery
MFN_CLEAN = "UNIT-0008"  # alpha, merchant-fulfilled: receiving, pack
BRAVO_UNIT = "UNIT-0003"  # belongs to bravo, not alpha
WF = f"WF-{ALPHA}-{FBA_RETURNED}"


def png(color=(200, 30, 90)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), color).save(buf, "PNG")
    return buf.getvalue()


@pytest.fixture
def ui(tmp_path, monkeypatch):
    monkeypatch.setattr(api, "STORE", FileStore(tmp_path / "out"))
    monkeypatch.setenv("INPUT_DIR", str(tmp_path / "input"))
    monkeypatch.setenv("POD_UI_STUBS", "1")
    c = TestClient(api.app, raise_server_exceptions=False, follow_redirects=False)
    c.input_root = tmp_path / "input"
    return c


def run(ui, org=ALPHA, unit=FBA_RETURNED):
    return ui.post("/ui/run", data={"org": org, "unit": unit})


def upload(ui, stage, files, org=ALPHA, unit=MFN_CLEAN):
    return ui.post(f"/ui/capture/{org}/{unit}/{stage}", files=[("files", f) for f in files])


# ---------------------------------------------------------------- pages
def test_home_lists_the_five_demo_units_and_every_unit(ui):
    r = ui.get("/")
    assert r.status_code == 200
    for unit in ("UNIT-0014", "UNIT-0008", "UNIT-0044", "UNIT-0016", "UNIT-0023"):
        assert unit in r.text
    assert "100" in r.text  # all units are listed, not only the demo ones


def test_stub_mode_is_announced_on_the_page_and_absent_otherwise(ui, monkeypatch):
    assert "Organiser stubs are answering" in ui.get("/").text
    monkeypatch.delenv("POD_UI_STUBS")
    assert "Organiser stubs are answering" not in ui.get("/").text


def test_running_a_unit_shows_one_card_per_stage_and_the_outcome(ui):
    r = run(ui)
    assert r.status_code == 303 and r.headers["location"] == f"/ui/w/{WF}"
    page = ui.get(f"/ui/w/{WF}")
    assert page.status_code == 200
    for stage in ("Receiving", "Prep", "Returns", "Recovery"):
        assert stage in page.text
    states = {s["stage"]: s["state"] for s in api.STORE.load_workflow(WF)["stage_results"]}
    assert states["pack"] == "skipped" and states["recovery"] == "completed"  # an fba unit skips Pack; the page shows the rest
    assert "Organiser stubs are answering" in page.text


def test_a_record_page_shows_the_checks_and_the_hash(ui):
    run(ui)
    wf = api.STORE.load_workflow(WF)
    rec_id = wf["evidence_references"][-1]
    page = ui.get(f"/ui/w/{WF}/r/{rec_id}")
    assert page.status_code == 200
    rec = api.STORE.get_evidence(rec_id)
    assert rec["content_hash"] in page.text
    for check in rec["checks"]:
        assert check["check_key"] in page.text


def test_a_record_that_is_not_in_the_workflow_is_a_404(ui):
    run(ui)
    assert ui.get(f"/ui/w/{WF}/r/NOT-A-RECORD").status_code == 404


def test_running_twice_resumes_instead_of_starting_over(ui):
    run(ui)
    first = api.STORE.load_workflow(WF)
    run(ui)
    again = api.STORE.load_workflow(WF)
    assert again["workflow_id"] == first["workflow_id"]
    assert again["evidence_references"][: len(first["evidence_references"])] == first["evidence_references"]


# ---------------------------------------------------------------- tenancy and ids
def test_a_unit_of_another_organisation_is_not_found(ui):
    assert run(ui, org=ALPHA, unit=BRAVO_UNIT).status_code == 404
    assert ui.get(f"/ui/capture/{ALPHA}/{BRAVO_UNIT}").status_code == 404
    assert upload(ui, "receiving", [("a.png", png(), "image/png")], unit=BRAVO_UNIT).status_code == 404


def test_an_unknown_unit_or_org_is_not_found(ui):
    assert run(ui, unit="UNIT-9999").status_code == 404
    assert run(ui, org="org_nobody").status_code == 404


@pytest.mark.parametrize("bad", ["..%5C..%5Csecret", "..%2F..%2Fsecret", "..%5Csecret", "a%0d%0aSet-Cookie:x"])
def test_odd_workflow_ids_are_404_and_never_leak_a_file(ui, tmp_path, bad):
    (tmp_path / "secret.json").write_text(json.dumps({"hello": "outside the store"}))
    for path in (f"/ui/w/{bad}", f"/ui/w/{bad}/r/x"):
        r = ui.get(path)
        assert r.status_code == 404 and "outside the store" not in r.text, path
    for path in (f"/ui/w/{bad}/override", f"/ui/w/{bad}/resume"):
        r = ui.post(path, data={"record_id": "x", "new_verdict": "PASS", "actor": "a", "reason": "r"})
        assert r.status_code == 404 and "Set-Cookie" not in r.headers, path


# ---------------------------------------------------------------- photos
def test_a_photo_is_saved_under_its_own_unit_and_stage_with_a_generated_name(ui):
    r = upload(ui, "pack", [("../../evil name!.PNG", png(), "image/png")])
    assert r.status_code == 303 and "bad=1" not in r.headers["location"]
    saved = list((ui.input_root / MFN_CLEAN / "pack").iterdir())
    assert len(saved) == 1 and saved[0].name.startswith("01-") and saved[0].suffix == ".png"
    assert "/" not in saved[0].name and ".." not in saved[0].name
    assert [p for p in ui.input_root.rglob("*") if p.is_file()] == saved  # nothing landed anywhere else


def test_only_real_images_are_accepted(ui):
    assert "bad=1" in upload(ui, "pack", [("x.png", b"this is not an image", "image/png")]).headers["location"]
    assert "bad=1" in upload(ui, "pack", [("x.gif", png(), "image/gif")]).headers["location"]
    assert "bad=1" in upload(ui, "pack", [("x.exe", png(), "application/octet-stream")]).headers["location"]
    assert not ui.input_root.exists() or not list(ui.input_root.rglob("*.*"))


def test_a_stage_takes_a_limited_number_of_photos(ui):
    for i in range(3):
        assert "bad=1" not in upload(ui, "pack", [(f"{i}.png", png((i, i, i)), "image/png")]).headers["location"]
    r = upload(ui, "pack", [("4.png", png(), "image/png")])
    assert "bad=1" in r.headers["location"]
    assert len(list((ui.input_root / MFN_CLEAN / "pack").iterdir())) == 3


def test_a_photo_cannot_be_added_to_a_stage_the_unit_does_not_have(ui):
    assert upload(ui, "prep", [("a.png", png(), "image/png")]).status_code == 404  # an mfn unit has no prep stage
    assert upload(ui, "recovery", [("a.png", png(), "image/png")]).status_code == 404  # recovery reads evidence, it has no photos
    assert upload(ui, "nonsense", [("a.png", png(), "image/png")]).status_code == 404


def test_a_saved_photo_is_served_back_and_only_that_one(ui):
    upload(ui, "pack", [("a.png", png(), "image/png")])
    name = next((ui.input_root / MFN_CLEAN / "pack").iterdir()).name
    ok = ui.get(f"/ui/photo/{MFN_CLEAN}/pack/{name}")
    assert ok.status_code == 200 and ok.headers["content-type"] == "image/png"
    (ui.input_root / MFN_CLEAN / "pack" / "notes.txt").write_text("private")
    assert ui.get(f"/ui/photo/{MFN_CLEAN}/pack/notes.txt").status_code == 404  # not a photo type
    for path in (f"/ui/photo/{MFN_CLEAN}/pack/..%2F..%2F..%2Fpod.json", f"/ui/photo/{MFN_CLEAN}/pack/..%5C..%5Cpod.json",
                 f"/ui/photo/..%2F../pack/{name}", f"/ui/photo/UNIT-0001/pack/{name}"):
        assert ui.get(path).status_code == 404, path


def test_deleting_a_photo_cannot_reach_outside_its_folder(ui, tmp_path):
    (tmp_path / "keep.png").write_bytes(png())
    (tmp_path / "pod.json").write_text("{}")
    upload(ui, "pack", [("a.png", png(), "image/png")])
    url = f"/ui/capture/{ALPHA}/{MFN_CLEAN}/pack/delete"
    for name in ("../../../keep.png", "..\\..\\..\\keep.png", "../../../pod.json", "/etc/passwd", "C:/Windows/win.ini"):
        r = ui.post(url, data={"name": name})
        assert r.status_code in (303, 400, 404), name
    assert (tmp_path / "keep.png").exists() and (tmp_path / "pod.json").exists()
    assert len(list((ui.input_root / MFN_CLEAN / "pack").iterdir())) == 1
    name = next((ui.input_root / MFN_CLEAN / "pack").iterdir()).name
    assert ui.post(url, data={"name": name}).status_code == 303
    assert list((ui.input_root / MFN_CLEAN / "pack").iterdir()) == []


def test_delete_only_removes_photos(ui):
    upload(ui, "pack", [("a.png", png(), "image/png")])
    note = ui.input_root / MFN_CLEAN / "pack" / "notes.txt"
    note.write_text("keep me")
    ui.post(f"/ui/capture/{ALPHA}/{MFN_CLEAN}/pack/delete", data={"name": "notes.txt"})
    assert note.exists()


# ---------------------------------------------------------------- overrides
def test_an_override_through_the_console_is_recorded_and_the_record_is_untouched(ui):
    run(ui)
    wf = api.STORE.load_workflow(WF)
    rec_id = wf["evidence_references"][0]
    before = api.STORE.get_evidence(rec_id)
    r = ui.post(f"/ui/w/{WF}/override", data={"record_id": rec_id, "new_verdict": "PASS", "actor": "Asha", "reason": "checked by hand"})
    assert r.status_code == 303 and "bad=1" not in r.headers["location"]
    after = api.STORE.load_workflow(WF)
    assert after["overrides"][-1]["actor"] == "Asha" and after["overrides"][-1]["new_verdict"] == "PASS"
    assert api.STORE.get_evidence(rec_id) == before  # evidence is immutable; the override sits beside it


def test_an_override_re_runs_the_stages_that_used_the_old_verdict_and_says_so(ui):
    from orchestration.orchestrator import stale_stages

    run(ui)
    wf = api.STORE.load_workflow(WF)
    first = {s["stage"]: s["record_id"] for s in wf["stage_results"] if s.get("record_id")}
    r = ui.post(f"/ui/w/{WF}/override", data={"record_id": first["receiving"], "new_verdict": "FAIL", "actor": "Asha", "reason": "crushed"})
    assert "Ran" in r.headers["location"] and "again" in r.headers["location"]
    wf = api.STORE.load_workflow(WF)
    assert stale_stages(wf) == [], "nothing is left standing on the overridden verdict"
    runs = {s["stage"]: s["runs"] for s in wf["stage_results"]}
    assert runs["receiving"] == 1 and runs["recovery"] == 2
    assert first["recovery"] in wf["evidence_references"], "the earlier Recovery record is kept, not rewritten"


def test_an_override_nobody_depended_on_runs_nothing_again(ui):
    run(ui)
    wf = api.STORE.load_workflow(WF)
    last = next(s["record_id"] for s in reversed(wf["stage_results"]) if s.get("record_id"))  # Recovery: nothing reads it
    r = ui.post(f"/ui/w/{WF}/override", data={"record_id": last, "new_verdict": "PASS", "actor": "Asha", "reason": "fine"})
    assert "Ran" not in r.headers["location"]
    assert all(s["runs"] == 1 for s in api.STORE.load_workflow(WF)["stage_results"] if s["state"] != "skipped")


@pytest.mark.parametrize("verdict", ["banana", "", "pass ", "fail"])
def test_an_override_with_a_bad_verdict_is_refused_and_stores_nothing(ui, verdict):
    run(ui)
    rec_id = api.STORE.load_workflow(WF)["evidence_references"][0]
    r = ui.post(f"/ui/w/{WF}/override", data={"record_id": rec_id, "new_verdict": verdict, "actor": "Asha", "reason": "x"})
    refused = (r.status_code == 303 and "bad=1" in r.headers["location"]) or r.status_code == 422  # 422: blank required field
    assert refused, (r.status_code, r.headers.get("location"))
    assert api.STORE.load_workflow(WF)["overrides"] == []


def test_an_override_of_a_record_outside_the_workflow_is_refused(ui):
    run(ui)
    r = ui.post(f"/ui/w/{WF}/override", data={"record_id": "NOT-IN-WORKFLOW", "new_verdict": "PASS", "actor": "Asha", "reason": "x"})
    assert "bad=1" in r.headers["location"] and api.STORE.load_workflow(WF)["overrides"] == []


def test_text_a_person_types_is_escaped_on_every_page(ui):
    run(ui)
    rec_id = api.STORE.load_workflow(WF)["evidence_references"][0]
    evil = "<script>alert(1)</script>"
    ui.post(f"/ui/w/{WF}/override", data={"record_id": rec_id, "new_verdict": "PASS", "actor": evil, "reason": evil})
    for path in (f"/ui/w/{WF}", f"/ui/w/{WF}/r/{rec_id}"):
        text = ui.get(path).text
        assert evil not in text and "&lt;script&gt;" in text, path
    r = ui.get("/", params={"msg": evil})
    assert evil not in r.text


# ---------------------------------------------------------------- forms from other sites
def test_a_form_posted_from_another_website_is_refused(ui):
    r = ui.post("/ui/run", data={"org": ALPHA, "unit": FBA_RETURNED}, headers={"Origin": "https://evil.example"})
    assert r.status_code == 403 and api.STORE.load_workflow(WF) is None
    r = ui.post("/ui/run", data={"org": ALPHA, "unit": FBA_RETURNED}, headers={"Origin": "null"})
    assert r.status_code == 403 and api.STORE.load_workflow(WF) is None


def test_a_form_from_the_console_itself_is_accepted(ui):
    r = ui.post("/ui/run", data={"org": ALPHA, "unit": FBA_RETURNED}, headers={"Origin": "http://testserver", "Host": "testserver"})
    assert r.status_code == 303 and api.STORE.load_workflow(WF) is not None


def test_a_delete_and_an_upload_from_another_website_are_refused_too(ui):
    upload(ui, "pack", [("a.png", png(), "image/png")])
    name = next((ui.input_root / MFN_CLEAN / "pack").iterdir()).name
    evil = {"Origin": "https://evil.example"}
    assert ui.post(f"/ui/capture/{ALPHA}/{MFN_CLEAN}/pack/delete", data={"name": name}, headers=evil).status_code == 403
    r = ui.post(f"/ui/capture/{ALPHA}/{MFN_CLEAN}/pack", files=[("files", ("b.png", png(), "image/png"))], headers=evil)
    assert r.status_code == 403
    assert len(list((ui.input_root / MFN_CLEAN / "pack").iterdir())) == 1
