"""Prep Manager (agents/prep): behaviour tests on our own fixtures.

The vision model is replaced by a scripted observer (class ``Script``) that returns exactly the observations a test
asks for, so these tests are deterministic, keyless and make NO network call. They test everything *except what a
real model sees*: capture handling, the work-order lookup, the deterministic rules, the mapping onto the contract's
check keys, fail-open, tenancy, idempotency, how Receiving's evidence and overrides are read, and the Gemini
adapter (against a fake client). Real-model accuracy has not been measured: see agents/prep/README.md, Limits.
"""
from __future__ import annotations

import io
import json
import os
import random
import time

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from agents.prep import app as prep
from agents.prep import vision
from agents.prep.requirements import CONTRACT_KEYS, DEMO_TAG, EXTRA_KEYS, RULE_SOURCE, RequirementPack
from agents.prep.settings import Settings
from agents.prep.vision import (GeminiObserver, LabelRead, Observation, Observed, PerceptionError, PhotoQuality,
                                VisionResponse)
from orchestration.orchestrator import discover_inputs
from shared.utils import sample_data
from shared.utils.hashing import verify
from shared.utils.records import build_record
from shared.utils.schema import errors

ALPHA, BRAVO = "org_demo_alpha", "org_demo_bravo"
FNSKU = "X00TEST001"
REASONS = {"poor_image", "occluded", "insufficient_evidence", "model_error", "rule_unavailable",
           "conflicting_evidence", "other"}
ALLOWED_KEYS = set(CONTRACT_KEYS) | set(EXTRA_KEYS)


# ---------------------------------------------------------------- fixtures and helpers
def jpeg(seed: int, size=(480, 640)) -> bytes:
    """A photo-like image. Different seed, different bytes."""
    rng = random.Random(seed)
    img = Image.frombytes("RGB", size, rng.randbytes(size[0] * size[1] * 3))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=85)
    return buf.getvalue()


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    """Own capture folder, own settings (no key, no .env), and a scripted model by default."""
    monkeypatch.setenv("INPUT_DIR", str(tmp_path / "input"))
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setattr(prep, "settings", lambda: Settings(_env_file=None, gemini_api_key=None))
    yield


def place(tmp_path, unit: str, *seeds: int) -> list[dict]:
    folder = tmp_path / "input" / unit / "prep"
    folder.mkdir(parents=True, exist_ok=True)
    for i, seed in enumerate(seeds, 1):
        (folder / f"photo{i}.jpg").write_bytes(jpeg(seed))
    return discover_inputs(unit, "prep")


def full_order(org=ALPHA, **req) -> dict:
    """A work order that requires everything, so every check is exercised."""
    requirements = {"polybag": True, "suffocation_warning": True, "expiry_date": True,
                    "handling_marks": ["fragile", "this_way_up"], "cover_original_barcode": True, **req}
    return {"org_id": org, "sku": "SKU-TEST", "fnsku": FNSKU, "work_order_id": "WO-T1", "fba_shipment_id": "FBA-T1",
            "requirements": requirements}


def request(unit: str, org: str, inputs: list[dict], *, order=None, previous=None, overrides=None, rid=None,
            route="fba"):
    wf = f"WF-{org}-{unit}"
    ctx = {"overrides": overrides or [], "case": {}}
    if order is not None:
        ctx["order"] = order
    return {"schema_version": "1.0", "request_id": rid or f"{wf}:prep", "workflow_id": wf, "stage": "prep",
            "subject": {"org_id": org, "subject_id": unit, "route": route}, "inputs": inputs,
            "previous_evidence": previous or [], "context": ctx}


class Script:
    """A scripted vision model. By default it sees a perfectly prepped unit; ``over`` changes single checks."""

    model, provider = "scripted-vision", None

    def __init__(self, over: dict | None = None, *, label=FNSKU, label_conf="high", label_legible=True,
                 marks=("fragile", "this_way_up"), unusable=(), raises=None, attempts=1, raw=None):
        self.over, self.label, self.label_conf, self.label_legible = over or {}, label, label_conf, label_legible
        self.marks, self.unusable, self.raises, self.attempts, self.raw = list(marks), set(unusable), raises, attempts, raw
        self.calls: list[tuple[int, list[str]]] = []

    def observe(self, photos, checks):
        self.calls.append((len(photos), [c.check_id for c in checks]))
        if self.raises:
            raise self.raises
        obs = []
        for c in checks:
            ov = self.over.get(c.check_id, {})
            if ov is None:
                continue  # the model forgot this check
            fields = {"check_id": c.check_id, "status": "met", "confidence": "high", "photo_index": 1,
                      "location": "centre", "evidence": f"clearly visible: {c.check_id}", "limit": None,
                      "marks_visible": self.marks if c.check_id == "handling_marks_present" else []}
            for extra in (ov if isinstance(ov, list) else [ov]):
                obs.append(Observation(**{**fields, **extra}))
        resp = VisionResponse(
            photo_quality=[PhotoQuality(photo_index=i, usable=i not in self.unusable, issues=[])
                           for i in range(1, len(photos) + 1)],
            label_text_read=LabelRead(value=self.label, photo_index=1, legible=self.label_legible,
                                      confidence=self.label_conf),
            observations=obs)
        return Observed(response=resp, model=self.model, model_version="test-1", provider=None,
                        usage={"input_tokens": 1000, "output_tokens": 200, "thinking_tokens": 0},
                        latency_ms=5, attempts=self.attempts)


def sees(monkeypatch, script: Script) -> Script:
    monkeypatch.setattr(prep, "get_observer", lambda st: script)
    return script


