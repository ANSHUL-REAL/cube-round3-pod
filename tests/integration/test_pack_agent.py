"""Pack Manager (agents/pack): behaviour tests on our own fixtures.

The vision model is replaced by scripted perceivers (core/vision/oracle.py and the classes below). That makes
these tests deterministic and keyless, and they test everything *except* what the real model sees:
the capture handling, the order lookup, the deterministic rules, the Round 3 record mapping, fail-open,
tenancy, idempotency and photo reuse. Real-model accuracy is measured in the Round 2 evaluation
(see agents/pack/PROVENANCE.md and agents/pack/README.md), not here.
"""
from __future__ import annotations

import io
import json

import numpy as np
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from agents.pack import app as pack
from agents.pack import ledger
from agents.pack.core.config import Settings
from agents.pack.core.models import Perception, Scene, SkuCount
from agents.pack.core.vision.base import PerceptionError
from agents.pack.core.vision.oracle import OraclePerceiver
from orchestration.orchestrator import discover_inputs
from shared.utils.hashing import verify
from shared.utils.records import build_record
from shared.utils.schema import errors

ALPHA, BRAVO = "org_demo_alpha", "org_demo_bravo"


# ---------------------------------------------------------------- fixtures
def jpeg(seed: int) -> bytes:
    """A sharp, evenly lit photo-like image that passes the quality gate. Different seed, different bytes."""
    rng = np.random.default_rng(seed)
    arr = rng.integers(70, 190, size=(480, 640, 3), dtype=np.uint8)
    buf = io.BytesIO()
    Image.fromarray(arr).save(buf, "JPEG", quality=90)
    return buf.getvalue()


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    """Own capture folder, own settings (no key, no .env), empty reuse ledger, scripted model by default."""
    monkeypatch.setenv("INPUT_DIR", str(tmp_path / "input"))
    monkeypatch.setenv("PACK_LEDGER_PATH", str(tmp_path / "ledger.json"))
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    st = Settings(_env_file=None, gemini_api_key=None, catalogue_dir=str(pack.HERE / "catalogue"),
                  cache_dir=str(tmp_path / "cache"))
    monkeypatch.setattr(pack, "settings", lambda: st)
    ledger.clear()
    yield
    ledger.clear()


def place(tmp_path, unit: str, *seeds: int) -> list[dict]:
    folder = tmp_path / "input" / unit / "pack"
    folder.mkdir(parents=True, exist_ok=True)
    for i, seed in enumerate(seeds, 1):
        (folder / f"box{i}.jpg").write_bytes(jpeg(seed))
    return discover_inputs(unit, "pack")


def request(unit: str, org: str, inputs: list[dict], *, previous=None, overrides=None, rid: str | None = None):
    wf = f"WF-{org}-{unit}"
    return {"schema_version": "1.0", "request_id": rid or f"{wf}:pack", "workflow_id": wf, "stage": "pack",
            "subject": {"org_id": org, "subject_id": unit, "route": "mfn"}, "inputs": inputs,
            "previous_evidence": previous or [], "context": {"overrides": overrides or [], "case": {}}}


def sees(monkeypatch, observed: dict, unknown: list[str] | None = None):
    monkeypatch.setattr(pack, "get_perceiver", lambda st: OraclePerceiver(observed, unknown))


def run(tmp_path, monkeypatch, unit, org, observed, seed=1, unknown=None):
    sees(monkeypatch, observed, unknown)
    out = pack.handle(request(unit, org, place(tmp_path, unit, seed)))
    assert errors("agent-output", out) == []
    assert verify(out["evidence"])
    return out


def checks(out) -> dict:
    return {c["check_key"]: c for c in out["evidence"]["checks"]}


# ---------------------------------------------------------------- the three answers
def test_exact_box_is_sealed(tmp_path, monkeypatch):
    out = run(tmp_path, monkeypatch, "UNIT-0023", ALPHA, {"SKU-PUZZLE-500": 1, "SKU-TOWEL-BLU": 1})
    ev = out["evidence"]
    assert ev["decision"]["outcome"] == "seal" and ev["decision"]["verdict"] == "PASS" and out["status"] == "completed"
    assert {c["check_key"] for c in ev["checks"]} >= {"items_present", "quantities_correct", "no_extra_items"}
    assert all(c["verdict"] == "PASS" for c in ev["checks"])
    assert ev["payload"]["order_lines"] == {"SKU-PUZZLE-500": 1, "SKU-TOWEL-BLU": 1}
    assert ev["payload"]["observed_in_box"] == {"SKU-PUZZLE-500": 1, "SKU-TOWEL-BLU": 1}


