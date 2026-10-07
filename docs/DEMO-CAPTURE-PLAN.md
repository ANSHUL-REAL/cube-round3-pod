# Demo capture plan

Every real agent answers `no_capture` until its folder holds a photo, so the integrated demo (`make run`, `make case`) needs the photos below. The organisers' sample data ships none.

```sh
python scripts/capture_plan.py            # what to photograph, per unit and stage, and what is already in place
python scripts/capture_plan.py --check    # exit 1 if any needed folder is still empty
make case UNIT=UNIT-0044 ORG=org_demo_alpha
```

Photos go in `data/input/<unit_id>/<stage>/` (`receiving`, `prep`, `pack`, `returns`; Recovery takes none). The orchestrator hashes and passes them on. The script's instructions come from the inputs in the sample CSVs (what was ordered, what the work order requires), never from the answer columns.

## The five demo units

| Unit | Route | Story | Photos needed |
|---|---|---|---|
| UNIT-0014 | FBA, returned | Receiving, Prep, Returns, Recovery. Recovery can recommend an inbound-defect claim | receiving, prep, returns |
| UNIT-0008 | merchant, clean | Pack the right item: expect `seal` | receiving, pack |
| UNIT-0044 | merchant, wrong box | The order is a candle trio. Put something else in the box: expect `stop_and_fix` | receiving, pack |
| UNIT-0016 | merchant, returned | Receiving, Pack, Returns, Recovery | receiving, pack, returns |
| UNIT-0023 | merchant, uncertain on purpose | Stack or hide items so the photo cannot settle it. Expect `pending_review`, then resolve it with a recorded override | receiving, pack, returns |

For the handbook's other required scenarios (a deliberate failure shown as FAILED, and a wrong-tenant refusal) no photo is needed: stop an agent, and ask for `UNIT-0008` under `org_demo_bravo`.

## Rules for the photos

- Stage them yourselves with real objects. The sample SKUs are fictional ("DUMMY"), so use any similar objects and **say so in the demo**: it shows the pipeline, not accuracy on those products.
- **No faces, no addresses, no real order labels, no personal data.** `data/input/` is deliberately not git-ignored, so a judge can re-run the demo from the repository. Whatever you put there is public if the fork is public.
- Only commit photos you took. Never commit other people's images.
- The model is the only part that sees them. A result from staged photos says nothing about real-world accuracy, and the README must not suggest it does.

## Before the demo

1. `GEMINI_API_KEY` in your own `.env` (never commit it), for the stages that use a model.
2. `python scripts/capture_plan.py --check` exits 0.
3. Run each unit once beforehand: the first model call per photo is slow, later calls hit the local cache.
