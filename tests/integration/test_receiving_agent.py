"""Receiving Manager (agents/receiving): behaviour tests on our own fixtures.

The vision model is replaced by a scripted perceiver (and, for the Gemini wrapper, a scripted client). That makes these
tests deterministic and keyless, and they test everything EXCEPT what a real model sees in a real photo: capture
handling, the PO lookup, the deterministic rules, the Round 3 record mapping, fail-open, tenancy, idempotency and the
request sent to the model. Real-model accuracy has never been measured for this agent (see agents/receiving/README.md).
"""
from __future__ import annotations

import io
import json
import random
from types import SimpleNamespace

import pytest
from PIL import Image

from agents.receiving import app as rcv
from agents.receiving import vision
from agents.receiving.config import Settings
from agents.receiving.models import Observation, OrderError, Perception, PurchaseOrderLine
from agents.receiving.orders import resolve_order
from agents.receiving.vision import GeminiPerceiver, PerceptionError
from orchestration.orchestrator import discover_inputs
from shared.utils.hashing import verify
from shared.utils.records import build_record
from shared.utils.schema import errors

REAL_AGENTS = {"receiving"}  # run workflows with the real Receiving, not the organiser stub (tests/conftest.py)
ALPHA, BRAVO = "org_demo_alpha", "org_demo_bravo"
SIX = ["identity_match", "carton_count", "quantity", "carton_damage", "unit_damage", "quality_flags"]
REASONS = {"poor_image", "occluded", "insufficient_evidence", "model_error", "rule_unavailable", "conflicting_evidence",
           "other"}


# ---------------------------------------------------------------- fixtures
def jpeg(seed: int, size=(320, 240)) -> bytes:
    """A photo-like image. Different seed, different bytes."""
    raw = random.Random(seed).randbytes(size[0] * size[1] * 3)
    buf = io.BytesIO()
    Image.frombytes("RGB", size, raw).save(buf, "JPEG", quality=90)
    return buf.getvalue()


def request_for(unit, org, inputs, *, previous=None, overrides=None, rid=None, context=None):
    wf = f"WF-{org}-{unit}"
    return {"schema_version": "1.0", "request_id": rid or f"{wf}:receiving", "workflow_id": wf, "stage": "receiving",
            "subject": {"org_id": org, "subject_id": unit, "route": "unknown"}, "inputs": inputs,
            "previous_evidence": previous or [],
            "context": {"overrides": overrides or [], "case": {}, **(context or {})}}


def po_for(unit, org) -> PurchaseOrderLine:
    return resolve_order(request_for(unit, org, []))[0]


def clean(po: PurchaseOrderLine, **over) -> Observation:
    """An observation that matches the PO exactly. Tests change one thing at a time."""
    base = dict(
        ocr_text=f"{po.sku} {po.product_title}", identified_sku=po.sku, barcode=po.asin,
        cartons_counted=po.cartons_ordered, units_per_carton_counted=po.units_per_carton_ordered,
        quantity_counted=po.qty_ordered, carton_damage="none", unit_damage="none",
        observed_colour=None if po.spec_colour == "n/a" else po.spec_colour, observed_variant=po.spec_variant,
        observed_components=list(po.spec_components), obvious_defect="no", image_clarity=0.9,
        identity_confidence=0.9, count_confidence=0.9, damage_confidence=0.9, spec_confidence=0.9)
    return Observation(**{**base, **over})


class Scripted:
    """A scripted stand-in for the vision model. Records what it was given and how often it was called."""

    def __init__(self, observation: Observation | None = None, exc: Exception | None = None):
        self.observation, self.exc, self.calls, self.photos = observation, exc, 0, []

    def perceive(self, photos):
        self.calls += 1
        self.photos = photos
        if self.exc:
            raise self.exc
        return Perception(observation=self.observation, model_name="scripted-test-model", model_version="0",
                          provider=None, prompt_version="scripted", calls=1, latency_ms=5)


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    """Own capture folder; no key and no .env; sleeps are instant."""
    monkeypatch.setenv("INPUT_DIR", str(tmp_path / "input"))
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    st = Settings(_env_file=None, gemini_api_key=None)
    monkeypatch.setattr(rcv, "settings", lambda: st)
    monkeypatch.setattr(vision.time, "sleep", lambda s: None)


def place(tmp_path, unit, *names_seeds):
    folder = tmp_path / "input" / unit / "receiving"
    folder.mkdir(parents=True, exist_ok=True)
    for name, seed in names_seeds:
        (folder / name).write_bytes(jpeg(seed))
    return discover_inputs(unit, "receiving")


PHOTOS = (("pallet.jpg", 1), ("carton.jpg", 2), ("unit.jpg", 3))


