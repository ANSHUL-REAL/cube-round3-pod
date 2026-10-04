# Orchestration Guide

For whoever on the Pod is building the orchestrator. In practice that is everyone, because **the Pod jointly owns the orchestration** and nobody owns the final system alone. The starter [`orchestration/`](orchestration/) already runs end to end on the stubs; this guide says what it must keep doing and where you are free to change it.

## 1. The source of truth

```text
Agent Result  →  Evidence Record  →  Orchestrator state transition  →  Next stage  →  New evidence  →  Updated workflow state  →  Final Outcome
```

> **The Pod Orchestrator is the authoritative owner of workflow state. Agent outputs are evidence used to inform state transitions.**

> The latest agent outcome alone is **not** the source of truth. Recovery's interpretation alone is **not** the source of truth. The authoritative system state is the **orchestrator state derived from the traceable evidence chain.**

Recovery consumes the accumulated evidence and can produce a *new* decision and evidence record, but it does not silently rewrite the history. In the starter this is literal: workflow status and final outcome are **derived** by pure functions ([`orchestration/rollup.py`](orchestration/rollup.py)) from the stored evidence and the overrides, never copied from an agent's last answer.

## 2. What the orchestrator is responsible for

| Responsibility | In the starter |
|---|---|
| **Starting workflows** | `run_workflow(case)` creates `WF-<org>-<subject>` (idempotent: an existing workflow is advanced, not duplicated) |
| **Identifying the current stage** | `current_stage` / `previous_stage`; every stage listed in `stage_results` from the start |
| **Invoking the appropriate agent** | `clients.py`: in-process `handle()` or HTTP `POST /run` |
| **Passing required context** | Agent Input: subject, this stage's captures, **all previous evidence**, workflow overrides, case context |
| **Receiving agent output** | Validated before it is accepted: schema, stage, workflow, tenant, hash, consistency |
| **Recording evidence** | Stored in the evidence store, immutable; `evidence_references` lists every record |
| **Updating workflow state** | `stage_results`, `status`, `transitions` (audit trail) |
| **Determining the next transition** | `flow.json` routing (`when`) and policy (`on_uncertain`, `on_error`) |
| **Handling failures** | Retry transient, never retry refusals, record every error, never hide one |
| **Handling UNCERTAIN** | Preserve it; policy decides continue or block; a person resolves it with an override |
| **Handling retries** | `retries` and `timeout_s` per flow/step; `resume` retries failed stages |
| **Producing the final outcome** | `derive_final_outcome`, with reasons, contributing evidence, `provisional` flag |

## 3. The orchestrator must NOT

- **Fabricate evidence.** A failed stage gets a *degraded record* (no checks, UNCERTAIN, status `pending`/`error`, the error). It says "this stage did not complete". It is never a judgment.
- **Delete previous evidence.** The store refuses to replace a record. Retried stages add new records; the failed attempt stays.
- **Silently overwrite an agent's result.** A person's disagreement is an **override** that references the record; the agent's own verdict stays on record.
- **Convert UNCERTAIN to PASS/FAIL without an explicit rule.** Only an override (actor, reason, timestamp) or a documented policy in `docs/decisions.md` may do that.
- **Mark a workflow successful when required stages were not completed.** `COMPLETED` is only possible when every required stage finished and nothing awaits a person. The tests check this.

## 4. Minimum orchestrator state

[`workflow-state.schema.json`](shared/schemas/workflow-state.schema.json). The state must be able to identify:

```text
workflow_id            WF-org_demo_alpha-UNIT-0014
subject_id             UNIT-0014              (+ org_id, flow_id, context: route, returned)
current_stage          prep                   previous_stage: receiving
status                 PENDING | IN_PROGRESS | COMPLETED | FAILED | BLOCKED | RECOVERY_REQUIRED     (+ status_reason)
stage_results[]        per stage: state, agent_id, record_id, verdict, outcome, needs_human, runs, attempts, error, timings
evidence_references[]  every record_id, in order; never removed
timestamps             created_at, updated_at, completed_at
errors[]               every failure, with code and retryable
overrides[]            append-only, each referencing the evidence it supersedes
final_outcome          derived; null until something has run
transitions[]          the audit trail
halted                 set when policy stopped the workflow for a person
```

