# agents/receiving/  ·  Receiving Manager

**Owner:** Member 1 (Receiving Manager)  (set `owner` in `agent.json` and the handle in `.github/CODEOWNERS`)

> **This folder currently contains an organiser stub** that replays the synthetic Round 2 CSV. It is *not* an agent. Replace it, then replace this README with one that describes what you actually built, how to run it, and its limits.

| | |
|---|---|
| **Reads (inputs)** | Photos at the point of receipt (pallet, carton, unit) and the PO line |
| **Reads (previous evidence)** | nothing: first in the chain |
| **Produces** | identity, quantity, carton count, damage and quality verdicts |
| **Recommended `check_key`s** | `identity_match, carton_count, quantity, carton_damage, unit_damage, quality_flags` |
| **`decision.outcome` values** | `accept, accept_with_exceptions, reject, pending_review` |

Your evidence is where supplier disputes begin and the only point at which a supplier claim is still possible. **Keep supplier-side shortfall (finding F-10) distinct from channel-side loss.** Set `subject.unit_scope` honestly: Round 2 Receiving rows are PO lines (finding F-08).

## Where your code goes

```text
agents/receiving/
├── app.py          ← expose  handle(agent_input: dict) -> dict  (an Agent Output). Keep `app = make_app(...)` to serve over HTTP.
├── agent.json      ← stage · agent_id · owner · mode (inproc | http) · url · an honest `implementation` description
├── PROVENANCE.md   ← your Round 2 repo URL + commit this came from (create it)
├── README.md       ← this file, rewritten
└── …               ← your Round 2 code, prompts, rules, fixtures
```

## Integrating, in order

1. Read [`INTEGRATION-GUIDE.md`](../../INTEGRATION-GUIDE.md) and [`EVIDENCE-CONTRACT.md`](../../EVIDENCE-CONTRACT.md); open [`examples/end-to-end/`](../../examples/) for a real Agent Output.
2. In `handle()`: read `request["subject"]`, `request["inputs"]` (your captures) and `request["previous_evidence"]`; run your agent (**one batched model call per unit**); build the record with `shared.utils.records.build_record()` and wrap it with `build_output()`.
3. **Fail open.** On a model error return `pending_output(...)`, not an exception. Never invent evidence: if you did not see it, say UNCERTAIN with an `uncertain_reason`.
4. **Refuse other tenants.** Raise `LookupError` (HTTP 404) for a subject that is not under `subject.org_id`.
5. Make it idempotent: the same `request_id` must yield the same `record_id`. Use the **latest override** of previous evidence (`context.overrides`).
6. Run `pytest tests/integration/test_agent_contracts.py`, first on the stub (it passes), then on yours, **with your own fixtures**.
7. Run the whole system: `make run` and `make test`.

## Run on its own

```sh
.venv/bin/uvicorn agents.receiving.app:app --port 8101
curl localhost:8101/health
```
Then set `"mode": "http"` in `agent.json` if you want the orchestrator to call it over HTTP.
