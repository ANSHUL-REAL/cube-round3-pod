# agents/prep/  ·  Prep Manager

**Owner:** Member 2 (Prep Manager; none in Specialist Pods)  (set `owner` in `agent.json` and the handle in `.github/CODEOWNERS`)

> **This folder currently contains an organiser stub** that replays the synthetic Round 2 CSV. It is *not* an agent. Replace it, then replace this README with one that describes what you actually built, how to run it, and its limits.

| | |
|---|---|
| **Reads (inputs)** | Photos of the prepped unit and the work order |
| **Reads (previous evidence)** | Receiving |
| **Produces** | per-requirement compliance verdicts, plus measured weight/dimensions if you can |
| **Recommended `check_key`s** | `polybag_sealed, suffocation_warning, fnsku_label_placement, original_barcode_covered, expiry_legible, handling_marks` |
| **`decision.outcome` values** | `compliant, non_compliant, pending_review` |

**Specialist Pods have no Prep Manager: this folder is unused there** (the Specialist flow skips it). For Standard Pods: Recovery has asked Prep to record measured weight and dimensions in `payload.measurements`, because most sample fee lines are weight-tier fees with no evidence (finding F-07, see [`docs/decisions.md`](../../docs/decisions.md)). Look up Amazon's published prep requirements; do not infer them from the sample CSV. Your cost per check has to fit inside $0.40–$1.10 per unit.

## Where your code goes

```text
agents/prep/
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
.venv/bin/uvicorn agents.prep.app:app --port 8102
curl localhost:8102/health
```
Then set `"mode": "http"` in `agent.json` if you want the orchestrator to call it over HTTP.