def test_wrong_item_stops_the_box_and_says_what_to_swap(tmp_path, monkeypatch):
    """UNIT-0044: a candle was ordered, a bottle is in the box (the operator sealed it anyway in the sample)."""
    out = run(tmp_path, monkeypatch, "UNIT-0044", ALPHA, {"SKU-BOTTLE-750": 1})
    ev, c = out["evidence"], checks(out)
    assert ev["decision"]["outcome"] == "stop_and_fix" and ev["decision"]["verdict"] == "FAIL"
    assert c["items_present"]["verdict"] == "FAIL" and c["no_extra_items"]["verdict"] == "FAIL"
    assert any("Replace" in f for f in ev["payload"]["fix_instructions"])
    assert ev["payload"]["operator_verdict"] == "seal" and ev["payload"]["agent_agrees_with_operator"] is False
    # the per-line detail from the Round 2 engine is kept whole, not lost in the roll-up
    assert any(l["check_key"].startswith("line_present:SKU-CANDLE-3") for l in ev["payload"]["line_checks"])


def test_extra_item_stops_the_box(tmp_path, monkeypatch):
    """UNIT-0034: an extra cable is in the box."""
    out = run(tmp_path, monkeypatch, "UNIT-0034", ALPHA,
              {"SKU-CANDLE-3": 2, "SKU-BOTTLE-750": 1, "SKU-CABLE-USBC": 1})
    c = checks(out)
    assert out["evidence"]["decision"]["outcome"] == "stop_and_fix"
    assert c["no_extra_items"]["verdict"] == "FAIL" and "SKU-CABLE-USBC" in c["no_extra_items"]["observed"]
    assert c["items_present"]["verdict"] == "PASS" and c["quantities_correct"]["verdict"] == "PASS"


def test_short_quantity_stops_the_box(tmp_path, monkeypatch):
    out = run(tmp_path, monkeypatch, "UNIT-0016", ALPHA, {"SKU-TOWEL-BLU": 1})  # 2 ordered
    c = checks(out)
    assert out["evidence"]["decision"]["outcome"] == "stop_and_fix"
    assert c["quantities_correct"]["verdict"] == "FAIL" and c["items_present"]["verdict"] == "PASS"
    assert c["quantities_correct"]["expected"] == {"SKU-TOWEL-BLU": 2}


class HiddenStack(OraclePerceiver):
    """Sees the right items but says part of the box is hidden: the photo cannot settle it."""

    def perceive(self, photos, candidates, allowed_inserts, catalogue_root):
        p = super().perceive(photos, candidates, allowed_inserts, catalogue_root)
        return p.model_copy(update={"scene": Scene(box_interior_fully_visible=True, items_may_be_hidden=True,
                                                   visibility_confidence=0.9)})


def test_photo_that_cannot_settle_it_is_uncertain_with_a_reason(tmp_path, monkeypatch):
    monkeypatch.setattr(pack, "get_perceiver", lambda st: HiddenStack({"SKU-TOWEL-BLU": 2}))
    out = pack.handle(request("UNIT-0016", ALPHA, place(tmp_path, "UNIT-0016", 7)))
    ev = out["evidence"]
    assert errors("agent-output", out) == [] and verify(ev)
    assert ev["decision"]["verdict"] == "UNCERTAIN" and ev["decision"]["outcome"] == "pending_review"
    assert ev["decision"]["needs_human"] is True
    unclear = [c for c in ev["checks"] if c["verdict"] == "UNCERTAIN"]
    assert unclear and all(c["uncertain_reason"] for c in unclear)
    assert checks(out)["scene_coverage"]["uncertain_reason"] == "occluded"
    assert ev["payload"]["uncertainty"]["next_action"]


# ---------------------------------------------------------------- fail open
class Down:
    def perceive(self, *a, **k):
        raise PerceptionError("model timed out")


def test_model_failure_is_a_pending_record_that_keeps_the_photo(tmp_path, monkeypatch):
    monkeypatch.setattr(pack, "get_perceiver", lambda st: Down())
    inputs = place(tmp_path, "UNIT-0008", 3)
    out = pack.handle(request("UNIT-0008", ALPHA, inputs))
    ev = out["evidence"]
    assert errors("agent-output", out) == [] and verify(ev)
    assert out["status"] == "pending" and ev["error"]["code"] == "model_unavailable" and ev["error"]["retryable"]
    assert ev["decision"]["verdict"] == "UNCERTAIN" and ev["decision"]["outcome"] == "pending_review"
    assert [i["ref"] for i in ev["inputs"]] == [i["ref"] for i in inputs], "the capture must not be lost"
    assert out["next_step_recommendation"]["action"] == "retry"