def run(tmp_path, monkeypatch, script=None, *, unit="UNIT-0900", org=ALPHA, order="full", seeds=(1, 2, 3), **kw):
    """Run one unit through handle() with a scripted model and check the output is contract-valid."""
    script = sees(monkeypatch, script or Script())
    order = full_order(org) if order == "full" else order
    out = prep.handle(request(unit, org, place(tmp_path, unit, *seeds), order=order, **kw))
    assert errors("agent-output", out) == [], errors("agent-output", out)
    assert verify(out["evidence"])
    return out


def checks(out) -> dict:
    return {c["check_key"]: c for c in out["evidence"]["checks"]}


# ---------------------------------------------------------------- the three outcomes
def test_a_perfectly_prepped_unit_is_compliant_on_every_key(tmp_path, monkeypatch):
    out = run(tmp_path, monkeypatch)
    ev = out["evidence"]
    assert ev["decision"]["outcome"] == "compliant" and ev["decision"]["verdict"] == "PASS"
    assert out["status"] == "completed" and ev["decision"]["needs_human"] is False
    assert set(checks(out)) == ALLOWED_KEYS and all(c["verdict"] == "PASS" for c in ev["checks"])
    refs = {i["ref"] for i in ev["inputs"]}
    assert all(c["evidence_refs"] and set(c["evidence_refs"]) <= refs for c in ev["checks"]), "every check cites a photo"
    assert out["next_step_recommendation"]["action"] == "continue"
    # the reference's eleven checks are kept whole in the payload; bag thickness is reported, not faked
    assert len(ev["payload"]["rule_checks"]) == 10
    assert [n["check_id"] for n in ev["payload"]["not_verified"]] == ["bag_thickness_material"]


def test_label_on_a_seam_is_non_compliant(tmp_path, monkeypatch):
    script = Script({"fnsku_placement": {"status": "not_met", "evidence": "the label crosses the bottom seam"}})
    out = run(tmp_path, monkeypatch, script)
    ev = out["evidence"]
    assert ev["decision"]["outcome"] == "non_compliant" and ev["decision"]["verdict"] == "FAIL"
    c = checks(out)["fnsku_label_placement"]
    assert c["verdict"] == "FAIL" and "seam" in c["detail"] and c["confidence"] == 0.9
    assert ev["decision"]["needs_human"] is False and out["next_step_recommendation"]["action"] == "route_to_recovery"


def test_every_failed_check_is_reported_not_only_the_first(tmp_path, monkeypatch):
    script = Script({"polybag_sealed": {"status": "not_met", "evidence": "the bag is open at one end"},
                     "fnsku_placement": {"status": "not_met", "evidence": "the label is on the edge"},
                     "expiry_visible": {"status": "not_met", "evidence": "the date is blurred by the wrap"}},
                    marks=("fragile",))  # and this_way_up is missing
    out = run(tmp_path, monkeypatch, script)
    ev = out["evidence"]
    failed = {"polybag_sealed", "fnsku_label_placement", "expiry_legible", "handling_marks"}
    assert set(ev["payload"]["failed_checks"]) == failed
    assert {k for k, c in checks(out).items() if c["verdict"] == "FAIL"} == failed
    assert all(k in ev["decision"]["reason"] for k in failed), ev["decision"]["reason"]
    assert "this_way_up" in checks(out)["handling_marks"]["detail"]


def test_a_failure_is_not_hidden_by_an_uncertain_check(tmp_path, monkeypatch):
    script = Script({"polybag_sealed": {"status": "not_met", "evidence": "open end"},
                     "expiry_visible": {"status": "cant_tell", "evidence": "", "limit": "blurry", "photo_index": 1}})
    out = run(tmp_path, monkeypatch, script)
    assert out["evidence"]["decision"]["verdict"] == "FAIL"
    assert checks(out)["expiry_legible"]["verdict"] == "UNCERTAIN"
    assert "polybag_sealed" in out["evidence"]["decision"]["reason"] and "expiry_legible" in out["evidence"]["decision"]["reason"]


def test_the_eleven_checks_map_onto_the_contract_keys(tmp_path, monkeypatch):
    """polybag present+sealed -> polybag_sealed; warning present+legible -> suffocation_warning; label present+placement
    -> fnsku_label_placement. The worst verdict of the group wins and the other member's detail is not lost."""
    script = Script({"polybag_present": {"status": "not_met", "evidence": "the unit is loose in a box, no bag"},
                     "suffocation_warning_legible": {"status": "not_met", "evidence": "folded over, half hidden"}})
    out = run(tmp_path, monkeypatch, script)
    c = checks(out)
    assert c["polybag_sealed"]["verdict"] == "FAIL" and "no bag" in c["polybag_sealed"]["detail"]
    assert c["suffocation_warning"]["verdict"] == "FAIL" and "folded" in c["suffocation_warning"]["detail"]
    assert set(c["polybag_sealed"]["observed"]) == {"polybag_present", "polybag_sealed"}
    internal = {r["check_id"]: r["verdict"] for r in out["evidence"]["payload"]["rule_checks"]}
    assert internal["polybag_sealed"] == "PASS" and internal["polybag_present"] == "FAIL"


# ---------------------------------------------------------------- what does not apply is omitted, not marked PASS
def test_checks_that_do_not_apply_are_omitted(tmp_path, monkeypatch):
    out = run(tmp_path, monkeypatch, order=full_order(polybag=False, suffocation_warning=False, expiry_date=False,
                                                       handling_marks=[]))
    assert set(checks(out)) == {"fnsku_label_placement", "fnsku_text_match", "original_barcode_covered"}
    assert out["evidence"]["payload"]["not_verified"] == []  # no bag, so thickness is moot too


