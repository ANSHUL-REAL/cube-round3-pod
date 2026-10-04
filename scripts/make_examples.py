"""Regenerate examples/ from real runs of the stub agents, so the examples always validate. `make examples`"""
import json
import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ["ORCH_MODE"] = "inproc"
from orchestration.clients import AgentTimeout, AgentUnavailable, client_for  # noqa: E402
from orchestration.orchestrator import apply_override, load_flow, resume, run_workflow  # noqa: E402
from orchestration.store import MemoryStore  # noqa: E402
from shared.utils.schema import errors  # noqa: E402
from tests.helpers import Boom, Mangle  # noqa: E402

FLOW = load_flow(ROOT / "orchestration/flow.json")
CASES = json.loads((ROOT / "data/sample/cases.json").read_text())
OUT = ROOT / "examples"
SCHEMA_BY_PREFIX = {"agent-input": "agent-input", "agent-output": "agent-output", "workflow-state": "workflow-state",
                    "final-outcome": "final-outcome", "evidence": "evidence"}


def save(folder: str, name: str, doc: dict) -> None:
    schema = SCHEMA_BY_PREFIX.get(name.split(".")[0])
    if schema:
        assert errors(schema, doc) == [], (folder, name, errors(schema, doc)[:3])
    (OUT / folder / name).write_text(json.dumps(doc, indent=2) + "\n")


def wf_of(case, flow=FLOW, clients=None, store=None):
    store = store or MemoryStore()
    return run_workflow(case, flow, store, clients), store


def outputs(case, wf, store, folder, stages=None):
    """Re-ask each stage that ran and save the Agent Output it returned (stubs are deterministic)."""
    previous = []
    for sr in wf["stage_results"]:
        if sr["state"] == "skipped" or (stages and sr["stage"] not in stages) or not sr["record_id"]:
            continue
        ev = store.get_evidence(sr["record_id"])
        req = {"schema_version": "1.0", "request_id": f"{wf['workflow_id']}:{sr['stage']}", "workflow_id": wf["workflow_id"],
               "stage": sr["stage"], "subject": {"org_id": case["org_id"], "subject_id": case["unit_id"], "route": case["route"]},
               "inputs": [], "previous_evidence": previous, "context": {"overrides": [], "case": wf["context"]}}
        out = client_for(sr["stage"]).run(req, 30)
        if sr["stage"] in ("prep",) and folder in ("end-to-end",):
            save(folder, "agent-input.prep.json", req)
        save(folder, f"agent-output.{sr['stage']}.json", out)
        previous.append(ev)


def first(pred):
    for c in CASES:
        wf, st = wf_of(c)
        if pred(c, wf, st):
            return c
    raise SystemExit("no sample case matches")


def cell(s):
    """How a stage shows in a markdown table."""
    if s["state"] == "skipped":
        return "skipped: " + s["skipped_reason"]
    return "{} / {}".format(s["outcome"], s["verdict"])


def stage(wf, name):
    return next(s for s in wf["stage_results"] if s["stage"] == name)


shutil.rmtree(OUT, ignore_errors=True)
for d in ("happy-path", "uncertain-path", "failure-path", "end-to-end"):
    (OUT / d).mkdir(parents=True)

# ---------------------------------------------------------------- end-to-end (canonical claim)
case = next(c for c in CASES if c["unit_id"] == "UNIT-0014")
wf, store = wf_of(case)
(OUT / "end-to-end" / "case.json").write_text(json.dumps(case, indent=2) + "\n")
outputs(case, wf, store, "end-to-end")
save("end-to-end", "workflow-state.json", wf)
save("end-to-end", "final-outcome.json", wf["final_outcome"])
claim = store.get_evidence(stage(wf, "recovery")["record_id"])
silent = [c for c in claim["payload"]["charges"] if c["position"] == "SILENT"]
(OUT / "end-to-end" / "README.md").write_text(f"""# End-to-end: {case['unit_id']} (FBA, returned) -> claim recommended

The canonical sample workflow. Run it yourself: `make case UNIT={case['unit_id']} ORG={case['org_id']}`
(or `python -m orchestration.run --case examples/end-to-end/case.json`).

| Stage | Result | Evidence record |
|---|---|---|
""" + "\n".join(
    f"| {s['stage']} | {cell(s)} | {s['record_id'] or '-'} |" for s in wf["stage_results"]) + f"""

**Final:** status `{wf['status']}`, outcome `{wf['final_outcome']['outcome']}`, claimable **${wf['final_outcome']['claimable_usd']:.2f}**.

## How the evidence moved

1. The orchestrator created workflow `{wf['workflow_id']}` (`workflow-state.json`) and decided routing: FBA, so Prep runs and Pack is skipped; a return happened, so Returns runs.
2. For each stage it sent an **Agent Input** (see `agent-input.prep.json`: note `previous_evidence` carries the Receiving record) and received an **Agent Output** (`agent-output.<stage>.json`) whose `evidence` is the stored Evidence Record.
3. Recovery received all earlier evidence. It found that Prep says the unit was compliant, so the channel's inbound-defect fee is **contradicted**, and it cites `{', '.join(next(c for c in claim['payload']['charges'] if c['position'] == 'CONTRADICTS')['evidence_record_ids'])}` as the supporting evidence.
4. {len(silent)} other charge(s) have no evidence ({', '.join(c['charge_type'] for c in silent)}) and are **left unclaimed with the reason recorded**: silence never becomes a claim.
5. The orchestrator **derived** the status and `final-outcome.json` from the evidence chain. Recovery recommended the claim; the orchestrator owns the decision.

Every link is traceable: `final-outcome.contributing_records` -> `agent-output.*.evidence` -> `checks[].evidence_refs` -> `inputs[].sha256`; `workflow-state.transitions` is the audit trail.

(The stub agents replay the Round 2 CSV and their claim rules are illustrative. Your agents decide for real.)
""")