def inspect(tmp_path, monkeypatch, unit, org, observation=None, *, exc=None, photos=PHOTOS, **kw):
    scripted = Scripted(observation, exc)
    monkeypatch.setattr(rcv, "get_perceiver", lambda st: scripted)
    out = rcv.handle(request_for(unit, org, place(tmp_path, unit, *photos), **kw))
    assert errors("agent-output", out) == []
    assert verify(out["evidence"])
    return out, scripted


def by_key(out):
    return {c["check_key"]: c for c in out["evidence"]["checks"]}


# ---------------------------------------------------------------- the four outcomes
def test_matching_delivery_is_accepted(tmp_path, monkeypatch):
    po = po_for("UNIT-0004", ALPHA)  # 2 cartons x 24 = 48
    out, _ = inspect(tmp_path, monkeypatch, "UNIT-0004", ALPHA, clean(po))
    ev, c = out["evidence"], by_key(out)
    assert ev["decision"] == {"verdict": "PASS", "outcome": "accept", "confidence": None,
                              "reason": ev["decision"]["reason"], "needs_human": False}
    assert out["status"] == "completed" and list(c) == SIX and all(x["verdict"] == "PASS" for x in c.values())
    assert all(x["confidence"] is None for x in c.values()), "deterministic checks do not invent a confidence"
    p = ev["payload"]
    assert (p["qty_ordered"], p["qty_received"], p["shortfall_units"], p["overage_units"]) == (48, 48, 0, 0)
    assert p["quality_flags"] == [] and p["shortfall_scope"] == "supplier_inbound"
    assert ev["subject"]["unit_scope"] == "po_line" and ev["subject"]["refs"]["po_number"] == po.po_number


def test_short_shipment_and_crushed_carton_are_accepted_with_exceptions(tmp_path, monkeypatch):
    po = po_for("UNIT-0004", ALPHA)  # 2 x 24
    obs = clean(po, cartons_counted=2, units_per_carton_counted=22, quantity_counted=44, carton_damage="crushing")
    out, _ = inspect(tmp_path, monkeypatch, "UNIT-0004", ALPHA, obs)
    ev, c = out["evidence"], by_key(out)
    assert (ev["decision"]["verdict"], ev["decision"]["outcome"]) == ("FAIL", "accept_with_exceptions")
    assert c["quantity"]["verdict"] == "FAIL" and c["quantity"]["expected"] == 48 and c["quantity"]["observed"] == 44
    assert c["carton_damage"]["verdict"] == "FAIL" and c["carton_damage"]["observed"] == "crushing"
    assert c["carton_count"]["verdict"] == "PASS" and c["identity_match"]["verdict"] == "PASS"
    assert ev["payload"]["shortfall_units"] == 4 and ev["payload"]["qty_received"] == 44
    assert ev["decision"]["needs_human"] is False


def test_short_carton_count_fails_the_carton_check(tmp_path, monkeypatch):
    po = po_for("UNIT-0004", ALPHA)
    obs = clean(po, cartons_counted=1, units_per_carton_counted=24, quantity_counted=24)
    c = by_key(inspect(tmp_path, monkeypatch, "UNIT-0004", ALPHA, obs)[0])
    assert c["carton_count"]["verdict"] == "FAIL" and "short by 1" in c["carton_count"]["detail"]


def test_overage_is_a_failure_too_and_is_reported_as_overage(tmp_path, monkeypatch):
    po = po_for("UNIT-0004", ALPHA)
    obs = clean(po, units_per_carton_counted=25, quantity_counted=50)
    out, _ = inspect(tmp_path, monkeypatch, "UNIT-0004", ALPHA, obs)
    assert by_key(out)["quantity"]["verdict"] == "FAIL"
    assert out["evidence"]["payload"]["overage_units"] == 2 and out["evidence"]["payload"]["shortfall_units"] == 0


def test_wrong_goods_are_rejected(tmp_path, monkeypatch):
    po = po_for("UNIT-0001", ALPHA)
    obs = clean(po, identified_sku="SKU-MUG-11", barcode="B0DUMMY999", ocr_text="SKU-MUG-11 Ceramic Mug")
    out, _ = inspect(tmp_path, monkeypatch, "UNIT-0001", ALPHA, obs)
    ev = out["evidence"]
    assert (ev["decision"]["verdict"], ev["decision"]["outcome"]) == ("FAIL", "reject")
    assert by_key(out)["identity_match"]["verdict"] == "FAIL"
    assert out["next_step_recommendation"]["action"] == "route_to_recovery"


def test_off_spec_goods_raise_named_quality_flags(tmp_path, monkeypatch):
    po = po_for("UNIT-0001", ALPHA)  # blue, bath, towel
    obs = clean(po, observed_colour="red", observed_components=[], obvious_defect="yes")
    out, _ = inspect(tmp_path, monkeypatch, "UNIT-0001", ALPHA, obs)
    q = by_key(out)["quality_flags"]
    assert q["verdict"] == "FAIL" and q["observed"] == ["wrong_colour", "missing_components", "obvious_defect"]
    assert out["evidence"]["payload"]["quality_flags"] == q["observed"]
    assert out["evidence"]["payload"]["missing_components"] == ["towel"]
    assert out["evidence"]["decision"]["outcome"] == "accept_with_exceptions"