def test_csv_work_order_flags_decide_which_checks_exist(tmp_path, monkeypatch):
    """UNIT-0002 (alpha): no bag, no expiry, 'fragile' required. The organisers' sample, mapped by the rules."""
    out = run(tmp_path, monkeypatch, Script(marks=("fragile",), label="X00DUMMY002"), unit="UNIT-0002", order=None)
    assert set(checks(out)) == {"fnsku_label_placement", "fnsku_text_match", "original_barcode_covered", "handling_marks"}
    ev = out["evidence"]
    assert ev["subject"]["refs"]["work_order_id"] == "WO-3000" and ev["subject"]["refs"]["fnsku"] == "X00DUMMY002"
    assert ev["payload"]["prep_price_usd"] == 0.40 and ev["payload"]["requirements"]["source"] == "prep_sample.csv"


def test_every_sample_unit_maps_cleanly(tmp_path, monkeypatch):
    """Whole organiser sample: the check set always follows the wo_* flags, and every output is contract-valid."""
    rows = sample_data.rows("prep")
    assert len(rows) >= 50
    for r in rows:
        marks = [m for m in r["wo_handling_marks"].split(";") if m]
        sees(monkeypatch, Script(marks=tuple(marks), label=r["fnsku"]))
        inputs = place(tmp_path, r["unit_id"], 5)
        out = prep.handle(request(r["unit_id"], r["org_id"], inputs, order=None))
        assert errors("agent-output", out) == [], r["unit_id"]
        keys = set(checks(out))
        assert ("polybag_sealed" in keys) == (r["wo_polybag"] == "True"), r["unit_id"]
        assert ("suffocation_warning" in keys) == (r["wo_suffocation_warning"] == "True"), r["unit_id"]
        assert ("expiry_legible" in keys) == (r["wo_expiry_date"] == "True"), r["unit_id"]
        assert ("handling_marks" in keys) == bool(marks), r["unit_id"]
        assert keys <= ALLOWED_KEYS and out["evidence"]["decision"]["outcome"] == "compliant", r["unit_id"]


# ---------------------------------------------------------------- the label text
def test_label_text_is_compared_by_rules_and_the_model_never_sees_the_expected_code():
    checks_asked = RequirementPack(fnsku=FNSKU).observable()
    assert "fnsku_text_match" not in {c.check_id for c in checks_asked}, "the code is read, then compared by rules"
    text = vision.SYSTEM + vision.build_prompt(checks_asked, 3)
    assert FNSKU not in text and "expected" not in text.lower()
    assert not any(w in vision.build_prompt(checks_asked, 3).lower() for w in ("pass", "fail", "compliant"))


@pytest.mark.parametrize("read,conf,verdict,reason", [
    ("x00-test 001", "high", "PASS", None),          # case, spaces and hyphens do not matter
    ("X00TEST001", "medium", "PASS", None),
    ("X00TEST999", "high", "FAIL", None),            # a confident mismatch: mislabelled
    ("X00TEST999", "medium", "UNCERTAIN", "insufficient_evidence"),  # may be a misread character
    ("X00TEST001", "low", "UNCERTAIN", "insufficient_evidence"),
    (None, "high", "UNCERTAIN", "insufficient_evidence"),            # unreadable
])
def test_label_text_verdicts(tmp_path, monkeypatch, read, conf, verdict, reason):
    out = run(tmp_path, monkeypatch, Script(label=read, label_conf=conf, label_legible=read is not None))
    c = checks(out)["fnsku_text_match"]
    assert c["verdict"] == verdict and c.get("uncertain_reason") == reason
    if verdict == "FAIL":
        assert out["evidence"]["decision"]["outcome"] == "non_compliant" and "mislabelled" in c["detail"]


def test_label_text_from_an_unusable_photo_is_not_trusted(tmp_path, monkeypatch):
    out = run(tmp_path, monkeypatch, Script(label="X00TEST999", unusable={1}))
    c = checks(out)["fnsku_text_match"]
    assert c["verdict"] == "UNCERTAIN" and c["uncertain_reason"] == "poor_image"


# ---------------------------------------------------------------- handling marks
def test_handling_marks_are_compared_with_the_work_order(tmp_path, monkeypatch):
    ok = run(tmp_path, monkeypatch, Script(marks=("This Way Up", "FRAGILE")))
    assert checks(ok)["handling_marks"]["verdict"] == "PASS"
    missing = run(tmp_path, monkeypatch, Script(marks=("fragile",)))
    c = checks(missing)["handling_marks"]
    assert c["verdict"] == "FAIL" and "this_way_up" in c["detail"]
    hidden = run(tmp_path, monkeypatch, Script({"handling_marks_present": {"status": "cant_tell", "evidence": "",
                                                                          "limit": "occluded"}}))
    assert checks(hidden)["handling_marks"]["uncertain_reason"] == "occluded"
    contradiction = run(tmp_path, monkeypatch, Script({"handling_marks_present": {"status": "not_met"}}))
    assert checks(contradiction)["handling_marks"]["uncertain_reason"] == "conflicting_evidence"