# ---------------------------------------------------------------- happy path
happy = first(lambda c, w, s: c["route"] == "fba" and c["returned"] and w["final_outcome"]["outcome"] == "CLEAN" and w["status"] == "COMPLETED")
happy_mfn = first(lambda c, w, s: c["route"] == "mfn" and w["final_outcome"]["outcome"] == "CLEAN" and w["status"] == "COMPLETED")
for c, suffix in ((happy, ""), (happy_mfn, ".mfn")):
    w, s = wf_of(c)
    (OUT / "happy-path" / f"case{suffix}.json").write_text(json.dumps(c, indent=2) + "\n")
    save("happy-path", f"workflow-state{suffix}.json", w)
    save("happy-path", f"final-outcome{suffix}.json", w["final_outcome"])
    if not suffix:
        outputs(c, w, s, "happy-path")
w, _ = wf_of(happy)
w2, _ = wf_of(happy_mfn)
def row(w):
    return "\n".join(f"| {s['stage']} | {cell(s)} |" for s in w["stage_results"])

(OUT / "happy-path" / "README.md").write_text(f"""# Happy path: everything passes -> CLEAN

`case.json` is {happy['unit_id']} (FBA, returned). `case.mfn.json` is {happy_mfn['unit_id']} (merchant-fulfilled, so Pack runs instead of Prep).

| Stage | Result ({happy['unit_id']}) |
|---|---|
{row(w)}

Final: status `{w['status']}`, outcome `{w['final_outcome']['outcome']}`, `needs_human: {w['final_outcome']['needs_human']}`.

{happy_mfn['unit_id']} (`workflow-state.mfn.json`): Receiving -> Pack -> Recovery, also `{w2['final_outcome']['outcome']}`. Prep is skipped because Amazon packs FBA boxes and the seller packs the others: a unit takes one route.

A stage is `skipped` only when the flow's `when` rule says it does not apply, and the reason is recorded. It is never skipped because an agent was inconvenient.
""")

# ---------------------------------------------------------------- uncertain path
unc = first(lambda c, w, s: c["route"] == "fba" and stage(w, "prep")["verdict"] == "UNCERTAIN")
(OUT / "uncertain-path" / "case.json").write_text(json.dumps(unc, indent=2) + "\n")
w_cont, st_cont = wf_of(unc)
outputs(unc, w_cont, st_cont, "uncertain-path", stages=["prep"])
save("uncertain-path", "workflow-state.continue.json", w_cont)
block_flow = {**FLOW, "flow_id": FLOW["flow_id"] + "+block-on-uncertain", "defaults": {**FLOW["defaults"], "on_uncertain": "block"}}
(OUT / "uncertain-path" / "flow.block.json").write_text(json.dumps(block_flow, indent=2) + "\n")
w_blk, st_blk = wf_of(unc, block_flow)
save("uncertain-path", "workflow-state.blocked.json", w_blk)
prep_id = stage(w_blk, "prep")["record_id"]
apply_override(w_blk["workflow_id"], st_blk, record_id=prep_id, new_verdict="PASS", actor="op_amira",
               reason="Retook the photo: label is flat and the polybag is sealed")