def test_no_api_key_is_pending_not_a_crash(tmp_path, monkeypatch):
    # the real get_perceiver, with no key configured
    out = pack.handle(request("UNIT-0008", ALPHA, place(tmp_path, "UNIT-0008", 4)))
    assert out["evidence"]["error"]["code"] == "model_not_configured" and out["status"] == "pending"
    assert errors("agent-output", out) == []


def test_no_photo_is_pending_not_an_invented_verdict():
    out = pack.handle(request("UNIT-0008", ALPHA, []))
    ev = out["evidence"]
    assert errors("agent-output", out) == [] and ev["error"]["code"] == "no_capture"
    assert ev["decision"]["outcome"] == "pending_review" and ev["checks"] == []


def test_tampered_photo_is_refused_and_recorded(tmp_path, monkeypatch):
    sees(monkeypatch, {"SKU-BOTTLE-750": 1})
    inputs = place(tmp_path, "UNIT-0008", 5)
    (tmp_path / "input" / "UNIT-0008" / "pack" / "box1.jpg").write_bytes(jpeg(99))  # changed after hashing
    out = pack.handle(request("UNIT-0008", ALPHA, inputs))
    assert out["evidence"]["error"]["code"] == "capture_unreadable" and out["evidence"]["error"]["retryable"] is False
    assert "sha256" in out["evidence"]["error"]["message"]


def test_capture_from_another_subject_or_outside_the_root_is_refused(tmp_path, monkeypatch):
    sees(monkeypatch, {"SKU-BOTTLE-750": 1})
    place(tmp_path, "UNIT-0016", 6)
    other = [{"ref": "UNIT-0016/pack/box1.jpg", "kind": "image", "sha256": None}]
    assert pack.handle(request("UNIT-0008", ALPHA, other))["evidence"]["error"]["code"] == "capture_unreadable"
    escape = [{"ref": "../../etc/hosts", "kind": "image", "sha256": None}]
    assert pack.handle(request("UNIT-0008", ALPHA, escape))["evidence"]["error"]["code"] == "capture_unreadable"


def test_not_a_photo_is_pending(tmp_path, monkeypatch):
    sees(monkeypatch, {"SKU-BOTTLE-750": 1})
    folder = tmp_path / "input" / "UNIT-0008" / "pack"
    folder.mkdir(parents=True)
    (folder / "box1.jpg").write_bytes(b"not really a jpeg")
    out = pack.handle(request("UNIT-0008", ALPHA, discover_inputs("UNIT-0008", "pack")))
    assert out["evidence"]["error"]["code"] == "capture_unreadable" and errors("agent-output", out) == []


def test_extra_photos_are_reported_not_silently_dropped(tmp_path, monkeypatch):
    sees(monkeypatch, {"SKU-BOTTLE-750": 1})
    out = pack.handle(request("UNIT-0008", ALPHA, place(tmp_path, "UNIT-0008", 11, 12, 13, 14, 15)))
    p = out["evidence"]["payload"]
    assert len(p["photos"]) == 3 and len(p["photos_not_used"]) == 2


# ---------------------------------------------------------------- tenancy, idempotency, reuse
def test_other_tenants_unit_is_refused(tmp_path, monkeypatch):
    sees(monkeypatch, {"SKU-BOTTLE-750": 1})
    with pytest.raises(LookupError):
        pack.handle(request("UNIT-0008", BRAVO, place(tmp_path, "UNIT-0008", 1)))  # UNIT-0008 belongs to alpha
    with pytest.raises(LookupError):
        pack.handle(request("UNIT-9999", ALPHA, []))


def test_http_other_tenant_is_404_and_unknown_stage_is_422(tmp_path, monkeypatch):
    client = TestClient(pack.app)
    assert client.get("/health").json()["stage"] == "pack"
    assert client.post("/run", json=request("UNIT-0008", BRAVO, [])).status_code == 404
    assert client.post("/run", json={**request("UNIT-0008", ALPHA, []), "stage": "prep"}).status_code == 422


