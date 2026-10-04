# Participant Guide

The main guide for Round 3. Read [`START-HERE.md`](START-HERE.md) first.

## 1. The Pod model

Every Pod has five participants, and each owns one Round 2 agent.

```text
Member 1 → Receiving        Member 4 → Returns
Member 2 → Prep             Member 5 → Recovery
Member 3 → Pack
```

**Specialist Pods** (no Prep Manager) have four agent owners (Receiving, Pack, Returns, Recovery) and a **Specialist / Integration Engineer**. The Specialist is not scored as having built a sixth agent: they are assessed on integration, orchestration, reliability and system-level contribution. They **lead and coordinate** the orchestration, the cross-agent hand-offs, validation and error handling, end-to-end testing, evidence and deployment, and help the others with their adapters. They do not own it alone. If you are a Specialist Pod (the organisers will tell you), set `pod_type` and `flow` in `pod.json` so the starter runs `orchestration/flow.specialist.json`.

## 2. Individual responsibility

You are responsible for:

- **Your Round 2 agent**: it is yours to understand, explain and defend.
- **Bringing it into the Round 3 repository** (`agents/<your-agent>/`, with `PROVENANCE.md`).
- **Adapting it to the shared contract**: Agent Input in, Agent Output with a valid Evidence Record out.
- **Maintaining its functionality**: integration must not quietly break what your agent did in Round 2. You may change it where integration, reliability, compatibility or improvement requires.
- **Providing integration tests** for your agent, on your own fixtures.
- **Working with the other participants**: reviewing their PRs, agreeing hand-offs, helping when their stage blocks yours.

## 3. Shared Pod responsibility

The entire Pod **jointly** owns:

- the **orchestration** (and workflow state)
- the **shared contracts** and schemas
- **integration** between stages
- **end-to-end testing**
- **deployment** where applicable
- the **final documentation** (README, ARCHITECTURE, decisions)
- the **demo**
- the **final submission**

> **No participant owns the final system alone.**

