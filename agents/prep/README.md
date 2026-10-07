# agents/prep/  ·  Prep Manager

**Owner:** Devisri (`@Devisri-074`). Ported from Manvith111's Round 2 Prep Manager (TypeScript) and fitted to the Round 3 contract. Where it came from, and what was changed: [PROVENANCE.md](PROVENANCE.md).

Checks whether a unit was prepped the way its work order asked, from photos of the prepped unit, and leaves a record the rest of the system can read.

| | |
|---|---|
| **Reads (inputs)** | Up to 6 photos of the prepped unit in `data/input/<unit_id>/prep/`; the unit's work order (see below) |
| **Reads (previous evidence)** | Receiving: recorded in `payload.upstream` and cited in `upstream_refs`, with the latest human override applied. It does not change any prep check |
| **Produces** | `compliant`, `non_compliant` or `pending_review`, with named checks |
| **Checks** | `polybag_sealed`, `suffocation_warning`, `fnsku_label_placement`, `original_barcode_covered`, `expiry_legible`, `handling_marks`, plus one we add: `fnsku_text_match` |
| **Runs for** | FBA units only (`route == "fba"`); anything else is refused |

## How it decides

1. **One batched vision call per unit** (Gemini, `gemini-3.5-flash-lite` by default) is shown all the photos and the list of things to look for. It reports only what is **visible**: for each check `met` / `not_met` / `cant_tell`, a confidence band, which photo and where, the evidence it can point to, a usability rating per photo, and a literal transcription of the barcode label. It is never told the expected code, never sees the work order, and never decides.
2. **Fixed rules** (`rules.py`) turn those observations into verdicts. A PASS or FAIL needs a cited photo that exists and is usable, visible evidence, and at least medium confidence. Anything else is `UNCERTAIN` with one of the contract's reasons (`poor_image`, `occluded`, `insufficient_evidence`, `conflicting_evidence`). Same observations in, same verdicts out.
3. **The work order decides which checks exist.** A check that does not apply to the unit is left out of the record, not marked PASS. Bag thickness cannot be judged from a photo, so it is listed in `payload.not_verified` and is never a PASS.
4. **Any FAIL makes the unit `non_compliant`; otherwise any UNCERTAIN makes it `pending_review` (a person decides); otherwise `compliant`.** Every failed and every uncertain check is named in `decision.reason` and `payload.failed_checks` / `payload.uncertain_checks`.

The eleven checks of the reference roll up (worst verdict wins) into the contract's keys, and are kept whole in `payload.rule_checks`:

| Reference check | Contract `check_key` |
|---|---|
| poly-bag present, poly-bag sealed | `polybag_sealed` |
| suffocation warning present, legible | `suffocation_warning` |
| FNSKU label present, placement | `fnsku_label_placement` |
| FNSKU text matches the product | `fnsku_text_match` (an addition; the contract allows new keys) |
| original barcode covered | `original_barcode_covered` |
| expiry date visible | `expiry_legible` |
| handling marks present | `handling_marks` |
| bag thickness / material | not a check: `payload.not_verified` |

`fnsku_text_match` compares the code the model transcribed with the FNSKU on the work order. A mismatch is a FAIL only when the transcription is high-confidence; a lower-confidence mismatch could be a misread character, so it goes to a person.

## What the record contains

- `payload.requirements`: what the work order required for this unit. `payload.rule_checks`: the eleven checks, each with its observation, photo and verdict. `payload.photos`: ref, hash of the original and of the image the model saw.
- `payload.photos_not_used`: photos beyond the limit of 6 (ref and hash), also stated in `decision.reason`. They are reported, never silently dropped.
- `payload.measurements` is `{}` on purpose: weight cannot be read from a photograph and the photos carry no scale, so no dimensions can be derived either. `payload.measurements_note` says so. Weight-tier fees therefore stay SILENT downstream (finding F-07) until a scale or measuring station supplies real numbers.
- `payload.rule_source`: **unverified**. See Limits.
- `payload.upstream.receiving`: Receiving's record id, its verdict, and the effective verdict after the latest override.
- `model`: the model, prompt version, **calls** (attempts made, so a retried call counts twice) and **cost** (tokens times `COST_PER_1M_INPUT_USD` / `COST_PER_1M_OUTPUT_USD`; `null` when those are not set, because we do not guess a price). Token counts are in `payload.usage`.
- Check `confidence` is the model's own band (high / medium / low) mapped to 0.9 / 0.6 / 0.3. It is not a calibrated probability, and `payload.confidence_note` says so. An UNCERTAIN check carries no confidence.

## Fail open (it never invents a verdict)

| Situation | What is returned |
|---|---|
| No photo | `pending_review`, `error.code = no_capture`, retryable, no model call |
| Photo missing, altered, from another unit or stage folder, outside the capture root, or not an image | `pending_review`, `capture_unreadable`, not retryable |
| No `GEMINI_API_KEY` | `pending_review`, `model_not_configured`, retryable, photos kept |
| Model error, timeout or any other model-side exception | `pending_review`, `model_unavailable`, retryable, photos kept, `model.calls` = attempts made |
| Model answer that does not match the schema | `pending_review`, `model_output_invalid`, retryable |
| Work order flag that cannot be read | refused with an error |

A pending record has no checks (it is not a judgment), `status: pending` (or `error`), `verdict: UNCERTAIN`, `needs_human: true`, and keeps the capture. The reference turned a model failure into a record of eleven UNCERTAIN-looking checks; that is not carried over.

