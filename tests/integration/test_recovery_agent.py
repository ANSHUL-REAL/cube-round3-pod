"""Recovery Manager (agents/recovery): behaviour tests on our own fixtures.

No model is involved (Recovery is deterministic rules), so there is nothing to script and no key is needed. The fee
lines and the earlier stages' evidence are built here, not read from the organisers' sample, except in the
whole-workflow tests at the end, which run the real orchestrator over the sample units.

These tests check that the rules do what the README says. They do NOT measure how many real claims would be right:
there are no ground-truth labels (see agents/recovery/REPLAY.md).
"""
from __future__ import annotations

import csv
import dataclasses
import json
import os
import re
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agents.recovery import adapter, fees, rules
from agents.recovery import app as recovery
from agents.recovery import policy as policy_module
from shared.utils.hashing import verify
from shared.utils.records import build_record, check
from shared.utils.schema import errors

REAL_AGENTS = {"recovery"}  # these run as the real agents here; every other stage runs on the organiser stub (tests/conftest.py)

ORG, OTHER, UNIT = "org_t", "org_other", "UNIT-T1"
ROOT = Path(__file__).resolve().parents[2]
SPECIALIST_FLOW = ROOT / "orchestration" / "flow.specialist.json"
KEY = re.compile(r"^[a-z][a-z0-9_]*$")
FEE_COLUMNS = ["line_id", "report_type", "unit_id", "org_id", "sku", "fnsku", "fba_shipment_id", "order_id",
               "charge_type", "quantity", "amount_usd", "posted_date"]


# ---------------------------------------------------------------- fixtures: fee lines
def fee(line_id="FEE-T1-1", charge_type="inbound_defect_fee", amount="2.00", **kw) -> dict:
    row = {"line_id": line_id, "report_type": "fee_report", "unit_id": UNIT, "org_id": ORG, "sku": "SKU-A", "fnsku": "X1",
           "fba_shipment_id": "FBA-T1", "order_id": "", "charge_type": charge_type, "quantity": "1",
           "amount_usd": amount, "posted_date": "2026-07-10"}
    return {**row, **kw}


