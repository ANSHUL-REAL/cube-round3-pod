# agents/recovery/  ·  Recovery Manager

**Owner:** Sai Tarun Reddy Velagala (`@DaKaufeeBoii`), who built the Round 2 Recovery Manager. Rebuilt for Round 3 on the organisers' fee-report schema by Anshul Nautiyal. Where it came from and what changed: [PROVENANCE.md](PROVENANCE.md).

Has no camera. For one unit it reads the organisers' fee-report lines and **all** the evidence the earlier stages left, and decides, charge by charge, whether that evidence contradicts the charge (a claim), supports it, or says nothing (never a claim).

| | |
|---|---|
| **Reads (inputs)** | The unit's fee / reimbursement / inventory-adjustment lines (`data/sample/fee_report_sample.csv`, tenant-scoped through `shared.utils.sample_data.fee_lines`) |
| **Reads (previous evidence)** | Every record in `previous_evidence` (Receiving, Prep, Pack, Returns), each with the **latest override** applied |
| **Produces** | One `charge_<line_id>` check per fee line; a claim for each contradicted charge with its evidence and dollar figure; an explicit list of what cannot be claimed and why |
| **Outcomes** | `claim_recommended`, `no_claim`, `insufficient_evidence`, `pending_review` |
| **Model** | None. Deterministic rules (`model.name = "rules"`, `calls = 0`). No key, no network. |
| **Runs for** | Every unit (the last stage of every flow) |

## The one thing that reads differently

The condition behind every check is **"this charge is supported by evidence"**.

| Check | Position | Meaning | Claim? |
|---|---|---|---|
| `FAIL` | CONTRADICTS | Evidence contradicts the charge (or the charge repeats an earlier one) | **Yes**, with its evidence attached |
| `PASS` | SUPPORTS | Evidence supports the charge, or it was already settled | No |
| `UNCERTAIN` | SILENT | Evidence is absent or not enough | **Never.** Listed in `payload.unclaimable` with the reason |

A wrongly filed claim costs a seller standing; a missed one costs only money. So every rule that can produce a claim also has the guards that stop it, and when in doubt the answer is SILENT.

## How each charge is decided

`payload.charges[].assessment` is the finer label (`CONTRADICTED`, `DUPLICATE`, `SUPPORTED`, `ALREADY_REIMBURSED`, `CREDIT`, `SILENT`); `position` is the contract's three-way reading.

| Charge type in the sample | Rule | Claim only when |
|---|---|---|
| `inbound_defect_fee` | Prep says compliant -> contradicted; Prep says non-compliant -> supported; anything else -> silent | Prep **completed** and its (override-adjusted) verdict is PASS with at least one check, it names the same SKU / FNSKU / shipment as the line, it was taken before the charge posted, the line covers one unit, **and** Receiving recorded no damage or quality flag on the unit |
| `refund_issued_item_not_returned` | Returns `identity_match` PASS -> contradicted (the item came back); FAIL -> supported | Only on a merchant-fulfilled unit (see F-11, F-12 below) |
| `fulfilment_fee_weight_tier` | Silent unless an earlier record carries `payload.measurements.weight_g` **and** the operator supplied a fee schedule with its source | A schedule puts the measured weight at a lower fee than billed (claims the **difference**) and the weight is not within the scale tolerance of a tier boundary |
| `lost_inbound` | Always silent: nothing in this pod comes from the channel's side of the dock (F-10) | Never |
| `damaged_in_warehouse` in a `reimbursement_report` | A credit the channel already paid: `CREDIT`, PASS, nothing to dispute | Never |
| Repeat of an identical line | `DUPLICATE` | Same SKU, shipment, **order id**, type, amount and date as an earlier line in the same unit |
| Charge already credited | `ALREADY_REIMBURSED` | Never. Matched by an explicit `related_line_id` / `original_line_id`, or by same SKU and exact amount credited on or after the charge |
| Any other charge type | Silent: `no_rule` | Never |
| Zero, negative, missing or unreadable amount | Silent, whatever the evidence says (F-09) | Never |

