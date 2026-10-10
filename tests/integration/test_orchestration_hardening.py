"""Orchestrator, store, in-process client and agent-server hardening.

Each test pins a defect that was found by review and reproduced before it was fixed:
  * an API id with a backslash read a JSON file outside the store (path traversal);
  * an override with any verdict ("banana") was accepted and stored;
  * an agent that returned nothing crashed the orchestrator and left a workflow stuck;
  * a missing agent folder crashed it the same way;
  * a non-text unit id or a null actor gave a 500;
  * a bug (KeyError) inside an agent was reported as a refusal / 404 instead of a failure;
  * in-process agents ignored the stage timeout.
The agents run on the organiser stubs here (tests/conftest.py routes every stage there).
"""
from __future__ import annotations

import json
import time

import httpx
import pytest
from fastapi.testclient import TestClient

import orchestration.api as api
import orchestration.orchestrator as orch
from orchestration.clients import AgentRejected, AgentTimeout, AgentUnavailable, HttpClient, InProcClient
from orchestration.orchestrator import apply_override, discover_inputs, run_workflow
from orchestration.store import FileStore, MemoryStore, is_safe_id
from shared.utils.server import make_app

CASE = {"org_id": "org_demo_alpha", "unit_id": "UNIT-0014", "route": "fba", "returned": True}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(api, "STORE", FileStore(tmp_path / "out"))
    return TestClient(api.app, raise_server_exceptions=False)


# ---------------------------------------------------------------- ids are file names
@pytest.mark.parametrize("bad", ["..\\x", "../x", "a/b", "a\\b", "C:x", "..", ".hidden", "", " ", "x" * 300, 14, None])
def test_unsafe_ids_are_rejected(bad):
    assert not is_safe_id(bad)


def test_normal_ids_are_accepted():
    for good in ("WF-org_demo_alpha-UNIT-0014", "RCV-WF-org_demo_alpha-UNIT-0014:receiving".replace(":", "-"), "PCK-PENDING-x.y"):
        assert is_safe_id(good)


def test_api_cannot_read_a_file_outside_the_store(client, tmp_path):
    (tmp_path / "secret.json").write_text(json.dumps({"hello": "outside the store"}))  # one level above out/
    for path in ("/workflows/..%5C..%5Csecret", "/workflows/..%5Csecret", "/workflows/..%2F..%2Fsecret",
                 "/workflows/..%5C..%5Csecret/evidence"):
        r = client.get(path)
        assert r.status_code == 404 and "outside the store" not in r.text, path


def test_file_store_refuses_to_write_an_unsafe_name(tmp_path):
    store = FileStore(tmp_path / "out")
    with pytest.raises(ValueError):
        store.put_evidence({"record_id": "..\\evil", "content_hash": "x"})
    with pytest.raises(ValueError):
        store.save_workflow({"workflow_id": "../evil"})
    assert store.get_evidence("..\\evil") is None and store.load_workflow("../evil") is None
    assert not (tmp_path / "evil.json").exists()


@pytest.mark.parametrize("unit", [14, ["UNIT-0014"], "../..", "a\\b", ""])
def test_creating_a_workflow_needs_a_safe_text_id(client, unit):
    r = client.post("/workflows", json={"org_id": "org_demo_alpha", "unit_id": unit})
    assert r.status_code == 422, r.text


def test_discover_inputs_never_leaves_the_capture_root(tmp_path, monkeypatch):
    monkeypatch.setenv("INPUT_DIR", str(tmp_path / "input"))
    (tmp_path / "input" / "UNIT-1" / "pack").mkdir(parents=True)
    (tmp_path / "input" / "UNIT-1" / "pack" / "a.jpg").write_bytes(b"x")
    (tmp_path / "other" / "pack").mkdir(parents=True)
    (tmp_path / "other" / "pack" / "b.jpg").write_bytes(b"y")
    assert [i["ref"] for i in discover_inputs("UNIT-1", "pack")] == ["UNIT-1/pack/a.jpg"]
    assert discover_inputs("../other", "pack") == [] and discover_inputs(14, "pack") == []


# ---------------------------------------------------------------- overrides
def _workflow(store):
    wf = run_workflow(CASE, store=store)
    return wf, next(s for s in wf["stage_results"] if s["stage"] == "prep")["record_id"]


