import json
from pathlib import Path

import pytest

from orchestration.orchestrator import flow_stages

ROOT = Path(__file__).resolve().parents[1]
AGENTS = flow_stages()  # the stages in this Pod's flow (pod.json)


@pytest.fixture(scope="session")
def cases():
    return json.loads((ROOT / "data/sample/cases.json").read_text())


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