No Amazon policy and no fee schedule is written into this agent. The weight-tier path uses only a schedule you supply (`RECOVERY_TIER_TABLE=path.json`: `source_url`, `retrieved`, `tolerance_g`, `tiers[{up_to_g, fee_usd}]`); a schedule that does not say where it came from is not used. The repository ships none, so on the sample all 42 weight-tier fees are silent.

## The open findings (F-07 to F-12): what this agent does, and where to change it

| Finding | What it does | Switch |
|---|---|---|
| **F-07** no weight evidence | Silent unless measurements **and** a sourced schedule exist | `RECOVERY_TIER_TABLE` |
| **F-08** `unit_id` ambiguity | Joins on the unit id **and** on every ref both sides carry (SKU, FNSKU, shipment, order): a disagreement is `conflicting_evidence`, not a claim. Receiving's PO-line record is never used to support or contradict a unit's charge. A line for several units is not contradicted by one unit's Prep record. A repeat with no order id is not claimed | `policy.single_unit_lines_only`, `policy.duplicate_needs_order_id` |
| **F-09** zero amount | A 0.00 line is never claimed; the record keeps `underlying_position` so a reviewer sees what the evidence said | fixed (D-005) |
| **F-10** supplier shortfall | Shown in the reason as a supplier shortfall and set aside; never counted for or against a channel loss | fixed |
| **F-11** FBA returns | Returns evidence does not contradict a refund-without-return on an FBA unit | `policy.fba_returns_can_contradict` |
| **F-12** no route | A unit with neither Prep nor Pack is `route: unknown`: Returns evidence is not trusted for it | same switch |

All switches are in [`policy.py`](policy.py); the active snapshot is copied into every record (`payload.policy`). Why each side was taken: `docs/decisions.md`, **D-RC01 to D-RC12**.

## The two flows

- **Standard flow** (Receiving, Prep or Pack, Returns, Recovery): Prep evidence exists for FBA units and can contradict an inbound-defect fee.
- **Specialist flow** (no Prep Manager): there is no Prep record, so every inbound-defect charge is SILENT (`no_prep_evidence`) and the reason says so. Pack evidence is read and cited but never stands in for Prep. The agent reads the pod's flow (`pod.json`, or `ORCH_FLOW`) only to word that reason.

## What the record contains

- `checks[]`: one per fee line, key `charge_<line_id>` (normalised to `^[a-z][a-z0-9_]*$`, unique). `evidence_refs` start with the fee row (`fee_report/<line_id>`) and then every upstream record id the position rests on.
- `inputs[]`: every fee row examined, as `csv_row` with the SHA-256 of the row as read.
- `upstream_refs`: every previous record consumed. Records about another subject, or whose `content_hash` no longer matches, are not used and are listed in `payload.ignored_evidence`.
- `payload.charges[]`: per charge: position, assessment, reason codes, the finding each belongs to, reason text, `claim_usd`, `evidence_record_ids`, `basis[]` (record, check, original and **effective** verdict, role), `what_would_settle_it`.
- `payload.claims[]`: per claim: amount, `evidence_record_ids`, `evidence_refs`, fee-row refs, rationale. `payload.claimable_usd` is their sum (exact decimal arithmetic).
- `payload.unclaimable[]`: everything not claimed, with the reason.
- `payload.upstream[]` (each record read, original and effective verdict, which override), `payload.overrides_applied[]`, `payload.findings`, `payload.policy`, `payload.rule_source`.
- `decision`: any claim -> `FAIL` / `claim_recommended`. Otherwise a conflict between records over a non-zero amount -> `UNCERTAIN` / `pending_review` / `needs_human: true`. Otherwise any SILENT charge -> `UNCERTAIN` / `insufficient_evidence`, **`needs_human: false`** (nothing for a person to decide, so it never halts a workflow). Otherwise `PASS` / `no_claim`. A unit with no fee lines is `UNCERTAIN` / `no_claim`: the absence of lines is not evidence the unit was charged fairly.

## Overrides

A workflow override of an earlier record is read from `context.overrides`, the latest wins (`shared.utils.stubs.effective_verdict`), and the original verdict is kept next to it in `payload.upstream`. Prep PASS makes an inbound-defect fee contradicted; overriding Prep to FAIL makes it supported and withdraws the claim; overriding it to UNCERTAIN makes it silent. An override of Receiving can create or clear the defect conflict.

