# Integration Guide

> **How exactly do five independently built agents become one system?** Through one contract, one hand-off model, and one owner of state.

```text
Receiving → Prep → Pack → Returns → Recovery → Final Commerce Outcome
```

That line is the *architecture*, not a mandatory wiring. A unit takes the FBA route (Prep) or the merchant-fulfilled route (Pack), and Returns exists only if something came back. The Pod designs the orchestration. The contract the agents speak is fixed.

## 1. The hand-off model

Agents never hand off to each other. Everything goes through the orchestrator:

```text
Agent A
  ↓  Agent Output  (result + Evidence Record)
Orchestrator   ── validates · stores the evidence · updates workflow state · decides the next transition
  ↓  Agent Input  (subject + this stage's captures + ALL previous evidence + overrides)
Agent B
  ↓  Agent Output
Orchestrator   …and so on through every stage, then derives the Final Outcome
```

Why: each agent stays testable alone, replaceable, and language-agnostic, and there is exactly one place where workflow state lives.

## 2. The common agent interface

The schemas are strict, the transport is your choice. Every agent logically supports:

**Input** ([`agent-input`](shared/schemas/agent-input.schema.json))

| Field | |
|---|---|
| `workflow_id` | The workflow this stage belongs to |
| `subject` `{org_id, subject_id, route}` | The unit/order, its tenant, and its route (`fba` / `mfn` / `unknown`) |
| `stage` | The current stage (which is the agent being called) |
| `inputs[]` | This stage's captures, content-addressed (`ref`, `sha256`) |
| `previous_evidence[]` | Every Evidence Record already produced in this workflow, in order (read-only) |
| `context` | Stage information and hints; always includes `context.overrides` (use the **latest** as effective) |
| `request_id` | Idempotency key |

**Output** ([`agent-output`](shared/schemas/agent-output.schema.json))

| Field | |
|---|---|
| `agent_id`, `stage` | Who, and at which stage |
| `status` | `completed` / `pending` / `error` |
| `verdict`, `confidence` | `PASS` / `FAIL` / `UNCERTAIN`, and how confident in it |
| `evidence` | The Evidence Record: checks, decision, inputs, hashes ([contract](EVIDENCE-CONTRACT.md)) |
| `timestamp`, `model` | When, and which model/version (or `rules` / `stub` / `human`) |
| `error` | Error information where applicable |
| `next_step_recommendation` | `continue` / `review` / `retry` / `stop` / `route_to_recovery` / `complete`: **advice**, the orchestrator decides |

**How it travels is yours to choose**: in-process Python `handle(agent_input) -> agent_output`, or an HTTP service with `GET /health` and `POST /run` (any language; spec in [`shared/contracts/agent-api.md`](shared/contracts/agent-api.md)). Your agent is described by `agents/<stage>/agent.json`.

## 3. State ownership

> **The Pod Orchestrator is the authoritative owner of workflow state. Agent outputs are evidence used to inform state transitions.**

Agents do not silently overwrite global workflow state. An agent cannot mark a workflow complete, failed or blocked, and cannot change another stage's result. It returns its own evidence and a recommendation; the orchestrator updates `stage_results`, `status` and `final_outcome` ([`ORCHESTRATION-GUIDE.md`](ORCHESTRATION-GUIDE.md)).

## 4. Evidence ownership

> Previous evidence must remain traceable. If a decision is changed or overridden, the new record must reference the previous evidence/decision and record the reason, actor, and timestamp.

- Each agent owns **its own** evidence records: it produces them, and they are immutable once produced.
- The orchestrator owns **storing** them and the **workflow-level overrides** that reference them.
- A downstream agent **reads** previous evidence and lists what it used in `upstream_refs`, citing the specific record in the `evidence_refs` of the check that relied on it. It never rewrites it. If it disagrees, it says `conflicting_evidence` in its own check.
- If a previous record has overrides, use the latest override as the effective verdict (and you may cite the original).

## 4a. What each stage reads and produces

| Stage | Reads (inputs) | Reads (previous evidence) | Produces | Hands on |
|---|---|---|---|---|
| **Receiving** | Photos at point of receipt, PO line | nothing | Identity, quantity, damage, quality verdicts | Condition on arrival; supplier-shortfall evidence |
| **Prep** | Photos of the prepped unit, work order | Receiving | Compliance verdict per requirement; measurements if available | Compliance proof; weight/dimensions |
| **Pack** | Photo of the open box, order lines | Receiving | Contents at seal: seal or stop-and-fix | What was actually sent |
| **Returns** | Photos of the returned parcel, parts list | Pack (what was sent), Receiving | Identity, completeness, condition, disposition | Condition and disposition |
| **Recovery** | Channel fee / reimbursement report lines | **All** earlier evidence | Per-charge position (supports / contradicts / silent), a claim with evidence and an amount | The claim |