def test_spellings_of_the_same_thing_are_not_flagged(tmp_path, monkeypatch):
    """'pack of 3' is '3-pack'; 'Blue ' is 'blue'; 'USB-C cable' contains the required 'cable'."""
    po = po_for("UNIT-0002", ALPHA)  # cream, 3-pack, "candle x3;gift box"
    obs = clean(po, observed_colour=" Cream ", observed_variant="pack of 3", observed_components=["Soy candle", "gift box"])
    assert by_key(inspect(tmp_path, monkeypatch, "UNIT-0002", ALPHA, obs)[0])["quality_flags"]["verdict"] == "PASS"


def test_unit_water_damage_fails_only_the_unit_check(tmp_path, monkeypatch):
    po = po_for("UNIT-0001", ALPHA)
    c = by_key(inspect(tmp_path, monkeypatch, "UNIT-0001", ALPHA, clean(po, unit_damage="water"))[0])
    assert c["unit_damage"]["verdict"] == "FAIL" and c["carton_damage"]["verdict"] == "PASS"


# ---------------------------------------------------------------- UNCERTAIN, with a reason
def test_blurry_photos_are_uncertain_not_a_confident_wrong_count(tmp_path, monkeypatch):
    po = po_for("UNIT-0004", ALPHA)
    obs = clean(po, image_clarity=0.2, cartons_counted=1, quantity_counted=24)  # a "count" read off a blur
    out, _ = inspect(tmp_path, monkeypatch, "UNIT-0004", ALPHA, obs)
    ev = out["evidence"]
    assert (ev["decision"]["verdict"], ev["decision"]["outcome"], ev["decision"]["needs_human"]) == \
           ("UNCERTAIN", "pending_review", True)
    for c in ev["checks"]:
        assert c["verdict"] == "UNCERTAIN" and c["uncertain_reason"] == "poor_image", c["check_key"]
    assert ev["payload"]["qty_received"] is None and ev["payload"]["shortfall_units"] is None, \
        "a count taken from an unusable photo must not be reported as received"


def test_low_confidence_cannot_be_a_pass(tmp_path, monkeypatch):
    po = po_for("UNIT-0001", ALPHA)
    out, _ = inspect(tmp_path, monkeypatch, "UNIT-0001", ALPHA, clean(po, identity_confidence=0.3))
    c = by_key(out)
    assert c["identity_match"]["verdict"] == "UNCERTAIN" and c["identity_match"]["uncertain_reason"] == "insufficient_evidence"
    assert c["quantity"]["verdict"] == "PASS"
    assert out["evidence"]["decision"]["outcome"] == "pending_review"


def test_every_uncertain_check_has_a_reason_from_the_schema_enum(tmp_path, monkeypatch):
    po = po_for("UNIT-0001", ALPHA)
    obs = clean(po, identified_sku=None, barcode=None, ocr_text=None, cartons_counted=None, units_per_carton_counted=None,
                quantity_counted=None, carton_damage="uncertain", unit_damage="uncertain", observed_colour=None,
                observed_variant=None, observed_components=None, obvious_defect="unsure")
    out, _ = inspect(tmp_path, monkeypatch, "UNIT-0001", ALPHA, obs)
    assert all(c["verdict"] == "UNCERTAIN" and c["uncertain_reason"] in REASONS for c in out["evidence"]["checks"])
    assert len(out["evidence"]["checks"]) == 6, "unread is UNCERTAIN, not omitted and not invented"


def test_conflicting_identity_evidence_is_not_resolved_by_picking_a_side(tmp_path, monkeypatch):
    po = po_for("UNIT-0001", ALPHA)
    out, _ = inspect(tmp_path, monkeypatch, "UNIT-0001", ALPHA, clean(po, barcode="B0DUMMY999"))  # SKU text ok, barcode not
    c = by_key(out)["identity_match"]
    assert c["verdict"] == "UNCERTAIN" and c["uncertain_reason"] == "conflicting_evidence"


def test_unfamiliar_barcode_format_says_nothing_about_identity(tmp_path, monkeypatch):
    po = po_for("UNIT-0001", ALPHA)  # an FNSKU is neither our SKU nor our ASIN, and not evidence against either
    assert by_key(inspect(tmp_path, monkeypatch, "UNIT-0001", ALPHA, clean(po, barcode="X001DUMMY600"))[0])[
        "identity_match"]["verdict"] == "PASS"


