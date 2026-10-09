# Real model runs through this repository

Every automated test uses scripted models. This file records the runs that called the real model, so nobody has to take
"it works with Gemini" on trust. These are **smoke runs, not an evaluation**: a handful of photos, no labels, no accuracy.

**Photos used:** real photographs from the Amazon Bin Image Dataset (public, CC BY-NC-SA 3.0 US), the same ones used in the
Round 2 Pack evaluation. They show warehouse bins, not the sample's fictional products, so most answers below are
correctly STOP, UNCERTAIN or "not seen". No AI-generated image was used. The photos stayed outside the repository.

**Key:** the pod owner's Gemini key, in the git-ignored `.env`. Both default models were checked as available to it
on 2026-10-09: `gemini-3.5-flash-lite` (Receiving, Prep, Pack) and `gemini-3.8-flash` (Returns).

## 2026-10-09

| Stage | Unit | Photo | Result | Calls | Time |
|---|---|---|---|---|---|
| Pack | UNIT-0044 (order: soy candle trio) | ABID bin D07 (mug, clamps, ...) | **STOP_AND_FIX**: "Not in the order: printed cardboard item ... (#1)"; `image_quality` UNCERTAIN (404x276 px); candles "not seen, but items may be hidden" | 1 | 10.0 s |
| Receiving | UNIT-0044 | ABID bin D03 | **PENDING_REVIEW**: every check UNCERTAIN, "clarity 0.40, minimum 0.50" | 1 | 7.7 s |
| Prep | UNIT-0014 (work order WO-3002, FNSKU X00DUMMY014) | ABID bin D05 | **NON_COMPLIANT**: `fnsku_text_match` FAIL (read "3C" off a label in the bin); two checks UNCERTAIN | 1 | 8.5 s |
| Returns | UNIT-0016 (towel), on a throwaway copy of the reference folder onboarded with a bin photo | ABID bin D03 | **pending_review**: all five checks UNCERTAIN ("insufficient product body evidence"; "0 of 1 photos passed the quality gate, fewer than 2 usable") | 1 | 11.6 s |
| Whole workflow | UNIT-0016 through the orchestrator CLI | D05, D07, D03 | Receiving UNCERTAIN, Pack refused the photo as **already used for another order** (the photo-reuse ledger, from the Pack run above), Returns `no_product_reference`, Recovery `no_claim` (no fee lines) | 2 | 10.3 s |
| Whole workflow | UNIT-0016 through the console (photos uploaded on the Photos page, **Run** pressed in the browser) | D05, D07, D03, D01 | the same shape: FAILED because Returns could not judge an un-onboarded product | 2 | not timed |
| Pack | UNIT-0008, live simulator in the console (Live mode, **Next step** pressed) | ABID bin photo | **STOP_AND_FIX**: "Not in the order: Nylon Dog Leash (#1); Nylon Dog Leash (#2)." | 1 | 4.2 s |
| Receiving | UNIT-0008, console **Snap & run** window (file picker, the preview pane blocks cameras) | ABID bin photo | **UNCERTAIN**: photo too unclear for a delivery check, sent to a person | 1 | 4.0 s |

The Prep row was run before a label-reading change made later the same day (`_codes_in` in `agents/prep/rules.py`):
the rule now compares only FNSKU-shaped codes (`X0...` / `B0...`), so a reading such as "3C", with no such code in it,
is UNCERTAIN (`insufficient_evidence`), not FAIL. The time limits then were 12 s per model attempt (24 s, one attempt, for Returns) and 30 s per stage;
they are now 28 s and 75 s (D-O07).

## What the real runs found that the tests could not

- **Prep's command-line checker crashed on Windows** printing an arrow (U+2191) that the model wrote. Fixed for every
  command-line entry point (`shared/utils/console.py`).
- **Returns can never judge an organiser SKU out of the box:** the cards have no reference photos. Added onboarding
  (`agents/returns/onboard.py`, and a card on the console's Photos page). See D-RT10.
- **Returns wants at least two usable photos** of a return; the demo guide says so.
- **Live photos cannot support a Recovery claim** on the sample's fees: they are dated after the charges (2026-07). That
  is the intended rule (`evidence_after_charge`); the demo guide shows it, and shows a provable claim on the organisers'
  recorded evidence instead.

## Reproduce

```
python -m agents.pack.check --unit UNIT-0044 --org org_demo_alpha <photo>
python -m agents.receiving.check --unit UNIT-0044 --org org_demo_alpha <photo>
python -m agents.prep.check --unit UNIT-0014 --org org_demo_alpha <photo>
python scripts/serve.py --data <empty folder>     # then the console
```
The command-line checkers copy the photo into `data/input/<unit>/<stage>/`; remove it afterwards if it is not a demo photo.