**Captures.** For the integrated demo, put each stage's captures in `data/input/<subject_id>/<stage>/`. The orchestrator finds them, hashes them and passes them as `inputs`. No images ship with the starter.

## 5. Bringing your Round 2 agent in

1. Copy your code into `agents/<stage>/` (or keep it as a service and put a thin adapter in `app.py`). Add `PROVENANCE.md`: your Round 2 repo URL and commit.
2. Make `handle()` map **Agent Input → your agent's input**, and **your agent's result → an Agent Output** (build it with `shared/utils/records.py: build_record()` and `build_output()`).
3. Update `agent.json`: `agent_id`, `owner`, `mode`, and an honest `implementation` description.
4. Run `pytest tests/integration/test_agent_contracts.py` on your fixtures. When it passes, your agent is integrated.

You may change your Round 2 agent where integration, reliability or compatibility requires it. You are not rebuilding it.

## 6. The flow, and common data

The flow is data ([`orchestration/flow.json`](orchestration/flow.json)); routing facts (`route`, `returned`) come with the case. See [`ORCHESTRATION-GUIDE.md`](ORCHESTRATION-GUIDE.md) section 7. Common structures: Agent Input, Agent Output, Evidence Record, Workflow State, Final Outcome, Error: all in [`shared/schemas/`](shared/schemas/), with validated examples in [`examples/`](examples/). Utilities: `shared/utils/` (hashing, schema validation, record builders, logging, FastAPI factory).

## 7. What happens on each outcome

| Agent outcome | Expected behaviour |
|---|---|
| **PASS** | Continue according to the workflow. |
| **FAIL** | Follow the defined failure/recovery path: the evidence is recorded; Recovery assesses what is recoverable; final outcome `EXCEPTION` or `CLAIM_RECOMMENDED`. |
| **UNCERTAIN** | **Preserve the evidence** and let the orchestrator determine the next action by policy (continue or block). It never silently becomes PASS or FAIL. A person resolves it with an override. |
| **Timeout** | Handled by the retry/error policy; recorded as `agent_timeout`; the workflow is `FAILED`/`INCOMPLETE` until a retry succeeds. |
| **Agent unavailable** | **Record the error. Never report success.** |
| **Invalid output / wrong tenant** | Rejected and recorded; a degraded record stands in; never accepted. |

Your agent's own obligations (engineering rules): **batch your model calls** (one per unit, report `model.calls`); **fail open** (on a model error return a `pending` output, do not raise to the line); **never invent evidence**; **refuse other tenants** (`LookupError` → HTTP 404).

## 8. Known open questions

The Round 2 pods found real contradictions in the shared data and documents: what `unit_id` means (F-08), no weight evidence for most fee lines (F-07), zero-amount reimbursements (F-09), supplier shortfall vs channel loss (F-10), returns on FBA-routed units (F-11), units with neither Prep nor Pack (F-12). They are recorded in [`docs/decisions.md`](docs/decisions.md). **Do not silently pick a side**: state your assumption there and design so that changing it is cheap.

## 9. Logging and health

Structured JSON logs via `shared/utils/log.py` (`LOG_LEVEL=INFO` shows the orchestrator's audit trail), always with `workflow_id`, `stage`, `org_id`; **never** secrets, tokens or image bytes. Every HTTP agent serves `GET /health`; the orchestrator API's `GET /health` reports every agent in the flow and says `degraded` if one is down.

## 10. Integration checklist

```text
[ ] Each agent passes tests/integration/test_agent_contracts.py on our own fixtures
[ ] Each next stage consumes the previous stage's output (the hand-off test passes)
[ ] agent.json says what each agent really is; no unlabelled stub in the flow
[ ] An UNCERTAIN case reaches a human-visible outcome with its evidence intact
[ ] Killing an agent leaves a recorded error and a FAILED/INCOMPLETE workflow, not a crash and not a success
[ ] A wrong-tenant request is refused by every agent
[ ] Recovery's claims each cite previous evidence and an amount; SILENT charges are listed, not claimed
[ ] An override on one record changes the effective outcome and references what it supersedes
[ ] The final-outcome rules are ours, written down, and tested
```