# ---------------------------------------------------------------- UNCERTAIN is first class, with a reason
@pytest.mark.parametrize("name,over,reason", [
    ("blurry", {"status": "cant_tell", "evidence": "", "limit": "blurry"}, "poor_image"),
    ("cropped", {"status": "cant_tell", "evidence": "", "limit": "cropped"}, "occluded"),
    ("not shown", {"status": "cant_tell", "evidence": "", "limit": "not_shown", "photo_index": None}, "insufficient_evidence"),
    ("no evidence cited", {"evidence": "   "}, "insufficient_evidence"),
    ("low confidence", {"confidence": "low"}, "insufficient_evidence"),
    ("no photo cited", {"photo_index": None}, "insufficient_evidence"),
    ("photo that does not exist", {"photo_index": 9}, "insufficient_evidence"),
])
def test_unsure_observations_are_uncertain_with_a_reason(tmp_path, monkeypatch, name, over, reason):
    out = run(tmp_path, monkeypatch, Script({"fnsku_placement": over}))
    ev = out["evidence"]
    c = checks(out)["fnsku_label_placement"]
    assert c["verdict"] == "UNCERTAIN" and c["uncertain_reason"] == reason, name
    assert ev["decision"]["verdict"] == "UNCERTAIN" and ev["decision"]["outcome"] == "pending_review"
    assert ev["decision"]["needs_human"] is True and out["status"] == "completed" and out["verdict"] == "UNCERTAIN"
    assert out["next_step_recommendation"]["action"] == "review"


def test_a_check_the_model_forgot_is_uncertain_not_passed(tmp_path, monkeypatch):
    out = run(tmp_path, monkeypatch, Script({"expiry_visible": None}))
    c = checks(out)["expiry_legible"]
    assert c["verdict"] == "UNCERTAIN" and c["uncertain_reason"] == "insufficient_evidence"


def test_a_confident_answer_from_an_unusable_photo_is_uncertain(tmp_path, monkeypatch):
    out = run(tmp_path, monkeypatch, Script(unusable={1}))
    assert all(c["verdict"] == "UNCERTAIN" and c["uncertain_reason"] == "poor_image" for c in out["evidence"]["checks"])
    assert out["evidence"]["decision"]["outcome"] == "pending_review"


def test_a_model_that_contradicts_itself_is_not_evidence(tmp_path, monkeypatch):
    twice = [{"status": "met"}, {"status": "not_met"}]
    out = run(tmp_path, monkeypatch, Script({"original_barcode_covered": twice}))
    c = checks(out)["original_barcode_covered"]
    assert c["verdict"] == "UNCERTAIN" and c["uncertain_reason"] == "conflicting_evidence"


# ---------------------------------------------------------------- fail open
def test_model_failure_is_a_pending_record_that_keeps_the_photos(tmp_path, monkeypatch):
    inputs = place(tmp_path, "UNIT-0900", 1, 2)
    sees(monkeypatch, Script(raises=PerceptionError("model timed out", attempts=2)))
    out = prep.handle(request("UNIT-0900", ALPHA, inputs, order=full_order()))
    ev = out["evidence"]
    assert errors("agent-output", out) == [] and verify(ev)
    assert out["status"] == "pending" and ev["error"]["code"] == "model_unavailable" and ev["error"]["retryable"]
    assert ev["decision"]["verdict"] == "UNCERTAIN" and ev["decision"]["outcome"] == "pending_review"
    assert ev["checks"] == [], "a pending record is not a judgment: no UNCERTAIN-looking checks"
    assert [i["ref"] for i in ev["inputs"]] == [i["ref"] for i in inputs], "the capture must not be lost"
    assert out["next_step_recommendation"]["action"] == "retry"
    assert ev["model"]["calls"] == 2, "two attempts were made, and the record says so"


def test_an_unusable_model_answer_is_pending_not_success(tmp_path, monkeypatch):
    sees(monkeypatch, Script(raises=PerceptionError("bad json", code="model_output_invalid", attempts=1)))
    out = prep.handle(request("UNIT-0900", ALPHA, place(tmp_path, "UNIT-0900", 1), order=full_order()))
    assert out["status"] == "pending" and out["evidence"]["error"]["code"] == "model_output_invalid"
    assert out["evidence"]["checks"] == [] and errors("agent-output", out) == []


def test_any_other_model_side_exception_is_still_pending(tmp_path, monkeypatch):
    sees(monkeypatch, Script(raises=RuntimeError("boom")))
    out = prep.handle(request("UNIT-0900", ALPHA, place(tmp_path, "UNIT-0900", 1), order=full_order()))
    assert out["status"] == "pending" and "boom" in out["evidence"]["error"]["message"]
    assert errors("agent-output", out) == []


def test_no_api_key_is_pending_not_a_crash(tmp_path):
    # the real get_observer, with no key configured
    out = prep.handle(request("UNIT-0900", ALPHA, place(tmp_path, "UNIT-0900", 1), order=full_order()))
    assert out["evidence"]["error"]["code"] == "model_not_configured" and out["status"] == "pending"
    assert errors("agent-output", out) == [] and len(out["evidence"]["inputs"]) == 1


def test_no_photo_is_pending_and_the_model_is_not_called(monkeypatch):
    script = sees(monkeypatch, Script())
    out = prep.handle(request("UNIT-0900", ALPHA, [], order=full_order()))
    ev = out["evidence"]
    assert errors("agent-output", out) == [] and ev["error"]["code"] == "no_capture" and ev["error"]["retryable"]
    assert ev["decision"]["outcome"] == "pending_review" and ev["checks"] == [] and script.calls == []
    assert ev["model"]["calls"] == 0


def test_tampered_photo_is_refused_and_recorded(tmp_path, monkeypatch):
    script = sees(monkeypatch, Script())
    inputs = place(tmp_path, "UNIT-0900", 5)
    (tmp_path / "input" / "UNIT-0900" / "prep" / "photo1.jpg").write_bytes(jpeg(99))  # changed after hashing
    out = prep.handle(request("UNIT-0900", ALPHA, inputs, order=full_order()))
    err = out["evidence"]["error"]
    assert err["code"] == "capture_unreadable" and err["retryable"] is False and "sha256" in err["message"]
    assert out["status"] == "error" and script.calls == []


@pytest.mark.parametrize("ref", ["UNIT-0016/prep/photo1.jpg", "UNIT-0900/pack/photo1.jpg", "../../etc/hosts",
                                 "UNIT-0900/photo1.jpg"])
