# Provenance: where the Pack Manager came from

| | |
|---|---|
| **Round 2 repository** | https://github.com/ANSHUL-REAL/cube26-pck-0076-anshul-real |
| **Commit copied from** | `ef9e958305984c6f4230c93a4433a55bf1071e07` (branch `main`) |
| **Author** | Anshul Nautiyal (`@ANSHUL-REAL`), built solo for CUBE Buildathon Round 2, Track 03 |
| **Round 2 live demo** | https://pack-manager-lzht.onrender.com |

## What was copied unchanged

`agents/pack/core/` is a verbatim copy of these Round 2 files (checked file by file against that commit):

- `pack_manager/` → `__init__.py`, `catalogue.py`, `config.py`, `contract.py`, `decision.py`, `evidence.py`, `models.py`, `pipeline.py`, `quality.py`
- `pack_manager/vision/` → `__init__.py`, `base.py`, `gemini.py`, `oracle.py`, `prompt.py`
- `catalogue/sample/catalogue.json` → `agents/pack/catalogue/sample/catalogue.json`

## What is new in Round 3 (written for this repository)

| File | What it does |
|---|---|
| `agents/pack/app.py` | The agent entry point: `handle()` and the HTTP app |
| `agents/pack/adapter.py` | Maps the Round 2 result onto a Round 3 Evidence Record |
| `agents/pack/orders.py` | Finds the order for a unit, scoped to the caller's organisation |
| `agents/pack/captures.py` | Reads the box photos named in `inputs`, safely and hash-checked |
| `agents/pack/ledger.py` | Remembers which order each photo was used for (photo-reuse check), persisted to a JSON file |
| `agents/pack/check.py` | Command-line checker: one box photo in, verdict out |
| `tests/integration/test_pack_agent.py` | 24 behaviour tests on our own fixtures |

## What was left behind

The Round 2 web app (`app/`), its Postgres schema and access-code login, the CLI, the CSV export and the Round 2
evaluation scripts. Round 3 needs the agent, not the product around it. They stay in the Round 2 repository.

## Not carried over, on purpose

No API keys, no `.env`, no cached model answers. The Round 2 repository's held-out evaluation uses its own data and
is described in its `EVAL.md`; this repository does not re-run it.
