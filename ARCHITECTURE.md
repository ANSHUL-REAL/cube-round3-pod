# Architecture

This document describes the **starter**. At the bottom is a section for **your Pod's architecture**, which you must fill in and which is part of the submission. A submission whose `ARCHITECTURE.md` still only describes the starter has not documented its system.

## 1. The system

```text
                POD
                 │
       ┌─────────▼─────────┐      owns workflow state; derives status and final outcome from the evidence chain
       │    Orchestrator   │      routes · validates · records evidence · retries · handles failures and UNCERTAIN
       └─────────┬─────────┘
                 │  Agent Input ▼          ▲ Agent Output (evidence)
       ┌─────────▼─────────┐
       │     Receiving     │
       └─────────┬─────────┘
                 ↓
       ┌───────────────────┐
       │       Prep        │   (FBA units)
       └─────────┬─────────┘
                 ↓
       ┌───────────────────┐
       │       Pack        │   (merchant-fulfilled / 3PL units)
       └─────────┬─────────┘
                 ↓
       ┌───────────────────┐
       │      Returns      │   (if a return happened)
       └─────────┬─────────┘
                 ↓
       ┌───────────────────┐
       │     Recovery      │   reads ALL accumulated evidence
       └─────────┬─────────┘
                 ↓
          Final Outcome        derived by the orchestrator, not copied from any agent

  shared/schemas · shared/contracts · shared/utils      data/input · data/sample · data/expected      examples/
```

The arrows show the *expected commerce journey*. Physically, every hand-off goes through the orchestrator ([`INTEGRATION-GUIDE.md`](INTEGRATION-GUIDE.md) section 1).

## 2. Responsibilities

| Component | Responsible for | Not responsible for |
|---|---|---|
| **Agent** (`agents/<stage>/`) | One stage's judgment, returned as an Agent Output with an Evidence Record. Failing open. Refusing other tenants. | Calling other agents. Setting workflow state. Rewriting earlier evidence. |
| **Orchestrator** (`orchestration/`) | Starting workflows; identifying the current stage; invoking agents with context; validating and recording evidence; updating state; routing; retries; failures; UNCERTAIN; the final outcome. | Making stage judgments. Fabricating or deleting evidence. Turning UNCERTAIN into PASS/FAIL without an explicit rule. |
| **Contract** (`shared/schemas/`) | One strict set of data shapes. | Agent-specific logic (that goes in `payload`). |
| **Stubs** (`agents/*/app.py` as shipped) | Replaying Round 2 CSV rows as valid evidence, so the plumbing can be tested. | Pretending to be agents. |

## 3. Shared data

| Object | Owner | Lives in |
|---|---|---|
| Evidence Record | the agent that produced it (immutable) | the evidence store |
| Workflow State | **the orchestrator** | the workflow store |
| Overrides | the orchestrator records them; a person makes them | Workflow State (`overrides[]`), referencing evidence |
| Final Outcome | **the orchestrator**, derived | Workflow State (`final_outcome`) |
| Captures | the Pod | `data/input/<subject>/<stage>/`, referenced by `sha256` |

## 4. Evidence flow and workflow state

```text
Agent Result → Evidence Record → Orchestrator state transition → Next stage → New evidence → Updated workflow state → Final Outcome
```

- Each stage's evidence is stored and passed to **every later stage** as `previous_evidence`.
- State is `PENDING → IN_PROGRESS → COMPLETED`, or `FAILED` / `BLOCKED` / `RECOVERY_REQUIRED` ([`ORCHESTRATION-GUIDE.md`](ORCHESTRATION-GUIDE.md) section 5), always derived from the evidence and overrides.
- `transitions[]` is the audit trail.
- A reviewer can walk from the Final Outcome to `contributing_records`, to checks, to `evidence_refs`, to the `sha256` of the exact bytes examined.

## 5. Error handling

Every failure is **recorded and never becomes success**: a degraded evidence record stands in (no checks, UNCERTAIN, the error), the stage is `error`, the workflow `FAILED` with outcome `INCOMPLETE`. Transient failures retry; refusals and invalid output do not; UNCERTAIN is preserved; `resume` retries. Full table: [`ORCHESTRATION-GUIDE.md`](ORCHESTRATION-GUIDE.md) section 8. Tenancy: `org_id` on every request, record and workflow; a record about another org is rejected as a security event; **your storage must enforce it too**.

## 6. Final outcome

`CLEAN`, `CLAIM_RECOMMENDED`, `EXCEPTION`, `NEEDS_REVIEW` or `INCOMPLETE`, with the reason, the contributing evidence, `needs_human`, and `provisional` (true unless the workflow is `COMPLETED`). Default rules: [`ORCHESTRATION-GUIDE.md`](ORCHESTRATION-GUIDE.md) section 6.

## 7. What is fixed and what is yours

**Fixed (the contract, strict):**