def test_a_capture_from_another_unit_stage_or_outside_the_root_is_refused(tmp_path, monkeypatch, ref):
    sees(monkeypatch, Script())
    place(tmp_path, "UNIT-0016", 6)
    (tmp_path / "input" / "UNIT-0900" / "pack").mkdir(parents=True)
    (tmp_path / "input" / "UNIT-0900" / "pack" / "photo1.jpg").write_bytes(jpeg(7))
    (tmp_path / "input" / "UNIT-0900" / "photo1.jpg").write_bytes(jpeg(8))
    out = prep.handle(request("UNIT-0900", ALPHA, [{"ref": ref, "kind": "image", "sha256": None}], order=full_order()))
    assert out["evidence"]["error"]["code"] == "capture_unreadable"


def test_a_file_that_is_not_a_photo_is_pending(tmp_path, monkeypatch):
    sees(monkeypatch, Script())
    folder = tmp_path / "input" / "UNIT-0900" / "prep"
    folder.mkdir(parents=True)
    (folder / "photo1.jpg").write_bytes(b"not really a jpeg")
    out = prep.handle(request("UNIT-0900", ALPHA, discover_inputs("UNIT-0900", "prep"), order=full_order()))
    assert out["evidence"]["error"]["code"] == "capture_unreadable" and errors("agent-output", out) == []


def test_photos_beyond_the_limit_are_reported_not_silently_dropped(tmp_path, monkeypatch):
    script = sees(monkeypatch, Script())
    inputs = place(tmp_path, "UNIT-0900", *range(10, 18))  # 8 photos, the limit is 6
    out = prep.handle(request("UNIT-0900", ALPHA, inputs, order=full_order()))
    ev = out["evidence"]
    assert script.calls[0][0] == 6, "the model sees exactly the first six"
    assert len(ev["inputs"]) == 6 and len(ev["payload"]["photos"]) == 6
    dropped = ev["payload"]["photos_not_used"]
    assert [d["ref"] for d in dropped] == [i["ref"] for i in inputs[6:]] and all(d["sha256"] for d in dropped)
    assert "2 further photo(s) were not examined" in ev["decision"]["reason"]
    assert errors("agent-output", out) == []


def test_captured_at_is_the_files_time_not_now_and_a_stated_time_wins(tmp_path, monkeypatch):
    inputs = place(tmp_path, "UNIT-0900", 1)
    old = time.time() - 86400 * 30
    os.utime(tmp_path / "input" / "UNIT-0900" / "prep" / "photo1.jpg", (old, old))
    sees(monkeypatch, Script())
    ev = prep.handle(request("UNIT-0900", ALPHA, inputs, order=full_order()))["evidence"]
    assert ev["captured_at"] < ev["produced_at"] and ev["payload"]["captured_at_source"] == "file_mtime"
    stated = prep.handle(request("UNIT-0900", ALPHA, inputs, order={**full_order(), "captured_at": "2026-05-01T08:00:00Z"}))
    assert stated["evidence"]["captured_at"] == "2026-05-01T08:00:00Z"
    assert stated["evidence"]["payload"]["captured_at_source"] == "context.order"


# ---------------------------------------------------------------- tenancy and routing
def test_other_tenants_unit_is_refused(tmp_path, monkeypatch):
    sees(monkeypatch, Script())
    assert sample_data.has("prep", "UNIT-0003", BRAVO) and not sample_data.has("prep", "UNIT-0003", ALPHA)
    with pytest.raises(LookupError):
        prep.handle(request("UNIT-0003", ALPHA, place(tmp_path, "UNIT-0003", 1), order=None))  # belongs to bravo
    with pytest.raises(LookupError):
        prep.handle(request("UNIT-9999", ALPHA, [], order=None))
    prep.handle(request("UNIT-0003", BRAVO, place(tmp_path, "UNIT-0003", 1), order=None))  # its own tenant: answered


def test_a_work_order_for_another_org_or_unit_is_refused(tmp_path, monkeypatch):
    sees(monkeypatch, Script())
    inputs = place(tmp_path, "UNIT-0900", 1)
    with pytest.raises(LookupError):
        prep.handle(request("UNIT-0900", ALPHA, inputs, order=full_order(BRAVO)))
    with pytest.raises(LookupError):
        prep.handle(request("UNIT-0900", ALPHA, inputs, order={**full_order(), "unit_id": "UNIT-0001"}))


def test_only_fba_units_are_handled(tmp_path, monkeypatch):
    sees(monkeypatch, Script())
    for route in ("mfn", "unknown"):
        with pytest.raises(LookupError):
            prep.handle(request("UNIT-0900", ALPHA, place(tmp_path, "UNIT-0900", 1), order=full_order(), route=route))


def test_http_status_codes(tmp_path, monkeypatch):
    client = TestClient(prep.app)
    assert client.get("/health").json()["stage"] == "prep"
    sees(monkeypatch, Script(label="X00DUMMY002", marks=("fragile",)))
    ok = request("UNIT-0002", ALPHA, place(tmp_path, "UNIT-0002", 3), order=None)
    assert client.post("/run", json=ok).status_code == 200
    assert client.post("/run", json=request("UNIT-0003", ALPHA, [], order=None)).status_code == 404   # wrong tenant
    assert client.post("/run", json=request("UNIT-0002", ALPHA, [], order=None, route="mfn")).status_code == 404
    assert client.post("/run", json={**ok, "stage": "pack"}).status_code == 422                       # not our stage
    assert client.post("/run", json={"hello": "world"}).status_code == 422                            # not an Agent Input


