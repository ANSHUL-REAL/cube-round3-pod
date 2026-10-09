# Provenance: where the Prep Manager came from

| | |
|---|---|
| **Reference repository** | https://github.com/Cube-Build-A-Thon/cube26-prp-0319-manvith111 |
| **Commit read** | `f5f8ad9` ("feat: multi-provider AI pool (Gemini/Claude/OpenAI) with key failover") |
| **Author** | Manvith111, Round 2, Track 02 (Prep) |
| **Licence** | None. The repository has no licence file, so there is no stated permission to copy code. This port re-implements the design and logic in Python; no TypeScript source was copied verbatim. The organisers supplied the reference for this purpose. |
| **Ported by** | Devisri (`@Devisri-074`), Round 3 pod |

## What was taken from the reference (design and logic, rewritten in Python)

- The split: a model **observes** (met / not_met / cant_tell, a confidence band, which photo and where, the evidence, a usability rating per photo, a transcription of the label) and fixed rules **decide**. (`vision.ts`, `rules.ts`)
- The decision logic: low confidence, no cited evidence, an unusable photo or `cant_tell` give UNCERTAIN; the FNSKU text comparison is derived from the transcribed label; any required FAIL gives FAIL. (`rules.ts` -> `rules.py`)
- Per-unit requirement flags (polybag, expiry, handling marks, cover the original barcode) deciding which checks apply. (`ProductInput` -> `requirements.py: RequirementPack`)
- The prompt's hard rules and the JSON response shape. (`vision.ts` -> `vision.py`)

## What was reviewed and changed (findings confirmed by reading the code)

| Reference behaviour | In this port |
|---|---|
| Next.js on Cloudflare; no Python `handle()` | Python agent, `handle()` plus the starter's HTTP app |
| 11 check ids; verdicts NOT_APPLICABLE and NOT_VERIFIABLE | Rolled up into the contract's `check_key`s; PASS / FAIL / UNCERTAIN only; inapplicable checks omitted; bag thickness listed in `payload.not_verified` |
| `orgId` is a field on the record, but `/api/inspect` never sets it and `GET /api/records` returns every record | Work order looked up per organisation; other tenants raise `LookupError` (404) |
| Rule clauses ("§2.1 Poly-bagging ...") and quotes invented; README says they "model the standard marketplace rules"; thresholds (5 inch, 1.5 mil) unsourced | Not carried over. Own wording; source recorded as unverified; every check says `[demo rule, source unverified]` |
| Model failure saved as a record of eleven UNCERTAIN checks (looks like a normal result) | A `pending` record with the error, no checks, the photos kept |
| Record ids random (`PREP-<time>-<random>`) | `PRP-<request_id>`: same request, same record |
| The expected FNSKU is written into the prompt (a model that knows the answer tends to confirm it) | The model is never told it; rules compare the transcription |
| Only the first failed check named in the overall reason | Every failed and uncertain check named |
| Photos beyond 6 silently sliced off | Reported in `payload.photos_not_used` and in `decision.reason` |
| Every error returned as HTTP 400 | 200 (including pending), 404, 422 as the contract says |
| README says parsing is "retried once"; the code makes one call per credential | One call, one retry on a busy server (429/5xx), 28 s per attempt (`settings.py`): worst case 58 s, inside the 75 s stage timeout (`orchestration/flow.json`, D-O07) |
| README says `npm test` covers "the deterministic rules"; only the browser capture gates (`frameQuality`, `motion`) have tests | The rules, the vision step, the mapping, fail-open and tenancy are all tested (`tests/integration/test_prep_agent.py`) |
| No cost or call count | `model.calls` and `model.cost_usd` (from configured prices) reported |
| No previous-evidence handling | Receiving's record cited in `upstream_refs`, latest override applied, recorded in `payload.upstream` |

## Not carried over, on purpose

The web app and every page of it (including the OpsConsole branding and the placeholder pages for the other managers), the unauthenticated `/api/settings` routes that read and write API keys, the `/api/recovery` route, `wrangler.jsonc` with Cloudflare account ids, the D1 / R2 repository, the catalogue importer and the browser capture gates. Round 3 needs the agent, not the product around it. No API keys, no `.env`, no sample images.

## New for Round 3 (written for this repository)

| File | What it does |
|---|---|
| `agents/prep/app.py` | The agent entry point: `handle()` and the HTTP app |
| `agents/prep/requirements.py` | The check catalogue and the per-unit requirement pack built from the work order |
| `agents/prep/rules.py` | The deterministic decision core and the roll-up onto the contract's keys |
| `agents/prep/vision.py` | The prompt, the response schema, the Gemini observer and cost calculation |
| `agents/prep/adapter.py` | Builds the Evidence Record and the pending record |
| `agents/prep/workorders.py` | Finds the work order for a unit, scoped to the caller's organisation |
| `agents/prep/captures.py` | Reads and prepares the photos named in `inputs`, safely and hash-checked |
| `agents/prep/settings.py`, `check.py` | Settings; a command-line checker |
| `tests/integration/test_prep_agent.py` | 70 behaviour tests on our own fixtures with a scripted model |