def test_two_counts_that_disagree_are_uncertain(tmp_path, monkeypatch):
    po = po_for("UNIT-0004", ALPHA)
    obs = clean(po, cartons_counted=2, units_per_carton_counted=24, quantity_counted=40)  # 2 x 24 is 48, not 40
    c = by_key(inspect(tmp_path, monkeypatch, "UNIT-0004", ALPHA, obs)[0])["quantity"]
    assert c["verdict"] == "UNCERTAIN" and c["uncertain_reason"] == "conflicting_evidence"


def test_failure_with_something_unresolved_still_asks_for_a_person(tmp_path, monkeypatch):
    po = po_for("UNIT-0004", ALPHA)
    obs = clean(po, carton_damage="crushing", unit_damage="uncertain")
    ev = inspect(tmp_path, monkeypatch, "UNIT-0004", ALPHA, obs)[0]["evidence"]
    assert ev["decision"]["verdict"] == "FAIL" and ev["decision"]["needs_human"] is True
    assert "unit_damage" in ev["decision"]["reason"]


# ---------------------------------------------------------------- fail open
def test_model_failure_is_a_pending_record_that_keeps_the_photos(tmp_path, monkeypatch):
    out, s = inspect(tmp_path, monkeypatch, "UNIT-0001", ALPHA, exc=PerceptionError("HTTP 503", calls=2))
    ev = out["evidence"]
    assert out["status"] == "pending" and ev["decision"]["outcome"] == "pending_review" and ev["decision"]["verdict"] == "UNCERTAIN"
    assert ev["error"]["code"] == "model_unavailable" and ev["error"]["retryable"] is True
    assert ev["checks"] == [], "nothing was judged, so nothing is invented"
    assert {i["ref"] for i in ev["inputs"]} == {f"UNIT-0001/receiving/{n}" for n, _ in PHOTOS}
    assert all(len(i["sha256"]) == 64 for i in ev["inputs"])
    assert ev["model"]["calls"] == 2 and ev["model"]["name"] != "none", "the failed calls are counted"
    assert out["next_step_recommendation"]["action"] == "retry"


def test_any_model_exception_fails_open_too(tmp_path, monkeypatch):
    out, _ = inspect(tmp_path, monkeypatch, "UNIT-0001", ALPHA, exc=RuntimeError("boom"))
    assert out["status"] == "pending" and out["evidence"]["error"]["code"] == "model_error"
    assert len(out["evidence"]["inputs"]) == 3


def test_no_api_key_is_a_pending_record_not_a_guess(tmp_path):
    out = rcv.handle(request_for("UNIT-0001", ALPHA, place(tmp_path, "UNIT-0001", *PHOTOS)))  # real get_perceiver, no key
    ev = out["evidence"]
    assert errors("agent-output", out) == [] and out["status"] == "pending"
    assert ev["error"]["code"] == "model_not_configured" and ev["model"]["calls"] == 0 and ev["model"]["name"] == "none"
    assert ev["checks"] == [] and len(ev["inputs"]) == 3


def test_no_capture_is_pending_and_never_calls_the_model(tmp_path, monkeypatch):
    scripted = Scripted(clean(po_for("UNIT-0001", ALPHA)))
    monkeypatch.setattr(rcv, "get_perceiver", lambda st: scripted)
    out = rcv.handle(request_for("UNIT-0001", ALPHA, []))
    assert errors("agent-output", out) == [] and verify(out["evidence"])
    assert out["status"] == "pending" and out["evidence"]["error"]["code"] == "no_capture" and scripted.calls == 0


def test_bad_po_data_is_pending_not_an_exception(tmp_path, monkeypatch):
    order = {"org_id": ALPHA, "po_number": "PO-1", "po_line": 1, "supplier": "S", "sku": "SKU-X", "cartons_ordered": 2,
             "units_per_carton_ordered": 10, "qty_ordered": 99}
    out, _ = inspect(tmp_path, monkeypatch, "UNIT-0001", ALPHA, clean(po_for("UNIT-0001", ALPHA)), context={"order": order})
    assert out["evidence"]["error"]["code"] == "order_invalid" and out["evidence"]["error"]["retryable"] is False
    assert out["status"] == "error"
    with pytest.raises(OrderError):
        PurchaseOrderLine.parse({**order, "cartons_ordered": 0})


def test_the_stage_never_raises_whatever_the_model_does(tmp_path, monkeypatch):
    for exc in (PerceptionError("x"), ValueError("x"), KeyError("x"), TimeoutError("x")):
        out, _ = inspect(tmp_path, monkeypatch, "UNIT-0001", ALPHA, exc=exc)
        assert out["status"] == "pending"