## 5. Workflow statuses (fixed meanings: do not invent others)

Derived, in this precedence (highest first) by [`derive_status`](orchestration/rollup.py):

| Status | Means | Typical cause | How it ends |
|---|---|---|---|
| `PENDING` | Created; nothing has run. | Just created. | First stage starts. |
| `FAILED` | A **required stage did not complete** after the retry policy. | Timeout, agent down, refusal, invalid output, tenant mismatch. | `resume` retries the stage. |
| `RECOVERY_REQUIRED` | A stage's effective verdict is **FAIL** and **Recovery has not completed**: something may be recoverable. | Workflow halted before Recovery; Recovery not yet run. | Recovery runs. |
| `BLOCKED` | **A person must decide**: a stage asks for a human, or policy halted the workflow. | `UNCERTAIN` with `needs_human`; `on_uncertain: block`. | An override, then `resume` if halted. |
| `IN_PROGRESS` | Required stages remain to run. | Mid-run. | Stages finish. |
| `COMPLETED` | Every required stage finished and nothing awaits a person. | | Terminal (until a later override changes the derivation). |

The agent-level `status` (`completed` / `pending` / `error`) is a different, smaller vocabulary: it says whether *an agent* managed to judge. Workflow statuses belong to the orchestrator alone.

## 6. Final outcome

Derived, highest precedence first. This is the **default policy**: improve it and justify the change in [`docs/decisions.md`](docs/decisions.md).

| Condition | Final outcome | Meaning |
|---|---|---|
| Recovery's effective verdict is FAIL | `CLAIM_RECOMMENDED` | A charge is contradicted by evidence. `claimable_usd` set. |
| Any other stage's effective verdict is FAIL | `EXCEPTION` | A real-world problem was found; no claim to make from it. |
| A required stage did not complete | `INCOMPLETE` | The system could not finish judging. |
| A stage asks for a human | `NEEDS_REVIEW` | A person must look. |
| Otherwise | `CLEAN` | Every required stage passed. |

Safeguards: `needs_human` is also set on a claim or exception that rests on an incomplete or uncertain stage; `provisional` is true whenever the status is not `COMPLETED`; `contributing_records` always lists the evidence behind the outcome (an outcome with no evidence is a bug); `effective_verdicts` shows each stage's verdict after overrides.

The questions that are yours to answer (and to write down): Should a Receiving shortfall make a later claim more or less likely? (Careful: a supplier-side shortfall is not channel-side loss, finding F-10.) Does evidence quality or confidence change the result? When should the system halt rather than continue? What should happen when Pack says `stop_and_fix`?

## 7. The flow

[`orchestration/flow.json`](orchestration/flow.json) is data:

```jsonc
{ "flow_id": "standard-v1",
  "steps": [ { "stage": "receiving" },
             { "stage": "prep",    "when": { "route": ["fba"] } },
             { "stage": "pack",    "when": { "route": ["mfn"] } },
             { "stage": "returns", "when": { "returned": [true] } },
             { "stage": "recovery" } ],
  "defaults": { "timeout_s": 30, "retries": 1, "on_uncertain": "continue", "on_error": "continue" } }
```

