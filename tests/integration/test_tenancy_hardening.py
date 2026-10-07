"""Tenancy and bad-data hardening for the agents. Each test pins a defect found by review and reproduced first:

  * a capture ref such as UNIT-1/pack/../../UNIT-2/pack/x.jpg started with the right words and resolved inside the
    capture root, so all four photo-reading agents accepted another unit's photo;
  * four agents used the latest earlier record without checking whose it was, so another organisation's evidence could
    enter a decision (only Recovery refused it);
  * the workflow id WF-<org>-<unit> is not unique, so one organisation could be handed another's stored workflow;
  * a bad order (quantity 0, no lines, "A:1,B:2") or an unreadable work order crashed the agent instead of leaving a
    pending record.
"""
from __future__ import annotations

import io

import numpy as np
import pytest
from fastapi.testclient import TestClient
from PIL import Image

import orchestration.api as api
from agents.pack import app as pack
from agents.pack import captures as pack_captures
from agents.pack import ledger
from agents.pack.core.config import Settings
from agents.prep import app as prep
from agents.receiving import app as receiving
from agents.returns import app as returns
from agents.prep import captures as prep_captures
from agents.receiving import captures as receiving_captures
from agents.returns import captures as returns_captures
from orchestration.orchestrator import WorkflowConflict, run_workflow
from orchestration.store import FileStore, MemoryStore
from shared.utils.captures import CapturePathError, resolve_capture
from shared.utils.records import build_record
from shared.utils.schema import errors
from shared.utils.stubs import previous

ALPHA, BRAVO = "org_demo_alpha", "org_demo_bravo"


def jpeg(seed=1) -> bytes:
    arr = np.random.default_rng(seed).integers(70, 190, size=(480, 640, 3), dtype=np.uint8)
    buf = io.BytesIO()
    Image.fromarray(arr).save(buf, "JPEG", quality=90)
    return buf.getvalue()


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("INPUT_DIR", str(tmp_path / "input"))
    monkeypatch.setenv("PACK_LEDGER_PATH", str(tmp_path / "ledger.json"))
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    st = Settings(_env_file=None, gemini_api_key=None, catalogue_dir=str(pack.HERE / "catalogue"), cache_dir=str(tmp_path / "cache"))
    monkeypatch.setattr(pack, "settings", lambda: st)
    ledger.clear()


def place(tmp_path, unit, stage):
    d = tmp_path / "input" / unit / stage
    d.mkdir(parents=True, exist_ok=True)
    (d / "a.jpg").write_bytes(jpeg())


# ---------------------------------------------------------------- capture paths
TRICKS = [
    "{u}/{s}/../../UNIT-OTHER/{s}/a.jpg",   # right words, ends up in another unit's folder
    "{u}/{s}/../a.jpg",                     # up one level: the unit's folder, not its stage folder
    "{u}/{s}/./a.jpg", "{u}/{s}//a.jpg",
    "/etc/{s}/a.jpg", "C:/{u}/{s}/a.jpg", "{u}\\{s}\\a.jpg", "UNIT-OTHER/{s}/a.jpg", "{u}/other/a.jpg", "", "{u}",
]


@pytest.mark.parametrize("trick", TRICKS)
def test_the_shared_rule_refuses_every_path_trick(tmp_path, trick):
    place(tmp_path, "UNIT-1", "pack")
    place(tmp_path, "UNIT-OTHER", "pack")
    with pytest.raises(CapturePathError):
        resolve_capture(tmp_path / "input", trick.format(u="UNIT-1", s="pack"), "UNIT-1", "pack")


def test_the_shared_rule_accepts_the_honest_ref(tmp_path):
    place(tmp_path, "UNIT-1", "pack")
    assert resolve_capture(tmp_path / "input", "UNIT-1/pack/a.jpg", "UNIT-1", "pack").is_file()


LOADERS = [("pack", pack_captures), ("prep", prep_captures), ("receiving", receiving_captures), ("returns", returns_captures)]


@pytest.mark.parametrize("stage,module", LOADERS)
def test_every_photo_reading_agent_refuses_another_units_photo(tmp_path, stage, module):
    place(tmp_path, "UNIT-OTHER", stage)
    place(tmp_path, "UNIT-1", stage)
    honest = [{"ref": f"UNIT-1/{stage}/a.jpg", "kind": "image"}]
    assert len(module.load(honest, "UNIT-1")) == 1
    sneaky = [{"ref": f"UNIT-1/{stage}/../../UNIT-OTHER/{stage}/a.jpg", "kind": "image"}]
    with pytest.raises(module.CaptureError):
        module.load(sneaky, "UNIT-1")


# ---------------------------------------------------------------- earlier evidence belongs to the same subject
def _record(org, unit, stage="receiving"):
    req = {"workflow_id": f"WF-{org}-{unit}", "request_id": f"WF-{org}-{unit}:{stage}", "stage": stage,
           "subject": {"org_id": org, "subject_id": unit}, "previous_evidence": []}
    return build_record(req, agent_id="t@0", record_id=f"RCV-{org}-{unit}", captured_at="2026-01-01T00:00:00Z", checks=[],
                        outcome="accept", reason="t", model={"name": "rules", "version": "0"}, verdict="PASS")


def _request(org, unit, prior):
    return {"stage": "pack", "subject": {"org_id": org, "subject_id": unit, "route": "mfn"}, "previous_evidence": prior}


def test_previous_evidence_of_another_organisation_is_refused():
    with pytest.raises(LookupError):
        previous(_request(ALPHA, "UNIT-0008", [_record(BRAVO, "UNIT-0008")]), "receiving")


