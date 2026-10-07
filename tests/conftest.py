import json
from pathlib import Path

import pytest

from orchestration.orchestrator import flow_stages

ROOT = Path(__file__).resolve().parents[1]
AGENTS = flow_stages()  # the stages in this Pod's flow (pod.json)


@pytest.fixture(scope="session")
def cases():
    return json.loads((ROOT / "data/sample/cases.json").read_text())


# The real agents need photos and a model, so they cannot answer the organiser's plumbing tests (orchestration, workflow
# state, examples, HTTP) the way a CSV-replay stub does. Those tests run every stage on the organiser's stub, kept verbatim
# in tests/stubs/. A test module that tests real agents says which: `REAL_AGENTS = {"pack", "returns"}` at module level.
STUB_STAGES = {stage: f"tests.stubs.{stage}_stub" for stage in ("receiving", "prep", "pack", "returns", "recovery")}


def stub_module(stage: str, module) -> str | None:
    """The stub module that stands in for `stage` in this test module, or None to use the real agent."""
    if stage in getattr(module, "REAL_AGENTS", ()):
        return None
    return STUB_STAGES.get(stage)


@pytest.fixture(autouse=True)
def plumbing_stubs(request, monkeypatch):
    import orchestration.clients as clients

    pinned = request.node.get_closest_marker("needs_stubs")
    if pinned:
        real_here = [s for s in pinned.args if s in getattr(request.module, "REAL_AGENTS", ())]
        if real_here:
            pytest.skip(f"pinned to the organiser stub; {', '.join(real_here)} is a real agent in this module")

    real = clients.load_manifest

    def load_manifest(stage):
        manifest = real(stage)
        stub = stub_module(stage, request.module)
        return {**manifest, "module": stub, "mode": "inproc"} if stub and manifest.get("mode") == "inproc" else manifest

    monkeypatch.setattr(clients, "load_manifest", load_manifest)


@pytest.fixture(autouse=True)
def inproc_by_default(monkeypatch):
    """Tests run in-process unless a test opts into HTTP. Remove this if all your agents are HTTP-only."""
    monkeypatch.setenv("ORCH_MODE", "inproc")


def applies(stage: str, case: dict) -> bool:
    return {"receiving": True, "recovery": True, "prep": case["route"] == "fba",
            "pack": case["route"] == "mfn", "returns": case["returned"]}[stage]


def make_input(stage: str, case: dict, previous=None, overrides=None) -> dict:
    wf = f"WF-{case['org_id']}-{case['unit_id']}"
    return {"schema_version": "1.0", "request_id": f"{wf}:{stage}", "workflow_id": wf, "stage": stage,
            "subject": {"org_id": case["org_id"], "subject_id": case["unit_id"], "route": case["route"]},
            "inputs": [], "previous_evidence": previous or [], "context": {"overrides": overrides or [], "case": case}}
