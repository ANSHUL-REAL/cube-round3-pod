"""The console's live simulator: start a story, run it one agent at a time, start over, and per-workflow modes."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from orchestration import api
from orchestration.orchestrator import advance, load_flow, new_workflow, restart, start, step
from orchestration.store import MemoryStore

REAL_AGENTS = {"receiving", "prep", "pack", "returns", "recovery"}
H = {"origin": "http://testserver"}
CASE = {"org_id": "org_demo_alpha", "unit_id": "UNIT-0014", "route": "fba", "returned": True}


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setattr(api, "STORE", MemoryStore())
    monkeypatch.delenv("POD_UI_STUBS", raising=False)
    return TestClient(api.app)


def _wid(resp) -> str:
    return resp.headers["location"].split("/ui/w/")[1].split("?")[0].split("#")[0]


def _state(client, wid):
    return client.get(f"/ui/w/{wid}/state").json()


def _ran(st):
    return [s["stage"] for s in st["stages"] if s["state"] in ("completed", "error")]


def test_simulator_page_lists_the_demo_stories_and_both_modes(client):
    page = client.get("/ui/sim")
    assert page.status_code == 200
    for unit in ("UNIT-0014", "UNIT-0008", "UNIT-0044", "UNIT-0016", "UNIT-0023"):
        assert unit in page.text
    assert 'value="live"' in page.text and 'value="replay"' in page.text


def test_start_runs_nothing_and_each_step_runs_exactly_one_agent(client):
    r = client.post("/ui/sim/start", data={"unit": "UNIT-0014", "mode": "replay"}, headers=H, follow_redirects=False)
    assert r.status_code == 303
    wid = _wid(r)
    st = _state(client, wid)
    assert st["status"] == "PENDING" and _ran(st) == [] and st["mode"] == "replay"
    order = []
    for _ in range(4):
        assert client.post(f"/ui/w/{wid}/step", headers=H, follow_redirects=False).status_code == 303
        st = _state(client, wid)
        order.append(_ran(st)[-1])
        assert len(_ran(st)) == len(order)
    assert order == ["receiving", "prep", "returns", "recovery"]
    assert st["status"] == "COMPLETED"
    assert st["final_outcome"]["outcome"] == "CLAIM_RECOMMENDED"  # our real Recovery, on the organisers' recorded evidence
    assert st["final_outcome"]["claimable_usd"] == 2.0


def test_stepping_moves_past_an_errored_stage_instead_of_repeating_it(client):
    """Live mode with no photos: Receiving honestly fails (no capture); the next step must run the next agent."""
    wid = _wid(client.post("/ui/sim/start", data={"unit": "UNIT-0014", "mode": "live"}, headers=H, follow_redirects=False))
    client.post(f"/ui/w/{wid}/step", headers=H)
    st = _state(client, wid)
    assert st["mode"] == "live" and _ran(st) == ["receiving"]
    assert st["stages"][0]["state"] == "error"
    client.post(f"/ui/w/{wid}/step", headers=H)
    assert _ran(_state(client, wid)) == ["receiving", "prep"]


def test_start_over_keeps_every_earlier_record_and_runs_again_under_new_ids(client):
    wid = _wid(client.post("/ui/sim/start", data={"unit": "UNIT-0014", "mode": "replay"}, headers=H, follow_redirects=False))
    client.post(f"/ui/w/{wid}/resume", headers=H)
    before = api.STORE.load_workflow(wid)
    first_recovery = next(sr["record_id"] for sr in before["stage_results"] if sr["stage"] == "recovery")
    assert client.post(f"/ui/w/{wid}/restart", headers=H, follow_redirects=False).status_code == 303
    wf = api.STORE.load_workflow(wid)
    assert all(sr["state"] in ("pending", "skipped") for sr in wf["stage_results"])
    assert set(before["evidence_references"]) <= set(wf["evidence_references"])  # nothing deleted
    assert any(t["event"] == "restarted" for t in wf["transitions"])
    client.post(f"/ui/w/{wid}/resume", headers=H)
    wf = api.STORE.load_workflow(wid)
    new_recovery = next(sr["record_id"] for sr in wf["stage_results"] if sr["stage"] == "recovery")
    assert new_recovery != first_recovery and api.STORE.get_evidence(first_recovery) is not None
    assert wf["final_outcome"]["outcome"] == "CLAIM_RECOMMENDED"


def test_starting_a_story_again_in_another_mode_starts_it_over_in_that_mode(client):
    wid = _wid(client.post("/ui/sim/start", data={"unit": "UNIT-0014", "mode": "replay"}, headers=H, follow_redirects=False))
    client.post(f"/ui/w/{wid}/resume", headers=H)
    client.post("/ui/sim/start", data={"unit": "UNIT-0014", "mode": "live"}, headers=H)
    st = _state(client, wid)
    assert st["mode"] == "live" and _ran(st) == []


def test_simulator_refuses_unknown_modes_units_and_other_sites(client):
    assert client.post("/ui/sim/start", data={"unit": "UNIT-0014", "mode": "fake"}, headers=H).status_code == 422
    assert client.post("/ui/sim/start", data={"unit": "UNIT-9999", "mode": "live"}, headers=H).status_code == 422
    assert client.post("/ui/sim/start", data={"unit": "UNIT-0014", "mode": "live"},
                       headers={"origin": "http://evil.example"}).status_code == 403
    assert client.post("/ui/w/..%5Cx/step", headers=H).status_code == 404
    assert client.get("/ui/w/WF-nope/state").status_code == 404


def test_workflow_page_shows_progress_controls_and_mode(client):
    wid = _wid(client.post("/ui/sim/start", data={"unit": "UNIT-0014", "mode": "replay"}, headers=H, follow_redirects=False))
    page = client.get(f"/ui/w/{wid}")
    assert "0 of 4 agents have answered" in page.text and "Next step" in page.text and "Replay of recorded evidence" in page.text
    client.post(f"/ui/w/{wid}/resume", headers=H)
    page = client.get(f"/ui/w/{wid}")
    assert "4 of 4 agents have answered" in page.text and "Next step" not in page.text


# ------------------------------------------------------------ orchestrator level
def _flow():
    return load_flow()


def test_advance_max_stages_and_retry_errors_flags():
    store = MemoryStore()
    calls = []

    class Boom:
        def run(self, request, timeout_s):
            calls.append(request["stage"])
            raise RuntimeError("no")

    clients = {s: Boom() for s in ("receiving", "prep", "returns", "recovery")}
    wf = new_workflow(CASE, _flow())
    wf = advance(wf, _flow(), store, clients, max_stages=1, retry_errors=False)
    assert calls == ["receiving"] and wf["stage_results"][0]["state"] == "error"
    wf = advance(wf, _flow(), store, clients, max_stages=1, retry_errors=False)
    assert calls == ["receiving", "prep"]  # the errored stage was not repeated
    advance(wf, _flow(), store, clients)  # a full run retries errors
    assert calls.count("receiving") == 2


def test_start_is_idempotent_and_step_and_restart_need_a_workflow():
    store = MemoryStore()
    a = start(CASE, _flow(), store, extra_context={"console_mode": "replay"})
    b = start(CASE, _flow(), store)
    assert a["workflow_id"] == b["workflow_id"] and b["context"]["console_mode"] == "replay"
    with pytest.raises(KeyError):
        step("WF-missing", _flow(), store)
    with pytest.raises(KeyError):
        restart("WF-missing", store, why="x")


def test_page_title_is_plain_text(client):
    """Regression: the step script was once pasted into the <title> block."""
    wid = _wid(client.post("/ui/sim/start", data={"unit": "UNIT-0014", "mode": "replay"}, headers=H, follow_redirects=False))
    page = client.get(f"/ui/w/{wid}").text
    title = page[page.index("<title>"):page.index("</title>")]
    assert "<script" not in title and "UNIT-0014" in title
    assert page.count("<script>") == 3  # theme, theme toggle, step controls: once each


def test_a_persons_override_shows_on_the_stage_card(client):
    wid = _wid(client.post("/ui/sim/start", data={"unit": "UNIT-0014", "mode": "replay"}, headers=H, follow_redirects=False))
    client.post(f"/ui/w/{wid}/resume", headers=H)
    prep = next(sr["record_id"] for sr in api.STORE.load_workflow(wid)["stage_results"] if sr["stage"] == "prep")
    client.post(f"/ui/w/{wid}/override", headers=H,
                data={"record_id": prep, "new_verdict": "FAIL", "actor": "Tester", "reason": "warning label missing"})
    page = client.get(f"/ui/w/{wid}").text
    assert "Tester</b> changed this to" in page and "warning label missing" in page
    st = _state(client, wid)
    assert st["final_outcome"]["claimable_usd"] in (0, 0.0, None)  # Recovery ran again on the overridden verdict
