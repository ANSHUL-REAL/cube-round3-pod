# Provenance: where the Returns Manager came from

| | |
|---|---|
| **Round 2 repository** | https://github.com/krishnababuprodduturu/cube26-rtn-0038-krishnababu |
| **Commit copied from** | `e0fcf51f18fbf7b725f6d7baa9182577a90590e8` (committed 1 Oct 2026, "Fix text visibility across website and rewrite unique README and ARCHITECTURE documentation for RTN-0038") |
| **Author** | Prodduturu Krishna Babu (`@krishnababuprodduturu`), CUBE Buildathon Round 2, Track 04 (Returns), RTN-0038 |
| **Round 2 deployment** | Render service `cube26-rtn-0038-krishnababu` (not checked by the pod) |
| **License** | MIT (stated in the Round 2 README; no LICENSE file was checked) |

The pod read this repository from a read-only clone; nothing was pushed to it.

## What was copied with only import lines changed

Each file below was compared with its Round 2 original after rewriting `from returns_manager.… import` to the relative
imports used here; the rest of each file is identical (checked file by file against that commit).

| Round 2 file (`agent/src/returns_manager/`) | Here (`agents/returns/core/`) |
|---|---|
| `judgment/types.py` `referential.py` `consistency.py` `fusion.py` `completeness.py` `grading.py` `escalation.py` `claims.py` `pipeline.py` | same names |
| `llm/schemas.py` (the `judgment/v1` schema; no disposition field) | `schemas.py` |
| `reference/models.py` | `models.py` |
| `disposition/engine.py` (rules R01 to R14, R99; sign-off S01 to S03) | `engine.py` |

And these data files, byte for byte, from the repository root:

- `reference/rubrics/` (without `_extracted_pages.json`), `reference/policies/`, `reference/categories/`, `reference/rules/`, `reference/quality/`, `reference/sources.yaml` → `agents/returns/reference/`
- `reference/products/` (the 17 product cards and the two reference images `SKU-PHONE-IQOO9/ref_contents.jpg`, `SKU-LAPTOP-DELL/ref_contents.jpg`) → `agents/returns/reference/products/`
- `agent/prompts/judgment/system.md`, `task.md` and `prompts.lock.json` → `agents/returns/prompts/` (the prompt is hash-locked and **was not edited**)

Because `engine.py`'s import line changed, `rules_version` (a hash of the engine source and parameters) differs from the version
Round 2 wrote into its own decisions.

## Joined or trimmed from Round 2 code

| Here | From | What changed |
|---|---|---|
| `core/canonical.py` | `canonical/jcs.py` + `canonical/hashing.py` | Joined; bodies unchanged |
| `core/refhash.py` | `reference/hashing.py` | Import line only |
| `core/prompts.py` | `llm/prompts.py` | Paths and error type; the `--lock` CLI dropped |
| `core/params.py` | `disposition/params.py` | Paths; the parameter file's `content_sha256` is now verified on load |
| `core/images.py` | `intake/images.py`, `intake/quality.py`, `vision/barcode.py` | Sharpness / exposure / resolution checks and the decoder unchanged; **dropped**: perceptual hashes, near-duplicate and cross-return photo-reuse checks (they needed the database), retake-guidance text. Barcode decoding made optional |
| `core/context.py` | `llm/context.py` | `card_subset` and `rubric_payload` unchanged; context assembly rewritten without a database; no tools offered |

## What is new in Round 3 (written for this repository)

| File | What it does |
|---|---|
| `agents/returns/app.py` | The agent entry point: `handle()` and the HTTP app |
| `agents/returns/adapter.py` | Maps the Round 2 result onto the Round 3 Evidence Record; the outcome rules and the no-contradiction guard |
| `agents/returns/upstream.py` | Reads Pack's and Receiving's evidence with the latest overrides; conflicts and context notes |
| `agents/returns/returned.py` | The return and order, scoped to the caller's organisation |
| `agents/returns/captures.py` | Reads the return photos named in `inputs`, safely and hash-checked (from the Pack agent's loader, plus a `returns/` folder check) |
| `agents/returns/judge.py` | One `generateContent` call (tried once more, on a fallback model, if the first is busy or times out), replacing Round 2's Interactions API session loop |
| `agents/returns/config.py` | Settings (environment only) |
| `agents/returns/core/refs.py` | Loads the card, rubric, policy and parameters from files, each hash-checked, replacing the Round 2 database loaders; keeps the "no reference, no model call" gate |
| `agents/returns/core/context.py` (assembly) | See above |
| `tests/integration/test_returns_agent.py` | Behaviour tests on our own fixtures |

## What was left behind

The web console (`ui/`), the Postgres schema with row-level security and its migrations (`agent/migrations/`), the job queue
and worker with circuit breaker and quota guards (`jobs/`), the Gemini Interactions API client and tool-calling loop
(`llm/client.py`, `gemini_client.py`, `loop.py`, `tools.py`, `quota.py`, `replay_client.py`, `pricing.py`), the hash-chain
audit ledger (`chain/`), webhooks, the explainer agent, the MCP server, the CLI, the batch CSV tool, the evaluation
tooling (`eval/`) and its runs, API keys and auth (`security/`), and the Round 2 documents. They stay in the Round 2
repository. The pod's own contract, evidence store and orchestrator replace the parts of that stack it needs.

## Not carried over, on purpose

No API keys, no `.env`, no recorded model answers. Round 2's evaluation is described in its `eval/runs/*/NOTES.md`; this
repository does not re-run it.

## Review notes on the Round 2 repository

Written by the pod after reading the code, for whoever integrates this next.

- **Good, and kept:** the model only observes and a deterministic engine decides; identity needs product-body evidence, not just packaging or a barcode; "missing" requires a clear view of where the part would be; every model reference (alias, component id, rubric quote) is validated; the prompt is hash-locked; reference documents are hashed; UNCERTAIN is preferred and carries retake requests; the evaluation notes are honest about being self-graded and small.
- **Overstated in its prose, not in its code:** the README and ARCHITECTURE describe "zero-hallucination" policy, an "enterprise-grade" system and tamper-evident chains. The code is a rules engine with synthetic prices and uncalibrated thresholds; ADR-007 itself says the chain is tamper-evident only inside its database. ARCHITECTURE describes consistency rule C02 differently from what `consistency.py` does. The README names Gemini 2.5 Flash while the settings default to a different model id. This pod relies on the code and its tests, not that prose.
- **Needs the owner:** the condition scale source (ADR-006 and finding F-001 in that repository: the Amazon UK PDF used for amazon.in), the placeholder product cards (every sample SKU lacks reference images and has invented features), and a real evaluation of the single-call path (`judge.py`): so far it has one logged real call, on a warehouse-bin photo rather than a returned product, which answered UNCERTAIN on every check ([`docs/REAL-RUNS.md`](../../docs/REAL-RUNS.md)).
