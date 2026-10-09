# Live demo: real photos, real model calls, nothing scripted

Every verdict in this demo comes from a real Gemini call on a photo you take in front of the audience, decided by our
rules. Nothing is pre-recorded. Because of that, results depend on your photos: **rehearse once with the same items**.

The sample's products (LED desk lamp, steel bottle, candle trio, bath towel, puzzle) are the organisers' fictional data.
Use real objects that look like them, and say so: the demo shows the pipeline and its honesty, not accuracy on those
products. No Round 3 accuracy figure exists; do not quote one.

## Before the day (30 minutes)

1. **Key.** The repo's git-ignored `.env` must hold `GEMINI_API_KEY` (and nothing is printed or committed).
   Check: `python -m agents.pack.check --unit UNIT-0044 --org org_demo_alpha some_photo.jpg` prints a verdict and
   `calls 1`, not `model_not_configured`.
2. **Labels for Prep.** `python scripts/print_labels.py` writes `out/print-labels.html`. Print it at 100%. It has the
   FNSKU label for UNIT-0014 (`X00DUMMY014`, a real Code 128 barcode, tested with a barcode reader), the FRAGILE and
   THIS WAY UP marks its work order asks for, and a second label with the wrong code for the failure demo.
3. **Items** (anything similar works; it is the photo that is judged):

   | Unit | Story | You need |
   |---|---|---|
   | UNIT-0044 | Wrong item in the box | an open box with anything that is **not** candles (a mug, a bottle) |
   | UNIT-0008 | Right item, seal | an open box with **one** black steel water bottle |
   | UNIT-0023 | The agent is unsure; a person decides | a box with items stacked or half hidden under paper |
   | UNIT-0014 | FBA prep label check | a small boxed item (the "desk lamp") with the printed label and marks stuck on |
   | UNIT-0016 | A return, judged against the product as sold | a blue towel: one photo new and folded, then 2 photos as "returned" |

4. **Start on an empty folder**, so nothing from a rehearsal shows up:
   ```
   python scripts/serve.py --data D:/pod12-demo-live
   ```
   Open http://localhost:8100/. Keep it on this laptop: there is no sign-in.
5. **Photos from the phone.** Take them with the phone camera at full resolution, then get them onto the laptop (cable,
   Drive, Nearby Share) and upload on the unit's **Photos** page. Small photos are flagged `image_quality`.

## The run of show (about 10 minutes)

Each **Run** calls the model once per stage, about 10 seconds each; the button says so while it works.

1. **Wrong box (UNIT-0044), the clearest result.** Photos page, Pack: upload the box with a mug. Run.
   Expect **Pack: STOP_AND_FIX**, naming the item that is not in the order and the fix ("Remove ..."). Open the Pack
   record: every check, the photo's SHA-256, the model and its one call, the content hash.
2. **Right box (UNIT-0008).** Pack: the bottle in its box, whole interior in frame. Expect **SEAL** when the bottle is
   clearly visible and nothing else is in the box. If it answers UNCERTAIN, say why on screen (it lists what it could
   not see): that is the design. It does not guess a seal.
3. **Unsure, then a person (UNIT-0023).** Pack: items stacked or hidden. Expect **UNCERTAIN / needs a person**. On the
   workflow page, record an override with your name and a reason. The original record is not edited; the override sits
   beside it, and the stages that used the old verdict run again on their own (the page says which).
4. **Prep label (UNIT-0014).** Prep: front, back and label photos of the labelled box. Expect `fnsku_text_match` PASS
   (it reads `X00DUMMY014` off the label, without being told the expected code) and the handling marks seen. Then stick
   on the wrong label from the sheet and run again: expect **NON_COMPLIANT, fnsku_text_match FAIL**.
5. **A return (UNIT-0016).** Photos page, the **Returns needs the product as sold** card: add the photo of the towel
   new. Its SHA-256 is written into the product card. Then Returns: 2 photos of the towel as returned. Run. Without the
   reference photo Returns refuses to judge (`no_product_reference`) and calls no model: show that first if time allows.
6. **Recovery, and why it does not claim on today's photos.** On UNIT-0014, Recovery reads every earlier record and
   the organisers' fee report. The inbound-defect fee was posted on 2026-07-18. If Prep failed, Recovery says the fee is
   supported. If Prep passed, your photo is still dated today, so Recovery answers **SILENT: the photo is newer than the
   charge and cannot show the unit's state when it was inspected** (`evidence_after_charge`).
   That refusal is the point: it never claims money on evidence that could not have existed at the time.
7. **A claim it can prove** (optional, 1 minute). Restart with the organisers' recorded evidence:
   `python scripts/serve.py --stubs --data D:/pod12-demo-stubs`. Every page says the four photo stages are replaying the
   organisers' CSVs. **Recovery is still our real agent**: run UNIT-0014 and it recommends a **$2.00 inbound-defect
   claim**, because the recorded Prep inspection predates the charge and shows the unit compliant. Then override that
   Prep record to FAIL: Recovery runs again on its own and the claim disappears; both records stay.
8. **Failure is recorded, not hidden** (optional). Run a unit with no photos for a stage: it ends `FAILED` with
   `no_capture` on that stage, not a made-up verdict.

## If something goes wrong on stage

| You see | Why | Do |
|---|---|---|
| `model_not_configured` | no key in `.env` | add `GEMINI_API_KEY`, restart `serve.py` |
| `model_unavailable` / timeout | network, or the free tier's rate limit | wait a minute, press **Run again**; the failed attempt stays in the record |
| `photo_reuse` UNCERTAIN on Pack | that exact photo was used for another order | take a new photo (this is the reuse check working) |
| `no_product_reference` on Returns | the product was never onboarded | add the "as sold" photo on the Photos page |
| A page from a rehearsal | same `--data` folder | start `serve.py` on a new folder |

## What not to claim

- No accuracy numbers for Round 3. The only measured figures are Round 2 Pack's, on 50 public Amazon warehouse photos
  (in the Round 2 repository), and they do not transfer.
- The products, orders, work orders and fees are the organisers' dummy data; the FNSKUs are `X00DUMMY...`.
- Prep's rules and Returns' condition scale are labelled **unverified** in every record: nobody looked up Amazon's
  published rules for this build.