@pytest.mark.parametrize("verdict", ["banana", "fail", "pass", "", "PASS ", None, 1])
def test_an_override_needs_a_real_verdict(verdict):
    store = MemoryStore()
    wf, rec = _workflow(store)
    with pytest.raises(ValueError):
        apply_override(wf["workflow_id"], store, record_id=rec, new_verdict=verdict, actor="a", reason="r")
    assert store.load_workflow(wf["workflow_id"])["overrides"] == [], "a rejected override must leave no trace"


@pytest.mark.parametrize("actor,reason", [(None, "r"), (7, "r"), ("a", None), ("a", ["r"]), ("  ", "r"), ("a", "")])
def test_an_override_needs_text_actor_and_reason(actor, reason):
    store = MemoryStore()
    wf, rec = _workflow(store)
    with pytest.raises(ValueError):
        apply_override(wf["workflow_id"], store, record_id=rec, new_verdict="PASS", actor=actor, reason=reason)


def test_override_api_answers_422_not_500(client):
    wid = client.post("/workflows", json={"org_id": "org_demo_alpha", "unit_id": "UNIT-0014"}).json()["workflow_id"]
    rec = client.get(f"/workflows/{wid}").json()["stage_results"][0]["record_id"]
    for body in ({"new_verdict": "banana", "actor": "a", "reason": "r"}, {"new_verdict": "PASS", "actor": None, "reason": "r"},
                 {"new_verdict": "PASS", "actor": "a", "reason": 5}):
        r = client.post(f"/workflows/{wid}/overrides", json={"record_id": rec, **body})
        assert r.status_code == 422, (body, r.text)
    ok = client.post(f"/workflows/{wid}/overrides", json={"record_id": rec, "new_verdict": "PASS", "actor": "a", "reason": "looked again"})
    assert ok.status_code == 200 and ok.json()["overrides"][0]["new_verdict"] == "PASS"


# ---------------------------------------------------------------- a misbehaving agent is recorded, never a crash
class Silent:
    def run(self, request, timeout_s):
        return None


def test_an_agent_that_returns_nothing_is_a_recorded_failure():
    store = MemoryStore()
    wf = run_workflow(CASE, store=store, clients={"receiving": Silent()})
    sr = wf["stage_results"][0]
    assert sr["state"] == "error" and sr["error"]["code"] == "agent_invalid_output"
    assert wf["status"] == "FAILED" and store.get_evidence(sr["record_id"])["status"] in ("pending", "error")


def test_a_stage_whose_agent_cannot_be_built_is_a_recorded_failure(monkeypatch):
    real = orch.client_for

    def client_for(stage):
        if stage == "receiving":
            raise FileNotFoundError("agents/receiving/agent.json")
        return real(stage)

    monkeypatch.setattr(orch, "client_for", client_for)
    wf = run_workflow(CASE, store=MemoryStore())
    sr = wf["stage_results"][0]
    assert sr["state"] == "error" and "could not be started" in sr["error"]["message"] and wf["status"] == "FAILED"


def test_the_run_counter_is_stored_before_the_agent_is_called():
    seen = {}

    class Spy:
        def __init__(self, store):
            self.store = store

        def run(self, request, timeout_s):
            seen["runs_when_called"] = self.store.load_workflow(request["workflow_id"])["stage_results"][0]["runs"]
            return None

    store = MemoryStore()
    run_workflow(CASE, store=store, clients={"receiving": Spy(store)})
    assert seen["runs_when_called"] == 1, "a crash during the call must not replay under the same request id"


# ---------------------------------------------------------------- in-process agents
def _inproc(handle):
    c = InProcClient.__new__(InProcClient)
    c.handle = handle
    return c


def test_an_in_process_agent_that_is_too_slow_times_out():
    t0 = time.monotonic()
    with pytest.raises(AgentTimeout):
        _inproc(lambda req: time.sleep(3) or {}).run({"stage": "x"}, 0.3)
    assert time.monotonic() - t0 < 2, "the caller must not wait for the slow agent"


def test_an_in_process_agent_that_answers_in_time_is_unaffected():
    assert _inproc(lambda req: {"ok": 1}).run({"stage": "x"}, 5) == {"ok": 1}


