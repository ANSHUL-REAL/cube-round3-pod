# Cube Buildathon · Round 3 · Pod Integration Build

**Commerce Context stream · Round 3 · Pod build**

> Five agents, one unit, one record that follows it. In Round 3 your Pod connects the five Round 2 agents into **one commerce system**.

**New here? Read [`START-HERE.md`](START-HERE.md) first.** This README is the concise overview; the detailed rules live in the guides.

## Pod 12: what is built, and what is not

Five agents plus the orchestrator, one `agents/<stage>/` folder each. Every stage is a real agent that reads photos (or, for Recovery, the fee report and all earlier evidence) and returns a Round 3 Evidence Record. **No stage is a stub.**

| Stage | Owner | Built from | What it does | Tests |
|---|---|---|---|---|
| Receiving | @cherryy-x23 | Sai charan's Round 2 agent, rebuilt | Model reports what it sees (never shown the PO); rules compare with the PO line | `tests/integration/test_receiving_agent.py` |
| Prep | @Devisri-074 | Organiser's reference Prep (Manvith111), ported to Python | Model transcribes what is on the unit; rules check the work order | `tests/integration/test_prep_agent.py` |
| Pack | @ANSHUL-REAL | Anshul's Round 2 agent, core unchanged | Model lists what is in the box; rules compare with the order: seal, stop and fix, or a person decides | `tests/integration/test_pack_agent.py` |
| Returns | @krishnababuprodduturu | Krishna Babu's Round 2 agent, core unchanged | Model observes the returned parcel; rules grade identity, completeness and condition, reading Pack and Receiving | `tests/integration/test_returns_agent.py` |
| Recovery | @DaKaufeeBoii | Sai Tarun's Round 2 rules, rebuilt on the organisers' fee report | Deterministic rules decide which charges evidence contradicts; precision first | `tests/integration/test_recovery_agent.py` |

Where each stage came from, and what changed, is in its `PROVENANCE.md`. Every design choice is in [`docs/decisions.md`](docs/decisions.md).

**Read this before you rely on any number.**
- **No real-model run has been done through this repository.** All tests use scripted models, so they check capture handling, rules, record mapping, fail-open, tenancy and hand-offs, **not what a real model sees**. No Round 3 accuracy is claimed. Round 2 results belong to the Round 2 repositories and do not transfer.
- **The organisers' sample has no photos**, so `make run` on the sample ends every workflow `FAILED` / `INCOMPLETE` with `no_capture`. That is the intended behaviour: an agent with nothing to look at must not invent a verdict. To demo, take the photos in [`docs/DEMO-CAPTURE-PLAN.md`](docs/DEMO-CAPTURE-PLAN.md) (`python scripts/capture_plan.py`).
- **Rule sources are unverified** for Prep (compliance rules) and Returns (condition scale): they are labelled so in the records. Nobody looked up Amazon's published rules.
- **Known orchestrator gap:** overriding an earlier stage after Recovery has run does not re-run Recovery (see D-RC12).

```sh
make setup && make test          # tests need no API key
python -m agents.pack.check --unit UNIT-0008 --org org_demo_alpha box.jpg   # one stage, one photo (Pack, Prep and Receiving have a check CLI)
python scripts/capture_plan.py   # which demo photos are still missing
```

## Objective

Integrate the five independently built Round 2 agents into one connected, end-to-end commerce workflow, and show it working. **Integrate → Orchestrate → Test → Deploy → Demonstrate.** Not a rebuild.

## What the Pod builds

```text
Receiving → Prep → Pack → Returns → Recovery → Final Commerce Outcome
```

| Member | Agent | Folder |
|---|---|---|
| 1 | Receiving Manager | `agents/receiving/` |
| 2 | Prep Manager | `agents/prep/` |
| 3 | Pack Manager | `agents/pack/` |
| 4 | Returns Manager | `agents/returns/` |
| 5 | Recovery Manager | `agents/recovery/` |

Each member owns one agent. The Pod jointly owns the orchestration, shared contracts, workflow state, integration, end-to-end testing, documentation, demo and submission. **No participant owns the final system alone.**

## Architecture in one picture

```text
              ┌────────────────────────── Orchestrator (owns workflow state) ───────────────────────────┐
 case ──────▶ │ route · pass previous evidence · validate · record evidence · retry · UNCERTAIN · outcome │ ──▶ Workflow State
              └────┬─────────┬─────────┬─────────┬─────────┬────────────────────────────────────────────┘      + Final Outcome
        Agent Input ▼         │         │         │         │  ▲ Agent Output (result + Evidence Record)
              Receiving     Prep      Pack     Returns   Recovery     ← each: in-process handle()  OR  HTTP /health + /run
```

- **One contract.** Every agent takes an *Agent Input* and returns an *Agent Output* containing an *Evidence Record*: per-check verdicts (PASS / FAIL / **UNCERTAIN**), confidence, model/version, timestamps, hashes.
- **One owner of state.** The orchestrator derives workflow status and the final outcome from the evidence chain. Agent outputs inform; they do not set state.
- **Failures are recorded, never hidden,** and never become success.

