# Failure path

What the orchestrator does when something goes wrong. **Failures are recorded, never hidden, and never become success.**

## A stage returns FAIL (a real finding): `workflow-state.agent-fail.json` (UNIT-0002)
Not an error: the agent worked and found a problem. Status **COMPLETED**, outcome **EXCEPTION** (Failed verdict from: prep; no claim recommended.)

## The agent breaks (Prep in UNIT-0039)

| Scenario | Recorded error code | Attempts | Workflow status | Final outcome |
|---|---|---:|---|---|
| `timeout` | `agent_timeout` | 2 | `FAILED` | `INCOMPLETE` |
| `unavailable` | `agent_unavailable` | 2 | `FAILED` | `INCOMPLETE` |
| `invalid-output` | `invalid_output` | 1 | `FAILED` | `INCOMPLETE` |
| `tenant-mismatch` | `tenant_mismatch` | 1 | `FAILED` | `INCOMPLETE` |

In every case:
- A **degraded evidence record** is stored (`evidence.degraded.<scenario>.json`): status `pending`/`error`, **no checks**, verdict `UNCERTAIN`, `needs_human: true`, and the `error`. It records that the stage did not complete. It is not a judgment, and nothing is fabricated.
- Transient failures (`agent_timeout`, `agent_unavailable`) are **retried** (`attempts: 2` = 1 try + 1 retry); refusals and invalid output are **not**.
- The stage is `error`, the workflow status is **FAILED**, the outcome is **INCOMPLETE** and `provisional`. It is never COMPLETED or CLEAN.
- By default the workflow continues so the other stages' evidence is not lost (`on_error: continue`; set `block` to halt).
- `resume` retries the failed stage. The failed attempt's evidence **stays** in `evidence_references`.
- `tenant-mismatch`: the agent returned evidence about a different org. It is rejected as a security event, not accepted as data.