@pytest.mark.parametrize("raised, expected", [
    (httpx.ConnectTimeout("no route"), AgentUnavailable),  # never reached the agent: it is not there (PR #2)
    (httpx.ConnectError("refused"), AgentUnavailable),
    (httpx.ReadTimeout("still thinking"), AgentTimeout),   # reached it, and it did not answer in time
])
def test_an_http_agent_that_cannot_be_reached_is_unavailable_and_a_slow_one_timed_out(monkeypatch, raised, expected):
    def post(*args, **kwargs):
        raise raised

    monkeypatch.setattr(httpx, "post", post)
    with pytest.raises(AgentUnavailable) as caught:
        HttpClient({"stage": "pack", "url": "http://127.0.0.1:9"}).run({"stage": "pack"}, 1)
    assert type(caught.value) is expected


def test_a_wrong_tenant_is_a_refusal_but_a_bug_is_not():
    def tenant(req):
        raise LookupError("no pack record for UNIT-1 in org_x")

    def bug(req):
        return {}["column"]

    with pytest.raises(AgentRejected):
        _inproc(tenant).run({"stage": "x"}, 5)
    with pytest.raises(KeyError):  # surfaces as agent_exception in the orchestrator, with its real cause
        _inproc(bug).run({"stage": "x"}, 5)


def test_a_bug_in_an_agent_is_recorded_as_agent_exception_not_as_a_refusal():
    class Buggy:
        def run(self, request, timeout_s):
            return _inproc(lambda req: {}["column"]).run(request, timeout_s)

    wf = run_workflow(CASE, store=MemoryStore(), clients={"receiving": Buggy()})
    assert wf["stage_results"][0]["error"]["code"] == "agent_exception"
    assert "column" in wf["stage_results"][0]["error"]["message"]


# ---------------------------------------------------------------- the agent HTTP server
def _body(stage="receiving"):
    return {"schema_version": "1.0", "request_id": "WF-o-u:receiving", "workflow_id": "WF-o-u", "stage": stage,
            "subject": {"org_id": "o", "subject_id": "u"}, "inputs": [], "previous_evidence": [], "context": {}}


def test_agent_server_answers_422_for_a_body_that_is_not_json():
    c = TestClient(make_app("receiving", lambda body: {}), raise_server_exceptions=False)
    assert c.post("/run", content=b"not json", headers={"content-type": "application/json"}).status_code == 422
    assert c.post("/run", json=[1, 2]).status_code == 422


def test_agent_server_reports_a_bug_as_a_pending_record_not_a_404():
    def buggy(body):
        return {}["column"]

    c = TestClient(make_app("receiving", buggy), raise_server_exceptions=False)
    r = c.post("/run", json=_body())
    assert r.status_code == 200 and r.json()["evidence"]["error"]["code"] == "agent_exception"


def test_agent_server_still_answers_404_for_a_wrong_tenant():
    def refuse(body):
        raise LookupError("no such unit in this org")

    assert TestClient(make_app("receiving", refuse), raise_server_exceptions=False).post("/run", json=_body()).status_code == 404


# ---------------------------------------------------------------- stale later stages are refreshed, evidence is never rewritten
from orchestration.clients import AgentUnavailable  # noqa: E402
from orchestration.orchestrator import resume, stale_stages  # noqa: E402
from orchestration.store import EvidenceConflict  # noqa: E402
from shared.utils import records as records_module  # noqa: E402
from shared.utils.hashing import seal  # noqa: E402
from tests.helpers import Boom, Fake  # noqa: E402

STAGES = ("receiving", "prep", "pack", "returns", "recovery")
FLOW = {"flow_id": "t", "steps": [{"stage": "receiving"}, {"stage": "prep", "when": {"route": ["fba"]}},
                                   {"stage": "pack", "when": {"route": ["mfn"]}}, {"stage": "returns", "when": {"returned": [True]}},
                                   {"stage": "recovery"}],
        "defaults": {"timeout_s": 5, "retries": 0, "on_uncertain": "continue", "on_error": "continue"}}


def all_fake(**over):
    return {**{s: Fake("PASS") for s in STAGES}, **over}


def _by_stage(wf):
    return {s["stage"]: s for s in wf["stage_results"]}


