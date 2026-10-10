# CLAUDE.md

Project rules for anyone changing this repository, person or AI coding assistant. `RULES.md` allows AI assistants;
whoever commits a change is accountable for it and must be able to explain it in the demo.

## What this is

Snitch, Pod 12's Round 3 system. Five agents check a warehouse unit from phone photos (Receiving, Prep, Pack,
Returns, Recovery). One orchestrator routes the unit, keeps its state, and stores each agent's decision as a sealed
evidence record. People review, override and label through the console.

## Map

| Path | What lives there |
|---|---|
| `agents/<stage>/` | One agent each: `app.py` exposes `handle(request) -> output`; `agent.json` says what it is; `README.md` and `PROVENANCE.md` say how it decides and where it came from |
| `orchestration/` | `orchestrator.py` (state, retries, resume, overrides), `flow.json` (routing, timeouts), `clients.py`, `store.py` (files or Postgres), `rollup.py` (final outcome), `api.py` |
| `orchestration/web/` | The console: pages, access codes and tenancy (`access.py`), admin, phone stations, added units, receipts, labelling |
| `shared/` | The contract: `schemas/`, `contracts/agent-api.md`; helpers in `utils/` (`records.py`, `hashing.py`, `db.py`) |
| `tests/` | `integration/` per agent and per feature, `e2e/`, the organisers' stubs in `stubs/` |
| `docs/` | `decisions.md`, `build-log.md`, `evaluation.md`, `REAL-RUNS.md`, `DEPLOYMENT.md`, `LIVE-DEMO.md` |

## Commands

```bash
make setup && make test          # or: python -m pytest
python -m pytest tests/integration/test_pack_agent.py     # one agent
python scripts/serve.py          # console and API on http://localhost:8100
python scripts/serve.py --stubs  # the organisers' stubs instead of our agents (labelled on every page)
python scripts/serve.py --data <empty folder>             # a clean session: its own photos and results
```

The Postgres tests run only with `TEST_DATABASE_URL` set; they work in their own schema and drop it afterwards.
CI (`.github/workflows/ci.yml`) runs the whole suite and a secret check.

## Rules that must not be broken

1. **The model observes; fixed rules decide.** A model reports what is in the photos. It is never shown the order,
   the purchase order or any expected value. Verdicts come from each agent's rules.
2. **UNCERTAIN is a verdict with a reason, and never becomes PASS.** No photo, no key, a model error or an unreadable
   answer gives a pending record (`shared/utils/records.py`, `pending_output`). Never an invented verdict.
3. **Every lookup is scoped by `org_id`.** Another seller's unit answers exactly like a missing one (`LookupError`,
   then a 404). A new route or lookup needs a test for the wrong seller.
4. **Evidence is write-once.** Records are sealed with a content hash (`shared/utils/hashing.py`). A person's override
   is appended; nothing is rewritten or deleted. Call it a content hash, not tamper-proof, immutable or a signature.
5. **The orchestrator owns workflow state.** Agents never call each other; they read `previous_evidence`.
6. **The contract changes only with the Pod's agreement:** `shared/schemas/`, `EVIDENCE-CONTRACT.md`,
   `shared/contracts/agent-api.md`.
7. **Model time stays inside the stage timeout** in `orchestration/flow.json` (now 2 x 28 s plus a 2 s pause, inside
   75 s). Each photo agent has a test for it.
8. **The sample CSVs are synthetic.** Never tune on them or treat them as ground truth. Their received, observed and
   damage columns are answers the agents must not read.
9. **Secrets live in `.env`,** which git ignores. Never print, log or commit a key. `.env.example` holds names only.
10. **No large binaries, model weights or datasets** in the repository (RULES.md, R9). Videos go on a release.
11. **Docs match the code.** A number in a README or in `docs/` is measured here, with the command or file that
    produced it. No accuracy figure without labelled data: write "not measured".

## Before you commit

- For a bug, write the failing test first, watch it fail, then fix it.
- Run the whole suite, not only the file you touched.
- Update what the change makes untrue: the agent's `README.md`, a new entry in `docs/decisions.md` (ids continue per
  area: `D-O` orchestration, `D-V` Receiving, `D-PR` Prep, `D-P` Pack, `D-RT` Returns, `D-RC` Recovery), and a row
  in `docs/build-log.md`.
- Write the commit message in plain sentences: what changed and why. Add nothing else to it.
- Do not commit run output or session data (`out/`, `.cache/`, photo folders).
