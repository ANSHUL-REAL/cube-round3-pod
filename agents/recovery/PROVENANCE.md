# Provenance: where the Recovery Manager came from

| | |
|---|---|
| **Round 2 repository** | https://github.com/DaKaufeeBoii/cube26-rcy-0250-dakaufeeboii |
| **Commit read** | `4b0f053d69e6ba5f964c1747012d8cdcdf692216` (2026-10-07, "another update man look away"), read-only clone; nothing was written to that repository |
| **Round 2 author and owner** | Sai Tarun Reddy Velagala (`@DaKaufeeBoii`), Track 05 Recovery Manager |
| **Round 3 rebuild** | Anshul Nautiyal (`@ANSHUL-REAL`), for the pod, in this repository |

## What was carried over: ideas and rules, not files

**No Round 2 file was copied.** The Round 2 engine is built on its own schema (`FeeCharge`, `OperationalEvidence`, free-text fee types and keyword matching) and its own 8 hand-made scenarios; it never read the organisers' fee report or the pod's evidence records. These parts were kept as rules and rebuilt on the organisers' schema:

| Round 2 (`recovery_manager/`) | Here |
|---|---|
| The assessment vocabulary CONTRADICTED / SUPPORTED / SILENT / DUPLICATE / ALREADY_REIMBURSED (`models.py`) | `rules.py` `Position.assessment`, mapped onto the contract's CONTRADICTS / SUPPORTS / SILENT |
| Prep PASS contradicts a defect fee; Prep FAIL supports it (`engine.py`) | `rules.inbound_defect_fee`, using the **effective** (override-applied) verdict |
| Prep PASS against a Receiving damage record is UNCERTAIN, not a claim (scenario 6) | The Receiving defect guard: a conflict, not a claim |
| F-07 weight tier, F-09 zero amount, F-10 supplier shortfall are SILENT (`engine.py`, `test_contract_and_tenancy.py`) | `rules.fulfilment_fee_weight_tier`, the amount guard in `rules.assess`, `rules.lost_inbound` |
| Duplicate and already-reimbursed detection (`engine.py`, `store.py`) | Rebuilt: see below |
| Tenancy and fail-open at the agent boundary (`contract.py`) | `fees.known_subject`, `upstream.Evidence`, `app.handle` |

## What was changed, and why

Run once on 2026-10-07, read-only, against the organisers' `fee_report_sample.csv` (their parser and engine, no evidence loaded): **14 of the 61 lines were classed `DUPLICATE` / `ACTIONABLE`, $53.70, all unrelated weight-tier fees on different units.** The duplicate signature was shipment id + fee type + amount, and the sample shares one shipment id across many units. Rebuilt here: a duplicate needs the same SKU, FNSKU, shipment, **order id**, type, amount and date within one unit; on the sample there are none (`test_the_sample_fee_report_has_no_duplicates_and_we_do_not_invent_any`). This is a count of what the Round 2 engine does on this data, not a claim about its quality elsewhere.

Other deliberate differences:

- **No generic fallback.** The Round 2 engine treated any PASS record as contradicting any fee whose text matched no keyword (relevance 0.6). Here a charge type without a rule is SILENT.
- **No keyword matching.** Rules key on the organisers' `charge_type` and on named upstream checks.
- **Reimbursements matched on shipment id + amount** (shared across units in the sample) became an explicit link or same SKU and exact amount.
- **Its tenancy check could never fail** (`requesting_org = ... or org_id`). Tenancy is now: the unit must exist under the org in the fee report or another organiser dataset, and another organisation's evidence refuses the request.
- **Override handling rewrote the upstream record's checks in place** (`apply_overrides`). Here upstream records are read-only and `effective_verdict` is used; both the original and the effective verdict are recorded.
- **Its Agent Output does not validate** against the pod's `agent-output` schema (checked on UNIT-0014 with its `handle_agent_request`: missing `schema_version` and `workflow_id`, a `trace` key in every check, `route` inside `subject`, `next_step_recommendation` as a string). The record here is built with `shared.utils.records` and validated in the tests.
- **Its evaluation** (`submissions/dakaufeeboii/eval-report.md`: 10 charges from its own scenarios, 100% precision, no false claims) is not carried over and is not repeated as a result.
- **Dispute-letter generator and web dashboard** were left behind. The record carries each claim's amount, evidence and rationale instead.

## What is new in Round 3 (written for this repository)

| File | What it does |
|---|---|
| `app.py` | `handle()` and the HTTP app; tenancy, fail-open, idempotent timestamps |
| `fees.py` | The organisers' fee lines as typed values, tenant-scoped; `charge_<line_id>` keys |
| `upstream.py` | Reads all previous evidence; effective verdicts; ref conflicts; hash check |
| `rules.py` | One position per charge, with the guards |
| `policy.py` | Where the open findings F-07 to F-12 are decided, in one place |
| `adapter.py` | Builds the Evidence Record, the claims and the "cannot claim" list |
| `replay.py`, `REPLAY.md` | Offline replay of the sample units through the orchestrator (not an accuracy measure) |
| `tests/integration/test_recovery_agent.py` | About 80 behaviour tests on our own fixtures |

## Not carried over, on purpose

No API keys, no `.env`, no model calls: the Round 2 engine used none either, and neither does this agent.