def test_rerunning_an_earlier_stage_refreshes_the_later_ones_and_keeps_the_old_records():
    store = MemoryStore()
    wf = run_workflow(CASE, FLOW, store, clients=all_fake(receiving=Boom(AgentUnavailable("down"))))
    assert _by_stage(wf)["receiving"]["state"] == "error" and _by_stage(wf)["recovery"]["state"] == "completed"
    old = {k: v["record_id"] for k, v in _by_stage(wf).items() if v.get("record_id")}
    wf = resume(wf["workflow_id"], FLOW, store, clients=all_fake())
    new = _by_stage(wf)
    assert wf["status"] == "COMPLETED"
    for stage in ("prep", "returns", "recovery"):
        assert new[stage]["runs"] == 2, f"{stage} decided on the failed receiving record, so it must run again"
        assert new[stage]["record_id"] != old[stage]
        assert old[stage] in wf["evidence_references"] and store.get_evidence(old[stage]) is not None, "old evidence is kept"
    assert new["receiving"]["record_id"] in store.get_evidence(new["recovery"]["record_id"])["upstream_refs"]
    assert any(t["event"] == "stage_invalidated" and t["stage"] == "recovery" for t in wf["transitions"])


def test_an_override_counts_at_once_and_lists_the_stages_that_used_the_old_verdict():
    store = MemoryStore()
    wf = run_workflow(CASE, FLOW, store, clients=all_fake())
    rcv = _by_stage(wf)["receiving"]["record_id"]
    wf = apply_override(wf["workflow_id"], store, record_id=rcv, new_verdict="FAIL", actor="op", reason="carton crushed")
    assert wf["final_outcome"]["effective_verdicts"]["receiving"] == "FAIL", "the override takes effect immediately"
    assert stale_stages(wf) == ["prep", "returns", "recovery"], "these three used the old verdict"
    wf = resume(wf["workflow_id"], FLOW, store, clients=all_fake())
    assert stale_stages(wf) == []
    assert [_by_stage(wf)[s]["runs"] for s in ("receiving", "prep", "returns", "recovery")] == [1, 2, 2, 2]


def test_resuming_without_an_override_does_not_rerun_finished_stages():
    store = MemoryStore()
    wf = run_workflow(CASE, FLOW, store, clients=all_fake())
    wf = resume(wf["workflow_id"], FLOW, store, clients=all_fake())
    assert all(s["runs"] == 1 for s in wf["stage_results"] if s["state"] != "skipped")


def _tick(monkeypatch):
    n = iter(range(10_000))
    monkeypatch.setattr(records_module, "utcnow", lambda: f"2026-10-08T00:{next(n) // 60 % 60:02d}:{next(n) % 60:02d}Z")


def test_a_replay_that_differs_only_in_timestamps_reuses_the_stored_record(tmp_path, monkeypatch):
    """After a crash the workflow file can be older than the evidence. The organiser stubs use fixed record ids."""
    _tick(monkeypatch)
    store = FileStore(tmp_path / "out")
    first = run_workflow(CASE, store=store)
    for p in (tmp_path / "out" / "workflows").glob("*.json"):
        p.unlink()  # the crash: evidence is on disk, the workflow state is not
    second = run_workflow(CASE, store=store)  # same request ids, new clock: used to raise EvidenceConflict
    assert [s["record_id"] for s in second["stage_results"]] == [s["record_id"] for s in first["stage_results"]]
    assert any(t["event"] == "evidence_replayed" for t in second["transitions"])


def test_a_different_record_under_the_same_id_is_still_refused(tmp_path):
    from tests.stubs import receiving_stub

    class Tamper:
        calls = 0

        def run(self, request, timeout_s):
            out = receiving_stub.handle(request)
            Tamper.calls += 1
            if Tamper.calls > 1:  # the same id, but a different decision
                ev = dict(out["evidence"])
                ev["decision"] = {**ev["decision"], "reason": "changed after the fact"}
                out = {**out, "evidence": seal(ev)}
            return out

    store = FileStore(tmp_path / "out")
    first = run_workflow(CASE, store=store, clients={"receiving": Tamper()})
    rid = next(s["record_id"] for s in first["stage_results"] if s["stage"] == "receiving")
    original = store.get_evidence(rid)
    for p in (tmp_path / "out" / "workflows").glob("*.json"):
        p.unlink()
    # Refused, and recorded as the stage's error rather than taking the whole request down: the stored record stands.
    wf = run_workflow(CASE, store=store, clients={"receiving": Tamper()})
    rcv = next(s for s in wf["stage_results"] if s["stage"] == "receiving")
    assert rcv["state"] == "error" and rcv["error"]["code"] == "invalid_output" and "reused" in rcv["error"]["message"]
    assert store.get_evidence(rid) == original, "evidence is immutable"
    assert (wf.get("final_outcome") or {}).get("outcome") != "CLEAN"

