# agents/receiving/  ·  Receiving Manager

**Owner:** Sai charan (`@cherryy-x23`), who built it in Round 2. Ported into this pod and reworked for Round 3 (see [PROVENANCE.md](PROVENANCE.md) for what was kept and what changed).

Checks what arrived from the supplier against the purchase-order line, from photos taken at receipt, and leaves a record the rest of the system can read. It is the only point where a supplier claim is still possible.

| | |
|---|---|
| **Reads (inputs)** | Up to 6 photos in `data/input/<unit_id>/receiving/` (pallet, carton, unit, barcode); the PO line (see below) |
| **Reads (previous evidence)** | An earlier Receiving record of the same workflow (a re-run), cited with its latest human override in `payload.previous_receiving`. First stage otherwise: nothing |
| **Produces** | `accept`, `accept_with_exceptions`, `reject` or `pending_review`, with named checks |
| **Checks** | `identity_match`, `carton_count`, `quantity`, `carton_damage`, `unit_damage`, `quality_flags` |
| **`subject.unit_scope`** | `po_line` (Round 2 finding F-08: in Receiving a unit is a PO line) |

## How it decides

1. **One Gemini call per delivery**, carrying every photo as image bytes. The model **observes only**: it reads the SKU, barcode and label text, counts cartons and units, grades carton and unit damage, and reports colour, variant, components and any obvious defect. **It is not shown the purchase order**, so it cannot "confirm" what it is told to expect (Round 2 put the expected SKU and quantities in the prompt). It also reports how clear the photos are and how sure it is of each group of readings.
2. **Fixed rules** (`rules.py`) compare that with the PO line. Every verdict is arithmetic or a string comparison. A photo that is too unclear, or a reading the model itself is not confident in, can only make a check `UNCERTAIN`, never a PASS or a FAIL.
3. **Outcome** is derived from the checks:

| Checks | Verdict | Outcome |
|---|---|---|
| all PASS | PASS | `accept` |
| any FAIL, identity is PASS or UNCERTAIN | FAIL | `accept_with_exceptions` (short, over, damaged or off-spec goods are usable stock, and the evidence is what a supplier claim needs) |
| `identity_match` FAIL (wrong goods) | FAIL | `reject` |
| no FAIL, any UNCERTAIN | UNCERTAIN | `pending_review`, `needs_human` |

A FAIL with something unresolved alongside it keeps the FAIL and sets `needs_human`.

### The six checks

| Check | PASS | FAIL | UNCERTAIN (`uncertain_reason`) |
|---|---|---|---|
| `identity_match` | SKU text, barcode or label text matches the ordered SKU/ASIN, nothing contradicts | SKU text or a SKU/ASIN-shaped barcode names a different product | nothing readable (`insufficient_evidence`); evidence both ways (`conflicting_evidence`); unclear photos (`poor_image`) |
| `carton_count` | counted = ordered | short or over | not countable |
| `quantity` | units received = ordered (a direct count, or cartons x units per carton) | shortfall or overage | not countable; the direct count and cartons x units disagree (`conflicting_evidence`) |
| `carton_damage`, `unit_damage` | `none` | `crushing`, `water` or `tears` | model says it cannot tell |
| `quality_flags` | colour, variant and required components match the spec and no obvious defect | any of `wrong_colour`, `wrong_variant`, `missing_components`, `obvious_defect` (named in `observed`) | could not confirm something the PO constrains |

Colour and variant only count when the PO constrains them (`n/a` and `standard` do not). Words are compared order-insensitively ("pack of 3" equals "3-pack"; a trailing quantity such as "x2" is ignored for components). Checks never carry an invented `confidence`: they are deterministic, so it is null, and the model's own self-reported figures are kept in `payload.observation`.

## What the record contains

- `payload`: `supplier`, `po_number`, `po_line`, `sku`, `qty_ordered`, `qty_received`, `shortfall_units`, `overage_units`, `cartons_ordered`, `cartons_received`, `units_per_carton_counted`, `quality_flags[]`, `missing_components[]`. A number that could not be read reliably is `null`, never a guess.
- `payload.shortfall_scope = "supplier_inbound"`: a shortfall here is supplier-side, before the goods reached any channel (finding F-10). It is evidence for a supplier dispute, not for a channel inbound-loss claim.
- `payload.observation`: exactly what the model reported. `payload.photos`: for each photo its ref, view, the hash of the original and of the (downscaled, metadata-stripped) image the model saw.
- `model`: the model, its reported version, `calls` (every request actually sent, including a retry), cost only if prices are configured.

## Fail open (it never invents a verdict)

| Situation | What is returned |
|---|---|
| No photo | `pending_review`, `error.code = no_capture`, retryable. The model is not called |
| Photo missing, altered (hash mismatch), outside this unit's `receiving/` folder, or not an image | `pending_review`, `capture_unreadable`, not retryable |
| No `GEMINI_API_KEY` | `pending_review`, `model_not_configured`, retryable, photos kept, `calls: 0` |
| Model error, timeout, throttling, or an answer that does not match the schema | `pending_review`, `model_unavailable` (or `model_error`), retryable, photos kept, calls counted |
| PO data missing or impossible (quantities do not add up) | `pending_review`, `order_invalid`, not retryable |

A pending record has no checks and `needs_human = true`.

