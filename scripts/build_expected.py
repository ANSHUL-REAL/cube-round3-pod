"""Rebuild data/expected/ from the organiser stubs + standard flow (the canonical sample workflows)."""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from orchestration.orchestrator import load_flow, run_workflow  # noqa: E402
from orchestration.store import MemoryStore  # noqa: E402

flow = load_flow(ROOT / "orchestration/flow.json")
cases = json.loads((ROOT / "data/sample/cases.json").read_text())
store, expected = MemoryStore(), {}
for case in cases:
    wf = run_workflow(case, flow, store)
    fo = wf["final_outcome"]
    expected[wf["workflow_id"]] = {"status": wf["status"], "outcome": fo["outcome"], "needs_human": fo["needs_human"],
                                   "claimable_usd": fo["claimable_usd"]}
(ROOT / "data/expected/final-outcomes.sample.json").write_text(json.dumps(expected, indent=2) + "\n")

canon = next(c for c in cases if c["unit_id"] == "UNIT-0014")
wf = run_workflow(canon, flow, MemoryStore())
(ROOT / "data/expected/canonical-workflow.json").write_text(json.dumps({
    "description": "The canonical sample workflow: UNIT-0014 (FBA, returned). With the organiser stubs, Prep says compliant, "
                   "so the channel's inbound-defect fee is contradicted and Recovery recommends a claim; the weight-tier fee "
                   "has no evidence and is left unclaimed.",
    "case": canon,
    "expected_stage_verdicts": {s["stage"]: s["verdict"] for s in wf["stage_results"]},
    "expected_stage_states": {s["stage"]: s["state"] for s in wf["stage_results"]},
    "expected_status": wf["status"],
    "expected_final_outcome": {k: wf["final_outcome"][k] for k in ("outcome", "verdict", "needs_human", "claimable_usd", "provisional")},
}, indent=2) + "\n")
import collections
print("expected:", dict(collections.Counter((v["status"], v["outcome"]) for v in expected.values())))