def test_same_request_same_record_id(tmp_path, monkeypatch):
    sees(monkeypatch, {"SKU-BOTTLE-750": 1})
    req = request("UNIT-0008", ALPHA, place(tmp_path, "UNIT-0008", 21))
    a, b = pack.handle(req), pack.handle(req)
    assert a["evidence"]["record_id"] == b["evidence"]["record_id"] and a["evidence"]["record_id"].startswith("PCK-")
    # a re-run of the stage is a new request id, so a new record
    assert pack.handle({**req, "request_id": req["request_id"] + ":r2"})["evidence"]["record_id"] != a["evidence"]["record_id"]


def test_same_photo_for_a_different_order_is_flagged_but_a_recheck_is_not(tmp_path, monkeypatch):
    sees(monkeypatch, {"SKU-BOTTLE-750": 1})
    first = pack.handle(request("UNIT-0008", ALPHA, place(tmp_path, "UNIT-0008", 31)))
    assert first["evidence"]["decision"]["outcome"] == "seal"
    again = pack.handle(request("UNIT-0008", ALPHA, place(tmp_path, "UNIT-0008", 31), rid="WF-x:pack:r2"))
    assert checks(again)["photo_reuse"]["verdict"] == "PASS", "the same photo for the same order is a re-check"

    # the very same photo bytes, offered as the box photo for a different order
    sees(monkeypatch, {"SKU-BOTTLE-750": 1})
    other = pack.handle(request("UNIT-0083", ALPHA, place(tmp_path, "UNIT-0083", 31)))  # also 1 bottle
    assert checks(other)["photo_reuse"]["verdict"] == "UNCERTAIN"
    assert checks(other)["photo_reuse"]["uncertain_reason"] == "conflicting_evidence"
    assert other["evidence"]["decision"]["outcome"] == "pending_review", "a reused photo cannot seal a box"


# ---------------------------------------------------------------- reading earlier evidence
def test_records_what_receiving_said_using_the_latest_override(tmp_path, monkeypatch):
    sees(monkeypatch, {"SKU-BOTTLE-750": 1})
    base = request("UNIT-0008", ALPHA, place(tmp_path, "UNIT-0008", 41))
    rcv_req = {**base, "stage": "receiving", "request_id": "WF:receiving"}
    rcv = build_record(rcv_req, agent_id="receiving-test@0", record_id="RCV-T1", captured_at="2026-01-01T00:00:00Z",
                       checks=[], outcome="accept", reason="test", model={"name": "rules", "version": "0"},
                       verdict="PASS")
    override = {"override_id": "OVR-1", "supersedes": {"record_id": "RCV-T1", "override_id": None},
                "target": "decision", "actor": "t", "at": "2026-01-01T00:00:00Z", "reason": "carton crushed",
                "original_verdict": "PASS", "previous_verdict": "PASS", "new_verdict": "FAIL"}
    out = pack.handle({**base, "previous_evidence": [rcv], "context": {"overrides": [override], "case": {}}})
    assert errors("agent-output", out) == []
    assert out["evidence"]["upstream_refs"] == ["RCV-T1"]
    assert out["evidence"]["payload"]["upstream"]["receiving"] == {
        "record_id": "RCV-T1", "verdict": "PASS", "effective_verdict": "FAIL"}


# ---------------------------------------------------------------- the record itself
def test_record_is_honest_about_the_model_and_the_photos(tmp_path, monkeypatch):
    ev = run(tmp_path, monkeypatch, "UNIT-0008", ALPHA, {"SKU-BOTTLE-750": 1})["evidence"]
    assert ev["model"]["name"] == "oracle" and ev["model"]["provider"] is None, "a scripted model says what it is"
    photo = ev["payload"]["photos"][0]
    assert photo["original_sha256"] == ev["inputs"][0]["sha256"] and photo["quality_gate"] in {"PASS", "FAIL"}
    assert ev["subject"]["unit_scope"] == "order" and ev["subject"]["refs"]["order_id"] == "ORD-DUMMY-50008"
    assert json.dumps(ev)  # serialisable


# ---------------------------------------------------------------- persistence, time budget, hand-offs
def test_reuse_ledger_survives_a_restart_and_keeps_organisations_apart(tmp_path):
    ledger.remember(ALPHA, ["abc"], "ORD-1", "PCK-1")
    ledger._STATE.clear()  # a "restart": memory gone, the file stays
    assert ledger.earlier_uses(ALPHA, ["abc"]) == [{"order_id": "ORD-1", "record_id": "PCK-1"}]
    assert ledger.earlier_uses(BRAVO, ["abc"]) == [], "another organisation never sees this photo's history"
    ledger.remember(ALPHA, ["abc"], "ORD-1", "PCK-1")  # remembering twice does not duplicate
    assert len(ledger.earlier_uses(ALPHA, ["abc"])) == 1