# ---------------------------------------------------------------- idempotency
def test_same_request_same_record_id(tmp_path, monkeypatch):
    sees(monkeypatch, Script())
    req = request("UNIT-0900", ALPHA, place(tmp_path, "UNIT-0900", 21), order=full_order())
    a, b = prep.handle(req), prep.handle(req)
    assert a["evidence"]["record_id"] == b["evidence"]["record_id"] and a["evidence"]["record_id"].startswith("PRP-")
    # a re-run of the stage is a new request id, so a new record
    assert prep.handle({**req, "request_id": req["request_id"] + ":r2"})["evidence"]["record_id"] != a["evidence"]["record_id"]


def test_pending_record_ids_are_stable_and_never_collide_with_judged_ones(tmp_path, monkeypatch):
    req = request("UNIT-0900", ALPHA, [], order=full_order())
    p1, p2 = prep.handle(req), prep.handle(req)
    assert p1["evidence"]["record_id"] == p2["evidence"]["record_id"] and "PENDING" in p1["evidence"]["record_id"]
    sees(monkeypatch, Script())
    judged = prep.handle({**req, "inputs": place(tmp_path, "UNIT-0900", 1)})
    assert judged["evidence"]["record_id"] != p1["evidence"]["record_id"]


# ---------------------------------------------------------------- reading Receiving's evidence
def receiving_record(verdict="PASS", record_id="RCV-T1", status="completed", failed=()):
    base = {"workflow_id": "WF", "stage": "receiving", "request_id": "WF:receiving",
            "subject": {"org_id": ALPHA, "subject_id": "UNIT-0900"}}
    checks_ = [{"check_key": k, "verdict": "FAIL", "confidence": None} for k in failed]
    return build_record(base, agent_id="receiving-test@0", record_id=record_id, captured_at="2026-01-01T00:00:00Z",
                        checks=checks_, outcome="accept", reason="test", model={"name": "rules", "version": "0"},
                        verdict=verdict, status=status)


def override(record_id, new, oid="OVR-1", prev=None):
    return {"override_id": oid, "supersedes": {"record_id": record_id, "override_id": prev}, "target": "decision",
            "actor": "t", "at": "2026-01-01T00:00:00Z", "reason": "checked by hand", "original_verdict": "PASS",
            "previous_verdict": "PASS", "new_verdict": new}


def test_receiving_is_cited_and_does_not_change_the_prep_checks(tmp_path, monkeypatch):
    plain = run(tmp_path, monkeypatch)
    rcv = receiving_record("FAIL", failed=("unit_damage",))
    out = run(tmp_path, monkeypatch, previous=[rcv])
    ev = out["evidence"]
    assert ev["upstream_refs"] == ["RCV-T1"]
    up = ev["payload"]["upstream"]["receiving"]
    assert up == {"record_id": "RCV-T1", "status": "completed", "verdict": "FAIL", "effective_verdict": "FAIL",
                  "overridden": False, "failed_checks": ["unit_damage"]}
    assert "Receiving's effective verdict for this unit is FAIL (RCV-T1)" in ev["decision"]["reason"]
    assert [(c["check_key"], c["verdict"]) for c in ev["checks"]] == [(c["check_key"], c["verdict"]) for c in plain["evidence"]["checks"]]


def test_the_latest_receiving_override_is_the_effective_verdict(tmp_path, monkeypatch):
    rcv = receiving_record("FAIL")
    out = run(tmp_path, monkeypatch, previous=[rcv], overrides=[override("RCV-T1", "PASS")])
    up = out["evidence"]["payload"]["upstream"]["receiving"]
    assert up["verdict"] == "FAIL" and up["effective_verdict"] == "PASS" and up["overridden"] is True
    assert "Receiving's effective" not in out["evidence"]["decision"]["reason"], "an overridden FAIL is no longer a flag"
    # two overrides: the latest wins, and an override of some other record is ignored
    out = run(tmp_path, monkeypatch, previous=[rcv], overrides=[
        override("RCV-T1", "PASS"), override("RCV-OTHER", "PASS", "OVR-X"), override("RCV-T1", "UNCERTAIN", "OVR-2", "OVR-1")])
    assert out["evidence"]["payload"]["upstream"]["receiving"]["effective_verdict"] == "UNCERTAIN"


def test_a_receiving_record_that_did_not_complete_is_still_recorded(tmp_path, monkeypatch):
    rcv = receiving_record("UNCERTAIN", status="pending")
    ev = run(tmp_path, monkeypatch, previous=[rcv])["evidence"]
    assert ev["payload"]["upstream"]["receiving"]["status"] == "pending" and ev["upstream_refs"] == ["RCV-T1"]


# ---------------------------------------------------------------- the record is honest
def test_record_is_honest_about_the_model_the_rules_and_the_measurements(tmp_path, monkeypatch):
    out = run(tmp_path, monkeypatch)
    ev = out["evidence"]
    assert ev["model"] == {"name": "scripted-vision", "version": "test-1", "provider": None,
                           "prompt_version": "prep-vision/1", "calls": 1, "cost_usd": None}
    assert ev["agent_id"].startswith("prep-manager@") and "stub" not in ev["model"]["name"]
    p = ev["payload"]
    assert p["measurements"] == {} and "photograph" in p["measurements_note"], "weight/size cannot be read from photos"
    assert p["rule_source"] == RULE_SOURCE and p["rule_source"]["status"] == "unverified" and p["rule_source"]["url"] is None
    assert all(c["detail"].endswith(DEMO_TAG) for c in ev["checks"]), "every detail says the rule's source is unverified"
    assert "§" not in json.dumps(ev), "no invented clause numbers"
    assert "not a calibrated probability" in p["confidence_note"]
    assert ev["subject"]["unit_scope"] == "unit" and ev["subject"]["refs"]["work_order_id"] == "WO-T1"
    photo = p["photos"][0]
    assert photo["original_sha256"] == ev["inputs"][0]["sha256"] and photo["analysed_sha256"] != photo["original_sha256"]
    assert {i["kind"] for i in ev["inputs"]} == {"image"} and ev["upstream_refs"] == []


