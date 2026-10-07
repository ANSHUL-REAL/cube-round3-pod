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

### D-PR01 · Prep: the reference is ported to Python, and the model only observes
- Date / Owner: 2026-10-07 / @Devisri-074
- Context: the organisers' reference Prep Manager (Manvith111, TypeScript on Next.js and Cloudflare) has no Python `handle()`, no tenancy, random record ids and unsourced rules. The pod needs a Prep stage that speaks the Round 3 contract.
- Options considered: (A) keep the Next.js app as an HTTP service behind the contract; (B) port the decision core to Python and drop the product around it; (C) rewrite from scratch.
- Decision: B. One batched vision call per unit reports what is visible (met / not_met / cant_tell, a confidence band, the photo, the evidence, a transcription of the label); fixed rules decide. The model is not told the expected FNSKU or the work order.
- Why: A would carry a second runtime, the unauthenticated key routes and the Cloudflare config into the pod for no gain. A model that knows the answer tends to confirm it, so the comparison is made by rules.
- Consequences: no UI from the reference (the pod's UI is separate). The vision model has not been run live (no key was available); every claim about real-model behaviour is open.

### D-PR02 · Prep: the reference's eleven checks roll up into the contract's keys; one key is added
- Date / Owner: 2026-10-07 / @Devisri-074
- Context: the reference has eleven check ids and the verdicts NOT_APPLICABLE and NOT_VERIFIABLE; the contract has six recommended keys and PASS / FAIL / UNCERTAIN only.
- Decision: worst verdict wins within a group (polybag present+sealed -> `polybag_sealed`; warning present+legible -> `suffocation_warning`; label present+placement -> `fnsku_label_placement`), the eleven stay whole in `payload.rule_checks`, inapplicable checks are omitted, bag thickness is listed in `payload.not_verified` and is never a check. The FNSKU text comparison is kept as a seventh key, `fnsku_text_match` (the contract allows new keys, section 11).
- Why: folding "the label reads the wrong code" into `fnsku_label_placement` would report the wrong reason for a failure. A consumer that only knows the six keys can ignore the seventh; the decision still rolls it up.
- Consequences: the owner may prefer to fold it into placement; the cost is one constant (`requirements.EXTRA_KEYS`) and one line in `rules.py`.

### D-PR03 · Prep: rule sources are unverified, and we say so instead of making every verdict UNCERTAIN
- Date / Owner: 2026-10-07 / @Devisri-074
- Context: the starter says to look up Amazon's published prep requirements and not infer them. The reference quoted clause numbers ("§2.1 ...") that it admits only model "the standard marketplace rules". This work could not browse, so no rule text was retrieved or checked.
- Options considered: (A) make every check UNCERTAIN with `rule_unavailable`; (B) keep the reference's clauses; (C) use our own plain wording, record the source as unverified everywhere, and let the work order decide which requirements apply.
- Decision: C. `payload.rule_source` is `{"status": "unverified", "url": null, "retrieved_at": null}`; every check detail ends `[demo rule, source unverified]`; the reference's clause numbers, quotes and thresholds are not used.
- Why: A would make the stage unable to say anything, although the requirement it checks is the work order's own flags (a bag, a warning, an expiry, a mark) and the observations are physical facts. B is fabrication. What is unverified is the link to Amazon's text, and that is stated rather than hidden.
- Consequences: nobody may present these checks as "Amazon's rules". Revisit when someone retrieves the rules: record the URL and date in `requirements.RULE_SOURCE`, and drop the tag.

### D-PR04 · Prep: no photo, a bad capture or a model failure is a pending record
- Date / Owner: 2026-10-07 / @Devisri-074
- Decision: no photo (`no_capture`, retryable), an altered, foreign or unreadable photo (`capture_unreadable`, not retryable), no key (`model_not_configured`), a model error or timeout (`model_unavailable`) and an unusable model answer (`model_output_invalid`) all return a `pending` record: no checks, UNCERTAIN, the error, the photos kept, `model.calls` as made. The model call is bounded to 2 x 12 s + 2 s so it ends inside the 30 s stage timeout.
- Why: the reference saved a model failure as eleven UNCERTAIN-looking checks, which reads as a normal judged result. A missing capture must not become evidence (D-003).
- Consequences: on the organiser sample with no photos every FBA unit ends `FAILED` / `INCOMPLETE` with `no_capture` recorded. That is correct but looks bad in a demo, so the demo needs real photos in `data/input/<unit>/prep/`.

### D-PR05 · Prep: where the work order comes from, tenancy, and what `captured_at` means
- Date / Owner: 2026-10-07 / @Devisri-074
- Decision: `context.order` if it carries the subject's `org_id`, otherwise the organisers' `prep_sample.csv` looked up by (`unit_id`, `org_id`). A request for another organisation's unit, a work order for another organisation or unit, or a route other than `fba` raises `LookupError` (404). `captured_at` is the time the caller states, otherwise the photo files' modification time; the sample CSV's timestamp belongs to photos that do not exist here and is not used.
- Why: the Agent Input carries no work order (F-08: `unit_id` is ambiguous across stages); we assume it is the FBA unit's id. File time is weak proof of when a photo was taken, so `payload.captured_at_source` says which one was used.

### D-PR06 · Prep: Receiving is recorded not used; confidence is a band; measurements stay empty (F-07)
- Date / Owner: 2026-10-07 / @Devisri-074
- Decision: Receiving's record is cited in `upstream_refs` and `payload.upstream.receiving` with the latest override applied, and a non-PASS effective verdict is stated in `decision.reason`, but no prep check depends on it. Check confidence is the model's own band mapped to 0.9 / 0.6 / 0.3 and labelled as not calibrated; UNCERTAIN checks carry none. `payload.measurements` is `{}` with a note: weight cannot be read from a photo and the photos have no scale.
- Why: what arrived and how it was prepped are different questions; letting a Receiving flag change a prep verdict would be an invented rule. On F-07: Prep is the natural source of weight, but only a scale or a measuring station can supply it. Estimating it from a photo would be a made-up number that Recovery might claim on.
- Consequences: weight-tier fee lines stay SILENT. To close F-07, add a scale reading to `context.order` or a weighing step and fill `payload.measurements` from it.

### D-PR07 · Prep: organiser tests pinned to the Prep stub are skipped once Prep is real
- Date / Owner: 2026-10-07 / @Devisri-074
- Context: six organiser tests assume the stub's outcomes (no photos needed, UNIT-0014 compliant): the example outcomes, one claim test, the HTTP full-workflow test and the Recovery override contract test.
- Decision: skip them while Prep is not an `organiser-stub` (`tests/helpers.py: needs_stubs`, the same pattern as the golden-outcome test) and cover the same behaviour in `tests/integration/test_prep_agent.py` with our own fixtures, including an override of our Prep record changing Recovery's answer.
- Consequences: the other four members will hit the same thing when their agents replace stubs; the pod should agree one approach. This touches shared test files, so it is its own commit and easy to drop.