- The five required agents and their stages (Specialist Pods: four agents plus integration work, see [`FAQ.md`](FAQ.md))
- Common evidence requirements: the Agent Input/Output and Evidence Record shapes; PASS / FAIL / UNCERTAIN; the status vocabularies
- Required traceability: workflow id, agent id, hashes, `upstream_refs`, overrides that reference what they supersede
- An orchestrator that owns workflow state and produces a **Final Outcome**
- Minimum testing, and the submission and evaluation requirements ([`SUBMISSION-GUIDE.md`](SUBMISSION-GUIDE.md), [`ROUND3-RUBRIC.md`](ROUND3-RUBRIC.md))

**Participant-designed (the implementation, flexible):**

- Internal architecture, programming language, frameworks, how each agent is built
- How the orchestrator is implemented (the starter is one option; LangGraph, a queue, a state machine, your own)
- The communication mechanism (in-process, HTTP, queue) as long as the contract holds
- Database, persistence, deployment platform
- UI, review queue, dashboards
- Additional services, additional features
- The final-outcome policy, routing and `on_uncertain` / `on_error` policies (documented in `docs/decisions.md`)

## 8. Extension points

| You want to… | Change |
|---|---|
| Add or reroute a stage | `orchestration/flow.json` (and write a decision) |
| Change the final decision or status rules | `orchestration/rollup.py` (and its tests, and a decision) |
| Plug in a real agent | `agents/<stage>/app.py` + `agent.json` |
| Run an agent as a service in any language | `agent.json` `mode: "http"` + [`agent-api.md`](shared/contracts/agent-api.md) |
| Run your own subjects | `data/input/<subject>/<stage>/` + a cases file |
| Add agent-specific data to evidence | `payload` (never the envelope) |
| Persist to a database | implement the four store methods in `orchestration/store.py` |

## 9. Deployment options (yours)

- **Single process:** `uvicorn orchestration.api:app` with all agents `inproc`. Simplest.
- **Orchestrator + agent services:** each agent its own process, `mode: "http"`, `<STAGE>_URL` set; `GET /health` for readiness.
- Whatever you pick, the demo runs from the submitted commit and any URL works without your accounts. The API ships with **no authentication**: add it before exposing it.

---

## Your Pod's architecture: Pod 12

### 1. Diagram

```text
  people ──► Console (orchestration/web, /ui)          scripts: evaluate.py · score_checks.py · print_labels.py
             live simulator · camera capture · overrides │
                          │ same functions, no second brain
  API (orchestration/api.py) ─────────────────────────────┤
                          ▼
                 Orchestrator (orchestration/orchestrator.py)
                 flow.json routing · timeout 30 s · 1 retry · validates every Agent Output
                 status + final outcome derived from evidence (rollup.py)
                          │ Agent Input ▼   ▲ Agent Output        ┌──────────────────────────┐
     ┌────────────────────┼────────────────────────┐              │ Store (store.py)          │
     ▼                    ▼                         ▼              │ FileStore: out/workflows/ │
 Receiving ──► Prep (FBA) | Pack (merchant) ──► Returns (if      ◄─┤ out/evidence/ (JSON,      │
 Gemini+rules  Gemini+rules  Gemini+rules        returned)         │ content-hashed, immutable)│
                                                 Gemini+rules      └──────────────────────────┘
                          ▼
                     Recovery: rules only, reads every earlier record (latest override applied)
  photos: data/input/<unit>/<stage>/ (hash-checked; another unit's photo is refused)
```

### 2. What each agent really is

| Stage | Owner | Model (default) | What decides | Built from |
|---|---|---|---|---|
| Receiving | @cherryy-x23 | `gemini-3.5-flash-lite`, 1 call per delivery, never shown the PO | rules in `agents/receiving/rules.py` | Sai charan's Round 2 agent, ported |
| Prep | @Devisri-074 | `gemini-3.5-flash-lite`, 1 call per unit, never told the expected FNSKU | rules in `agents/prep/rules.py` (demo rules, sources unverified) | Manvith111's reference, re-implemented in Python |
| Pack | @ANSHUL-REAL | `gemini-3.5-flash-lite`, 1 call per box, never shown the order | Round 2 rules, copied unchanged | Anshul's Round 2 agent |
| Returns | @krishnababuprodduturu | `gemini-3.8-flash`, 1 call per return (0 without a product reference photo) | rules grade against a condition rubric snapshot | Krishna's Round 2 agent, adapted |
| Recovery | @DaKaufeeBoii | none (0 calls) | rules per fee line, claims only on CONTRADICTS | Sai Tarun's rules, rebuilt on the organisers' fee report |

**No stage is a stub.** Every model stage returns a `pending` record (not a guess) when there is no photo, no key, or
the model fails. The four photo stages have each made real Gemini calls on real photos (`docs/REAL-RUNS.md`). The
organisers' stubs are kept in `tests/stubs/` for the plumbing tests and for the console's labelled **Replay** mode only.

### 3. Orchestrator