## Tenancy, routing and repeatability

- A unit that belongs to another organisation raises `LookupError` (HTTP 404). So does a work order in the request for another organisation or another unit, and a route other than `fba`.
- Captures must sit under `<unit>/prep/` inside the capture root and must match their declared hash.
- The same `request_id` gives the same `record_id` (`PRP-<request_id>`). A re-run of the stage has a new request id and gets a new record. Pending records use `PRP-PENDING-<request_id>`.
- Status codes over HTTP: 200 for any output including pending, 404 for an unknown or other-tenant subject or a non-FBA route, 422 for input that does not match the schema or the wrong stage.
- The model call is bounded to 2 attempts x 12 s plus a 2 s back-off (26 s worst case) so it finishes inside the orchestrator's 30 s stage timeout; a test enforces it.

## Where the work order comes from

The request does not carry what the unit had to be prepped to, so `workorders.py` looks it up, scoped to the organisation:

1. `context.order` on the request, if it has this organisation's `org_id`: `{"org_id", "sku", "fnsku", "work_order_id", "fba_shipment_id", "asin", "operator_id", "captured_at", "requirements": {"polybag", "suffocation_warning", "expiry_date", "handling_marks": ["fragile", ...], "cover_original_barcode"}}`.
2. Otherwise the organisers' `data/sample/prep_sample.csv` (`wo_*` columns), looked up by (`unit_id`, `org_id`).

`captured_at` is the time the caller states, otherwise the photo files' modification time (`payload.captured_at_source` says which). The sample CSV's timestamp is not used: it belongs to photos that do not exist here.

## Run it

```sh
# GEMINI_API_KEY goes in .env (git-ignored). Never commit it.
python -m agents.prep.check --unit UNIT-0002 --org org_demo_alpha front.jpg back.jpg label.jpg   # one unit, readable summary
make case UNIT=UNIT-0002 ORG=org_demo_alpha      # the whole workflow for that unit
.venv/bin/uvicorn agents.prep.app:app --port 8102
```

`agents.prep.check` copies the photos into `data/input/<unit>/prep/`, runs the same code the orchestrator runs, and prints the verdict, each check and the record id. It refuses another organisation's unit before writing anything. `--json` prints the full Agent Output.

Without a key, or without photos, every Prep unit comes back `pending_review` and says why. That is intended, not a bug. Settings (`settings.py`): `GEMINI_API_KEY`, `PREP_GEMINI_MODEL`, `PREP_TIMEOUT_S`, `PREP_MAX_RETRIES`, `PREP_MAX_PHOTOS`, `COST_PER_1M_INPUT_USD`, `COST_PER_1M_OUTPUT_USD`.

## Test it

```sh
pytest tests/integration/test_prep_agent.py     # 66 tests, no key, no network
pytest tests/integration/test_agent_contracts.py
```

The tests replace the model with a scripted observer, so they check everything **except what a real model sees**: the rules for every outcome, the mapping onto the contract keys, UNCERTAIN with each reason, model failure, no capture, a tampered capture, the photo limit, tenancy, idempotency, Receiving's evidence and overrides, the whole organiser sample's work-order flags, the Gemini adapter (against a fake client), and whole orchestrated workflows in which Recovery receives our record and an override of it changes Recovery's answer. Several organiser tests were pinned to the stub's outcomes and are skipped now that Prep is real (`tests/helpers.py: needs_stubs`); the equivalent behaviour is tested in `test_prep_agent.py`.

## Limits (read these)

- **The vision model has never been run through this repository.** No API key was available. The Gemini adapter is exercised only against a fake client, so the real request format, the response schema handling and the real latency are untested. A live run with a key and real photos is still to do.
- **Real-model accuracy is unknown.** Nothing here measures how often the model's observations are right, how often it says `cant_tell`, or the false-positive and false-negative rate of any check. The confidence numbers are a fixed mapping of the model's own band, not measured probabilities. Do not quote an accuracy for this agent.
- **The rule sources are unverified.** Amazon's published prep requirements were not retrieved, so the requirement wording is ours, and `payload.rule_source` is `{"status": "unverified", "url": null, "retrieved_at": null}`. Every check's `detail` ends with `[demo rule, source unverified]`. Which requirements apply to a unit comes from the work order flags, and the organisers' sample flags are dummy values, not Amazon's rules. The reference's invented clause numbers, quotes and thresholds (5 inch opening, 1.5 mil) are deliberately not used.
- **Handling marks can give a false FAIL.** The model lists the marks it can see; if a required mark is on a face the photos do not show and the model still says it can see enough, the unit fails on that mark. The prompt tells the model to say `cant_tell` in that case, but this is untested with a real model.
- **No local photo-quality gate.** Photo usability is the model's own rating; there is no blur or exposure check before the call (the reference had one in the browser only). A bad photo still costs a call.
- **No response cache and no reuse check.** Re-running a unit calls the model again. Nothing detects the same photo being used for two units.
- **No measurements.** `payload.measurements` is empty (see above), so Recovery still cannot speak to weight-tier fees.
- **Receiving is recorded, not acted on.** A Receiving exception appears in `payload.upstream` and in `decision.reason`; no prep check depends on it.
- **A model reading a barcode can misread it.** `fnsku_text_match` demands a high-confidence transcription to FAIL, but that is a rule of thumb, not a measured threshold.
- Photos beyond the sixth are not examined (they are reported). The limit is a setting, not a tested optimum.
- The content hash detects a change only if someone kept the original hash. It is not tamper-evident or immutable on its own.