## Tenancy and repeatability

- A unit that belongs to another organisation raises `LookupError` (HTTP 404) before any photo is read. A `context.order` for a different organisation is ignored.
- Captures must be `<unit_id>/receiving/<file>` under the capture root and must match their declared hash.
- The same `request_id` gives the same `record_id` (`RCV-<request_id>`); a re-run of the stage has a new request id and gets a new record.

## Where the PO line comes from

1. `context.order` (or `context.case.order`) on the request, if it carries this organisation's `org_id`: `po_number`, `po_line`, `supplier`, `sku`, `asin`, `product_title`, `spec_colour`, `spec_variant`, `spec_components`, `cartons_ordered`, `units_per_carton_ordered`, `qty_ordered`.
2. Otherwise the organisers' `data/sample/receiving_sample.csv`, looked up by (`unit_id`, `org_id`). **Only the ordered and spec columns are read.** The received, damage and identity columns are the answers the agent is supposed to find from the photos; they are never used.

## Run it

```sh
# GEMINI_API_KEY goes in .env (git-ignored). Never commit it.
python -m agents.receiving.check --unit UNIT-0003 --org org_demo_bravo pallet.jpg carton.jpg unit.jpg
make case UNIT=UNIT-0003 ORG=org_demo_bravo      # the whole workflow for that unit
.venv/bin/uvicorn agents.receiving.app:app --port 8101
```

`agents.receiving.check` copies 1 to 6 photos into `data/input/<unit>/receiving/` (name them after what they show: the file name is passed to the model as the operator's label), runs the code the orchestrator runs, and prints the outcome, each check and the record id. It refuses another organisation's unit before writing anything. `--json` prints the full Agent Output.

Without a key, or without photos, every unit comes back `pending_review` and says why. That is the intended behaviour, not a bug. Consequently `make run` on the unmodified sample (which has no photos) shows every workflow as `FAILED` / `INCOMPLETE` at Receiving.

Settings (environment or `.env`): `GEMINI_API_KEY`, `GEMINI_MODEL` (default `gemini-3.5-flash-lite`), `RCV_MIN_CLARITY` (0.5), `RCV_MIN_CONFIDENCE` (0.6), `MAX_PHOTOS` (6), optional `COST_PER_1M_INPUT_USD` / `COST_PER_1M_OUTPUT_USD`.

## Test it

```sh
pytest tests/integration/test_receiving_agent.py     # 50 tests, no key needed
pytest tests/integration/test_agent_contracts.py
```

The tests replace the model with a scripted perceiver (and, for the Gemini wrapper, a scripted client), so they check everything **except what a real model sees in a real photo**: every outcome, UNCERTAIN with its reason, the clarity and confidence gates, model failure, no key, no capture, wrong tenant, altered and out-of-folder captures, idempotency, the request sent to the model (one call, all photos, no purchase-order data), the model block, override citation, and whole orchestrated workflows. The organiser's orchestration tests run Receiving on the organiser stub kept in `tests/stubs/receiving_stub.py` (see D-V05 in `docs/decisions.md`).

## Limits (read these)

- **The real model has never been run through this agent.** There is no API key in this environment, and no real receiving photos exist. Nothing here measures how well Gemini reads cartons, labels or counts. The Gemini wrapper is tested only against a scripted client: it proves what we send (photo bytes, no PO data), retry and failure handling, and that the response schema converts offline; it does not prove the live API accepts it.
- **No accuracy figure exists for this agent.** Round 2's evaluation harness (Cohen's kappa, confusion matrices) was built but never fed data: its own report says "NOT YET AVAILABLE". Nothing was carried over as a result and none is claimed.
- **The prompt and model default are untested.** `gemini-3.5-flash-lite` is the model the pod used for Pack, not one chosen or measured for receiving.
- **The thresholds are guesses.** `RCV_MIN_CLARITY` and `RCV_MIN_CONFIDENCE` are uncalibrated defaults. The model's self-reported clarity and confidence are not calibrated probabilities. They can only turn a verdict into UNCERTAIN, so a wrong value costs a human look rather than a wrong claim, but they have not been tuned on any data.
- **A supplier claim built on a FAIL needs a person to look first.** A false FAIL is possible: colour and variant are matched by words after normalisation, so a true synonym ("navy" for "blue") reads as `wrong_colour`; a required component is looked for by name, and a quantity inside a name ("candle x3") is not counted separately. An exception is accepted with the evidence, not filed anywhere.
- **Rules about suppliers or Amazon are not encoded.** What counts as acceptable damage, reject thresholds and claim windows are not looked up from any source, so they are not applied: every damage type is a FAIL, wrong goods are rejected, everything else is accepted with exceptions. The accept/reject mapping is our decision (D-V02), not an organiser or channel rule.
- **`captured_at` comes from the organisers' sample row** (or `context.order`), because a file's own timestamp is not reliable after a copy. It is not the time a photo was taken.
- **No reuse check.** Unlike Pack, nothing detects the same photo being used for two deliveries.
- **Model time is bounded** to 2 attempts x 12 s (worst case 26 s) so it ends inside the orchestrator's 30 s stage timeout; a slower answer becomes a retryable pending record.
- The Round 2 web UI, REST backend, demo-scenario seeding and evaluation harness were left in the Round 2 repository, not ported.