- **State:** one JSON document per workflow (`out/workflows/<id>.json`); each Evidence Record is its own immutable
  file (`out/evidence/<record_id>.json`) with a content hash. A record id that already exists with a different hash
  is refused (`EvidenceConflict`); a replay of the same request is accepted.
- **Calls:** in-process (`mode: inproc`) by default; any agent can be an HTTP service instead. The 30 s timeout is
  enforced for in-process agents too (a worker thread). One retry on a timeout or a retryable error.
- **Validation:** every Agent Output is checked against the JSON schemas; verdicts are a closed set
  (PASS / FAIL / UNCERTAIN); an agent that returns nothing, crashes, or can't be built is a recorded stage error.
  The run counter and request id are saved before the agent is called, so a crash mid-stage can be resumed (D-O01).
- **Overrides:** a person's override is a new entry; it never edits evidence. The latest override wins. Stages that
  used the overridden record (and later stages) are marked stale and run again on `resume`; the console resumes for
  you (D-O03). Example: overriding Prep to FAIL on UNIT-0014 re-runs Recovery and the $2.00 claim is withdrawn.

### 4. Routing and final outcome

Routing is `orchestration/flow.json`: Receiving, then Prep for FBA units or Pack for merchant-fulfilled units, Returns
when the unit came back, then Recovery. `on_uncertain` and `on_error` are `continue`, so Recovery always sees the
whole chain and a person sees one result.

Status and outcome come from `rollup.py` (precedence as documented there): a claim needs Recovery's effective verdict
to be FAIL; any other FAIL is an EXCEPTION; a stage that asks for a person makes the workflow BLOCKED / NEEDS_REVIEW;
a stage that did not finish makes it FAILED / INCOMPLETE. **UNCERTAIN is never turned into PASS.** Recovery is
precision-first: no claim on SILENT evidence, none when Receiving recorded damage that may explain the fee (D-RC06),
none from evidence captured after the charge, and weight-tier fees stay SILENT without a measurement and a sourced fee
schedule (F-07).

### 5. Tenancy

Enforced in three places: every agent refuses another organisation's unit (`LookupError`, HTTP 404); the orchestrator
refuses evidence whose subject or organisation is not the workflow's (D-O02); photo inputs must sit in the unit's own
folder, by hash. Workflow ids are validated before they touch the file system. Tested per agent
(`tests/integration/test_*_agent.py`), in `tests/integration/test_*_hardening.py`, and by `scripts/evaluate.py`
(a `wrong_tenant` injection on every stage, 100 units each). On the deployment, a fourth place: every page, photo
and API call is checked against the org(s) of the signed-in access code, and the store's list queries name those orgs,
so another org's case, workflow, record or photo answers 404 (`orchestration/web/access.py`,
`tests/integration/test_deployment_access.py`).

### 6. Failure model

`scripts/evaluate.py` breaks each stage five ways (down, timeout, invalid output, crash, evidence about another
organisation) across all 100 sample units: 2,500 workflows, 1,575 of which reach the broken stage, **0 crashes, 0 reported as clean**; every one records the
error on the right stage and ends FAILED or INCOMPLETE (`docs/evaluation.md`). In the demo: start a live workflow
with no photo, or with the model key removed, and the stage shows a recorded `no_capture` / `model_not_configured`
error; the workflow is FAILED, never clean. On the deployment, an admin switches any agent off live (down, timeout or
contract-breaking output, `orchestration/faults.py`): the stage records the error, the workflow ends FAILED or
INCOMPLETE, and after the switch is restored, Resume finishes it.

### 7. Deployment

**Live on Render, state in Supabase Postgres** (`Dockerfile`, `render.yaml`, [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md)):
one container runs the console, the API and the five agents in-process; Postgres (schema `pod12`) holds workflows,
evidence (write-once), capture and reference photos, the Pack ledger, the audit log, access codes and fault switches.
The public front page explains the system; the admin signs in with a password, everyone else with their own access
code (one org, optionally one station), and every step records who ran it.

Locally, one command on the submitted commit, no database, no sign-in:

```bash
python scripts/serve.py --data D:/pod12-demo
```

then open `http://localhost:8100/ui/sim`. `--stubs` starts the labelled Replay mode. A Gemini key goes in the
git-ignored `.env` (`GEMINI_API_KEY`); without it every model stage records `model_not_configured`.

### 8. Known limits

- Per-check accuracy on labelled photos exists for Pack only (Round 2 held-out set, warehouse bins, not packing-bench
  boxes); none yet for Receiving, Prep or Returns, and no two-person agreement figure (`docs/evaluation.md`).
- Prep's rules and Returns' condition scale were not looked up from Amazon's published sources; both say so.
- The sample data is invented and unlabelled, so claim precision against the truth is unknown.
- One container, one worker: two people pressing Run on the same unit at the same moment can race (the phone
  stations take turns by design). Photos are stored in Postgres as bytes: fine for a demo, not for production.
- Access codes and sessions are a demo-grade account system: no per-person passwords, no SSO.
- A timed-out in-process agent keeps running in its thread until it returns.
