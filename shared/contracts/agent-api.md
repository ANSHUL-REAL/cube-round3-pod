# Agent HTTP API (language-agnostic)

Any agent, in any language, plugs in by serving two endpoints. Set `"mode": "http"` and `"url"` in `agents/<stage>/agent.json` (or env `<STAGE>_URL`, e.g. `PREP_URL`). Python agents get this for free: `make_app(stage, handle)` in `shared/utils/server.py`.

## `GET /health`

```json
{ "status": "ok", "stage": "prep", "version": "1.2.0", "contract_version": "1.0" }
```

`status` is `ok` or `degraded` (up, but a dependency such as the model API is failing). Return fast; do not call the model here.

## `POST /run`

**Request body:** an [Agent Input](../schemas/agent-input.schema.json). Example: [`examples/end-to-end/agent-input.prep.json`](../../examples/end-to-end/agent-input.prep.json).

**Success `200`:** an [Agent Output](../schemas/agent-output.schema.json) containing an [Evidence Record](../schemas/evidence.schema.json). Examples: [`examples/`](../../examples/).

| Status | When | Orchestrator reaction |
|---|---|---|
| `200` | You produced an output, **including** a `pending` one when you could not judge. | Validates it, records the evidence, decides the transition. |
| `404` | Unknown subject, **or it belongs to another org.** Never answer a wrong-tenant request. | Refused, **not retried**, recorded as `agent_rejected`. |
| `422` | The input does not match the schema, or `stage` is not yours. | Refused, not retried. |
| `5xx` / timeout | Your service is broken. | Retried (`retries` in the flow), then recorded as `agent_unavailable` / `agent_timeout`. |

Rules:

1. **Idempotent.** The same `request_id` yields the same `record_id`.
2. **Fail open.** If your model call fails, return `200` with a `pending` output (`verdict: "UNCERTAIN"`, `status: "pending"`, `error` set), not a `5xx`. Use `5xx` only when you could not even build an output.
3. **Read-only previous evidence.** Never modify it. Use the **latest override** in `context.overrides` as a record's effective verdict.
4. **Tenancy.** `evidence.subject.org_id` must equal the request's, or the orchestrator discards the output as a security event.
5. **Consistent.** `output.verdict`, `status` and `agent_id` must equal the evidence's; `content_hash` must verify.
6. **Time budget.** Respect `timeout_s` (default 30 s). Batch your model calls: one per unit carrying all checks.
7. **No secrets in responses or logs.**

## Quick check

```sh
curl -s localhost:8102/health
curl -s -X POST localhost:8102/run -H 'content-type: application/json' \
     -d @examples/end-to-end/agent-input.prep.json | python -m json.tool | head -30
```
