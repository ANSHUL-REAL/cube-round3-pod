# Decisions

Every non-obvious design choice gets one entry, so a reviewer can see **what you chose, why, and what you rejected.** Newest last. This is where *Decision quality* and *Orchestration* in the [rubric](../ROUND3-RUBRIC.md) are won or lost. It is not meant to become a long report: a few lines per decision.

Write an entry whenever you: change the flow or its policies; change how the orchestrator stores state or evidence; change the final-outcome or status rules; choose a communication mechanism; decide how retries and overrides work; pick a side on a **known finding** (below); add to the contract's `payload`; or choose a deployment shape.

## Template

```text
### D-NNN · Short title
- Date / Owner:
- Context: what forced a decision?
- Options considered: A, B, C
- Decision: what we chose
- Why: the evidence or reasoning
- Consequences: what gets easier / harder; what would make us revisit
```

## Questions your Pod's decisions should answer

- Why this orchestration approach, and who owns what in it?
- Why this communication mechanism (in-process, HTTP, queue)?
- How is workflow state stored, and how does it survive a restart?
- How are retries, timeouts and resume handled?
- How is evidence persisted, and how is its immutability enforced?
- How are overrides captured, referenced, and used downstream?
- What do we do about UNCERTAIN: continue or block, and who decides?
- How does the final outcome treat weak or uncertain evidence?

## Starter decisions (made by the organisers; change them with a new entry)

### D-000 · The default flow is routed, not strictly sequential
- Context: the Round 2 sample gives each unit a Prep record *or* a Pack record, never both, and Returns only for returned units.
- Decision: `flow.json` routes FBA units through Prep, merchant-fulfilled units through Pack, and runs Returns only when a return happened. A stage that does not apply is `skipped` with the reason recorded.
- Why: forcing every unit through all five stages would invent evidence. Units with neither route (F-12) skip both.

### D-001 · The orchestrator owns state; status and outcome are derived
- Decision: workflow status and final outcome are pure functions of the stored evidence and the overrides (`orchestration/rollup.py`). Agents return evidence and a recommendation; they never write state.
- Why: "the latest agent outcome" and "Recovery's reading of it" are not the source of truth; the traceable evidence chain is.

### D-002 · UNCERTAIN continues by default; blocking is a policy
- Decision: `on_uncertain: continue` by default; `block` halts only when the UNCERTAIN result asks for a person (`needs_human`). Either way the workflow is `BLOCKED` with outcome `NEEDS_REVIEW` until an override resolves it.
- Why: a warehouse line must not wait, and the evidence of later stages is not lost. Recovery's SILENT (UNCERTAIN, `needs_human: false`) must not halt anything.

### D-003 · Failures are recorded, never hidden; never success
- Decision: a failed stage gets a degraded evidence record (no checks, UNCERTAIN, the error) and the workflow ends `FAILED` / `INCOMPLETE` (`provisional`). `resume` retries it and keeps the failed attempt's evidence.

### D-004 · Overrides are workflow entries that reference evidence
- Decision: evidence is immutable. A person's override is appended to the workflow's `overrides` with actor, reason, timestamp, the record it supersedes, the previous effective verdict and the new one. The latest wins; downstream agents receive them in `context.overrides`.

### D-005 · Zero-amount reimbursements are not claimable (F-09)
- Decision: the Recovery stub treats a 0.00 line as SILENT. Why: claiming $0 is meaningless and the meaning of 0.00 is unresolved.

### D-006 · Field names (F-15)
- Decision: `check_key`, `detail`, `content_hash`, `latency_ms`, `client_id` follow the Round 2 Returns list; `org_id`, `operator_id`, `inputs`, `model.version` follow the CSVs. Mapping in [`EVIDENCE-CONTRACT.md`](../EVIDENCE-CONTRACT.md). Open for the organisers.

## Known findings carried over from Round 2

Round 2 participants raised these contradictions and gaps in the shared data and documents. They are **open**: the organisers will rule on them. Until then **do not silently pick a side**: add an entry above with your assumption, and design so that changing it is cheap. `F-07` to `F-12` match the issue numbers on the Round 2 Recovery repo.