# ---------------------------------------------------------------- tenancy and captures
def test_another_organisations_unit_is_refused_before_anything_is_read(tmp_path, monkeypatch):
    scripted = Scripted(clean(po_for("UNIT-0003", BRAVO)))
    monkeypatch.setattr(rcv, "get_perceiver", lambda st: scripted)
    inputs = place(tmp_path, "UNIT-0003", *PHOTOS)
    with pytest.raises(LookupError):
        rcv.handle(request_for("UNIT-0003", ALPHA, inputs))  # UNIT-0003 is bravo's
    with pytest.raises(LookupError):
        rcv.handle(request_for("UNIT-9999", ALPHA, inputs))
    assert scripted.calls == 0
    assert rcv.handle(request_for("UNIT-0003", BRAVO, inputs))["status"] == "completed"  # bravo's own unit is answered


def test_a_context_order_for_another_organisation_is_ignored(tmp_path, monkeypatch):
    order = {"org_id": BRAVO, "po_number": "PO-1", "po_line": 1, "supplier": "S", "sku": "SKU-X", "cartons_ordered": 1,
             "units_per_carton_ordered": 1, "qty_ordered": 1}
    with pytest.raises(LookupError):  # UNIT-0003 is not alpha's; bravo's order must not rescue the request
        rcv.handle(request_for("UNIT-0003", ALPHA, place(tmp_path, "UNIT-0003", *PHOTOS), context={"order": order}))


def test_context_order_is_used_when_it_belongs_to_the_caller(tmp_path, monkeypatch):
    order = {"org_id": ALPHA, "po_number": "PO-8", "po_line": 3, "supplier": "Acme", "sku": "SKU-NEW", "asin": "B0DUMMY111",
             "product_title": "Thing", "spec_colour": "n/a", "spec_variant": "standard", "spec_components": [],
             "cartons_ordered": 3, "units_per_carton_ordered": 5, "qty_ordered": 15}
    po = PurchaseOrderLine.parse(order)
    out, _ = inspect(tmp_path, monkeypatch, "UNIT-0001", ALPHA, clean(po), context={"order": order})
    ev = out["evidence"]
    assert ev["decision"]["outcome"] == "accept" and ev["payload"]["supplier"] == "Acme" and ev["subject"]["refs"]["sku"] == "SKU-NEW"
    assert set(by_key(out)) == {"identity_match", "carton_count", "quantity", "carton_damage", "unit_damage", "quality_flags"}


def test_the_sample_answer_columns_are_never_read(tmp_path, monkeypatch):
    """UNIT-0003 in the organisers' CSV has crushing, a 44-of-48 shortfall and a missing carton. Those are the answers a
    stub replays. We look at photos: a clean observation of the same unit must come out clean."""
    po = po_for("UNIT-0003", BRAVO)
    out, _ = inspect(tmp_path, monkeypatch, "UNIT-0003", BRAVO, clean(po))
    assert out["evidence"]["decision"]["outcome"] == "accept" and out["evidence"]["payload"]["shortfall_units"] == 0
    assert not {"qty_received", "cartons_received", "identity_match", "carton_damage"} & set(PurchaseOrderLine.model_fields)


def test_tampered_capture_is_refused_and_says_so(tmp_path, monkeypatch):
    inputs = place(tmp_path, "UNIT-0001", *PHOTOS)
    (tmp_path / "input" / "UNIT-0001" / "receiving" / "carton.jpg").write_bytes(jpeg(99))  # changed after it was hashed
    scripted = Scripted(clean(po_for("UNIT-0001", ALPHA)))
    monkeypatch.setattr(rcv, "get_perceiver", lambda st: scripted)
    out = rcv.handle(request_for("UNIT-0001", ALPHA, inputs))
    ev = out["evidence"]
    assert errors("agent-output", out) == [] and out["status"] == "error"
    assert ev["error"]["code"] == "capture_unreadable" and ev["error"]["retryable"] is False
    assert "sha256" in ev["error"]["message"] and "carton.jpg" in ev["error"]["message"]
    assert scripted.calls == 0, "an altered photo must never reach the model"


@pytest.mark.parametrize("ref", ["../../etc/passwd", "UNIT-0002/receiving/pallet.jpg", "UNIT-0001/pack/pallet.jpg"])
def test_captures_must_be_inside_this_units_receiving_folder(tmp_path, monkeypatch, ref):
    root = tmp_path / "input"
    for sub in ("UNIT-0002/receiving", "UNIT-0001/pack"):
        (root / sub).mkdir(parents=True, exist_ok=True)
        (root / sub / "pallet.jpg").write_bytes(jpeg(5))
    scripted = Scripted(clean(po_for("UNIT-0001", ALPHA)))
    monkeypatch.setattr(rcv, "get_perceiver", lambda st: scripted)
    out = rcv.handle(request_for("UNIT-0001", ALPHA, [{"ref": ref, "kind": "image", "sha256": None}]))
    assert out["evidence"]["error"]["code"] == "capture_unreadable" and scripted.calls == 0