Details: [`ARCHITECTURE.md`](ARCHITECTURE.md) · [`INTEGRATION-GUIDE.md`](INTEGRATION-GUIDE.md) · [`ORCHESTRATION-GUIDE.md`](ORCHESTRATION-GUIDE.md).

## Quick setup and how to run

Requires Python 3.11+.

```sh
make setup            # venv + dependencies + .env
make test             # integration, end-to-end, failure, UNCERTAIN, override and HTTP tests
make run              # all sample workflows end to end -> out/workflows/*.json and out/evidence/*.json
make case UNIT=UNIT-0014 ORG=org_demo_alpha     # one workflow, in full
make serve            # orchestrator API on :8100 (POST /workflows, GET /workflows/{id}, GET /health)
```

Out of the box everything runs on **organiser stub agents** replaying the synthetic Round 2 CSVs. **Replacing a stub with your real agent is your job.**

Run an agent as its own service:

```sh
.venv/bin/uvicorn agents.prep.app:app --port 8102
curl localhost:8102/health          # then set "mode": "http" in agents/prep/agent.json
```

## Where participants put their agents

`agents/<stage>/` (`app.py` exposes `handle()`; `agent.json` describes your agent). Shared areas need Pod-level coordination: `orchestration/`, `shared/`, `tests/`, `docs/`. See [`PARTICIPANT-GUIDE.md`](PARTICIPANT-GUIDE.md).

## How the agents connect

Through the orchestrator only. It sends each agent an Agent Input (subject, this stage's captures, **all previous evidence**, overrides), validates and stores the Agent Output's evidence, updates workflow state, and decides what runs next. See [`INTEGRATION-GUIDE.md`](INTEGRATION-GUIDE.md).

## Required environment variables

Copy `.env.example` to `.env`. **Never commit `.env`.**

| Variable | Purpose | Default |
|---|---|---|
| `ORCH_MODE` | Force `inproc` or `http` for all agents | each `agent.json` |
| `ORCH_FLOW` | Flow file for the API | the flow in `pod.json` |
| `<STAGE>_URL` | Where an `http`-mode agent listens (`PREP_URL`, …) | `agent.json` `url` |
| `OUT_DIR` | Where workflow state and evidence are written | `out` |
| `DATA_DIR`, `INPUT_DIR` | Sample CSVs for the stubs; your per-stage captures | `data/sample`, `data/input` |
| `LOG_LEVEL`, `LOG_FORMAT` | Logging | `WARNING`, `json` |
| Model provider keys | Whatever *your* agents use (e.g. `ANTHROPIC_API_KEY`) | none |

## Example end-to-end workflow

`UNIT-0014` (FBA, returned). Full files in [`examples/end-to-end/`](examples/end-to-end/).

```text
Receiving  RCV-0014  accept          PASS   ─┐
Prep       PRP-0014  compliant       PASS    │  every record is stored and handed forward as previous_evidence
Pack       skipped (route = fba: Amazon packs it)
Returns    RTN-0014  liquidate       PASS   ─┤
Recovery   RCY-UNIT-0014  claim_recommended  FAIL ◀─┘
             • inbound_defect_fee  $2.00  CONTRADICTS  <- cites PRP-0014 (Prep says compliant)
             • weight-tier fee     $4.75  SILENT       <- no measured weight upstream; NOT claimed
Workflow status COMPLETED · Final outcome CLAIM_RECOMMENDED ($2.00, evidence attached)
```

(The stubs' claim rules are illustrative; your Recovery agent decides for real.) Also see [`examples/happy-path/`](examples/happy-path/), [`examples/uncertain-path/`](examples/uncertain-path/), [`examples/failure-path/`](examples/failure-path/).

## Repository structure

```text
START-HERE.md  README.md  PARTICIPANT-GUIDE.md  GITHUB-GUIDE.md  RULES.md  FAQ.md
ARCHITECTURE.md  INTEGRATION-GUIDE.md  EVIDENCE-CONTRACT.md  ORCHESTRATION-GUIDE.md
ROUND3-RUBRIC.md  SUBMISSION-GUIDE.md  DEMO-GUIDE.md  pod.json  .env.example
agents/{receiving,prep,pack,returns,recovery}/   app.py · agent.json · README.md
orchestration/       flow.json · orchestrator.py · rollup.py · store.py · clients.py · run.py (CLI) · api.py
shared/schemas/      agent-input · agent-output · evidence · workflow-state · final-outcome · error
shared/contracts/    agent-api.md        shared/utils/   hashing · schema validation · record builders · logging · server
data/input/ (yours) · data/sample/ (Round 2 synthetic CSVs) · data/expected/ (golden outcomes for the stubs)
examples/{happy-path,uncertain-path,failure-path,end-to-end}/      tests/{integration,e2e}/      docs/{build-log,decisions}.md
```

## Submission overview

Your Pod's final repository (tagged), a working integrated system, documentation and architecture, a demo, a deployment URL if applicable, evaluation and testing evidence, and the LinkedIn post URL. The literal checklist, the process and the finality rules are in [`SUBMISSION-GUIDE.md`](SUBMISSION-GUIDE.md). Dates and the submission form are **TBA**.

---

*CUBE Buildathon · Commerce Context · Round 3*
