# orchestration/

The starter orchestrator: a small workflow engine that **owns workflow state**. It is small on purpose: enough that the whole system runs end to end on day one, short enough that you can read all of it and **own it as a Pod**. The full guide is [`ORCHESTRATION-GUIDE.md`](../ORCHESTRATION-GUIDE.md).

| File | What it does |
|---|---|
| `flow.json` | The flow as data: stages, `when` routing, policy (`timeout_s`, `retries`, `on_uncertain`, `on_error`). Standard Pods. |
| `flow.specialist.json` | The flow for Specialist Pods (no Prep). `pod.json` selects which one runs. |
| `orchestrator.py` | `run_workflow` · `advance` · `resume` · `apply_override` · `bundle`: starts workflows, runs stages, validates and records evidence, keeps the audit trail. |
| `rollup.py` | `derive_status` and `derive_final_outcome`: pure functions that derive state from the evidence and overrides. **A default, not the answer.** |
| `store.py` | `MemoryStore` / `FileStore`: workflow state and **immutable** evidence. Swap in a database by implementing the same four methods. |
| `clients.py` | In-process / HTTP clients; retry-vs-refusal semantics. |
| `run.py` | CLI. |
| `api.py` | Optional HTTP front door (no authentication: add it before exposing it). |

## Running

```sh
make run                                                      # all sample workflows -> out/workflows, out/evidence
make case UNIT=UNIT-0014 ORG=org_demo_alpha                   # one, printed in full
python -m orchestration.run --all --flow orchestration/flow.specialist.json
python -m orchestration.run --case examples/uncertain-path/case.json --flow examples/uncertain-path/flow.block.json
python -m orchestration.run --resume WF-org_demo_bravo-UNIT-0012
python -m orchestration.run --override WF-org_demo_bravo-UNIT-0012 --record PRP-0012 --verdict PASS \
       --actor you --reason "retook the photo" --and-resume
LOG_LEVEL=INFO make run                                       # the audit trail as JSON log lines
make serve                                                    # API on :8100
curl -s -X POST localhost:8100/workflows -H 'content-type: application/json' \
     -d '{"org_id":"org_demo_alpha","unit_id":"UNIT-0014"}' | python -m json.tool
```

## A case

```jsonc
{ "org_id": "org_demo_alpha", "unit_id": "UNIT-0014", "route": "fba", "returned": true }
```

`route` is `fba`, `mfn` or `unknown`; `returned` says whether a return happened. In the starter they are **derived from the Round 2 sample CSVs** (`scripts/build_sample_cases.py`). In your system they come from wherever your Pod decides (an order event, an operator action, an API call): write down how a unit gets its route.

## Behaviour you must keep (tested)

1. Previous evidence and workflow overrides are passed to every stage.
2. Every agent output is validated (schema, stage, workflow, **tenant**, **hash**, consistency) before it is accepted.
3. Transient failures retry; refusals (4xx, wrong tenant) do not.
4. Every failure is **recorded** (error + degraded evidence) and **never** becomes success.
5. Evidence is never deleted or replaced; overrides reference what they supersede.
6. Status and final outcome are **derived from the evidence**, and a workflow is never `COMPLETED` or `CLEAN` with a required stage incomplete.

## Deliberately not here

Concurrency across workflows, a database, a human-review queue and UI, authentication on the API, retry backoff, parallel or looping flows. Add what your design needs and say so in `ARCHITECTURE.md`.