def test_a_file_that_is_not_an_image_is_unreadable(tmp_path, monkeypatch):
    folder = tmp_path / "input" / "UNIT-0001" / "receiving"
    folder.mkdir(parents=True)
    (folder / "pallet.jpg").write_bytes(b"this is not a jpeg")
    scripted = Scripted(clean(po_for("UNIT-0001", ALPHA)))
    monkeypatch.setattr(rcv, "get_perceiver", lambda st: scripted)
    out = rcv.handle(request_for("UNIT-0001", ALPHA, discover_inputs("UNIT-0001", "receiving")))
    assert out["evidence"]["error"]["code"] == "capture_unreadable" and scripted.calls == 0


def test_photos_beyond_the_limit_are_listed_not_silently_dropped(tmp_path, monkeypatch):
    st = Settings(_env_file=None, gemini_api_key=None, max_photos=2)
    monkeypatch.setattr(rcv, "settings", lambda: st)
    out, s = inspect(tmp_path, monkeypatch, "UNIT-0001", ALPHA, clean(po_for("UNIT-0001", ALPHA)))
    assert len(s.photos) == 2 and len(out["evidence"]["inputs"]) == 2
    assert out["evidence"]["payload"]["photos_not_used"] == ["UNIT-0001/receiving/unit.jpg"]


# ---------------------------------------------------------------- idempotency, model block, overrides
def test_same_request_same_record_id_and_a_rerun_gets_a_new_one(tmp_path, monkeypatch):
    po = po_for("UNIT-0001", ALPHA)
    a, _ = inspect(tmp_path, monkeypatch, "UNIT-0001", ALPHA, clean(po))
    b, _ = inspect(tmp_path, monkeypatch, "UNIT-0001", ALPHA, clean(po))
    r, _ = inspect(tmp_path, monkeypatch, "UNIT-0001", ALPHA, clean(po), rid="WF-org_demo_alpha-UNIT-0001:receiving:r2")
    assert a["evidence"]["record_id"] == b["evidence"]["record_id"] == "RCV-WF-org_demo_alpha-UNIT-0001-receiving"
    assert r["evidence"]["record_id"] != a["evidence"]["record_id"]
    assert a["evidence"]["checks"] == b["evidence"]["checks"]
    p1 = rcv.handle(request_for("UNIT-0001", ALPHA, []))["evidence"]["record_id"]
    assert p1 == rcv.handle(request_for("UNIT-0001", ALPHA, []))["evidence"]["record_id"] and p1.startswith("RCV-")


def test_one_batched_call_per_unit_with_every_photo(tmp_path, monkeypatch):
    out, s = inspect(tmp_path, monkeypatch, "UNIT-0001", ALPHA, clean(po_for("UNIT-0001", ALPHA)))
    assert s.calls == 1 and len(s.photos) == 3, "one call, carrying every photo"
    assert {p.view for p in s.photos} == {"pallet", "carton", "unit"}
    assert out["evidence"]["model"]["calls"] == 1


def test_model_block_describes_what_actually_ran(tmp_path, monkeypatch):
    out, _ = inspect(tmp_path, monkeypatch, "UNIT-0001", ALPHA, clean(po_for("UNIT-0001", ALPHA)))
    m = out["evidence"]["model"]
    assert m["name"] == "scripted-test-model" and m["provider"] is None and m["calls"] == 1 and m["cost_usd"] is None
    assert out["model"] == m
    pa = out["evidence"]["payload"]["photos"][0]
    assert pa["original_sha256"] == out["evidence"]["inputs"][0]["sha256"], "inputs[] carries the hash the orchestrator computed"


def test_an_earlier_receiving_record_is_cited_with_its_latest_override(tmp_path, monkeypatch):
    po = po_for("UNIT-0001", ALPHA)
    first, _ = inspect(tmp_path, monkeypatch, "UNIT-0001", ALPHA, clean(po, carton_damage="tears"))
    old = first["evidence"]
    override = {"override_id": "OVR-1", "supersedes": {"record_id": old["record_id"], "override_id": None},
                "target": "decision", "actor": "t", "at": "2026-01-01T00:00:00Z", "reason": "recounted",
                "original_verdict": "FAIL", "previous_verdict": "FAIL", "new_verdict": "PASS"}
    out, _ = inspect(tmp_path, monkeypatch, "UNIT-0001", ALPHA, clean(po), previous=[old], overrides=[override],
                     rid="WF-org_demo_alpha-UNIT-0001:receiving:r2")
    ev = out["evidence"]
    assert ev["upstream_refs"] == [old["record_id"]]
    assert ev["payload"]["previous_receiving"] == {"record_id": old["record_id"], "verdict": "FAIL", "effective_verdict": "PASS"}
    assert ev["decision"]["outcome"] == "accept", "the new photos decide; the old verdict is cited, not copied"


