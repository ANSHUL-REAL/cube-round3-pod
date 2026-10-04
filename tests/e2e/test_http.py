"""The same flow over real HTTP: proves /health, /run, 404 tenancy, 422 validation, and timeouts work as documented.

Starts all stub agents as uvicorn servers on free ports. If your agent is not Python, this still exercises it as long
as agent.json has mode "http" and your service is up (see shared/contracts/agent-api.md).
"""
import importlib
import socket
import threading
import time

import httpx
import pytest
import uvicorn

from orchestration.orchestrator import load_flow, run_workflow
from tests.conftest import AGENTS, make_input

pytestmark = pytest.mark.http


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def servers():
    started, urls = [], {}
    for stage in AGENTS:
        port = free_port()
        server = uvicorn.Server(uvicorn.Config(importlib.import_module(f"agents.{stage}.app").app,
                                               host="127.0.0.1", port=port, log_level="error"))
        threading.Thread(target=server.run, daemon=True).start()
        urls[stage] = f"http://127.0.0.1:{port}"
        started.append(server)
    for url in urls.values():
        for _ in range(100):
            try:
                httpx.get(f"{url}/health", timeout=1)
                break
            except httpx.HTTPError:
                time.sleep(0.05)
    yield urls
    for s in started:
        s.should_exit = True


@pytest.fixture
def http_mode(servers, monkeypatch):
    monkeypatch.setenv("ORCH_MODE", "http")
    for stage, url in servers.items():
        monkeypatch.setenv(f"{stage.upper()}_URL", url)
    return servers


def test_health_endpoints(http_mode):
    for stage, url in http_mode.items():
        body = httpx.get(f"{url}/health").json()
        assert body["status"] == "ok" and body["stage"] == stage and body["contract_version"] == "1.0"


def test_bad_input_is_422_and_wrong_tenant_is_404(http_mode, cases):
    url = http_mode["receiving"]
    assert httpx.post(f"{url}/run", json={"nonsense": True}).status_code == 422
    case = cases[0]
    other = "org_demo_bravo" if case["org_id"] == "org_demo_alpha" else "org_demo_alpha"
    assert httpx.post(f"{url}/run", json=make_input("receiving", {**case, "org_id": other})).status_code == 404
    assert httpx.post(f"{url}/run", json=make_input("recovery", case)).status_code == 422, "an agent refuses another stage's input"


def test_full_workflow_over_http_matches_in_process(http_mode, cases, monkeypatch):
    case = next(c for c in cases if c["route"] == "fba" and c["returned"])
    over_http = run_workflow(case)
    monkeypatch.setenv("ORCH_MODE", "inproc")
    in_proc = run_workflow(case)
    assert (over_http["status"], over_http["final_outcome"]["outcome"]) == (in_proc["status"], in_proc["final_outcome"]["outcome"])
    assert all(s["state"] in ("completed", "skipped") for s in over_http["stage_results"])


def test_dead_agent_is_recorded_not_hidden(monkeypatch, cases):
    monkeypatch.setenv("ORCH_MODE", "http")
    monkeypatch.setenv("RECEIVING_URL", f"http://127.0.0.1:{free_port()}")  # nothing listening
    flow = {**load_flow(), "defaults": {"timeout_s": 1, "retries": 0, "on_uncertain": "continue", "on_error": "continue"}}
    wf = run_workflow(cases[0], flow)
    sr = wf["stage_results"][0]
    assert sr["state"] == "error" and sr["error"]["code"] == "agent_unavailable" and wf["status"] == "FAILED"