| ID | Finding | Why it matters for integration | Source |
|---|---|---|---|
| **F-07** | 42 of 61 sample fee lines are `fulfilment_fee_weight_tier`, and no upstream sample records measured weight or dimensions. | Recovery can only mark these SILENT. Prep is the natural source: see `payload.measurements`. | [Recovery #7](https://github.com/Cube-Build-A-Thon/cube-05-recovery-manager/issues/7) |
| **F-08** | `unit_id` means a **PO line** in Receiving (RCV-0003: 48 ordered, 44 received) but a **single unit** in the fee report. UNIT-0003 is lost inbound, then charged a fulfilment fee, then returned: that cannot be one physical unit. | Joins on a bare id can be wrong. The contract adds `subject.unit_scope` and `subject.refs`. | [Recovery #8](https://github.com/Cube-Build-A-Thon/cube-05-recovery-manager/issues/8) |
| **F-09** | A `lost_inbound` adjustment is posted with `amount_usd` 0.00. "Not reimbursed" (a claim to raise) or "amount missing"? | The answer flips the verdict. See D-005. | [Recovery #9](https://github.com/Cube-Build-A-Thon/cube-05-recovery-manager/issues/9) |
| **F-10** | Receiving shortfalls are supplier-side and happen before goods reach the channel, so they cannot support a channel `lost_inbound` claim. | Keep supplier shortfall and channel loss separate in your decision logic. | [Recovery #10](https://github.com/Cube-Build-A-Thon/cube-05-recovery-manager/issues/10) |
| **F-11** | Returns records exist for FBA-routed units (UNIT-0003 has a Prep record **and** a seller-side Returns record). Do FBA returns come back to the seller or to the channel's warehouse? | Decides whether Returns evidence can contradict `refund_issued_item_not_returned`. | [Recovery #11](https://github.com/Cube-Build-A-Thon/cube-05-recovery-manager/issues/11) |
| **F-12** | 9 of the 100 sample units have neither a Prep nor a Pack record, although each unit is meant to take one route. | The starter marks these `route: "unknown"` and skips both stages. Recovery's SILENT rate depends on it. | [Recovery #12](https://github.com/Cube-Build-A-Thon/cube-05-recovery-manager/issues/12) |
| **F-13** | The Round 2 rules said the organisers would provide an official evidence contract. None was published, and one participant's v0 proposal was withdrawn pending it. | **Resolved for Round 3:** [`EVIDENCE-CONTRACT.md`](../EVIDENCE-CONTRACT.md) v1.0. | [Receiving #4](https://github.com/Cube-Build-A-Thon/cube-01-receiving-manager/issues/4), [Recovery #13](https://github.com/Cube-Build-A-Thon/cube-05-recovery-manager/issues/13) |
| **F-14** | The Round 2 repos do not all carry the same rules: Receiving, Prep and Recovery share one short `RULES.md`; Pack's differs in wording; Returns has a much longer one (field names, evaluation method, mandatory LinkedIn post) that also ends mid-sentence. | Round 3 carries over the **union**; the organisers should confirm which is authoritative and finish the truncated section (presumably how Round 2 counts towards the final result). | [Returns `RULES.md`](https://github.com/Cube-Build-A-Thon/cube-04-returns-manager/blob/main/RULES.md) |
| **F-15** | The only organiser-authored list of "official evidence contract" fields is in the Returns repo (`organization_id`, `operator_label`, `images`, …) and is "concepts such as", not a schema. The sample CSVs use `org_id`, `operator_id`. | v1.0 uses a mix; see D-006 and the note at the top of [`EVIDENCE-CONTRACT.md`](../EVIDENCE-CONTRACT.md). | [Returns README](https://github.com/Cube-Build-A-Thon/cube-04-returns-manager/blob/main/README.md) |

### Raising a new finding

A contradiction between documents or data is a **finding**, not a failure. Open an issue on your Pod's repo with the `finding` label: what contradicts what, an example row, and what you assumed (and add the assumption above). Good findings are credited under *Decision quality*.

## Your Pod's decisions

_Add entries below._

### D-P01 · Pack: the model reports, rules decide, and no photo means pending
- Date / Owner: 2026-10-07 / @ANSHUL-REAL
- Context: the Round 2 Pack Manager has to fit the Round 3 contract, and the organisers' sample has no box photos and no order lines in the request.
- Options considered: (A) keep the stub's CSV replay; (B) call the model with the order and let it judge; (C) model lists what it sees, fixed rules compare with the order, and with no capture return a pending record.
- Decision: C. `agents/pack/core/` is our Round 2 engine, unchanged. A missing, altered or unreadable photo, a missing key and a model error all return `pending_review` (UNCERTAIN, with the error and the photos kept).
- Why: a model shown the order tends to confirm it; separating the two steps keeps every verdict traceable to a named check. Inventing a verdict for a box nobody photographed would turn a missing capture into evidence (D-003).
- Consequences: on the organiser sample with no photos every Pack unit ends `FAILED` / `INCOMPLETE` with `no_capture` recorded. That is correct but looks bad in a demo, so the demo needs real box photos in `data/input/<unit>/pack/`.

### D-P02 · Pack: Round 2 per-line checks roll up into the contract's three keys
- Date / Owner: 2026-10-07 / @ANSHUL-REAL
- Context: Round 2 emitted `line_present:<SKU>` and `line_quantity:<SKU>`; the contract requires `^[a-z][a-z0-9_]*$` keys and recommends `items_present`, `quantities_correct`, `no_extra_items`.
- Decision: roll up with worst-verdict-wins into the three keys (plus `image_quality`, `photo_reuse`, `scene_coverage` one to one) and keep every per-line check whole in `payload.line_checks`. The roll-up must equal the engine's own decision or the agent raises, so a contradiction can never ship.
- Why: nothing is lost, and Returns and Recovery read stable keys.
- Consequences: a consumer that wants per-line detail reads `payload.line_checks`; `payload.order_lines` and `payload.observed_in_box` give `{sku: count}` for what was ordered and what was seen.

### D-P03 · Pack: where the order comes from, and how tenancy is enforced
- Date / Owner: 2026-10-07 / @ANSHUL-REAL
- Context: the Agent Input has no order lines (F-08: `unit_id` is ambiguous across stages).
- Decision: `context.order` if it carries the subject's `org_id`, otherwise the organisers' `pack_sample.csv` looked up by (`unit_id`, `org_id`). Anything else raises `LookupError` (HTTP 404). Captures must sit under the unit's own folder and match their hash.
- Why: a unit under another organisation must never be answered. We assume `unit_id` is the order's unit; revisit when the organisers rule on F-08.

### D-P04 · Pack: a reused photo cannot seal a box
- Date / Owner: 2026-10-07 / @ANSHUL-REAL
- Decision: the same photo used for a different order makes `photo_reuse` UNCERTAIN, so the box goes to a person. The same photo for the same order is a re-check. The ledger is a JSON file keyed by (org, photo hash), written atomically; if it cannot be read or written the agent carries on without it rather than stop packing. It is local to one machine, so two servers would each see only their own history; a shared store is the fix if that matters.

### D-P05 · Pack: the model call must finish inside the stage timeout
- Date / Owner: 2026-10-07 / @ANSHUL-REAL
- Context: the orchestrator allows a stage 30 s (`flow.json` defaults). Round 2's model settings were 25 s per attempt with one retry, so a slow model could take about 52 s and be cut off, losing the capture's result.
- Decision: 2 attempts x 12 s plus the 2 s back-off, 26 s worst case, enforced by a test. Measured p95 latency in Round 2 was 10.4 s.
- Why: a model that is too slow should produce a retryable pending record that keeps the photo, not an orchestrator timeout.
- Consequences: a rare slow-but-correct answer is abandoned at 12 s and retried. Revisit if the retry rate is high.


### D-RC01 · Recovery: rebuild the Round 2 rules on the organisers' schema, do not port the engine
- Date / Owner: 2026-10-07 / @ANSHUL-REAL for @DaKaufeeBoii's stage
- Context: the Round 2 engine has its own schema, keyword matching and 8 hand-made scenarios. It never read the organisers' fee report or the pod's evidence records, and its Agent Output does not validate against our schema.
- Options considered: (A) wrap the Round 2 engine behind an adapter; (B) keep its rules, rebuild the plumbing on `fee_report_sample.csv` and the pod's records.
- Decision: B. Rule ideas kept (Prep PASS contradicts, Receiving damage makes it a conflict, F-07/F-09/F-10 silent, duplicates, prior reimbursement). Dropped: the generic "any PASS contradicts any unmatched fee" fallback, keyword matching, shipment-keyed duplicates.
- Why: run as-is on the organisers' fee report, the Round 2 engine classed 14 unrelated weight-tier fees ($53.70) as actionable duplicates, because the sample shares a shipment id across units. A wrapper would have filed them.
- Consequences: no Round 2 file is reused; PROVENANCE.md lists what was kept and why. The Round 2 "100% on 10 charges" is not carried over.

### D-RC02 · Recovery: F-07 weight-tier fees are silent unless a measurement and a sourced schedule both exist
- Date / Owner: 2026-10-07 / @ANSHUL-REAL
- Decision: all 42 sample weight-tier fees are SILENT (`no_measurements`). If an earlier record carries `payload.measurements.weight_g` the fee can be judged only against a fee schedule the operator supplies (`RECOVERY_TIER_TABLE`) that names its `source_url` and `retrieved` date, and only for the difference from the schedule's fee, not the whole line; a weight within the scale's tolerance of a tier boundary is SILENT. No schedule ships with the repository.
- Why: the fee line carries neither the billed weight nor the tier, and inventing Amazon's tiers would be a model's memory dressed as a rule. Rejected: claiming whenever a measurement exists.
- Consequences: that path is tested only against a test-only schedule. Revisit when Prep records real weights and someone supplies a real, dated schedule.

### D-RC03 · Recovery: F-08 unit_id ambiguity is handled by joining on refs and by narrow claims
- Date / Owner: 2026-10-07 / @ANSHUL-REAL
- Decision: Recovery sets `unit_scope: unit` and takes `sku`, `fnsku`, `fba_shipment_id` from the fee lines. A fee line and an earlier record that both carry a SKU, FNSKU, shipment or order id and disagree are `conflicting_evidence` (no claim, a person may resolve it). Receiving's PO-line record never supports or contradicts a unit's charge (it only blocks, see D-RC06). A line with quantity other than 1 is not contradicted by one unit's Prep record. A repeated line is a duplicate claim only with the same non-empty order id.
- Why: a bare unit id can join records about different physical things; every one of these guards costs only a missed claim.
- Consequences: change `policy.single_unit_lines_only` / `policy.duplicate_needs_order_id` when the organisers rule on F-08.

### D-RC04 · Recovery: a zero amount is never a claim, in every rule (F-09, extends D-005)
- Date / Owner: 2026-10-07 / @ANSHUL-REAL
- Decision: a line of 0.00, a negative amount or an unreadable amount is SILENT whatever the evidence says. The record keeps `underlying_position` ("CONTRADICTS") so a reviewer can see what the evidence would have said if the amount were real.
- Why: a claim for $0 is meaningless whichever way "0.00" is read (not reimbursed, or amount missing); a negative amount has an unknown sign convention.
- Consequences: all 9 zero-amount `lost_inbound` and `refund_issued_item_not_returned` lines in the sample are silent. This is not a switch.

### D-RC05 · Recovery: a supplier shortfall is shown and set aside, never used for a channel loss (F-10)
- Date / Owner: 2026-10-07 / @ANSHUL-REAL
- Decision: `lost_inbound` is always SILENT. If Receiving recorded a shortfall it is named in the reason as a supplier shortfall, listed in `basis` with role `set_aside`, and not counted for or against the charge.
- Why: it happened before the goods reached the channel, so it cannot support or contradict a channel-side loss. Nothing in this pod comes from the channel's side of the dock.
- Consequences: Recovery can never file a `lost_inbound` claim until a channel-side record exists.

### D-RC06 · Recovery: Prep PASS contradicts an inbound-defect fee only if Receiving saw no defect; a conflict asks a person
- Date / Owner: 2026-10-07 / @ANSHUL-REAL
- Decision: Prep PASS (effective, completed, at least one check, captured before the charge, same refs) contradicts the fee. If Receiving recorded damage or a quality flag on the unit, that is `conflicting_evidence`: no claim, outcome `pending_review`, `needs_human: true` (only when the line's amount is above zero). If Receiving was UNCERTAIN or did not complete on those checks, SILENT with `needs_human: false`. A claim in the same record still goes out; the conflict is flagged beside it.
- Why: the Round 2 author's own scenario 6. A defect recorded at receipt can be the real cause of a channel's inbound-defect charge. A person can resolve it by overriding either record.
- Consequences: in the sample 3 of the 9 inbound-defect fees are held back by this and ask for review (`BLOCKED` / `NEEDS_REVIEW`). That is deliberate and visible. Switch: `policy.receiving_defect_blocks_claim`. How many real claims it costs is not measured.

### D-RC07 · Recovery: no Prep evidence means silent, in either flow, and Pack does not stand in
- Date / Owner: 2026-10-07 / @ANSHUL-REAL
- Decision: with no usable Prep record an inbound-defect charge is SILENT (`no_prep_evidence`). The reason says whether this pod's flow has no Prep stage (Specialist flow, read from `pod.json` or `ORCH_FLOW`) or Prep did not run for this unit. Pack evidence is read and cited but not used for these charges.
- Why: the Specialist flow has no Prep Manager; guessing from Pack or Receiving would invent evidence. The replay shows the effect on the organiser sample: 4 claims ($6.00) in the standard flow, 0 in the Specialist flow.
- Consequences: a Specialist Pod's Recovery can only claim duplicates and, on merchant-fulfilled units, refunds without return.

### D-RC08 · Recovery: F-11 and F-12, a seller-side Returns record does not contradict a channel refund on an FBA or route-unknown unit
- Date / Owner: 2026-10-07 / @ANSHUL-REAL
- Decision: `refund_issued_item_not_returned` is contradicted by a Returns `identity_match` PASS only when the unit's route is `mfn`. On `fba` (F-11) and `unknown` (F-12) the charge is SILENT with the Returns record cited as consulted.
- Why: it is unsettled whether an FBA return reaches the seller, and a unit with neither Prep nor Pack has no known route. The organiser stub contradicted on any route.
- Consequences: all 4 such lines in the sample are silent (they are also 0.00). One switch: `policy.fba_returns_can_contradict`; tested both ways.

### D-RC09 · Recovery: overrides are read, never rewritten, and the latest one wins
- Date / Owner: 2026-10-07 / @ANSHUL-REAL
- Decision: each earlier record's verdict is `effective_verdict` (latest workflow override). The original and the effective verdict are both recorded in `payload.upstream` and in each charge's `basis`, and the reason text names who overrode what. An override of Receiving counts the same way (it can create or clear the defect conflict). The previous record is never edited.
- Why: EVIDENCE-CONTRACT section 6; the Round 2 code patched the upstream checks in place.
- Consequences: Prep PASS -> FAIL flips a contradicted charge to supported and withdraws the claim (tested).

### D-RC10 · Recovery: what PASS and UNCERTAIN mean for lines that are not disputed charges
- Date / Owner: 2026-10-07 / @ANSHUL-REAL
- Decision: a reimbursement credit (`report_type: reimbursement_report`) and a charge already credited are PASS ("supported or settled": nothing to claim), with assessment `CREDIT` / `ALREADY_REIMBURSED`. A unit with no fee lines is `UNCERTAIN` / `no_claim` (the contract's empty-checklist default), not PASS.
- Why: a PASS line must never produce a claim; but "no fee lines" is not evidence that the unit was charged fairly (reports lag).
- Consequences: 56 of 100 sample units have no fee lines and are UNCERTAIN in Recovery; with `needs_human: false` they do not block anything. Open for the owner: a reviewer may prefer PASS.

### D-RC11 · Recovery: the same request gives the identical record, hash included
- Date / Owner: 2026-10-07 / @ANSHUL-REAL
- Decision: `record_id = RCY-<request_id>`, and `produced_at` and `latency_ms` are measured once per (request id, inputs) and reused. A re-run of the stage (new request id) gets a new record.
- Why: the store refuses a second record with the same id and a different hash, so a retry after a timeout would otherwise be rejected.
- Consequences: the pin is process memory; after a restart the same request would get a new timestamp.

### D-RC12 · Recovery: open, a late override leaves a stale claim (candidate finding, not fixed here)
- Date / Owner: 2026-10-07 / @ANSHUL-REAL
- Context: `apply_override` re-derives status and outcome from stored records but never re-runs Recovery, and `resume` skips completed stages. Overriding Prep PASS to FAIL on UNIT-0014 after the claim was filed leaves `CLAIM_RECOMMENDED $2.00`, whose basis is the overridden record.
- Decision: no change to `orchestration/` (shared). Recovery honours any override present when it runs; the README states the gap.
- Consequences: needs a pod decision: re-run downstream stages of an overridden record, or mark a claim stale when its basis record is overridden.