def test_a_broken_ledger_file_never_stops_packing(tmp_path, monkeypatch):
    path = tmp_path / "ledger.json"
    path.write_text("{ not json")
    ledger._STATE.clear()
    assert ledger.earlier_uses(ALPHA, ["abc"]) == []  # starts empty instead of raising
    ledger.remember(ALPHA, ["abc"], "ORD-1", "PCK-1")  # and recovers by rewriting it
    assert json.loads(path.read_text())


def test_model_time_budget_fits_inside_the_orchestrators_stage_timeout():
    from orchestration.orchestrator import load_flow

    attempts = pack.MODEL_RETRIES + 1
    worst = attempts * pack.MODEL_TIMEOUT_S + sum(2 * (i + 1) for i in range(pack.MODEL_RETRIES))  # 2 s back-off
    assert worst < load_flow()["defaults"]["timeout_s"], f"worst case {worst}s would be cut off by the orchestrator"
    st = Settings(_env_file=None, gemini_api_key="x", gemini_timeout_s=pack.MODEL_TIMEOUT_S,
                  gemini_max_retries=pack.MODEL_RETRIES)
    assert st.gemini_timeout_s == pack.MODEL_TIMEOUT_S


def test_whole_workflow_for_a_returned_box_uses_our_record_downstream(tmp_path, monkeypatch):
    """UNIT-0016 (alpha): a merchant-fulfilled order of 2 towels that was later returned. Our Pack record goes
    through the real orchestrator, and Returns and Recovery must receive it as previous evidence."""
    from orchestration.orchestrator import run_workflow
    from orchestration.store import MemoryStore

    place(tmp_path, "UNIT-0016", 51)
    sees(monkeypatch, {"SKU-TOWEL-BLU": 1})  # one towel packed, two ordered
    case = {"org_id": ALPHA, "unit_id": "UNIT-0016", "route": "mfn", "returned": True}
    store = MemoryStore()
    wf = run_workflow(case, store=store)
    assert errors("workflow-state", wf) == []
    stages = {s["stage"]: s for s in wf["stage_results"]}
    assert stages["pack"]["state"] == "completed" and stages["pack"]["outcome"] == "stop_and_fix"
    assert stages["prep"]["state"] == "skipped" and "fba" in stages["prep"]["skipped_reason"]
    pack_id = stages["pack"]["record_id"]
    assert pack_id in wf["evidence_references"] and errors("evidence", store.get_evidence(pack_id)) == []
    for later in ("returns", "recovery"):
        assert pack_id in store.get_evidence(stages[later]["record_id"])["upstream_refs"], f"{later} did not receive Pack's record"
    assert wf["final_outcome"]["contributing_records"], "the final outcome must cite evidence"
    assert pack_id in wf["final_outcome"]["contributing_records"], "a stop_and_fix box is part of the final story"


def test_workflow_without_a_photo_fails_visibly_with_the_reason(tmp_path):
    from orchestration.orchestrator import run_workflow
    from orchestration.store import MemoryStore

    wf = run_workflow({"org_id": ALPHA, "unit_id": "UNIT-0008", "route": "mfn", "returned": False}, store=MemoryStore())
    assert wf["status"] == "FAILED" and wf["final_outcome"]["provisional"] is True
    assert wf["errors"][0]["code"] == "no_capture" and wf["errors"][0]["stage"] == "pack"


def test_check_cli_reports_a_verdict_and_refuses_other_tenants_without_writing(tmp_path, monkeypatch, capsys):
    from agents.pack.check import main

    photo = tmp_path / "mybox.jpg"
    photo.write_bytes(jpeg(61))
    sees(monkeypatch, {"SKU-BOTTLE-750": 1})
    assert main(["--unit", "UNIT-0008", "--org", ALPHA, str(photo)]) == 0
    assert "SEAL" in capsys.readouterr().out

    assert main(["--unit", "UNIT-0006", "--org", ALPHA, str(photo)]) == 2  # UNIT-0006 belongs to bravo
    assert "Refused" in capsys.readouterr().err
    assert not (tmp_path / "input" / "UNIT-0006").exists(), "a refused request must leave nothing behind"
    assert main(["--unit", "UNIT-0008", "--org", ALPHA, str(tmp_path / "missing.jpg")]) == 2
    assert "not found" in capsys.readouterr().err.lower()