# ---------------------------------------------------------------- the Gemini wrapper, with a scripted client (not the real API)
class FakeClient:
    def __init__(self, replies):
        self.replies, self.requests = list(replies), []
        self.models = SimpleNamespace(generate_content=self._generate)

    def _generate(self, model, contents, config):
        self.requests.append({"model": model, "contents": contents, "config": config})
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


def answer(po, **over):
    return SimpleNamespace(text=clean(po, **over).model_dump_json(), model_version="fake-model-001",
                           usage_metadata=SimpleNamespace(prompt_token_count=1000, candidates_token_count=200,
                                                          thoughts_token_count=0))


def photos_of(tmp_path):
    from agents.receiving import captures
    inputs = place(tmp_path, "UNIT-0001", *PHOTOS)
    return [captures.prepare(i, (tmp_path / "input" / i["ref"]).read_bytes(), 1600) for i in inputs]


def api_error(code):
    from google.genai import errors as gerrors
    return gerrors.APIError(code, {"error": {"message": "x", "status": "X"}})


def test_the_request_to_the_model_carries_the_photos_and_never_the_order(tmp_path):
    po = po_for("UNIT-0001", ALPHA)
    client = FakeClient([answer(po)])
    perception = GeminiPerceiver(Settings(_env_file=None, gemini_api_key=None), client=client).perceive(photos_of(tmp_path))
    assert len(client.requests) == 1 and perception.calls == 1
    contents = client.requests[0]["contents"]
    assert sum(1 for c in contents if not isinstance(c, str)) == 3, "three photos, as bytes"
    assert all(c.inline_data.mime_type == "image/jpeg" and c.inline_data.data for c in contents if not isinstance(c, str))
    text = " ".join(c for c in contents if isinstance(c, str)) + client.requests[0]["config"].system_instruction
    for secret in (po.sku, po.asin, po.product_title, str(po.qty_ordered), po.spec_colour, po.spec_variant, "Supplier"):
        assert secret.lower() not in text.lower(), f"the model must not be told the order ({secret!r})"
    assert perception.model_version == "fake-model-001" and perception.provider == "google"
    assert perception.usage["input_tokens"] == 1000 and perception.observation.identified_sku == po.sku


def test_cost_is_reported_only_when_prices_are_configured(tmp_path):
    from agents.receiving.vision import cost_usd
    usage = {"input_tokens": 1_000_000, "output_tokens": 500_000}
    assert cost_usd(Settings(_env_file=None), usage) is None
    assert cost_usd(Settings(_env_file=None, cost_per_1m_input_usd=0.1, cost_per_1m_output_usd=0.4), usage) == pytest.approx(0.3)


def test_a_throttled_call_is_retried_once_and_both_calls_are_counted(tmp_path):
    po = po_for("UNIT-0001", ALPHA)
    client = FakeClient([api_error(429), answer(po)])
    p = GeminiPerceiver(Settings(_env_file=None, gemini_api_key=None), client=client).perceive(photos_of(tmp_path))
    assert p.calls == 2 and len(client.requests) == 2


def test_persistent_failure_raises_with_the_call_count(tmp_path):
    client = FakeClient([api_error(503), api_error(503)])
    with pytest.raises(PerceptionError) as exc:
        GeminiPerceiver(Settings(_env_file=None, gemini_api_key=None), client=client).perceive(photos_of(tmp_path))
    assert exc.value.calls == 2


def test_a_client_error_is_not_retried(tmp_path):
    client = FakeClient([api_error(400)])
    with pytest.raises(PerceptionError) as exc:
        GeminiPerceiver(Settings(_env_file=None, gemini_api_key=None), client=client).perceive(photos_of(tmp_path))
    assert exc.value.calls == 1 and len(client.requests) == 1


@pytest.mark.parametrize("text", ["", "not json", json.dumps({"cartons_counted": 2})])
def test_a_malformed_answer_fails_open_instead_of_being_guessed_at(tmp_path, text):
    client = FakeClient([SimpleNamespace(text=text, model_version="m", usage_metadata=None)])
    with pytest.raises(PerceptionError):
        GeminiPerceiver(Settings(_env_file=None, gemini_api_key=None), client=client).perceive(photos_of(tmp_path))


def test_gemini_failure_through_the_whole_agent_is_a_pending_record(tmp_path, monkeypatch):
    client = FakeClient([api_error(503), api_error(503)])
    monkeypatch.setattr(rcv, "get_perceiver", lambda st: GeminiPerceiver(st, client=client))
    out = rcv.handle(request_for("UNIT-0001", ALPHA, place(tmp_path, "UNIT-0001", *PHOTOS)))
    assert out["status"] == "pending" and out["evidence"]["error"]["code"] == "model_unavailable"
    assert out["evidence"]["model"]["calls"] == 2 and out["evidence"]["model"]["provider"] == "google"


