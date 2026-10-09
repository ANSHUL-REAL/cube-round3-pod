# Provenance: where the Receiving Manager came from

| | |
|---|---|
| **Round 2 repository** | https://github.com/cherryy-x23/cube26-rcv-0095-cherryy-x23 (code under `submissions/cherryy-x23/`) |
| **Commit read** | `422067fbf09e8ec13c31e3e5c3bac6b09f0eb711` (branch `main`, "fix: allow deployed frontend origin") |
| **Author** | Sai charan (`@cherryy-x23`), built solo for CUBE Buildathon Round 2, Pod 01 (Receiving Manager) |
| **Ported by** | Anshul Nautiyal (`@ANSHUL-REAL`), for the pod's Round 3 integration, with Sai charan as owner of the agent |

## What was kept (the design and the logic)

The Round 2 design is sound and is the spine of this agent:

- **The model observes, deterministic code decides.** Round 2's central rule, kept.
- **One batched model call per unit.** Kept.
- **UNCERTAIN is a first-class verdict, fail-open to pending review on any model error, no fabricated results when the key is missing.** Kept.
- **The rule logic** of `agent/core/comparison_engine.py`: SKU / barcode / OCR identity matching, carton and quantity arithmetic (`cartons x units per carton`), the damage categories (`none`, `crushing`, `water`, `tears`, `uncertain`), colour / variant / component conformity, and the roll-up (any FAIL beats any UNCERTAIN beats PASS). Ported into `rules.py`.
- **The domain models** (`POExpected`, `VisualObservation`, `DamageType`) and the structured-output idea, ported into `models.py`; the observation prompt, ported into `vision.py`.
- The sample-data vocabulary for quality flags (`wrong_colour`, `wrong_variant`, `missing_components`, `obvious_defect`).

Nothing was copied byte for byte: the code was rewritten against the Round 3 contract, so there is no "unchanged" folder as in `agents/pack/core/`.

## What was weak in Round 2, and what changed

| Round 2 | Round 3 here |
|---|---|
| The live Gemini path read **server-side file paths** (`Path(img_path).read_bytes()` inside the API process). Photos were never uploaded, so the real model loop was never shown working on a photo someone captured. | Photos arrive as Round 3 `inputs` (`data/input/<unit>/receiving/`, hash-checked) and are sent to the model **as image bytes**. |
| The prompt told the model the expected SKU, ASIN, colour, variant, components and totals "for context". That invites the model to confirm what it was told to expect. | The model is **not shown the purchase order**. A test asserts none of it appears in the request. |
| Hashes of missing files were computed from the **path string** ("placeholder"), so a record could carry a hash that is not a hash of any photo. | Hashes are always of real bytes. A missing, altered or foreign file is `capture_unreadable`, and nothing is hashed that was not read. |
| Identity took the first signal (SKU text) and returned `confidence 1.0` (hard-coded). | All identity evidence is weighed; evidence both ways is `conflicting_evidence` UNCERTAIN. No invented confidence: deterministic checks report `null`. |
| `compare_carton_count` was written and tested but **never called** by `evaluate_all_checks`. | `carton_count` is a check. |
| `image_clarity` was extracted and then ignored. | Clarity below a threshold turns the observation-based checks UNCERTAIN (`poor_image`); so does low self-reported confidence. Neither can ever produce a PASS or FAIL. |
| Total quantity trusted either the model's total or cartons x units, silently. | If both are present and disagree: `conflicting_evidence`. |
| Component matching tested `"mug x2" in "mug"`, so quantity-annotated spec names (`mug x2`, `candle x3`) always read as missing. | Order-insensitive word matching with a trailing `x<N>` ignored, and a plural read as its singular ("candles" is the ordered "candle x3"). |
| Spec flags were `missing_<component>` and had no `obvious_defect`. | `quality_flags` uses the sample vocabulary, including `obvious_defect`. |
| Outcome: PASS / FAIL / UNCERTAIN / PENDING_REVIEW with an operator override on the record. | Round 3 outcomes (`accept`, `accept_with_exceptions`, `reject`, `pending_review`), the six recommended check keys, workflow-level overrides honoured via `context.overrides`. |
| `MockVisionExtractor` returned canned answers keyed on **unit-id text** (`FAIL`, `UNCERTAIN`, `PENDING`) and fell back to a clean match. The Round 2 backend's `VISION_PROVIDER` defaults to `mock`, so unless it was set otherwise its demo shows canned answers (we cannot tell what the deployed demo used). | No mock in the product code. Tests inject a scripted perceiver; with no key the agent returns a pending record. |
| Gemini call: no timeout or retry configuration, timeout detected by string match on the error text. | 28 s per attempt (`vision.py`, `MODEL_TIMEOUT_S`), one retry on 429/5xx after 2 s, `calls` counted honestly; worst case 58 s, inside the orchestrator's 75 s stage timeout (`orchestration/flow.json`, D-O07). |
| `unit_id` treated as one thing. | `unit_scope = po_line` and the PO line keys in `subject.refs` (finding F-08). |

## What is new in Round 3 (written for this repository)

| File | What it does |
|---|---|
| `agents/receiving/app.py` | The agent entry point: `handle()` and the HTTP app |
| `agents/receiving/rules.py` | The deterministic rules, from Round 2's comparison engine, reworked |
| `agents/receiving/vision.py` | The model prompt, the Gemini call (bytes, retry, timeout) and the perceiver interface |
| `agents/receiving/models.py` | PO line, model observation, perception |
| `agents/receiving/adapter.py` | Builds the Round 3 Evidence Record (judged or pending) |
| `agents/receiving/orders.py` | Finds the PO line for a unit, scoped to the caller's organisation |
| `agents/receiving/captures.py` | Reads and prepares the photos named in `inputs`, hash-checked |
| `agents/receiving/check.py` | Command-line checker |
| `agents/receiving/config.py` | Settings |
| `tests/integration/test_receiving_agent.py` | 52 behaviour tests with a scripted model |
| `tests/stubs/receiving_stub.py` | The organiser's Receiving stub, kept verbatim as a test fixture for the plumbing tests |

## What was left behind

The Round 2 web frontend (React), the FastAPI backend with its in-memory inspection repository and demo-scenario seeding, the presentation documents (customer letter, PR/FAQ, one-pager, demo script, architecture write-up), and the evaluation harness (`agent/eval/`: Cohen's kappa, confusion matrices, failure-mode taxonomy). Round 3 needs the agent, not the product around it. The harness is a reasonable starting point if real labelled photos are ever collected; it is not wired into this repository.

## Not carried over, on purpose

No API keys, no `.env`. **No evaluation results**: Round 2's `eval-report.md` says official metrics are "NOT YET AVAILABLE" and the ground truth file is an empty template; this repository does not claim otherwise.