w_res = resume(w_blk["workflow_id"], block_flow, st_blk)
save("uncertain-path", "workflow-state.resolved.json", w_res)
save("uncertain-path", "final-outcome.resolved.json", w_res["final_outcome"])
(OUT / "uncertain-path" / "README.md").write_text(f"""# Uncertain path: Prep says UNCERTAIN ({unc['unit_id']})

UNCERTAIN means "the evidence is insufficient for a reliable judgment". It is not a low-confidence PASS, and the system never turns it into one.

| File | Policy | What happened |
|---|---|---|
| `agent-output.prep.json` | | Prep returned `UNCERTAIN` with an `uncertain_reason` on the failing checks, and `needs_human: true`. The evidence is stored as is. |
| `workflow-state.continue.json` | `on_uncertain: continue` (default) | Later stages still ran. Final: **{w_cont['status']} / {w_cont['final_outcome']['outcome']}**, `provisional: {str(w_cont['final_outcome']['provisional']).lower()}`. A person must decide. |
| `workflow-state.blocked.json` | `on_uncertain: block` (`flow.block.json`) | The orchestrator **halted** at Prep (`halted` is set); Returns and Recovery are still `pending`. Status **{w_blk['status']}**. |
| `workflow-state.resolved.json` | | `op_amira` overrode Prep's decision to PASS with a reason (`overrides[0]`, which references Prep's record and the previous verdict), then the workflow was resumed. Status **{w_res['status']}**, outcome **{w_res['final_outcome']['outcome']}**. |

Things to notice:
- Prep's original evidence record is **unchanged**. The override is a new entry in the workflow state, with actor, timestamp, reason, `original_verdict`, `previous_verdict`, `new_verdict`.
- `final_outcome.effective_verdicts.prep` shows the verdict after the override; the stage still reports the agent's own verdict.
- `transitions` records the halt, the override, the resume and every status change.

Try it: `python -m orchestration.run --case examples/uncertain-path/case.json --flow examples/uncertain-path/flow.block.json`
then `--override {w_blk['workflow_id']} --record {prep_id} --verdict PASS --actor you --reason "retook the photo" --and-resume`.
""")

# ---------------------------------------------------------------- failure path
base = happy
exc = first(lambda c, w, s: w["status"] == "COMPLETED" and w["final_outcome"]["outcome"] == "EXCEPTION" and c["route"] == "fba")
w_fail, st_fail = wf_of(exc)
save("failure-path", "workflow-state.agent-fail.json", w_fail)
scenarios = {
    "timeout": Boom(AgentTimeout("no answer in 30s")),
    "unavailable": Boom(AgentUnavailable("connection refused")),
    "invalid-output": Mangle("prep", "tampered"),
    "tenant-mismatch": Mangle("prep", "other_tenant"),
}
rows = []
for name, client in scenarios.items():
    w, s = wf_of(base, clients={"prep": client})
    sr = stage(w, "prep")
    save("failure-path", f"workflow-state.{name}.json", w)
    save("failure-path", f"evidence.degraded.{name}.json", s.get_evidence(sr["record_id"]))
    rows.append(f"| `{name}` | `{sr['error']['code']}` | {sr['attempts']} | `{w['status']}` | `{w['final_outcome']['outcome']}` |")
(OUT / "failure-path" / "README.md").write_text(f"""# Failure path

What the orchestrator does when something goes wrong. **Failures are recorded, never hidden, and never become success.**

## A stage returns FAIL (a real finding): `workflow-state.agent-fail.json` ({exc['unit_id']})
Not an error: the agent worked and found a problem. Status **{w_fail['status']}**, outcome **{w_fail['final_outcome']['outcome']}** ({w_fail['final_outcome']['reason']})

## The agent breaks (Prep in {base['unit_id']})

| Scenario | Recorded error code | Attempts | Workflow status | Final outcome |
|---|---|---:|---|---|
{chr(10).join(rows)}

In every case:
- A **degraded evidence record** is stored (`evidence.degraded.<scenario>.json`): status `pending`/`error`, **no checks**, verdict `UNCERTAIN`, `needs_human: true`, and the `error`. It records that the stage did not complete. It is not a judgment, and nothing is fabricated.
- Transient failures (`agent_timeout`, `agent_unavailable`) are **retried** (`attempts: 2` = 1 try + 1 retry); refusals and invalid output are **not**.
- The stage is `error`, the workflow status is **FAILED**, the outcome is **INCOMPLETE** and `provisional`. It is never COMPLETED or CLEAN.
- By default the workflow continues so the other stages' evidence is not lost (`on_error: continue`; set `block` to halt).
- `resume` retries the failed stage. The failed attempt's evidence **stays** in `evidence_references`.
- `tenant-mismatch`: the agent returned evidence about a different org. It is rejected as a security event, not accepted as data.
""")
print("examples written:", sorted(str(p.relative_to(OUT)) for p in OUT.rglob("*") if p.is_file()))
