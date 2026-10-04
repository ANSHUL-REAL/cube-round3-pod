# agents/pack/  ·  Pack Manager

**Owner:** Member 3 (Pack Manager)  (set `owner` in `agent.json` and the handle in `.github/CODEOWNERS`)

> **This folder currently contains an organiser stub** that replays the synthetic Round 2 CSV. It is *not* an agent. Replace it, then replace this README with one that describes what you actually built, how to run it, and its limits.

| | |
|---|---|
| **Reads (inputs)** | A photo of the open box before sealing, and the order lines |
| **Reads (previous evidence)** | Receiving |
| **Produces** | items present, quantities correct, nothing extra; seal or stop-and-fix |
| **Recommended `check_key`s** | `items_present, quantities_correct, no_extra_items` |
| **`decision.outcome` values** | `seal, stop_and_fix, pending_review` |

Only merchant-fulfilled / 3PL units reach Pack (`route == "mfn"`): Amazon packs FBA boxes. Your record is what Returns and Recovery rely on to say what was actually sent, so the `observed_in_box` evidence must be citable.

## Where your code goes

```text
agents/pack/
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
.venv/bin/uvicorn agents.pack.app:app --port 8103
curl localhost:8103/health
```
Then set `"mode": "http"` in `agent.json` if you want the orchestrator to call it over HTTP.