def test_the_model_time_budget_fits_inside_the_orchestrators_stage_timeout():
    from orchestration.orchestrator import load_flow
    worst = (vision.MODEL_RETRIES + 1) * vision.MODEL_TIMEOUT_S + sum(2 * (i + 1) for i in range(vision.MODEL_RETRIES))
    assert worst < load_flow()["defaults"]["timeout_s"]


# ---------------------------------------------------------------- the pod
def test_manifest_says_what_this_agent_is():
    from pathlib import Path
    manifest = json.loads((Path(__file__).resolve().parents[2] / "agents" / "receiving" / "agent.json").read_text())
    assert manifest["owner"] == "@cherryy-x23" and manifest["implementation"] != "organiser-stub"
    assert manifest["agent_id"] == "receiving-manager@0.1.0" == rcv.adapter.AGENT_ID
    # Real runs happened (logged); accuracy has not been measured, and the manifest must say both, not more.
    assert "docs/REAL-RUNS.md" in manifest["implementation"]
    assert "No accuracy has been measured" in manifest["implementation"]


def test_whole_workflow_records_our_receiving_record_and_hands_it_on(tmp_path, monkeypatch):
    """A real orchestrated workflow: Receiving (ours, scripted model) then the organiser stubs. Prep must receive our record."""
    from orchestration.orchestrator import run_workflow
    from orchestration.store import MemoryStore

    case = {"org_id": ALPHA, "unit_id": "UNIT-0004", "route": "unknown", "returned": False}
    po = po_for("UNIT-0004", ALPHA)
    place(tmp_path, "UNIT-0004", *PHOTOS)
    monkeypatch.setattr(rcv, "get_perceiver", lambda st: Scripted(clean(po, units_per_carton_counted=23, quantity_counted=46)))
    store = MemoryStore()
    wf = run_workflow(case, store=store)
    assert errors("workflow-state", wf) == []
    stage = {s["stage"]: s for s in wf["stage_results"]}["receiving"]
    assert stage["state"] == "completed" and stage["outcome"] == "accept_with_exceptions" and stage["verdict"] == "FAIL"
    rcv_id = stage["record_id"]
    assert errors("evidence", store.get_evidence(rcv_id)) == []
    later = store.get_evidence({s["stage"]: s for s in wf["stage_results"]}["recovery"]["record_id"])
    assert rcv_id in later["upstream_refs"] and rcv_id in wf["final_outcome"]["contributing_records"]


def test_workflow_without_photos_shows_the_reason(tmp_path):
    from orchestration.orchestrator import run_workflow
    from orchestration.store import MemoryStore

    wf = run_workflow({"org_id": ALPHA, "unit_id": "UNIT-0004", "route": "unknown", "returned": False}, store=MemoryStore())
    assert wf["errors"][0]["code"] == "no_capture" and wf["errors"][0]["stage"] == "receiving"
    assert wf["final_outcome"]["provisional"] is True


def test_check_cli_reports_and_refuses_other_tenants_without_writing(tmp_path, monkeypatch, capsys):
    from agents.receiving.check import main

    po = po_for("UNIT-0004", ALPHA)
    monkeypatch.setattr(rcv, "get_perceiver", lambda st: Scripted(clean(po)))
    photo = tmp_path / "my carton photo.jpg"
    photo.write_bytes(jpeg(61))
    assert main(["--unit", "UNIT-0004", "--org", ALPHA, str(photo)]) == 0
    out = capsys.readouterr().out
    assert "ACCEPT" in out and "identity_match" in out
    assert main(["--unit", "UNIT-0003", "--org", ALPHA, str(photo)]) == 2  # bravo's unit
    assert "Refused" in capsys.readouterr().err and not (tmp_path / "input" / "UNIT-0003").exists()
    assert main(["--unit", "UNIT-0004", "--org", ALPHA, str(tmp_path / "missing.jpg")]) == 2
    assert main(["--unit", "UNIT-0004", "--org", ALPHA, "--json", str(photo)]) == 0
    assert json.loads(capsys.readouterr().out)["stage"] == "receiving"


def test_pending_output_is_buildable_with_the_shared_helper_shape():
    """Sanity: our pending record has the same required shape as shared.utils.records.pending_output."""
    req = request_for("UNIT-0001", ALPHA, [])
    ours = rcv.handle(req)["evidence"]
    ref = build_record(req, agent_id="x", record_id="RCV-PENDING-x", captured_at="2026-01-01T00:00:00Z", checks=[],
                       outcome="pending_review", reason="r", model={"name": "none", "version": "0"}, verdict="UNCERTAIN",
                       needs_human=True, status="pending")
    assert set(ref) <= set(ours)
    assert (ours["status"], ours["decision"]["needs_human"]) == ("pending", True)