def test_every_uncertain_check_has_a_contract_reason_and_keys_are_stable(tmp_path, monkeypatch):
    script = Script({"polybag_sealed": {"status": "cant_tell", "evidence": "", "limit": "glare"},
                     "expiry_visible": {"confidence": "low"}, "fnsku_placement": {"photo_index": 9}}, label=None,
                    label_legible=False)
    out = run(tmp_path, monkeypatch, script)
    for c in out["evidence"]["checks"]:
        assert c["check_key"] in ALLOWED_KEYS
        if c["verdict"] == "UNCERTAIN":
            assert c["uncertain_reason"] in REASONS


def test_cost_is_computed_only_from_configured_prices():
    usage = {"input_tokens": 1000, "output_tokens": 200, "thinking_tokens": 300}
    assert vision.cost_usd(usage, Settings(_env_file=None)) is None, "no price configured, none guessed"
    st = Settings(_env_file=None, cost_per_1m_input_usd=0.10, cost_per_1m_output_usd=0.40)
    assert vision.cost_usd(usage, st) == pytest.approx((1000 * 0.10 + 500 * 0.40) / 1e6)


def test_model_time_budget_fits_inside_the_orchestrators_stage_timeout():
    from orchestration.orchestrator import load_flow

    st = Settings(_env_file=None)
    attempts = st.prep_max_retries + 1
    worst = attempts * st.prep_timeout_s + sum(2 * i for i in range(1, attempts))  # 2 s back-off per retry
    assert worst < load_flow()["defaults"]["timeout_s"], f"worst case {worst}s would be cut off by the orchestrator"


# ---------------------------------------------------------------- the Gemini adapter, against a fake client
GOOD_JSON = json.dumps({
    "photo_quality": [{"photo_index": 1, "usable": True, "issues": []}],
    "label_text_read": {"value": FNSKU, "photo_index": 1, "legible": True, "confidence": "high"},
    "observations": [{"check_id": "fnsku_placement", "status": "met", "confidence": "high", "photo_index": 1,
                      "location": "centre", "evidence": "flat label", "limit": None, "marks_visible": []}]})


class FakeResponse:
    def __init__(self, text, version="gemini-fake-001"):
        self.text, self.model_version = text, version
        self.usage_metadata = type("U", (), {"prompt_token_count": 1200, "candidates_token_count": 150,
                                             "thoughts_token_count": 50})()


class FakeClient:
    """Stands in for google.genai.Client: no network."""

    def __init__(self, script):
        self.script, self.calls = list(script), 0
        self.models = self

    def generate_content(self, **kwargs):
        self.calls += 1
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def gemini(monkeypatch, script, **kw):
    import google.genai as genai

    st = Settings(_env_file=None, gemini_api_key="not-a-real-key", cost_per_1m_input_usd=0.10,
                  cost_per_1m_output_usd=0.40, **kw)
    obs = GeminiObserver(st)
    obs.client = FakeClient(script)
    monkeypatch.setattr(vision.time, "sleep", lambda s: None)
    assert genai  # the real client class is constructed (offline) but never used
    return obs


def asked():
    return RequirementPack(fnsku=FNSKU).observable()


def test_gemini_adapter_parses_the_answer_and_reports_tokens_and_cost(monkeypatch):
    obs = gemini(monkeypatch, [FakeResponse("```json\n" + GOOD_JSON + "\n```")])  # a fenced answer is tolerated
    got = obs.observe([jpeg(1)], asked())
    assert got.response.label_text_read.value == FNSKU and got.model == "gemini-3.5-flash-lite"
    assert got.model_version == "gemini-fake-001" and got.attempts == 1 and got.provider == "google"
    assert got.usage == {"input_tokens": 1200, "output_tokens": 150, "thinking_tokens": 50}
    assert got.cost_usd == pytest.approx((1200 * 0.10 + 200 * 0.40) / 1e6)
    assert obs.client.calls == 1, "one batched call"


def test_gemini_adapter_retries_a_busy_server_once_and_counts_both_calls(monkeypatch):
    from google.genai import errors as gerrors

    busy = gerrors.APIError(503, {"error": {"message": "overloaded", "status": "UNAVAILABLE"}})
    obs = gemini(monkeypatch, [busy, FakeResponse(GOOD_JSON)])
    got = obs.observe([jpeg(1)], asked())
    assert got.attempts == 2 and obs.client.calls == 2


def test_gemini_adapter_gives_up_after_the_retry_and_does_not_retry_a_bad_request(monkeypatch):
    from google.genai import errors as gerrors

    busy = gerrors.APIError(503, {"error": {"message": "overloaded", "status": "UNAVAILABLE"}})
    obs = gemini(monkeypatch, [busy, busy])
    with pytest.raises(PerceptionError) as exc:
        obs.observe([jpeg(1)], asked())
    assert exc.value.code == "model_unavailable" and exc.value.attempts == 2

    bad = gerrors.APIError(400, {"error": {"message": "bad request", "status": "INVALID_ARGUMENT"}})
    obs = gemini(monkeypatch, [bad, FakeResponse(GOOD_JSON)])
    with pytest.raises(PerceptionError) as exc:
        obs.observe([jpeg(1)], asked())
    assert exc.value.attempts == 1 and obs.client.calls == 1