def write_world(tmp_path, monkeypatch, rows: list[dict], known: list[tuple[str, str]] = ()) -> None:
    """Our own organiser-style data directory: a fee report, and (optionally) units known to Receiving but fee-less."""
    with open(tmp_path / "fee_report_sample.csv", "w", newline="", encoding="utf-8") as fh:
        extra = sorted({k for r in rows for k in r} - set(FEE_COLUMNS))  # optional columns such as related_line_id
        w = csv.DictWriter(fh, fieldnames=[*FEE_COLUMNS, *extra], restval="")
        w.writeheader()
        w.writerows(rows)
    with open(tmp_path / "receiving_sample.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=["unit_id", "org_id"])
        w.writeheader()
        w.writerows({"unit_id": u, "org_id": o} for u, o in known)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    monkeypatch.delenv("RECOVERY_TIER_TABLE", raising=False)
    monkeypatch.delenv("ORCH_FLOW", raising=False)
    monkeypatch.setattr(policy_module, "POLICY", policy_module.Policy())  # tests may flip a switch; always restored


# ---------------------------------------------------------------- fixtures: earlier stages' evidence
def request(previous=None, overrides=None, *, unit=UNIT, org=ORG, route="fba", rid=None, inputs=None) -> dict:
    wf = f"WF-{org}-{unit}"
    return {"schema_version": "1.0", "request_id": rid or f"{wf}:recovery", "workflow_id": wf, "stage": "recovery",
            "subject": {"org_id": org, "subject_id": unit, "route": route}, "inputs": inputs or [],
            "previous_evidence": previous or [], "context": {"overrides": overrides or [], "case": {}}}


PREFIX = {"receiving": "RCV", "prep": "PRP", "pack": "PCK", "returns": "RTN"}


def upstream(stage, verdict="PASS", *, rid=None, checks=None, status="completed", refs=None, payload=None,
             captured="2026-06-01T00:00:00Z", unit=UNIT, org=ORG, model="rules", outcome=None) -> dict:
    """An earlier stage's Evidence Record, built the way that stage's agent would."""
    if checks is None:
        checks = [check("c1", verdict, None, uncertain_reason="poor_image")]
    rid = rid or f"{PREFIX[stage]}-{unit}"
    req = {"workflow_id": f"WF-{org}-{unit}", "stage": stage, "subject": {"org_id": org, "subject_id": unit},
           "previous_evidence": []}
    return build_record(req, agent_id=f"{stage}-fixture@1", record_id=rid, captured_at=captured, checks=checks,
                        outcome=outcome or {"PASS": "ok", "FAIL": "bad", "UNCERTAIN": "pending_review"}[verdict],
                        reason="fixture", model={"name": model, "version": "1"}, status=status, verdict=verdict,
                        refs=refs if refs is not None else {"sku": "SKU-A", "fnsku": "X1", "fba_shipment_id": "FBA-T1"},
                        payload=payload, needs_human=False)


def prep(verdict="PASS", **kw):
    checks = {"PASS": [check("polybag_sealed", "PASS", None), check("fnsku_label_placement", "PASS", None)],
              "FAIL": [check("polybag_sealed", "PASS", None), check("fnsku_label_placement", "FAIL", None)],
              "UNCERTAIN": [check("polybag_sealed", "UNCERTAIN", None, uncertain_reason="poor_image")]}[verdict]
    return upstream("prep", verdict, checks=kw.pop("checks", checks), **kw)


def receiving(damage="PASS", **kw):
    checks = [check("quantity", "PASS", None), check("carton_damage", "PASS", None),
              check("unit_damage", damage, None, uncertain_reason="poor_image"), check("quality_flags", "PASS", None)]
    return upstream("receiving", damage if damage != "PASS" else "PASS", checks=checks,
                    payload=kw.pop("payload", {"shortfall_units": 0}), refs={}, **kw)


def returns(identity="PASS", **kw):
    checks = [check("identity_match", identity, None, uncertain_reason="poor_image"), check("completeness", "PASS", None)]
    return upstream("returns", identity, checks=checks, refs=kw.pop("refs", {"order_id": "ORD-T1", "sku": "SKU-A"}), **kw)


def override(record_id, new, *, n=1, actor="op_t", prev=None, original="PASS", earlier=None) -> dict:
    return {"override_id": f"OVR-{n:03d}", "supersedes": {"record_id": record_id, "override_id": earlier}, "target": "decision",
            "actor": actor, "at": "2026-08-01T00:00:00Z", "reason": "checked the unit by hand", "original_verdict": original,
            "previous_verdict": prev or original, "new_verdict": new}


# ---------------------------------------------------------------- helpers
def run(tmp_path, monkeypatch, rows, previous=None, overrides=None, *, route="fba", known=(), **kw) -> dict:
    write_world(tmp_path, monkeypatch, rows, known)
    out = recovery.handle(request(previous, overrides, route=route, **kw))
    assert errors("agent-output", out) == [], errors("agent-output", out)
    assert errors("evidence", out["evidence"]) == [] and verify(out["evidence"])
    return out


def charges(out) -> dict:
    return {c["line_id"]: c for c in out["evidence"]["payload"]["charges"]}


def the_check(out, n=0) -> dict:
    return out["evidence"]["checks"][n]


def assert_no_claim(out):
    ev = out["evidence"]
    assert ev["payload"]["claims"] == [] and ev["payload"]["claimable_usd"] == 0
    assert all(c["verdict"] != "FAIL" for c in ev["checks"]) and ev["decision"]["verdict"] != "FAIL"
    assert ev["decision"]["outcome"] != "claim_recommended"


# ================================================================ the positions
def test_prep_pass_contradicts_an_inbound_defect_fee_and_files_a_claim(tmp_path, monkeypatch):
    previous = [receiving(), prep("PASS")]
    out = run(tmp_path, monkeypatch, [fee(amount="2.00")], previous)
    ev, c = out["evidence"], the_check(out)
    assert (c["check_key"], c["verdict"], c["observed"]) == ("charge_fee_t1_1", "FAIL", "CONTRADICTS")
    assert ev["decision"]["verdict"] == "FAIL" and ev["decision"]["outcome"] == "claim_recommended"
    assert ev["decision"]["needs_human"] is False and out["next_step_recommendation"]["action"] == "complete"
    claim = ev["payload"]["claims"][0]
    assert claim["claim_usd"] == 2.0 == ev["payload"]["claimable_usd"]
    assert claim["evidence_record_ids"] == [f"PRP-{UNIT}", f"RCV-{UNIT}"], "a claim cites every record it rests on"
    assert set(claim["evidence_record_ids"]) <= set(c["evidence_refs"]) and c["evidence_refs"][0] == "fee_report/FEE-T1-1"
    row = next(i for i in ev["inputs"] if i["ref"] == "fee_report/FEE-T1-1")
    assert row["kind"] == "csv_row" and re.fullmatch(r"[a-f0-9]{64}", row["sha256"]), "the fee row examined is hashed"
    assert ev["upstream_refs"] == [f"RCV-{UNIT}", f"PRP-{UNIT}"]
    assert ev["model"]["name"] == "rules" and ev["model"]["calls"] == 0, "a rules engine is not called a model"
    assert ev["payload"]["charges"][0]["basis"][0]["role"] == "supports_claim"


def test_prep_fail_supports_the_charge_so_nothing_is_claimed(tmp_path, monkeypatch):
    out = run(tmp_path, monkeypatch, [fee()], [receiving(), prep("FAIL")])
    c = the_check(out)
    assert (c["verdict"], c["observed"]) == ("PASS", "SUPPORTS") and "fnsku_label_placement" in c["detail"]
    assert out["evidence"]["decision"]["outcome"] == "no_claim" and out["evidence"]["decision"]["verdict"] == "PASS"
    assert_no_claim(out)


def test_prep_uncertain_is_silent_never_a_claim_and_asks_no_one(tmp_path, monkeypatch):
    out = run(tmp_path, monkeypatch, [fee()], [receiving(), prep("UNCERTAIN")])
    c, ev = the_check(out), out["evidence"]
    assert (c["verdict"], c["observed"], c["uncertain_reason"]) == ("UNCERTAIN", "SILENT", "insufficient_evidence")
    assert ev["decision"]["outcome"] == "insufficient_evidence" and ev["decision"]["needs_human"] is False
    assert ev["payload"]["unclaimable"][0]["line_id"] == "FEE-T1-1" and ev["payload"]["unclaimable"][0]["reason"]
    assert_no_claim(out)


def test_every_line_gets_exactly_one_check_with_a_valid_unique_key(tmp_path, monkeypatch):
    rows = [fee("FEE-A.1", "fulfilment_fee_weight_tier", "4.25"), fee("fee a 1", "lost_inbound", "0.00"),
            fee("FEE-A-1", "inbound_defect_fee", "1.00"), fee("1", "refund_issued_item_not_returned", "0.00")]
    out = run(tmp_path, monkeypatch, rows, [receiving(), prep("PASS")])
    keys = [c["check_key"] for c in out["evidence"]["checks"]]
    assert len(keys) == len(rows) == len(set(keys)) and all(KEY.match(k) for k in keys), keys
    assert [c["line_id"] for c in out["evidence"]["payload"]["charges"]] == [r["line_id"] for r in rows]


def test_a_unit_with_no_fee_lines_is_uncertain_not_clean(tmp_path, monkeypatch):
    out = run(tmp_path, monkeypatch, [], known=[(UNIT, ORG)])
    ev = out["evidence"]
    assert ev["checks"] == [] and ev["decision"]["verdict"] == "UNCERTAIN" and ev["decision"]["outcome"] == "no_claim"
    assert ev["decision"]["needs_human"] is False and "not evidence" in ev["decision"]["reason"]


def test_an_unknown_charge_type_is_left_alone(tmp_path, monkeypatch):
    out = run(tmp_path, monkeypatch, [fee(charge_type="storage_fee_unheard_of")], [receiving(), prep("PASS")])
    assert the_check(out)["observed"] == "SILENT" and charges(out)["FEE-T1-1"]["codes"] == ["no_rule"]
    assert_no_claim(out)


# ================================================================ precision: the wrong claims it must not file
def prep_after_the_charge():
    return prep("PASS", captured="2026-07-20T00:00:00Z")  # the fee was posted 2026-07-10


@pytest.mark.parametrize("name, previous, line, code", [
    ("receiving recorded a defect", [receiving("FAIL"), prep("PASS")], {}, "receiving_defect_conflict"),
    ("receiving could not rule one out", [receiving("UNCERTAIN"), prep("PASS")], {}, "receiving_defect_unresolved"),
    ("receiving did not complete", [receiving(status="pending"), prep("PASS")], {}, "receiving_defect_unresolved"),
    ("prep is about another SKU", [receiving(), prep("PASS", refs={"sku": "SKU-OTHER", "fnsku": "X1"})], {},
     "evidence_identity_mismatch"),
    ("prep is about another shipment", [receiving(), prep("PASS", refs={"fba_shipment_id": "FBA-ZZ"})], {},
     "evidence_identity_mismatch"),
    ("prep was taken after the charge", [receiving(), prep_after_the_charge()], {}, "evidence_after_charge"),
    ("prep never completed", [receiving(), prep("PASS", status="pending")], {}, "prep_not_completed"),
    ("prep passed with no checks", [receiving(), prep("PASS", checks=[])], {}, "prep_no_checks"),
    ("line covers two units, prep saw one", [receiving(), prep("PASS")], {"quantity": "2"}, "quantity_scope"),
    ("a zero-amount line", [receiving(), prep("PASS")], {"amount_usd": "0.00"}, "amount_zero"),
    ("an unreadable amount", [receiving(), prep("PASS")], {"amount_usd": "n/a"}, "amount_missing"),
    ("a negative amount", [receiving(), prep("PASS")], {"amount_usd": "-2.00"}, "amount_negative"),
])
def test_precision_cases_where_a_claim_would_be_wrong_are_not_filed(tmp_path, monkeypatch, name, previous, line, code):
    out = run(tmp_path, monkeypatch, [fee(**line)], previous)
    assert the_check(out)["verdict"] == "UNCERTAIN", name
    assert code in charges(out)["FEE-T1-1"]["codes"], (name, charges(out)["FEE-T1-1"]["codes"])
    assert_no_claim(out)


def test_a_conflict_over_money_asks_a_person_but_never_claims(tmp_path, monkeypatch):
    out = run(tmp_path, monkeypatch, [fee(amount="2.00")], [receiving("FAIL"), prep("PASS")])
    ev, c = out["evidence"], the_check(out)
    assert c["uncertain_reason"] == "conflicting_evidence"
    assert ev["decision"]["outcome"] == "pending_review" and ev["decision"]["needs_human"] is True
    assert out["next_step_recommendation"]["action"] == "review" and ev["status"] == "completed"
    assert charges(out)["FEE-T1-1"]["what_would_settle_it"]


def test_a_conflict_on_a_zero_amount_line_does_not_bother_anyone(tmp_path, monkeypatch):
    out = run(tmp_path, monkeypatch, [fee(amount="0.00")], [receiving("FAIL"), prep("PASS")])
    assert out["evidence"]["decision"]["needs_human"] is False


def test_the_conflict_policy_can_be_switched_and_the_record_says_which_side_it_took(tmp_path, monkeypatch):
    monkeypatch.setattr(policy_module, "POLICY", policy_module.Policy(receiving_defect_blocks_claim=False))
    out = run(tmp_path, monkeypatch, [fee()], [receiving("FAIL"), prep("PASS")])
    assert out["evidence"]["decision"]["outcome"] == "claim_recommended"
    assert out["evidence"]["payload"]["policy"]["receiving_defect_blocks_claim"] is False


# ================================================================ overrides: the latest one wins
def test_overriding_prep_to_fail_flips_a_contradicted_charge_to_supported(tmp_path, monkeypatch):
    previous = [receiving(), prep("PASS")]
    base = run(tmp_path, monkeypatch, [fee()], previous)
    flipped = run(tmp_path, monkeypatch, [fee()], previous, [override(f"PRP-{UNIT}", "FAIL")])
    assert the_check(base)["observed"] == "CONTRADICTS" and the_check(flipped)["observed"] == "SUPPORTS"
    assert_no_claim(flipped)
    ev = flipped["evidence"]
    assert ev["payload"]["overrides_applied"][0]["override_id"] == "OVR-001"
    up = next(u for u in ev["payload"]["upstream"] if u["stage"] == "prep")
    assert (up["verdict"], up["effective_verdict"], up["overridden"]) == ("PASS", "FAIL", True), "the original is kept"
    assert "overrode it to FAIL" in the_check(flipped)["detail"]
    assert ev["upstream_refs"] == base["evidence"]["upstream_refs"]


def test_overriding_prep_to_pass_turns_an_uncertain_prep_into_a_claim(tmp_path, monkeypatch):
    previous = [receiving(), prep("UNCERTAIN")]
    assert_no_claim(run(tmp_path, monkeypatch, [fee()], previous))
    out = run(tmp_path, monkeypatch, [fee()], previous, [override(f"PRP-{UNIT}", "PASS", original="UNCERTAIN")])
    assert out["evidence"]["decision"]["outcome"] == "claim_recommended"
    assert out["evidence"]["payload"]["claims"][0]["evidence_record_ids"][0] == f"PRP-{UNIT}"


def test_the_latest_of_several_overrides_is_the_effective_one(tmp_path, monkeypatch):
    rid = f"PRP-{UNIT}"
    first = override(rid, "FAIL", n=1)
    second = override(rid, "PASS", n=2, prev="FAIL", earlier="OVR-001")
    out = run(tmp_path, monkeypatch, [fee()], [receiving(), prep("PASS")], [first, second])
    assert the_check(out)["observed"] == "CONTRADICTS"  # FAIL, then back to PASS: the last word wins
    out = run(tmp_path, monkeypatch, [fee()], [receiving(), prep("PASS")], [second, first])
    assert the_check(out)["observed"] == "SUPPORTS"


def test_overriding_receiving_clears_or_creates_the_conflict(tmp_path, monkeypatch):
    rid = f"RCV-{UNIT}"
    cleared = run(tmp_path, monkeypatch, [fee()], [receiving("FAIL"), prep("PASS")], [override(rid, "PASS", original="FAIL")])
    assert cleared["evidence"]["decision"]["outcome"] == "claim_recommended"
    created = run(tmp_path, monkeypatch, [fee()], [receiving("PASS"), prep("PASS")], [override(rid, "FAIL")])
    assert "receiving_defect_conflict" in charges(created)["FEE-T1-1"]["codes"]


def test_an_override_of_a_record_from_another_subject_changes_nothing(tmp_path, monkeypatch):
    out = run(tmp_path, monkeypatch, [fee()], [receiving(), prep("PASS")], [override("PRP-SOMETHING-ELSE", "FAIL")])
    assert the_check(out)["observed"] == "CONTRADICTS" and out["evidence"]["payload"]["overrides_applied"] == []


# ================================================================ the Specialist flow: no Prep evidence
def test_without_prep_evidence_an_inbound_defect_charge_is_silent_not_guessed(tmp_path, monkeypatch):
    out = run(tmp_path, monkeypatch, [fee(amount="2.00")], [receiving()])
    c = charges(out)["FEE-T1-1"]
    assert c["position"] == "SILENT" and c["codes"] == ["no_prep_evidence"] and c["claim_usd"] == 0
    assert the_check(out)["uncertain_reason"] == "insufficient_evidence"
    assert_no_claim(out)


def test_the_reason_says_whether_this_pods_flow_has_a_prep_stage(tmp_path, monkeypatch):
    monkeypatch.setenv("ORCH_FLOW", str(SPECIALIST_FLOW))
    out = run(tmp_path, monkeypatch, [fee()], [receiving()])
    assert "Specialist flow" in the_check(out)["detail"] and out["evidence"]["payload"]["flow_stages"] == [
        "receiving", "pack", "returns", "recovery"]
    monkeypatch.delenv("ORCH_FLOW")
    out = run(tmp_path, monkeypatch, [fee()], [receiving()])
    assert "produced no record" in the_check(out)["detail"], "Prep is in the standard flow, it just did not run for this unit"


def test_pack_evidence_is_read_and_cited_but_does_not_stand_in_for_prep(tmp_path, monkeypatch):
    pack = upstream("pack", "PASS", outcome="seal", refs={})
    out = run(tmp_path, monkeypatch, [fee()], [receiving(), pack], route="mfn")
    assert charges(out)["FEE-T1-1"]["codes"] == ["no_prep_evidence"]
    assert out["evidence"]["upstream_refs"] == [f"RCV-{UNIT}", f"PCK-{UNIT}"]
    assert [u["stage"] for u in out["evidence"]["payload"]["upstream"]] == ["receiving", "pack"]


# ================================================================ F-07: weight-tier fees
TABLE = {"source_url": "https://example.invalid/TEST-ONLY-fee-schedule", "retrieved": "2026-10-01", "tolerance_g": 10,
         "tiers": [{"up_to_g": 100, "fee_usd": 3.00}, {"up_to_g": 500, "fee_usd": 4.00}, {"up_to_g": None, "fee_usd": 6.00}]}


def measured(weight_g, **kw):
    return prep("PASS", payload={"measurements": {"weight_g": weight_g, "length_mm": 100}}, **kw)


def table_file(tmp_path, monkeypatch, table=TABLE):
    path = tmp_path / "tiers.json"
    path.write_text(json.dumps(table), encoding="utf-8")
    monkeypatch.setenv("RECOVERY_TIER_TABLE", str(path))


def test_weight_tier_fees_are_silent_when_nothing_upstream_measured_a_weight(tmp_path, monkeypatch):
    out = run(tmp_path, monkeypatch, [fee(charge_type="fulfilment_fee_weight_tier", amount="4.25")],
              [receiving(), prep("PASS", payload={"measurements": None})])
    c = charges(out)["FEE-T1-1"]
    assert (c["position"], c["codes"], c["findings"]) == ("SILENT", ["no_measurements"], ["F-07"])
    assert_no_claim(out)


def test_a_measurement_alone_is_not_enough_without_a_fee_schedule(tmp_path, monkeypatch):
    out = run(tmp_path, monkeypatch, [fee(charge_type="fulfilment_fee_weight_tier", amount="4.25")], [receiving(), measured(250)])
    c = charges(out)["FEE-T1-1"]
    assert c["codes"] == ["measurements_not_comparable"] and c["evidence_record_ids"] == [f"PRP-{UNIT}"]
    assert out["evidence"]["payload"]["rule_source"]["fee_schedule"] is None
    assert_no_claim(out)


@pytest.mark.parametrize("weight, billed, position, claim", [
    (250, "6.00", "CONTRADICTS", 2.00),   # 250 g is the 4.00 tier; billed 6.00: claim the 2.00 difference, not the whole line
    (250, "4.00", "SUPPORTS", 0.0),
    (250, "3.00", "SUPPORTS", 0.0),       # billed less than the schedule: nothing to dispute
    (105, "6.00", "SILENT", 0.0),         # within the scale's tolerance of the 100 g boundary: not reliable enough
    (900, "6.00", "SUPPORTS", 0.0),       # the open-ended tier
])
def test_a_measured_weight_and_a_sourced_schedule_can_contradict_or_support_a_tier_fee(tmp_path, monkeypatch, weight, billed,
                                                                                       position, claim):
    table_file(tmp_path, monkeypatch)
    out = run(tmp_path, monkeypatch, [fee(charge_type="fulfilment_fee_weight_tier", amount=billed)], [receiving(), measured(weight)])
    c = charges(out)["FEE-T1-1"]
    assert (c["position"], c["claim_usd"]) == (position, claim)
    if claim:
        assert out["evidence"]["payload"]["claims"][0]["evidence_record_ids"] == [f"PRP-{UNIT}"]
    sources = out["evidence"]["payload"]["rule_source"]["fee_schedule"]
    assert sources and sources[0]["source_url"] == TABLE["source_url"] and sources[0]["retrieved"] == "2026-10-01"


def test_a_fee_schedule_that_does_not_say_where_it_came_from_is_not_used(tmp_path, monkeypatch):
    table_file(tmp_path, monkeypatch, {k: v for k, v in TABLE.items() if k != "source_url"})
    out = run(tmp_path, monkeypatch, [fee(charge_type="fulfilment_fee_weight_tier", amount="6.00")], [receiving(), measured(250)])
    c = charges(out)["FEE-T1-1"]
    assert c["codes"] == ["measurements_not_comparable"] and "source_url is required" in c["reason"]
    assert_no_claim(out)


def test_a_measurement_about_another_item_is_not_used(tmp_path, monkeypatch):
    table_file(tmp_path, monkeypatch)
    other = measured(250, refs={"sku": "SKU-OTHER"})
    out = run(tmp_path, monkeypatch, [fee(charge_type="fulfilment_fee_weight_tier", amount="6.00")], [receiving(), other])
    assert charges(out)["FEE-T1-1"]["codes"] == ["no_measurements"]


# ================================================================ F-09, F-10, F-11, F-12
def test_zero_amount_lines_are_never_claimed_even_where_evidence_contradicts(tmp_path, monkeypatch):
    out = run(tmp_path, monkeypatch, [fee(amount="0.00")], [receiving(), prep("PASS")])
    c = charges(out)["FEE-T1-1"]
    assert c["position"] == "SILENT" and c["underlying_position"] == "CONTRADICTS" and "F-09" in c["findings"]
    assert c["codes"][0] == "amount_zero" and "0.00" in c["reason"]
    assert_no_claim(out)


def test_a_supplier_shortfall_is_never_a_channel_loss_claim_even_with_a_dollar_figure(tmp_path, monkeypatch):
    rcv = receiving(payload={"shortfall_units": 4})
    out = run(tmp_path, monkeypatch, [fee(charge_type="lost_inbound", amount="25.00", report_type="inventory_adjustment")], [rcv])
    c = charges(out)["FEE-T1-1"]
    assert c["position"] == "SILENT" and c["findings"] == ["F-10"] and c["codes"] == ["channel_loss_unevidenced"]
    assert "supplier shortfall" in c["reason"] and "4 unit(s)" in c["reason"] and "not used here" in c["reason"]
    assert c["basis"][0]["role"] == "set_aside" and c["evidence_record_ids"] == [f"RCV-{UNIT}"]
    assert_no_claim(out)


RETURNS_FEE = dict(charge_type="refund_issued_item_not_returned", amount="18.00", order_id="ORD-T1")


def test_returns_evidence_can_contradict_a_refund_without_return_on_a_merchant_fulfilled_unit(tmp_path, monkeypatch):
    out = run(tmp_path, monkeypatch, [fee(**RETURNS_FEE)], [receiving(), returns("PASS")], route="mfn")
    claim = out["evidence"]["payload"]["claims"][0]
    assert claim["claim_usd"] == 18.0 and claim["evidence_record_ids"] == [f"RTN-{UNIT}"]
    out = run(tmp_path, monkeypatch, [fee(**RETURNS_FEE)], [receiving(), returns("FAIL")], route="mfn")
    assert the_check(out)["observed"] == "SUPPORTS"
    out = run(tmp_path, monkeypatch, [fee(**RETURNS_FEE)], [receiving(), returns("UNCERTAIN")], route="mfn")
    assert the_check(out)["observed"] == "SILENT"


def test_f11_an_fba_unit_returns_record_is_not_taken_as_proof_unless_the_policy_says_so(tmp_path, monkeypatch):
    out = run(tmp_path, monkeypatch, [fee(**RETURNS_FEE)], [receiving(), prep("PASS"), returns("PASS")], route="fba")
    c = charges(out)["FEE-T1-1"]
    assert c["position"] == "SILENT" and c["findings"] == ["F-11"] and c["underlying_position"] is None
    assert_no_claim(out)
    monkeypatch.setattr(policy_module, "POLICY", policy_module.Policy(fba_returns_can_contradict=True))
    out = run(tmp_path, monkeypatch, [fee(**RETURNS_FEE)], [receiving(), prep("PASS"), returns("PASS")], route="fba")
    assert out["evidence"]["payload"]["claimable_usd"] == 18.0, "flipping F-11 is one policy line"


def test_f12_a_unit_with_no_route_is_not_given_the_benefit_of_the_doubt(tmp_path, monkeypatch):
    out = run(tmp_path, monkeypatch, [fee(**RETURNS_FEE)], [receiving(), returns("PASS")], route="unknown")
    assert charges(out)["FEE-T1-1"]["findings"] == ["F-12"]
    assert_no_claim(out)


def test_a_returns_record_for_a_different_order_is_a_conflict_not_a_claim(tmp_path, monkeypatch):
    out = run(tmp_path, monkeypatch, [fee(**RETURNS_FEE)],
              [receiving(), returns("PASS", refs={"order_id": "ORD-OTHER"})], route="mfn")
    c = charges(out)["FEE-T1-1"]
    assert c["codes"] == ["evidence_identity_mismatch"] and c["needs_person"] is True
    assert out["evidence"]["decision"]["outcome"] == "pending_review"


# ================================================================ duplicates and refunds already made
def test_the_repeat_of_an_identical_charge_is_a_duplicate_claim_citing_both_fee_lines(tmp_path, monkeypatch):
    rows = [fee("FEE-T1-1", "fulfilment_fee_weight_tier", "4.25", order_id="ORD-T1"),
            fee("FEE-T1-2", "fulfilment_fee_weight_tier", "4.25", order_id="ORD-T1")]
    out = run(tmp_path, monkeypatch, rows, [receiving()])
    ch = charges(out)
    assert ch["FEE-T1-1"]["assessment"] == "SILENT", "the first one is judged on its own (F-07), the repeat is the claim"
    assert (ch["FEE-T1-2"]["assessment"], ch["FEE-T1-2"]["claim_usd"]) == ("DUPLICATE", 4.25)
    claim = out["evidence"]["payload"]["claims"][0]
    assert claim["basis_kind"] == "duplicate" and claim["fee_line_refs"] == ["fee_report/FEE-T1-2", "fee_report/FEE-T1-1"]
    assert set(claim["fee_line_refs"]) <= {i["ref"] for i in out["evidence"]["inputs"]}, "both rows are hashed inputs"
    assert out["evidence"]["decision"]["outcome"] == "claim_recommended" and out["evidence"]["payload"]["claimable_usd"] == 4.25


def test_without_an_order_id_a_repeat_could_be_a_second_real_unit_so_it_is_not_claimed(tmp_path, monkeypatch):
    rows = [fee("FEE-T1-1", amount="1.00"), fee("FEE-T1-2", amount="1.00")]
    out = run(tmp_path, monkeypatch, rows, [receiving()])
    assert charges(out)["FEE-T1-2"]["codes"] == ["possible_duplicate"] and charges(out)["FEE-T1-2"]["findings"] == ["F-08"]
    assert_no_claim(out)


@pytest.mark.parametrize("change", [{"posted_date": "2026-07-11"}, {"order_id": "ORD-2"}, {"amount_usd": "4.50"}, {"sku": "SKU-B"}])
def test_lines_that_differ_in_date_order_amount_or_sku_are_not_duplicates(tmp_path, monkeypatch, change):
    rows = [fee("FEE-T1-1", "fulfilment_fee_weight_tier", "4.25", order_id="ORD-T1"),
            fee("FEE-T1-2", "fulfilment_fee_weight_tier", "4.25", **{"order_id": "ORD-T1", **change})]
    out = run(tmp_path, monkeypatch, rows, [receiving()])
    assert "DUPLICATE" not in {c["assessment"] for c in charges(out).values()}


def test_duplicates_are_not_found_across_units_or_organisations(tmp_path, monkeypatch):
    rows = [fee("FEE-T1-1", "fulfilment_fee_weight_tier", "4.25", order_id="ORD-T1"),
            fee("FEE-U2-1", "fulfilment_fee_weight_tier", "4.25", order_id="ORD-T1", unit_id="UNIT-T2"),
            fee("FEE-O1-1", "fulfilment_fee_weight_tier", "4.25", order_id="ORD-T1", org_id=OTHER)]
    out = run(tmp_path, monkeypatch, rows, [receiving()])
    assert list(charges(out)) == ["FEE-T1-1"]


def test_a_charge_that_was_already_credited_is_not_claimed_a_second_time(tmp_path, monkeypatch):
    credit = fee("FEE-T1-9", "damaged_in_warehouse", "2.00", report_type="reimbursement_report", posted_date="2026-07-15")
    out = run(tmp_path, monkeypatch, [fee(amount="2.00"), credit], [receiving(), prep("PASS")])
    ch = charges(out)
    assert (ch["FEE-T1-1"]["assessment"], ch["FEE-T1-1"]["position"]) == ("ALREADY_REIMBURSED", "SUPPORTS")
    assert ch["FEE-T1-1"]["fee_line_refs"] == ["fee_report/FEE-T1-1", "fee_report/FEE-T1-9"]
    assert ch["FEE-T1-9"]["assessment"] == "CREDIT" and the_check(out, 1)["verdict"] == "PASS"
    assert out["evidence"]["decision"]["outcome"] == "no_claim" and out["evidence"]["decision"]["verdict"] == "PASS"
    assert_no_claim(out)


def test_an_explicit_link_settles_a_charge_whatever_the_amounts(tmp_path, monkeypatch):
    rows = [fee(amount="2.00"), fee("FEE-T1-9", "damaged_in_warehouse", "14.00", report_type="reimbursement_report")]
    rows[1]["related_line_id"] = "FEE-T1-1"
    out = run(tmp_path, monkeypatch, rows, [receiving(), prep("PASS")])
    assert charges(out)["FEE-T1-1"]["assessment"] == "ALREADY_REIMBURSED"


def test_an_unrelated_credit_on_the_same_unit_does_not_hide_a_real_claim(tmp_path, monkeypatch):
    credit = fee("FEE-T1-9", "damaged_in_warehouse", "14.00", report_type="reimbursement_report")
    out = run(tmp_path, monkeypatch, [fee(amount="2.00"), credit], [receiving(), prep("PASS")])
    assert charges(out)["FEE-T1-1"]["assessment"] == "CONTRADICTED" and out["evidence"]["payload"]["claimable_usd"] == 2.0


# ================================================================ tenancy, idempotency, fail-open, contract
def test_a_unit_under_another_organisation_is_refused_not_answered(tmp_path, monkeypatch):
    write_world(tmp_path, monkeypatch, [fee(org_id=OTHER)])
    with pytest.raises(LookupError):
        recovery.handle(request(org=ORG))
    assert recovery.handle(request(org=OTHER))["evidence"]["subject"]["org_id"] == OTHER


def test_another_organisations_evidence_in_the_request_is_refused(tmp_path, monkeypatch):
    write_world(tmp_path, monkeypatch, [fee()])
    foreign = prep("PASS", org=OTHER)
    with pytest.raises(LookupError):
        recovery.handle(request([receiving(), foreign]))


def test_evidence_about_a_different_unit_is_ignored_and_not_cited(tmp_path, monkeypatch):
    elsewhere = prep("PASS", unit="UNIT-T9", rid="PRP-UNIT-T9")
    out = run(tmp_path, monkeypatch, [fee()], [receiving(), elsewhere])
    ev = out["evidence"]
    assert charges(out)["FEE-T1-1"]["codes"] == ["no_prep_evidence"] and ev["upstream_refs"] == [f"RCV-{UNIT}"]
    assert ev["payload"]["ignored_evidence"] == [{"record_id": "PRP-UNIT-T9", "reason": "about a different subject"}]


def test_a_record_whose_hash_no_longer_matches_is_not_used_for_a_claim(tmp_path, monkeypatch):
    tampered = prep("FAIL")
    tampered["decision"] = {**tampered["decision"], "verdict": "PASS"}  # body changed, content_hash not
    assert not verify(tampered)
    out = run(tmp_path, monkeypatch, [fee()], [receiving(), tampered])
    ev = out["evidence"]
    assert charges(out)["FEE-T1-1"]["codes"] == ["no_prep_evidence"] and ev["upstream_refs"] == [f"RCV-{UNIT}"]
    assert ev["payload"]["ignored_evidence"][0]["reason"].startswith("content_hash does not match")
    assert_no_claim(out)


def test_the_http_service_answers_404_for_another_tenant_and_validates_input(tmp_path, monkeypatch):
    write_world(tmp_path, monkeypatch, [fee(org_id=OTHER)])
    client = TestClient(recovery.app)
    assert client.get("/health").json()["stage"] == "recovery"
    assert client.post("/run", json=request(org=ORG)).status_code == 404
    ok = client.post("/run", json=request(org=OTHER))
    assert ok.status_code == 200 and errors("agent-output", ok.json()) == []
    assert client.post("/run", json={"stage": "recovery"}).status_code == 422


def test_the_same_request_gives_the_same_record_byte_for_byte(tmp_path, monkeypatch):
    write_world(tmp_path, monkeypatch, [fee()])
    req = request([receiving(), prep("PASS")])
    first, second = recovery.handle(req), recovery.handle(req)
    assert first == second and first["evidence"]["content_hash"] == second["evidence"]["content_hash"]
    assert first["evidence"]["record_id"] == adapter.record_id_for(req) == "RCY-WF-org_t-UNIT-T1-recovery"
    assert recovery.handle(request([receiving(), prep("PASS")], rid="WF-org_t-UNIT-T1:recovery:r2"))["evidence"]["record_id"] \
        != first["evidence"]["record_id"], "a re-run of the stage is a new request and gets a new record"


def test_changed_inputs_under_the_same_request_id_give_a_new_timestamp_not_a_stale_record(tmp_path, monkeypatch):
    write_world(tmp_path, monkeypatch, [fee()])
    a = recovery.handle(request([receiving(), prep("PASS")]))
    b = recovery.handle(request([receiving(), prep("FAIL")]))
    assert a["evidence"]["content_hash"] != b["evidence"]["content_hash"]
    assert the_check(a)["observed"] == "CONTRADICTS" and the_check(b)["observed"] == "SUPPORTS"


def test_an_unexpected_error_fails_open_with_a_pending_record_and_no_claim(tmp_path, monkeypatch):
    write_world(tmp_path, monkeypatch, [fee()])
    monkeypatch.setattr(recovery.adapter, "build", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    out = recovery.handle(request([receiving(), prep("PASS")]))
    assert errors("agent-output", out) == [] and verify(out["evidence"])
    assert out["status"] == "pending" and out["verdict"] == "UNCERTAIN" and out["evidence"]["checks"] == []
    assert out["evidence"]["decision"]["outcome"] == "pending_review" and out["error"]["code"] == "agent_exception"
    assert out["agent_id"] == adapter.AGENT_ID


def test_the_check_decision_and_payload_agree_with_each_other(tmp_path, monkeypatch):
    rows = [fee("FEE-T1-1", amount="2.00"), fee("FEE-T1-2", "lost_inbound", "0.00"),
            fee("FEE-T1-3", "refund_issued_item_not_returned", "5.00", order_id="ORD-T1"),
            fee("FEE-T1-4", "damaged_in_warehouse", "3.00", report_type="reimbursement_report")]
    out = run(tmp_path, monkeypatch, rows, [receiving(), prep("PASS"), returns("PASS")])
    ev = out["evidence"]
    claimed = [c for c in ev["payload"]["charges"] if c["position"] == "CONTRADICTS"]
    assert [c["check_key"] for c in claimed] == [c["check_key"] for c in ev["checks"] if c["verdict"] == "FAIL"]
    assert ev["payload"]["claimable_usd"] == round(sum(c["claim_usd"] for c in claimed), 2)
    assert {u["line_id"] for u in ev["payload"]["unclaimable"]} | {c["line_id"] for c in claimed} == {r["line_id"] for r in rows}
    assert all(u["reason"] for u in ev["payload"]["unclaimable"]), "every charge left alone says why"
    for c in claimed:
        assert c["claim_usd"] > 0 and (c["evidence_record_ids"] or c["assessment"] == "DUPLICATE")
    assert ev["payload"]["summary"]["by_position"] == {"CONTRADICTS": 1, "SUPPORTS": 1, "SILENT": 2}


def test_it_reads_all_previous_evidence_and_cites_all_of_it(tmp_path, monkeypatch):
    previous = [receiving(), prep("PASS"), upstream("pack", "PASS", outcome="seal", refs={}), returns("PASS")]
    out = run(tmp_path, monkeypatch, [fee()], previous)
    ev = out["evidence"]
    assert ev["upstream_refs"] == [r["record_id"] for r in previous]
    assert {u["record_id"] for u in ev["payload"]["upstream"]} == set(ev["upstream_refs"])
    assert errors("evidence", ev) == []


def test_only_the_claim_amounts_use_decimals_so_cents_never_drift(tmp_path, monkeypatch):
    rows = [fee(f"FEE-T1-{i}", amount="0.10", order_id="ORD-T1", posted_date=f"2026-07-{10 + i:02d}") for i in range(1, 8)]
    out = run(tmp_path, monkeypatch, rows, [receiving(), prep("PASS")])
    assert out["evidence"]["payload"]["claimable_usd"] == 0.7  # 7 x 0.10 is 0.7000000000000001 in floats
    assert fees.parse(rows[0]).amount == Decimal("0.10")


# ================================================================ whole workflows through the real orchestrator
def test_a_whole_workflow_files_the_claim_and_every_record_is_navigable(cases):
    """UNIT-0014 on the organisers' sample: Receiving, Prep, Returns (stubs) then our Recovery."""
    from orchestration.orchestrator import run_workflow
    from orchestration.store import MemoryStore

    case = next(c for c in cases if c["unit_id"] == "UNIT-0014")
    store = MemoryStore()
    wf = run_workflow(case, store=store)
    fo = wf["final_outcome"]
    assert errors("workflow-state", wf) == [] and (wf["status"], fo["outcome"], fo["claimable_usd"]) == ("COMPLETED", "CLAIM_RECOMMENDED", 2.0)
    rcy = store.get_evidence(next(s["record_id"] for s in wf["stage_results"] if s["stage"] == "recovery"))
    assert rcy["agent_id"] == adapter.AGENT_ID and set(rcy["upstream_refs"]) == {"RCV-0014", "PRP-0014", "RTN-0014"}
    claim = rcy["payload"]["claims"][0]
    for rid in claim["evidence_record_ids"]:  # final outcome -> claim -> upstream record -> its own inputs
        assert rid in wf["evidence_references"] and rid in fo["contributing_records"]
        assert store.get_evidence(rid)["inputs"], "the upstream record carries the inputs it examined"
    assert rcy["payload"]["findings"]["F-07"] == ["FEE-0014-3"] and "F-10" in rcy["payload"]["findings"]


def test_every_sample_unit_produces_valid_records_and_never_claims_on_silence(cases):
    from orchestration.orchestrator import run_workflow
    from orchestration.store import MemoryStore

    store, claimed_usd, claims = MemoryStore(), 0.0, 0
    for case in cases:
        wf = run_workflow(case, store=store)
        sr = next(s for s in wf["stage_results"] if s["stage"] == "recovery")
        assert sr["state"] == "completed", (case["unit_id"], wf["errors"])
        rec = store.get_evidence(sr["record_id"])
        assert errors("evidence", rec) == [] and verify(rec), case["unit_id"]
        assert all(KEY.match(c["check_key"]) and c["check_key"].startswith("charge_") for c in rec["checks"])
        assert len(rec["checks"]) == len(fees.lines_for(case["unit_id"], case["org_id"]))
        for ch in rec["payload"]["charges"]:
            if ch["position"] == "SILENT":
                assert ch["claim_usd"] == 0, "a SILENT charge is never claimed"
        for cl in rec["payload"]["claims"]:
            assert cl["claim_usd"] > 0 and cl["evidence_record_ids"] and all(r in rec["upstream_refs"] for r in cl["evidence_record_ids"])
            claims, claimed_usd = claims + 1, claimed_usd + cl["claim_usd"]
    assert claims > 0 and round(claimed_usd, 2) > 0


def test_the_sample_fee_report_has_no_duplicates_and_we_do_not_invent_any(cases):
    """The Round 2 engine keyed duplicates on the shipment id, which the sample shares between units: it called 14
    unrelated weight-tier fees (53.70 USD) duplicates. Keyed on the order and the unit, there are none."""
    from orchestration.orchestrator import run_workflow
    from orchestration.store import MemoryStore

    store = MemoryStore()
    assessments = set()
    for case in cases:
        wf = run_workflow(case, store=store)
        rcy = store.get_evidence(next(s["record_id"] for s in wf["stage_results"] if s["stage"] == "recovery"))
        assessments |= {c["assessment"] for c in rcy["payload"]["charges"]}
    assert "DUPLICATE" not in assessments and "ALREADY_REIMBURSED" not in assessments


def test_a_specialist_flow_has_no_inbound_defect_claims(cases, monkeypatch):
    from orchestration.orchestrator import load_flow, run_workflow
    from orchestration.store import MemoryStore

    monkeypatch.setenv("ORCH_FLOW", str(SPECIALIST_FLOW))
    flow, store, silent_defects = load_flow(SPECIALIST_FLOW), MemoryStore(), 0
    for case in cases:
        wf = run_workflow(case, flow, store)
        rcy = store.get_evidence(next(s["record_id"] for s in wf["stage_results"] if s["stage"] == "recovery"))
        assert rcy["payload"]["claims"] == [], case["unit_id"]
        assert wf["final_outcome"]["outcome"] != "CLAIM_RECOMMENDED"
        silent_defects += sum(c["codes"] == ["no_prep_evidence"] for c in rcy["payload"]["charges"])
    assert silent_defects == 9, "all nine inbound-defect fees in the sample are silent without Prep"


def test_a_person_resolving_prep_after_a_halt_changes_what_recovery_decides(cases):
    """Prep is UNCERTAIN and the flow blocks on it. A person overrides Prep; on resume Recovery sees the override.
    PASS gives a claim, FAIL gives none. Uses fake Receiving/Prep so the verdicts are ours."""
    from orchestration.orchestrator import apply_override, load_flow, resume, run_workflow
    from orchestration.store import MemoryStore
    from tests.helpers import Fake

    base = load_flow()
    flow = {**base, "defaults": {**base["defaults"], "on_uncertain": "block"}}
    case = {"org_id": "org_demo_alpha", "unit_id": "UNIT-0014", "route": "fba", "returned": False}
    outcomes = {}
    for new in ("PASS", "FAIL"):
        store = MemoryStore()
        fakes = {"receiving": Fake("PASS"), "prep": Fake("UNCERTAIN", needs_human=True)}
        wf = run_workflow(case, flow, store, fakes)
        states = {s["stage"]: s["state"] for s in wf["stage_results"]}
        assert wf["status"] == "BLOCKED" and states["recovery"] == "pending", "halted at Prep, Recovery has not run"
        prep_id = next(s["record_id"] for s in wf["stage_results"] if s["stage"] == "prep")
        apply_override(wf["workflow_id"], store, record_id=prep_id, new_verdict=new, actor="op_t", reason="looked at the unit")
        wf = resume(wf["workflow_id"], flow, store, fakes)
        rcy = store.get_evidence(next(s["record_id"] for s in wf["stage_results"] if s["stage"] == "recovery"))
        outcomes[new] = (wf["final_outcome"]["outcome"], rcy["payload"]["overrides_applied"][0]["new_verdict"], len(rcy["upstream_refs"]))
    assert outcomes["PASS"] == ("CLAIM_RECOMMENDED", "PASS", 2)
    assert outcomes["FAIL"][0] != "CLAIM_RECOMMENDED" and outcomes["FAIL"][1:] == ("FAIL", 2)
