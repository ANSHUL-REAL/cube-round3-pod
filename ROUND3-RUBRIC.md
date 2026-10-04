# Round 3 Rubric

The Pod is evaluated **as a system**. Round 3 assesses team integration and system-level execution; Round 2 performance remains part of the final result. How the two combine is **TBA**.

> **Five agents working independently is not equivalent to a successfully integrated Pod system. The complete workflow must work.**

> **Status: the point values are a draft proposal (total 100) and are TBA until the organisers confirm them.** The nine criteria are fixed.

## At a glance

| # | Criterion | Points (draft) | The question it asks |
|---|---|---:|---|
| 1 | **End-to-end integration** | 15 | Do the agents work as one system, on real units, from receipt to a final outcome? |
| 2 | **Agent interoperability** | 10 | Does each agent honour the contract, and can the next agent consume its output? |
| 3 | **Orchestration** | 10 | Does an orchestrator own workflow state, and is the design justified and robust? |
| 4 | **Decision quality** | 15 | Is the final decision sound, and does it use evidence and uncertainty well? |
| 5 | **Evidence & traceability** | 15 | Can you walk from the final outcome back to the exact inputs, check by check? |
| 6 | **Reliability & error handling** | 10 | What happens when things go wrong? |
| 7 | **UX & demo** | 10 | Can a person use it and understand it, and does the demo show the real thing? |
| 8 | **Engineering quality** | 10 | Is it clean, tested, secure, reproducible and honest? |
| 9 | **Team collaboration** | 5 | Did five people build one thing together? |
| | **Total** | **100** | |

Each criterion is judged on what is **demonstrably working in the submitted commit**, backed by the repo, tests, logs and demo. Claims without evidence score nothing.

## How each criterion is judged

### 1 · End-to-end integration (15)
| Strong | Weak |
|---|---|
| Real units, with the Pod's real agents, go through the whole workflow and produce a Final Outcome, live in the demo. | Runs only on the organiser stubs, or on one hand-picked unit. |
| Different routes (FBA / merchant-fulfilled / returned / not returned) all work. | A stub is silently left in the flow. |
| `agent.json` honestly says what each agent is. | Five demos stitched together by hand. |

### 2 · Agent interoperability (10)
| Strong | Weak |
|---|---|
| Every output validates; hash, timestamps, model/version, `upstream_refs` correct. | Valid only by accident; no `evidence_refs`. |
| Downstream agents genuinely **use** previous evidence (Recovery cites Prep/Returns records; honours overrides). | Output is valid but nothing downstream reads it. |
| Passes the contract and hand-off tests on the Pod's **own** fixtures. | Tests only run on the sample CSVs. |
| Honest `unit_scope` and `refs` (finding F-08). | Joins on a bare id with no thought for it. |

### 3 · Orchestration (10)
| Strong | Weak |
|---|---|
| The orchestrator **owns workflow state**: statuses, `stage_results`, audit trail, evidence references, derived from the evidence chain. | State lives in an agent or is "whatever the last agent said". |
| Flow, routing and `on_uncertain` / `on_error` policy are deliberate and in `docs/decisions.md`, with options rejected. | The starter, unchanged and unexplained. |
| Retries, timeouts, idempotency, resume; a human-review path for `BLOCKED`. | Works only when everything is healthy. |
| Never fabricates or deletes evidence; never converts UNCERTAIN without an explicit rule. | Silent overwrites, silent skips. |

### 4 · Decision quality (15)
| Strong | Weak |
|---|---|
| The final-outcome and status rules were **thought about**: how evidence quality, confidence and UNCERTAIN change the result; what is claimed and what is left alone. | The default roll-up, unexamined. |
| Never claims on SILENT evidence (a wrong claim costs more than a missed one) and can say why. | Claims what the evidence does not support. |
| Documents a position on each known finding that affects it (F-07 … F-12). | Silently picks a side. |
| Quantified: per-check numbers, false positives and negatives **separately**, UNCERTAIN rate, method written down. | "It works well." |

Specialist Pods are assessed on the four-agent flow; lacking Prep is not penalised, but how the system behaves without Prep evidence is part of this score.

### 5 · Evidence & traceability (15)
| Strong | Weak |
|---|---|
| Final outcome → `contributing_records` → checks → `evidence_refs` → `inputs[].sha256` is complete and navigable, ideally in a UI. | Verdicts with no cited evidence. |
| Overrides reference what they supersede, with actor, reason and timestamp; the original is preserved; the latest is used downstream. | Overrides overwrite the original. |
| Honest about what the hash is. | Calls a content hash "tamper-proof". |
| Rule sources recorded (where a requirement came from). | A model "remembered" the rule. |