@pytest.mark.parametrize("text", ["", "I cannot help with that", '{"observations": []}',
                                  GOOD_JSON.replace('"met"', '"maybe"')])
def test_gemini_adapter_rejects_an_unusable_answer(monkeypatch, text):
    obs = gemini(monkeypatch, [FakeResponse(text)])
    with pytest.raises(PerceptionError) as exc:
        obs.observe([jpeg(1)], asked())
    assert exc.value.code == "model_output_invalid" and exc.value.attempts == 1


def test_the_real_get_observer_needs_a_key():
    with pytest.raises(PerceptionError) as exc:
        prep.get_observer(Settings(_env_file=None, gemini_api_key=None))
    assert exc.value.code == "model_not_configured"


# ---------------------------------------------------------------- the whole system
def workflow_0014(tmp_path, monkeypatch, script=None):
    """UNIT-0014 (FBA, returned) through the real orchestrator with our Prep agent in place of the stub."""
    from orchestration.orchestrator import run_workflow
    from orchestration.store import MemoryStore

    row = sample_data.row("prep", "UNIT-0014", ALPHA)
    place(tmp_path, "UNIT-0014", 51)
    sees(monkeypatch, script or Script(marks=tuple(m for m in row["wo_handling_marks"].split(";") if m),
                                       label=row["fnsku"]))
    case = {"org_id": ALPHA, "unit_id": "UNIT-0014", "route": "fba", "returned": True}
    store = MemoryStore()
    wf = run_workflow(case, store=store)
    assert errors("workflow-state", wf) == []
    return case, wf, store, {s["stage"]: s for s in wf["stage_results"]}


def defect_fee_position(store, stages):
    charges = store.get_evidence(stages["recovery"]["record_id"])["payload"]["charges"]
    return {c["charge_type"]: c["position"] for c in charges}["inbound_defect_fee"]


def test_whole_workflow_prep_evidence_reaches_recovery(tmp_path, monkeypatch):
    """Recovery cites our record, and the final outcome lists it."""
    case, wf, store, stages = workflow_0014(tmp_path, monkeypatch)
    assert stages["prep"]["state"] == "completed" and stages["prep"]["outcome"] == "compliant"
    prep_id = stages["prep"]["record_id"]
    prep_ev = store.get_evidence(prep_id)
    assert errors("evidence", prep_ev) == [] and verify(prep_ev)
    assert stages["receiving"]["record_id"] in prep_ev["upstream_refs"], "Prep received Receiving's record"
    assert prep_id in store.get_evidence(stages["recovery"]["record_id"])["upstream_refs"], "Recovery received ours"
    assert prep_id in wf["final_outcome"]["contributing_records"]
    assert defect_fee_position(store, stages) == "CONTRADICTS", "a compliant prep contradicts the defect fee"


def test_a_non_compliant_prep_supports_the_defect_fee_so_no_claim(tmp_path, monkeypatch):
    bad = Script({"fnsku_placement": {"status": "not_met", "evidence": "label on the seam"}},
                 marks=("fragile", "this_way_up"), label=sample_data.row("prep", "UNIT-0014", ALPHA)["fnsku"])
    case, wf, store, stages = workflow_0014(tmp_path, monkeypatch, bad)
    assert stages["prep"]["outcome"] == "non_compliant"
    assert defect_fee_position(store, stages) == "SUPPORTS"


def test_an_override_of_our_prep_verdict_changes_what_recovery_says(tmp_path, monkeypatch):
    """The organisers' contract test of the same name is pinned to their stub; this is its equivalent for ours."""
    from orchestration.clients import client_for
    from tests.conftest import make_input

    case, wf, store, stages = workflow_0014(tmp_path, monkeypatch)
    prior = [store.get_evidence(stages[s]["record_id"]) for s in ("receiving", "prep", "returns")]
    assert prior[1]["decision"]["verdict"] == "PASS"
    ovr = override(prior[1]["record_id"], "FAIL")
    changed = client_for("recovery").run(make_input("recovery", case, prior, [ovr]), 30)["evidence"]
    assert {c["charge_type"]: c["position"] for c in changed["payload"]["charges"]}["inbound_defect_fee"] == "SUPPORTS"
    assert prior[1]["decision"]["verdict"] == "PASS", "the original record is untouched"


def test_workflow_without_a_photo_fails_visibly_with_the_reason():
    from orchestration.orchestrator import run_workflow
    from orchestration.store import MemoryStore

    wf = run_workflow({"org_id": ALPHA, "unit_id": "UNIT-0002", "route": "fba", "returned": False}, store=MemoryStore())
    assert wf["status"] == "FAILED" and wf["final_outcome"]["provisional"] is True
    assert wf["errors"][0]["code"] == "no_capture" and wf["errors"][0]["stage"] == "prep"


def test_check_cli_reports_a_verdict_and_refuses_other_tenants_without_writing(tmp_path, monkeypatch, capsys):
    from agents.prep.check import main

    photo = tmp_path / "front.jpg"
    photo.write_bytes(jpeg(61))
    sees(monkeypatch, Script(label="X00DUMMY002", marks=("fragile",)))
    assert main(["--unit", "UNIT-0002", "--org", ALPHA, str(photo)]) == 0
    assert "COMPLIANT" in capsys.readouterr().out

    assert main(["--unit", "UNIT-0003", "--org", ALPHA, str(photo)]) == 2  # UNIT-0003 belongs to bravo
    assert "Refused" in capsys.readouterr().err
    assert not (tmp_path / "input" / "UNIT-0003").exists(), "a refused request must leave nothing behind"
    assert main(["--unit", "UNIT-0002", "--org", ALPHA, str(tmp_path / "missing.jpg")]) == 2
    assert "not found" in capsys.readouterr().err.lower()
