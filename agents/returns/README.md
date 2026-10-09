# agents/returns/  ·  Returns Manager

**Owner:** Prodduturu Krishna Babu (`@krishnababuprodduturu`), who built it in Round 2 (RTN-0038). Adapted for the pod by the pod. Where it came from and what changed: [PROVENANCE.md](PROVENANCE.md).

Checks a returned item from 1 to 3 photos: is it what was sold, is it complete, what condition is it in (Amazon's scale), and what should happen to it. It leaves a record the rest of the system can read.

| | |
|---|---|
| **Reads (inputs)** | 1 to 3 photos in `data/input/<unit_id>/returns/`; the order and sold SKU (see below); the seller's product card under `agents/returns/reference/` |
| **Reads (previous evidence)** | **Pack** (order id, `order_lines`, `observed_in_box`) and **Receiving** (verdict, `quality_flags`), each with the latest human override applied. Every other earlier record is listed in `upstream_refs` |
| **Produces** | `restock`, `refurbish`, `liquidate`, `dispose`, or `pending_review` (a person decides) |
| **Checks** | `identity_match`, `completeness`, `condition`, plus `unit_presence` and `photo_quality` |
| **Runs for** | Returned units only. The flow's routing decides that; the agent does not skip on its own |

## How it decides

1. **A local photo check** (sharpness, light, size) runs on each photo, and zxing-cpp reads any barcode.
2. **One Gemini call per return** is shown the product card (parts list, distinguishing features, look-alike SKUs), the condition rubric, reference images of the right product, and the return photos. It **reports** what it sees. The output schema has no disposition field, and it never sees the operator's tap, the customer, or any earlier record.
3. **Deterministic code decides** (`core/`, Round 2's engine): it drops anything the model invented, applies the consistency rules (e.g. "missing" needs a clear view of where the part would be), fuses identity (a barcode alone never proves the product inside the box), does the parts arithmetic, grades condition against the rubric text, and runs the disposition rules.
4. **Round 3 reads the earlier evidence** (next section) and turns the result into an Evidence Record.

If the model can't tell, the answer is UNCERTAIN with a reason, never a guess. The `payload.disposition` block says which rule fired (`rule_id`), the inputs it was given (`inputs_sha256`) and the engine's own route even when the outcome is `pending_review`.

## What it takes from Pack and Receiving

Pack's payload says what was ordered and what was in the box. Returns compares that with what came back (`core` never sees it, `upstream.py` does):

| Situation | Effect |
|---|---|
| Pack judged a **different order** than the one returned, or the returned SKU is **not on Pack's order lines**, or Pack (verdict FAIL) **counted none of the returned SKU** in the box | `identity_match` becomes **UNCERTAIN** (`conflicting_evidence`), the outcome is `pending_review`, a person decides. The contract says two records that disagree about scope are UNCERTAIN, not a guess (EVIDENCE-CONTRACT section 9, finding F-08) |
| Pack's effective verdict is PASS | What was sent is the order lines (`payload.sent_vs_returned`) |
| The returned wrong item is the one Pack counted in the box | The check stays FAIL (it is not the ordered item) and says it may be what was packed, not a customer swap |
| Receiving's effective verdict is FAIL and it flagged `missing_components`, `wrong_variant`, `wrong_colour`, `obvious_defect` (or a failed `unit_damage`) | The matching check's detail says "may predate the sale" and cites Receiving's record. **No verdict changes**: who is at fault is Recovery's question |
| Pack or Receiving missing, pending, or not saying what was sent | No conflict, and `payload.upstream.notes` says so. FBA units skip Pack, so its record id can be absent |

**Overrides.** The latest workflow override of a record (`context.overrides`) is its effective verdict. Overriding Pack from FAIL to PASS removes a `not_packed` conflict; overriding Receiving to PASS removes the "may predate the sale" note. The original verdict stays in `payload.upstream`.

The check that used an earlier record cites its id in `evidence_refs`; `upstream_refs` lists every earlier record in the request.

## The condition scale (read this)

Condition is graded on the rubric snapshot in `reference/rubrics/`, which quotes **Amazon's published condition guidelines**. The grades are `New`, `Used - Like New`, `Used - Very Good`, `Used - Good`, `Used - Acceptable`, plus a list of conditions that make an item unacceptable.

**The source is an unverified substitute.** Amazon's seller documentation for the target marketplace (amazon.in) needs a login, so the Round 2 author used the public **Amazon UK Condition Guidelines PDF (16 Dec 2020)** and stamped every snapshot `source_marketplace: amazon.co.uk`, `applies_to_marketplace: amazon.in`, `verification_status: unverified_substitute`. Each record carries this in `payload.rule_source` (snapshot id and hash, document URL, retrieval date and SHA-256 of the PDF as recorded in Round 2). **This pod did not download the PDF or re-check that hash, and has not confirmed that the grade labels or definitions match what Amazon publishes for any marketplace.** Whether the scale is right for the pod's marketplace is for the owner to confirm. The model proposes a grade only by quoting the rubric verbatim; quotes that are not exact rubric text are discarded.

If no grade can be assigned the condition check is UNCERTAIN (`insufficient_evidence`), the amazon condition is `null`, and the unit goes to a person. The function of an item is never tested (`functional_check: not_performed`), so opened electrical items are routed to refurbish by rule.

## What the record contains

- **Checks.** `identity_match` first (the organiser Recovery stub reads `checks[0]` as "the right item came back"), then `completeness`, `condition`, `unit_presence`, `photo_quality`. `condition` is PASS when a grade was assigned and nothing physically unacceptable was seen, FAIL for severe damage, dirt, a used consumable, or an opened item in a New-only category, UNCERTAIN when ungradable. `photo_quality` is never FAIL: nobody is there to retake the photo, so fewer than 2 usable photos is UNCERTAIN (`poor_image`) and the unit is a pending review.
- **Outcome vs verdict.** The outcome is a route only when the verdict is not UNCERTAIN, nothing asks for review, and the earlier records do not conflict; otherwise `pending_review` (the engine's route is kept as `payload.disposition.engine_recommendation`). Disposing always needs sign-off, so `dispose` and high-value routes set `needs_human`. A record that contradicts itself is never returned: the adapter raises.
- **Payload.** `amazon_condition`, `condition_grade`, `observed_state`, `parts_missing`, per-component results (`components`), `claim_signals` for Recovery (`item_not_returned`, `wrong_item_returned`, `returned_damaged`, each `yes`/`no`/`uncertain` with its basis), `sent_vs_returned`, `upstream`, `operator` (what the operator tapped, and whether the agent agrees), retake requests, validator actions, `rule_source`, the model manifest (prompt hashes), photos with their original and analysed hashes.
- `model`: model id and version as reported, prompt versions, `calls` (1), cost only if `COST_PER_1M_*` are set (otherwise `null`; no price is guessed). The disposition is **not** the model's; `payload.disposition.decided_by` says `deterministic_engine`.

## Fail open (it never invents a verdict)

| Situation | What is returned |
|---|---|
| No photo | `pending_review`, `error.code = no_capture`, retryable |
| Photo missing, altered (hash), another unit's, outside the unit's `returns/` folder, or not an image | `pending_review`, `capture_unreadable`, not retryable (status `error`) |
| The product has no verified reference (card, reference image, rubric, policy, category entry) | `pending_review`, `no_product_reference`, not retryable, **model not called** |
| No `GEMINI_API_KEY` | `pending_review`, `model_not_configured`, retryable, photos kept |
| Model error, timeout, empty or unparseable answer | `pending_review`, `model_unavailable` / `model_output_invalid`, retryable, photos kept |
| A reference document fails its own hash | Treated as missing (the line above) |

More than 3 photos: the first 3 are used and the rest are listed in `payload.photos_not_used`.

## Tenancy and repeatability

- A unit, or a return context, that belongs to another organisation raises `LookupError` (HTTP 404). Product cards are looked up under the caller's own organisation only.
- Captures must sit under the capture root, in `<unit_id>/returns/`, and match their declared hash.
- The same `request_id` gives the same `record_id` (`RTN-<request_id>`). A re-run has a new request id and gets a new record.

## Where the return comes from

`returned.py`: `context.return` (or `context.case.return`) if it names this organisation (`{"org_id","order_id","ordered_sku","operator_state"?,"operator_disposition"?,...}`), otherwise the organisers' `data/sample/returns_sample.csv` row for (`unit_id`, `org_id`). The operator's tap is never sent to the model.

## Putting a product on the books

A product is judged only if `agents/returns/reference/products/<org_id>/<SKU>.yaml` is a valid card **with reference images**, mapped in `reference/categories/sku-category-map.yaml`, and its category has a rubric and a policy. Every document carries `content_sha256`; edit one and re-hash it, or it is ignored. **The organisers' ten sample SKUs have placeholder cards with invented features and values and no reference images, so units of those SKUs are not judged until someone onboards them.** Only `SKU-PHONE-IQOO9` and `SKU-LAPTOP-DELL` (organisation alpha) are onboarded, and neither is in the sample CSV, so use `context.return` for them. A card with only one critical product-body feature cannot reach identity `yes` without a matching barcode.

**Onboard a product** with one real photo of it as sold (new, parts laid out); the photo's SHA-256 goes into the card and the card is re-sealed (D-RT10):

```
python -m agents.returns.onboard --org org_demo_alpha --sku SKU-TOWEL-BLU towel_new.jpg
```

or use the **Returns needs the product as sold** card on a returned unit's Photos page in the console. Only the photo becomes real: the rest of a placeholder card is still invented and says so. Take **at least two photos of the return** itself; with fewer, the quality gate leaves the unit UNCERTAIN and asks for a retake (seen on the first real run, [`docs/REAL-RUNS.md`](../../docs/REAL-RUNS.md)).

**Two code trees.** `returns_manager/` (merged in PR #1) is the owner's full Round 2 product, kept as reference. Nothing imports it; the agent that runs is `app.py` with `core/` (D-RT11).

## Run it

```sh
# GEMINI_API_KEY goes in .env (git-ignored). Never commit it. RETURNS_MODEL, RETURNS_MODEL_TIMEOUT_S,
# RETURNS_THINKING (minimal|low|medium|high), RETURNS_OUTPUT_MODE (json_prompted|json_schema) are optional.
make case UNIT=UNIT-0014 ORG=org_demo_alpha      # the whole workflow for that unit
.venv/bin/uvicorn agents.returns.app:app --port 8104
```

Without a key, without photos, or without a product reference, every Returns unit comes back pending and says why. That is the intended behaviour, not a bug.

## Test it

```sh
pytest tests/integration/test_returns_agent.py     # 49 tests, no key needed
pytest tests/integration/test_agent_contracts.py
```

The tests replace the model with a scripted judge, so they check everything **except what the real model sees**: every outcome, UNCERTAIN with reasons, model failure, no capture, no product reference, wrong tenant, tampered captures, idempotency, the blinding of the model, reading Pack's and Receiving's evidence including overrides, the Gemini wrapper against a fake SDK client, and two whole orchestrated workflows in which Pack's record reaches Returns and Returns' record reaches Recovery.

## Limits (read these)

- **No live model run through this pod.** The Gemini call (`judge.py`) is tested only against a fake SDK client. The default model id (`gemini-3.8-flash`) is the Round 2 default and has not been checked against any key here. Round 2 called the Interactions API with tools; this agent calls `generateContent` once with no tools, JSON mime type and the schema in the prompt (`RETURNS_OUTPUT_MODE=json_prompted`). **That path is untested against the real service**, and Round 2's accuracy numbers do not carry over to it automatically.
- **Round 2's own evaluation is small and self-graded** (its `NOTES.md` says so): 30 real Wikimedia product photos of 8 products, one labeller who also designed the cases, fewer than the 50 units its rules asked for, 10 of 30 never reached the model. On those runs the model left condition UNCERTAIN on 24 of 30 units (strict condition accuracy 10 %, coverage 20 %), completeness agreed with the labeller on 18 and disagreed on 9, and the author's "identity" column was a pass-through, not a measurement. **No accuracy figure for this agent was measured here.** Those photos are catalogue photos, not returned parcels.
- **The condition scale is an unverified substitute** (above). The category policies (which route an opened or damaged item takes) are the Round 2 author's business assumptions, labelled `business_policy` or `assumption` in the files, not Amazon's.
- **All money is synthetic.** List prices, recovery rates and refurbish costs on the cards are invented (`synthetic: true`, INR). The thresholds in `reference/rules/disposition-params.yaml` (restockable grades, minimum refurbish gain, dispose-below salvage, high-value) are Round 2 placeholders. `expected_recovery_minor` is labelled synthetic wherever it appears.
- **The photo-quality thresholds are uncalibrated placeholders** (`reference/quality/quality-gate.yaml` says so). Small or dim photos may be flagged poor, or poor ones accepted; the gate only ever makes a unit UNCERTAIN, never FAIL.
- Round 2's checks for a photo reused across returns and near-duplicate photos needed its database and are **not carried over**. Barcode decoding needs `zxing-cpp`; without it identity needs two matching body features.
- **Latency is unmeasured through the pod.** The model call is one attempt bounded to 24 s so it finishes inside the orchestrator's 30 s stage timeout, and thinking defaults to `low` for that reason (Round 2 used `medium` and targeted a 45 s p95, which would not fit). A slow answer becomes a retryable pending record. Raising `timeout_s` for the returns step in `orchestration/flow.json` is a pod decision.
- Upstream rules read Pack's SKU counts, which are a model's counts (Pack stopped 12 of 24 good boxes in its own Round 2 eval). A `not_packed` conflict therefore asks a person; it does not claim the item was never sent.
- Pack's `observed_in_box` is per SKU, not per part, so it cannot say which part of a unit was missing. Returns' completeness stays the card's parts list against the photos.
- A returned unit that was FBA-routed is judged like any other (finding F-11): the record keeps the route and says what the photos show. Whether that contradicts a channel charge is Recovery's call; `claim_signals` are observations, not claims.
- `captured_at` is the operator's capture time when known, otherwise the time of the check (the request has no capture timestamp).