### 6 · Reliability & error handling (10)
| Strong | Weak |
|---|---|
| Demonstrably breaks an agent (timeout, down, invalid output, wrong tenant) and the system **records the error**, ends `FAILED`/`INCOMPLETE`, and can `resume`. | A failure crashes the run, or is reported as success. |
| Wrong-tenant requests are refused at every agent **and** the storage layer. | Tenancy only in the orchestrator. |
| A model failure still saves the capture and produces a `pending` record. | The operator waits, or data is lost. |
| Health endpoints and structured logs make a failure diagnosable. | `print` statements. |

### 7 · UX & demo (10)
| Strong | Weak |
|---|---|
| Follows [`DEMO-GUIDE.md`](DEMO-GUIDE.md): a real run, an UNCERTAIN, a failure, a claim, the evidence trail. | A deck about a system. |
| A person sees verdicts, confidence, UNCERTAIN and evidence without reading JSON. | Raw JSON on screen. |
| In time, rehearsed, every member speaks to their part. | Overruns; one person talks. |
| Shows limits and failures honestly. | Only the happy path. |

### 8 · Engineering quality (10)
| Strong | Weak |
|---|---|
| Clean clone → `make setup && make test && make run`. CI green. | Works on one laptop. |
| Tests cover integration, end-to-end, failure, UNCERTAIN, overrides; none weakened to pass. | Few tests, or deleted ones. |
| No secrets; complete `.env.example`; sensible structure; accurate docs. | Keys in history; README describes the starter. |
| Cost and calls reported; batched model calls. | One call per check; no cost awareness. |

### 9 · Team collaboration (5)
| Strong | Weak |
|---|---|
| PRs with real reviews; shared-file changes discussed; everyone's commits under their own name; conflicts resolved well. | One person's commits, or one giant last-day merge. |
| `docs/build-log.md` and `decisions.md` show how the Pod worked. | Empty, or written the night before. |
| Everyone can explain the orchestrator and the others' agents. | Only the "owner" can. |

## Individuals and Specialists

- The **Pod's** Round 3 score is shared. Individual contribution is read from history, PRs and the demo and may inform the final result.
- **Standard Pod members** are expected to have integrated their own agent (criteria 1, 2, 6, 8 look at each agent separately).
- **Specialists** are not scored as having built a sixth agent; their contribution is read mostly through criteria **1, 3, 5, 6, 8 and 9**.

## Scoring bands (per criterion)

| Band | Share of the points | Meaning |
|---|---:|---|
| **Excellent** | 85-100 % | Fully working and demonstrated with evidence; trade-offs and limits explained. |
| **Good** | 65-84 % | Main paths work; gaps honestly stated. |
| **Partial** | 35-64 % | Works in part, or only on stubs / one example. |
| **Minimal** | 1-34 % | Attempted, not demonstrably working. |
| **None** | 0 % | Absent, or claimed without evidence. |

## Honesty adjustments

These can reduce a score under any criterion: presenting organiser stub behaviour as the Pod's agent result; calling a content hash tamper-evident without showing it; hiding, dropping or converting UNCERTAIN results; reporting a failure as success; numbers without a method, or one "accuracy" with no false-positive/negative breakdown; a demo that is not of the submitted commit. Credited: a well-argued finding, a documented kill condition, a failure shown honestly.

## For judges: running and inspecting a submission

This section is the operational guide, so no separate judging document is needed.

```sh
git clone <repo> && cd <repo> && git checkout round3-final
make setup && make test                  # tests green? (CI status on the tag as well)
make run                                 # the Pod's own run command: see its README if different
make case UNIT=<id> ORG=<org>            # one workflow, in full
```

| To check | Do |
|---|---|
| **Trigger a workflow** | `make case UNIT=… ORG=…`, or `POST /workflows` on the Pod's URL; the Pod's README names its own sample units |
| **Outputs to inspect** | `out/workflows/<id>.json` (status, `stage_results`, `transitions`, `final_outcome`); `out/evidence/<record_id>.json` |
| **Verify evidence** | Recompute `content_hash` (`shared/utils/hashing.py: verify`); check each `evidence_refs` points at a real input/`sha256`; check `upstream_refs` |
| **Trace a final decision backward** | `final_outcome.contributing_records` → the record's `decision` and `checks` → `evidence_refs` → `inputs[].sha256`; `transitions` for the why |
| **Test UNCERTAIN** | Run a subject whose stage returns UNCERTAIN: the evidence stays UNCERTAIN, status `BLOCKED`, outcome `NEEDS_REVIEW`; then record an override: it must reference the record and keep the original |
| **Test failure handling** | Stop one agent (or set its URL wrong) and run: expect an `error` stage, a degraded record, `FAILED`/`INCOMPLETE`, **not** `COMPLETED`/`CLEAN`; restore it and `resume` |
| **Test tenancy** | Ask for a subject under the other demo org: every agent must refuse |
| **Stubs** | `agents/*/agent.json` `implementation`: any `organiser-stub` left in the flow must have been disclosed |
