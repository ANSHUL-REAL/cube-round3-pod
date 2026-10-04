"""Every agent, on every applicable sample subject, must return a valid Agent Output with a valid Evidence Record.

This runs against whatever agents/<stage>/agent.json points at (stub, in-process, or HTTP).
When you replace the stub, replace the sample loop with your own fixtures, but keep the assertions.
"""
import pytest

from orchestration.clients import AgentRejected, client_for
from shared.utils.hashing import verify
from shared.utils.records import add_agent_override
from shared.utils.schema import errors
from tests.conftest import AGENTS, applies, make_input

PREFIX = {"receiving": "RCV", "prep": "PRP", "pack": "PCK", "returns": "RTN", "recovery": "RCY"}


@pytest.mark.parametrize("stage", AGENTS)
def test_outputs_are_contract_valid(stage, cases):
    client, checked = client_for(stage), 0
    for case in (c for c in cases if applies(stage, c)):
        out = client.run(make_input(stage, case), 30)
        assert errors("agent-output", out) == [], f"{stage} {case['unit_id']}"
        ev = out["evidence"]
        assert ev["record_id"].startswith(PREFIX[stage] + "-")
        assert ev["stage"] == out["stage"] == stage and ev["agent_id"] == out["agent_id"]
        assert ev["workflow_id"] == out["workflow_id"] == f"WF-{case['org_id']}-{case['unit_id']}"
        assert (ev["subject"]["org_id"], ev["subject"]["subject_id"]) == (case["org_id"], case["unit_id"])
        assert out["verdict"] == ev["decision"]["verdict"] and out["status"] == ev["status"]
        assert verify(ev), "content_hash does not match the record body"
        for c in ev["checks"]:
            if c["verdict"] == "UNCERTAIN":
                assert c.get("uncertain_reason"), "UNCERTAIN needs a reason (it is a verdict, not a shrug)"
        checked += 1
    assert checked > 0


@pytest.mark.parametrize("stage", AGENTS)
def test_other_tenant_gets_nothing(stage, cases):
    """Tenancy: asking for a subject under the wrong org must be refused, never answered."""
    case = next(c for c in cases if applies(stage, c))
    other = "org_demo_bravo" if case["org_id"] == "org_demo_alpha" else "org_demo_alpha"
    with pytest.raises(AgentRejected):
        client_for(stage).run(make_input(stage, {**case, "org_id": other}), 30)


@pytest.mark.parametrize("stage", AGENTS)
def test_same_request_same_record_id(stage, cases):
    case = next(c for c in cases if applies(stage, c))
    req = make_input(stage, case)
    client = client_for(stage)
    assert client.run(req, 30)["evidence"]["record_id"] == client.run(req, 30)["evidence"]["record_id"]


def test_each_stage_can_consume_the_previous_stages_output(cases):
    """The hand-off: feed every stage the evidence the earlier stages produced; it must work and reference it."""
    case = next(c for c in cases if c["route"] == "fba" and c["returned"])
    previous = []
    for stage in [s for s in AGENTS if applies(s, case)]:
        out = client_for(stage).run(make_input(stage, case, previous), 30)
        assert errors("agent-output", out) == []
        assert set(out["evidence"]["upstream_refs"]) == {r["record_id"] for r in previous}, \
            f"{stage} must list the previous evidence it consumed in upstream_refs"
        previous.append(out["evidence"])


def test_recovery_honours_overrides_of_previous_evidence(cases):
    """Prep PASS makes the inbound-defect fee contradicted. A person overriding Prep to FAIL must change that."""
    if "recovery" not in AGENTS or "prep" not in AGENTS:
        pytest.skip("needs both Prep and Recovery in the flow")
    case = next(c for c in cases if c["unit_id"] == "UNIT-0014")
    prior = []
    for stage in ("receiving", "prep", "returns"):
        prior.append(client_for(stage).run(make_input(stage, case, prior), 30)["evidence"])
    prep_id = prior[1]["record_id"]
    base = client_for("recovery").run(make_input("recovery", case, prior), 30)["evidence"]
    override = {"override_id": "OVR-001", "supersedes": {"record_id": prep_id, "override_id": None}, "target": "decision",
                "actor": "t", "at": "2026-01-01T00:00:00Z", "reason": "label creased", "original_verdict": "PASS",
                "previous_verdict": "PASS", "new_verdict": "FAIL"}
    changed = client_for("recovery").run(make_input("recovery", case, prior, [override]), 30)["evidence"]
    pos = lambda ev: {c["charge_type"]: c["position"] for c in ev["payload"]["charges"]}  # noqa: E731
    assert pos(base)["inbound_defect_fee"] == "CONTRADICTS"
    assert pos(changed)["inbound_defect_fee"] == "SUPPORTS"


def test_agent_level_override_is_append_only(cases):
    out = client_for("receiving").run(make_input("receiving", cases[0]), 30)
    rec, target = out["evidence"], out["evidence"]["checks"][0]
    new = add_agent_override(rec, by="op_test", target=target["check_key"], new_verdict="FAIL", reason="operator disagrees")
    assert new["overrides"][0]["original_verdict"] == target["verdict"]
    assert new["checks"] == rec["checks"], "an override must never rewrite the original check"
    assert verify(new), "agent-level overrides sit outside the content hash"
    assert len(add_agent_override(new, by="op2", target="decision", new_verdict="PASS", reason="second look")["overrides"]) == 2
