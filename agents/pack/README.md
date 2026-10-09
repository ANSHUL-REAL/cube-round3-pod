# agents/pack/  ·  Pack Manager

**Owner:** Anshul Nautiyal (`@ANSHUL-REAL`) · built in Round 2, adapted here. Where it came from: [PROVENANCE.md](PROVENANCE.md).

Checks the open box against its order before it is sealed, from one phone photo, and leaves a record the rest of the system can read.

| | |
|---|---|
| **Reads (inputs)** | Up to 3 photos of the open box in `data/input/<unit_id>/pack/`; the order (see below) |
| **Reads (previous evidence)** | Receiving: recorded in `payload.upstream`, with the latest human override applied |
| **Produces** | `seal`, `stop_and_fix` or `pending_review`, with named checks |
| **Checks** | `items_present`, `quantities_correct`, `no_extra_items`, plus `image_quality`, `photo_reuse`, `scene_coverage` |
| **Runs for** | Merchant-fulfilled units only (`route == "mfn"`), which the flow decides |

## How it decides

1. **A local photo check** (sharpness, light, size) runs before the model.
2. **One Gemini call per box** lists what is in the box. It is shown the catalogue, never the order and never the quantities, and it does not decide anything.
3. **Fixed rules** (`core/decision.py`) compare that list with the order: any failed check means `stop_and_fix`; otherwise any unclear check means `pending_review` (a person decides); otherwise `seal`.
4. Every verdict traces back to named checks, and the per-line detail is kept in `payload.line_checks`.

The model cannot "see 2" because the order says 2. If it cannot see a thing, the answer is `UNCERTAIN` with a reason, never a guess.

## What the record contains

- **Checks** use the three recommended keys. The Round 2 per-line checks are rolled up (worst verdict wins) and kept whole in `payload.line_checks`.
- `payload.order_lines` (what was ordered) and `payload.observed_in_box` (what the agent counted with confidence), as `{sku: count}`. `payload.unclear_objects` counts things it saw but could not name. Returns and Recovery can cite these.
- `payload.fix_instructions` (e.g. "Replace Bottle with Candle") and `payload.uncertainty` (what is known, what is not, what would settle it).
- `payload.photos`: for each photo, its ref, the hash of the original and of the image the model saw, and the quality gate result.
- `model`: the model and prompt version, calls (0 if the answer came from the local cache), and cost when `COST_PER_1M_*` are set (otherwise `null`; we don't guess a price).

## Fail open (it never invents a verdict)

| Situation | What is returned |
|---|---|
| No photo | `pending_review`, `error.code = no_capture`, retryable |
| Photo missing, altered, from another unit, or not an image | `pending_review`, `capture_unreadable`, not retryable |
| No `GEMINI_API_KEY` | `pending_review`, `model_not_configured`, retryable, photos kept |
| Model error or timeout | `pending_review`, `model_unavailable`, retryable, photos kept |
| Photo too dark or blurry | An answer is still given, but `image_quality` and the box are `UNCERTAIN` ("retake the photo") |
| The same photo already used for a different order | `photo_reuse` is `UNCERTAIN`; a reused photo cannot seal a box |

More than 3 photos: the first 3 are used and the rest are listed in `payload.photos_not_used`.

## Tenancy and repeatability

- A unit that belongs to another organisation raises `LookupError` (HTTP 404). It is never answered.
- Captures must sit under the capture root and under the unit's own folder, and must match their declared hash.
- The same `request_id` gives the same `record_id`. A re-run of the stage has a new request id and gets a new record.

## Where the order comes from

The request does not carry the order lines, so `orders.py` looks them up, scoped to the organisation:

1. `context.order` on the request, if it has this organisation's `org_id`: `{"org_id", "order_id", "lines": "SKU:qty;..."}` (or a list of `{"sku","qty"}`).
2. Otherwise the organisers' `data/sample/pack_sample.csv`.

## Run it

```sh
# GEMINI_API_KEY goes in .env (git-ignored). Never commit it.
python -m agents.pack.check --unit UNIT-0008 --org org_demo_alpha my_box.jpg   # one box, readable summary
make case UNIT=UNIT-0008 ORG=org_demo_alpha      # the whole workflow for that unit
.venv/bin/uvicorn agents.pack.app:app --port 8103
```

`agents.pack.check` copies 1 to 3 photos into `data/input/<unit>/pack/`, runs the same code the orchestrator runs, and prints the verdict, each check, what to fix and the record id. It refuses another organisation's unit before writing anything. `--json` prints the full Agent Output.

Without a key, or without photos, every Pack unit comes back `pending_review` and says why. That is the intended behaviour, not a bug.

## Test it

```sh
pytest tests/integration/test_pack_agent.py     # 24 tests, no key needed
pytest tests/integration/test_agent_contracts.py
```

The tests replace the model with scripted perceivers, so they check everything **except what the real model sees**: capture handling, the order lookup, the rules, the record mapping, fail-open, tenancy, idempotency, photo reuse (including across a restart), the model time budget, and a whole orchestrated workflow in which Returns and Recovery receive our Pack record. Fixtures include the two boxes in the organisers' sample where the human operator sealed a wrong box (UNIT-0044 has a bottle where a candle was ordered; UNIT-0034 has an extra cable).

## Limits (read these)

- **Real-model accuracy is from Round 2, not re-measured here.** One frozen run of `gemini-3.5-flash-lite` on 50 real warehouse bin photos (Amazon Bin Image Dataset): 2 of 26 wrong boxes let through, 12 of 24 good boxes stopped, 18 of 50 sent to a person. A limit we set in advance was crossed, so on photos like these it should **record evidence and let a person decide, not block sealing**. Details: Round 2 `EVAL.md` (link in PROVENANCE.md).
- **Run live through this pod repository on real warehouse photos only** (`docs/REAL-RUNS.md`, and the console on 2026-10-09: STOP_AND_FIX in 4.2 s). Not yet on a packing-bench box; the tests use scripted models.
- Those photos are bin photos, not packing-bench photos, and the labels come from Amazon's records, not human labellers.
- The organisers' sample has **no photos**, so the sample flow shows Pack as pending until someone takes box photos.
- The photo-reuse ledger is a local JSON file (`PACK_LEDGER_PATH`, default `out/pack-ledger.json`). It survives restarts but is not shared between machines; two servers would each see only their own history.
- The model call is bounded to 28 s per attempt (`MODEL_TIMEOUT_S` in `app.py`), with one retry after a 2 s back-off only when the API answers 429 or 5xx. Worst case 2 x 28 s + 2 s = 58 s, inside the orchestrator's 75 s stage timeout (`orchestration/flow.json`, `defaults.timeout_s`, D-O07); a test enforces it. A slower answer becomes a retryable pending record. Round 2's measured p95 was 10.4 s, but a real call in the 2026-10-09 rehearsal took longer than the old 12 s bound (comment in `app.py`).
- Only the organisers' 10 sample products are in the catalogue. Add your own under `agents/pack/catalogue/<org_id>/` (see the Round 2 catalogue README for the format).
- A Receiving exception is recorded, not acted on: Pack judges the box against the order.