- **`when`** decides if a stage applies. A stage that does not apply is `skipped` **with the reason recorded**, decided when the workflow is created. A stage is **never** skipped because an agent was unavailable (that is an error, recorded as one).
- **`on_uncertain`**: `continue` (the UNCERTAIN evidence travels on, a person is asked) or `block` (halt; remaining stages stay `pending`). Block only applies when the UNCERTAIN result actually asks for a person: Recovery's "no evidence, so no claim" does not halt anything.
- **`on_error`**: `continue` (fail open: other stages' evidence is not lost; the workflow ends `FAILED`/`INCOMPLETE`) or `block`.
- Any key can be set per step. The step order is the order of execution. Need parallel stages, a re-check loop, or a different order? Change `advance()` and write a decision.
- **Specialist Pods** use [`flow.specialist.json`](orchestration/flow.specialist.json) (no Prep). `pod.json` selects the flow.

## 8. How each situation is handled

| Agent outcome | Orchestrator behaviour |
|---|---|
| PASS | Record; continue to the next applicable stage. |
| FAIL | Record; continue (default). The stage is a real finding, not an error. Status becomes `RECOVERY_REQUIRED` only if the workflow stops before Recovery completes. |
| UNCERTAIN | **Preserve the evidence exactly.** Policy decides continue or block; final outcome `NEEDS_REVIEW`; status `BLOCKED` until a person decides. |
| Timeout | Retry (`retries`), then a degraded `pending` record with `agent_timeout`; stage `error`; workflow `FAILED`. |
| Agent unavailable | Same, `agent_unavailable`. **Recorded as an error. Never reported as success.** |
| Refused (4xx, wrong tenant) | **No retry.** Degraded record, `agent_rejected`. |
| Crash | Degraded record, `agent_exception`. The orchestrator survives. |
| Invalid output | Rejected, degraded record: `invalid_output` (schema, wrong stage/workflow, bad hash, output and evidence disagree) or `tenant_mismatch` (a **security event**: evidence about another org). |

Examples of each: [`examples/`](examples/).

## 9. Persistence, overrides, resume

- **Store.** `MemoryStore` (tests) and `FileStore` (`out/workflows/<id>.json`, `out/evidence/<record_id>.json`, atomic writes) share a four-method interface. Replace with a database if you need one; keep evidence immutable. Tenancy must be enforced **there** too (row-level security, scoped object keys).
- **Override.** `apply_override(workflow_id, store, record_id=…, new_verdict=…, actor=…, reason=…)`: requires an actor and a reason, must target real evidence, references the previous effective decision, never touches the original record, re-derives status and outcome.
- **Resume.** `resume(workflow_id)` runs any stage that has not completed (errored stages are retried; the failed attempt's evidence stays) and clears a halt. An override alone does not resume a halted workflow: a person decides to.
- **Idempotency.** `request_id = <workflow_id>:<stage>` (`:rN` on a re-run), so a retry of the same request must return the same `record_id`. Completed stages are never re-run.

## 10. Run and inspect it

```sh
make run                                                    # all sample workflows -> out/
make case UNIT=UNIT-0014 ORG=org_demo_alpha                 # one, printed in full
python -m orchestration.run --resume WF-org_demo_alpha-UNIT-0014
python -m orchestration.run --override WF-… --record PRP-0014 --verdict PASS --actor you --reason "…" --and-resume
make serve        # POST /workflows · GET /workflows/{id}[/evidence] · POST …/resume · POST …/overrides · GET /health
```

## 11. What is left to you

Concurrency across workflows; a real database; a human-review queue and UI for `BLOCKED` workflows; authentication on the API; backoff; parallel or looping flows; a richer final-outcome policy; deployment. Add what your design needs, document it in `ARCHITECTURE.md`, and keep the six behaviours that the tests enforce:

1. Previous evidence and overrides are passed to every stage.
2. Every agent output is validated (including **tenant** and **hash**) before it is accepted.
3. Transient failures retry; refusals do not.
4. Every failure is **recorded** as evidence + error and **never** becomes success.
5. Evidence is never deleted or replaced; overrides reference what they supersede.
6. Status and final outcome are derived from the evidence, and a workflow is never `COMPLETED` with a required stage incomplete.