## Fail open, tenancy, repeatability

| Situation | What is returned |
|---|---|
| Unit not under `subject.org_id` | `LookupError` (HTTP 404). Never "no claim" |
| Previous evidence from another organisation | `LookupError`: the whole request is refused |
| Any unexpected error | `pending_review`, status `pending`, `agent_exception`, no checks, no claim |
| Previous record from another subject, or with a wrong hash | Ignored, listed, not cited |

The same request over the same inputs gives the identical record, hash included (`produced_at` is pinned for that request in memory). A re-run of the stage is a new `request_id` and gets a new `record_id` (`RCY-<request id>`).

## Run it

```sh
.venv/bin/uvicorn agents.recovery.app:app --port 8105
make case UNIT=UNIT-0014 ORG=org_demo_alpha                 # a whole workflow: one inbound-defect claim of $2.00
python -m agents.recovery.replay --write-md agents/recovery/REPLAY.md   # offline replay of all 100 sample units
```

[`REPLAY.md`](REPLAY.md) is what the replay prints for the standard flow and the Specialist flow. **It is not an accuracy measure**: the sample has no labels, and every claim in it rests on organiser-stub upstream evidence.

## Test it

```sh
pytest tests/integration/test_recovery_agent.py     # 70 tests, no key, no network
pytest tests/integration/test_agent_contracts.py
```

The tests build their own fee lines and earlier-stage records and cover: each position and outcome; twelve precision cases where a claim would be wrong; the override flip (and the latest of several); the Specialist flow; F-07 to F-12; duplicates and refunds already made; wrong tenant (agent and HTTP); tampered evidence; idempotency; fail-open; contract validity and hash; reading all previous evidence; and whole workflows through `run_workflow`, including a person resolving Prep after a halt. Six mutations (ignore overrides, drop the Receiving guard, claim a zero amount, claim a duplicate without an order id, let an FBA return contradict, treat an UNCERTAIN Prep as compliant) were each applied by hand and caught by at least one test.

## Limits (read these)

- **No accuracy has been measured.** There are no ground-truth labels, so false positives and false negatives are unknown. The Round 2 repository's "100% precision on 10 charges" was 8 self-written scenarios and is not reproduced here.
- **Every claim in the replay rests on organiser-stub evidence** (`csv-replay-stub`). Nothing has been run against the pod's real Receiving, Prep, Pack or Returns agents.
- **"Prep PASS contradicts an inbound-defect fee" is an assumption**, the organisers' own and the Round 2 author's. The sample fee line does not say which defect the channel cited, so the agent cannot check that Prep's checks cover it. The guards (Receiving damage, ref mismatch, timing, quantity) reduce the risk; they are our choices, not channel rules, and how many real claims they cost is not measured.
- **No channel policy or fee schedule is encoded and none was looked up.** The weight-tier claim path has only been run against a test-only schedule in the tests; it has never seen a real one, and the sample fee lines carry neither a billed weight nor a tier.
- **The fee source is the organisers' sample CSV** (dummy values). A real report with other columns is not supported without a change in `fees.py`. Unit existence (tenancy) is also taken from the sample CSVs.
- Duplicates are looked for **within one unit**; ALREADY_REIMBURSED needs an explicit link or an exact amount match, and the sample has neither, so both paths are tested only on our fixtures.
- **A late override leaves a stale claim.** The orchestrator never re-runs Recovery after it has completed: overriding Prep PASS to FAIL after the claim was filed leaves `CLAIM_RECOMMENDED` with the old claim (checked on UNIT-0014). Overrides made before Recovery runs (for example after a halt and `resume`) are honoured.
- The pinned `produced_at` lives in process memory, so the byte-identical guarantee holds within one process only.
- Run as its own HTTP service it was exercised only through FastAPI's test client, not a live `uvicorn`. A non-default flow needs `ORCH_FLOW` set for the "no Prep stage" wording; the decision itself does not depend on it.
- Not carried over from Round 2: the web dashboard, CLI, dispute-letter generator and its own schema. The agent produces a claim record, not a letter.