def test_previous_evidence_of_another_unit_is_refused():
    with pytest.raises(LookupError):
        previous(_request(ALPHA, "UNIT-0008", [_record(ALPHA, "UNIT-0016")]), "receiving")


def test_previous_evidence_of_the_same_subject_is_used():
    rec = _record(ALPHA, "UNIT-0008")
    assert previous(_request(ALPHA, "UNIT-0008", [rec]), "receiving")["record_id"] == rec["record_id"]


@pytest.mark.parametrize("stage,module,route,unit", [("pack", pack, "mfn", "UNIT-0008"), ("prep", prep, "fba", "UNIT-0014"),
                                                     ("receiving", receiving, "fba", "UNIT-0014"), ("returns", returns, "fba", "UNIT-0014")])
def test_every_agent_refuses_a_request_carrying_another_organisations_evidence(stage, module, route, unit):
    """With no photo at all, so the refusal cannot depend on which input an agent happens to look at first."""
    req = {"schema_version": "1.0", "request_id": f"WF-x:{stage}", "workflow_id": "WF-x", "stage": stage,
           "subject": {"org_id": ALPHA, "subject_id": unit, "route": route}, "inputs": [],
           "previous_evidence": [_record(BRAVO, "UNIT-0003")], "context": {"overrides": [], "case": {}}}
    with pytest.raises(LookupError):
        module.handle(req)
    assert TestClient(module.app).post("/run", json=req).status_code == 404


# ---------------------------------------------------------------- workflow ids are not unique
def test_a_colliding_workflow_id_does_not_hand_over_another_organisations_workflow():
    store = MemoryStore()
    mine = run_workflow({"org_id": "org_x", "unit_id": "UNIT-7-1", "route": "unknown", "returned": False}, store=store)
    clash = {"org_id": "org_x-UNIT", "unit_id": "7-1", "route": "unknown", "returned": False}
    from orchestration.orchestrator import workflow_id_for

    assert workflow_id_for(clash) == mine["workflow_id"], "this is the collision the review described"
    with pytest.raises(WorkflowConflict):
        run_workflow(clash, store=store)


def test_the_api_answers_409_for_a_colliding_workflow(tmp_path, monkeypatch):
    monkeypatch.setattr(api, "STORE", FileStore(tmp_path / "out"))
    c = TestClient(api.app, raise_server_exceptions=False)
    assert c.post("/workflows", json={"org_id": "org_x", "unit_id": "UNIT-7-1"}).status_code == 200
    assert c.post("/workflows", json={"org_id": "org_x-UNIT", "unit_id": "7-1"}).status_code == 409


# ---------------------------------------------------------------- Pack: org ids are folder names; bad orders are pending
@pytest.mark.parametrize("org", ["..\\..\\x", "../x", "a/b", "C:x", ".."])
def test_pack_refuses_an_organisation_id_that_is_not_a_plain_name(org):
    req = {"stage": "pack", "subject": {"org_id": org, "subject_id": "UNIT-0008", "route": "mfn"}, "inputs": [],
           "previous_evidence": [], "context": {"order": {"org_id": org, "order_id": "O", "lines": "SKU-BOTTLE-750:1"}}}
    with pytest.raises(LookupError):
        pack.resolve_order(req)


BAD_ORDERS = [
    {"order_id": "O-1", "lines": "SKU-BOTTLE-750:0"},                      # quantity 0
    {"order_id": "O-2", "lines": ""},                                       # no lines
    {"order_id": "O-3", "lines": []},
    {"order_id": "O-4", "lines": "SKU-BOTTLE-750:1,SKU-MUG-11:2"},          # commas instead of semicolons
]


@pytest.mark.parametrize("order", BAD_ORDERS)
def test_pack_turns_a_bad_order_into_a_pending_record(tmp_path, order):
    place(tmp_path, "UNIT-0008", "pack")
    req = {"schema_version": "1.0", "request_id": "WF-x:pack", "workflow_id": "WF-x", "stage": "pack",
           "subject": {"org_id": ALPHA, "subject_id": "UNIT-0008", "route": "mfn"}, "inputs": [],
           "previous_evidence": [], "context": {"overrides": [], "case": {"order": {"org_id": ALPHA, **order}}}}
    out = pack.handle(req)
    assert errors("agent-output", out) == []
    ev = out["evidence"]
    assert ev["error"]["code"] == "order_invalid" and ev["error"]["retryable"] is False and ev["decision"]["outcome"] == "pending_review"
    assert ev["checks"] == [], "nothing was checked, so no check is invented"


@pytest.mark.parametrize("bad", [{"prep_price_usd": "abc"}, {"requirements": {"polybag": "maybe"}}])
def test_prep_turns_an_unreadable_work_order_into_a_pending_record(bad):
    order = {"org_id": ALPHA, "unit_id": "UNIT-0014", "work_order_id": "WO-1", "sku": "SKU-LAMP-LED", "fnsku": "X00DUMMY014",
             "requirements": {"polybag": False}, **bad}
    req = {"schema_version": "1.0", "request_id": "WF-x:prep", "workflow_id": "WF-x", "stage": "prep",
           "subject": {"org_id": ALPHA, "subject_id": "UNIT-0014", "route": "fba"}, "inputs": [], "previous_evidence": [],
           "context": {"overrides": [], "case": {"order": order}}}
    out = prep.handle(req)
    assert errors("agent-output", out) == []
    assert out["evidence"]["error"]["code"] == "order_invalid" and out["evidence"]["decision"]["outcome"] == "pending_review"