Pick an **orchestration coordinator** in your first session (a Specialist Pod's Specialist is the natural choice). A coordinator keeps the orchestration moving and merges its PRs *after the Pod has agreed*. A coordinator is not an owner, and everyone must be able to explain the orchestrator in the demo.

## 4. How the Pod works together

```text
 1. Fork the official starter repository (one fork per Pod; see GITHUB-GUIDE.md)
 2. Assign responsibilities (fill in pod.json and .github/CODEOWNERS)
 3. Bring in the Round 2 agents
 4. Agree the integration architecture (read INTEGRATION-GUIDE + ORCHESTRATION-GUIDE together)
 5. Implement contract compatibility (each agent passes the contract tests)
 6. Build / connect the orchestrator
 7. Integrate the agents one by one (replace stubs one at a time; the system always runs)
 8. Test each hand-off
 9. Run end-to-end tests
10. Test the failure and UNCERTAIN scenarios
11. Finalise the documentation
12. Prepare and rehearse the demo
13. Submit
```

**Integrate on day one with the stubs, and replace them one at a time.** The system should never be broken for long, and you should always have something that runs end to end.

## 5. Where each participant works

```text
agents/
├── receiving/   ← Member 1        ├── pack/       ← Member 3        ├── recovery/ ← Member 5
├── prep/        ← Member 2        └── returns/    ← Member 4
```

Inside your folder:

```text
agents/<your-agent>/
├── app.py          ← REPLACE THE STUB: expose handle(agent_input) -> agent_output   (and `app = make_app(...)` for HTTP)
├── agent.json      ← agent_id · owner · mode (inproc | http) · url · an honest `implementation` description
├── PROVENANCE.md   ← your Round 2 repo URL + commit this came from (you create it)
├── README.md       ← what it does, how to run it, its limits (rewrite the starter text)
└── …               ← your Round 2 code, prompts, rules, fixtures
```

**Shared areas** need Pod-level coordination: a change there affects everyone.

| Area | What | Rule |
|---|---|---|
| `orchestration/` | The orchestrator | Discuss as a Pod before merging; any member may propose |
| `shared/` | Schemas, contracts, utilities | The contract envelope is organiser-owned; add to `payload`, or open a `contract` issue. Always discuss as a Pod |
| `tests/` | Integration and end-to-end tests | Add freely; never weaken a test to turn CI green |
| `docs/` | Build log, decisions | Everyone edits; keep both sides on conflict |

## 6. Integrating your agent: step by step

1. Copy your Round 2 code into `agents/<your-agent>/`; add `PROVENANCE.md`.
2. In `handle()`: read the Agent Input (`subject`, `inputs`, `previous_evidence`, `context`), run your agent (**one batched model call per unit**), and return `build_output(build_record(...))` from `shared/utils/records.py`.
3. **Fail open:** on a model error return `pending_output(...)`, never an exception. **Never invent evidence:** if you did not see it, say UNCERTAIN with a reason.
4. **Refuse other tenants:** raise `LookupError` for a subject that is not under `subject.org_id`.
5. Update `agent.json` honestly.
6. `pytest tests/integration/test_agent_contracts.py` on your own fixtures, then `make test`.

Study the stub next to yours and the examples in [`examples/`](examples/).

## 7. Build expectations

- **It runs.** `make setup && make run` works on a clean clone.
- **It integrates for real.** By submission, `agent.json` describes what you actually built; a stub left in the flow is shown as a stub.
- **It speaks the contract.** Valid outputs, honest verdicts and confidence, real `evidence_refs`, hashes of the real inputs.
- **UNCERTAIN is real.** Your agent can say it; the system shows it to a human.
- **Rules are looked up.** Where the channel publishes a requirement, retrieve it and record its source; do not let a model recall it. The sample CSVs' flags and amounts are dummy.
- **Cost-aware.** Report `model.calls` and `model.cost_usd`.
- **Say what you built.** A content hash is not "tamper-evident"; a stub is not an agent.

## 8. Testing expectations

| Level | Question | Where | Minimum |
|---|---|---|---|
| **Agent integration** | Can the next agent consume the previous agent's output? | `tests/integration/test_agent_contracts.py` | Passes for every agent in your flow, on **your** fixtures |
| **Workflow state / routing** | Do status and final outcome follow the rules? | `tests/integration/test_workflow_state.py` | Cover every rule you changed |
| **End to end** | Can the complete system process one case from start to final outcome? | `tests/e2e/` | ≥ 1 clean, ≥ 1 exception, ≥ 1 claim, on your own units |
| **Failure** | Does it behave correctly when something goes wrong? | `test_workflow_state.py` | Timeout, unavailable, invalid output, wrong tenant: recorded, never success |
| **UNCERTAIN** | Does evidence stay intact and does the orchestrator handle the state? | `test_workflow_state.py` | An UNCERTAIN case ends `BLOCKED`/`NEEDS_REVIEW` with its evidence untouched, and an override resolves it |
| **Tenancy** | Does a second org see nothing? | contract + e2e | Wrong tenant refused at every agent |

Report **numbers with a method**, not "it works well": units, matches, false positives and negatives separately, UNCERTAIN rate. Do not delete a test to make CI green.

## 9. Final submission checklist

See [`SUBMISSION-GUIDE.md`](SUBMISSION-GUIDE.md) for the literal list. Your Pod should be able to say yes to:

```text
[ ] All required agents integrated; no unlabelled stubs in the flow
[ ] An orchestrator that owns workflow state is working
[ ] make test is green on a clean clone; CI green on main
[ ] End-to-end on our own units incl. an UNCERTAIN, a failure and a claim
[ ] docs/decisions.md records our flow, final-outcome rules, orchestration and position on each known finding
[ ] ARCHITECTURE.md describes OUR system; README.md accurate for our Pod
[ ] No secrets in the repo or its history
[ ] Demo rehearsed to time; deployment URL verified (if applicable)
[ ] LinkedIn post published (mandatory in Round 2; confirm for Round 3)
[ ] Tag round3-final on the commit we submit
```

## 10. Do's and don'ts

**Do:** integrate early with the stubs; keep `main` runnable; small PRs reviewed by someone else; raise contradictions as findings; treat `needs_human` as a feature and show it; ask for a `contract` change early; credit your sources.

**Don't:** rewrite your agent from scratch; change the contract's envelope inside your Pod; turn UNCERTAIN into PASS to make a demo look clean; let an agent write workflow state; edit another member's `agents/` folder without telling them and getting a review; commit secrets, `.env`, real customer data or large datasets; touch other Pods' repos or your Round 2 repo; call something "tamper-evident", "immutable" or "production-ready" unless you can show it; leave integration for the last day.
