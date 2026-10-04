# agents/recovery/  ·  Recovery Manager

**Owner:** Member 5 (Recovery Manager)  (set `owner` in `agent.json` and the handle in `.github/CODEOWNERS`)

> **This folder currently contains an organiser stub** that replays the synthetic Round 2 CSV. It is *not* an agent. Replace it, then replace this README with one that describes what you actually built, how to run it, and its limits.

| | |
|---|---|
| **Reads (inputs)** | Channel fee / reimbursement report lines (no camera) |
| **Reads (previous evidence)** | **All** earlier records |
| **Produces** | per-charge position (supports / contradicts / silent), a claim with attached evidence and a dollar figure, and an explicit list of what cannot be claimed and why |
| **Recommended `check_key`s** | one `charge_<line_id>` check per fee line |
| **`decision.outcome` values** | `claim_recommended, no_claim, insufficient_evidence, pending_review` |

Your check semantics are the one place verdicts read differently: the condition is *"this charge is supported by evidence"*, so `FAIL` = contradicted = **claim**, `UNCERTAIN` = SILENT = **never a claim**. A wrongly filed claim costs a seller standing; a missed one costs only money, so report **precision**. You will meet every contract and data problem first (findings F-07 to F-12): raise them early. In a **Specialist Pod** there is no Prep evidence: inbound-defect charges must be SILENT, not guessed.

## Where your code goes

```text
agents/recovery/
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
.venv/bin/uvicorn agents.recovery.app:app --port 8105
curl localhost:8105/health
```
Then set `"mode": "http"` in `agent.json` if you want the orchestrator to call it over HTTP.
