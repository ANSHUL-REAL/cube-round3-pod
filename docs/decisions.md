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


### D-V01 · Receiving: the model reads the delivery and is never shown the purchase order
- Date / Owner: 2026-10-07 / @cherryy-x23 (Round 2 author), ported by @ANSHUL-REAL
- Context: Round 2's prompt listed the expected SKU, colour, variant, components and totals "for context", and its live path read server-side file paths, so no uploaded photo ever reached the model.
- Options considered: A) keep the order in the prompt; B) hide the order and compare in code; C) two calls (read, then judge).
- Decision: B. One batched call per delivery carries the photos as bytes and asks only what is visible (identifiers, counts, damage, colour, variant, parts, clarity). Rules in `rules.py` compare that with the PO line.
- Why: a model told what to expect tends to confirm it, which is the failure that hurts a supplier claim most. A test asserts no PO value reaches the request.
- Consequences: the model cannot use the order to disambiguate a poor label, so more cases end UNCERTAIN. Not measured: no real-model run exists yet.

### D-V02 · Receiving: how the checks become an outcome
- Date / Owner: 2026-10-07 / @ANSHUL-REAL (owner to confirm)
- Context: the contract names four outcomes but not when each applies, and no source says what is acceptable at receipt.
- Decision: all PASS is `accept`; any FAIL is `accept_with_exceptions`, except a FAIL on `identity_match` (wrong goods), which is `reject`; no FAIL and any UNCERTAIN is `pending_review` with `needs_human`; a FAIL with an unresolved check keeps the FAIL and also sets `needs_human`.
- Why: short, over, damaged or off-spec stock is usable and its evidence is what a supplier dispute needs; wrong goods are not. The organiser stub also maps FAIL to `accept_with_exceptions`.
- Consequences: every damage type counts as a FAIL; there is no severity scale, because we have no rule to cite for one (we did not invent one). Revisit if the owner or the organisers define reject thresholds.

### D-V03 · Receiving: unclear photos and unsure readings can only produce UNCERTAIN
- Date / Owner: 2026-10-07 / @ANSHUL-REAL
- Decision: if the model's `image_clarity` is under 0.5, or its self-reported confidence for a group of readings is under 0.6, every check built on those readings is UNCERTAIN (`poor_image` or `insufficient_evidence`). Conflicting evidence (identity signals disagreeing; a direct unit count disagreeing with cartons x units) is UNCERTAIN `conflicting_evidence`. Check `confidence` is null.
- Why: Round 2 extracted clarity and ignored it, and hard-coded `confidence: 1.0` on identity. A count read off a blur must not become a supplier claim, and the model's self-reported confidence is not a calibrated probability, so we do not publish it as the check's confidence.
- Consequences: 0.5 and 0.6 are untuned guesses (README, Limits). They cost a human look when wrong; they cannot cause a wrong PASS or FAIL.

### D-V04 · Receiving: where the PO line comes from, and how F-08 and F-10 are handled
- Date / Owner: 2026-10-07 / @ANSHUL-REAL
- Decision: `context.order` if it carries the subject's `org_id`, otherwise the organisers' `receiving_sample.csv` by (`unit_id`, `org_id`), reading only the ordered and spec columns. Anything else raises `LookupError` (404). `subject.unit_scope` is `po_line` (F-08) and the PO keys go in `subject.refs`. A shortfall is recorded with `payload.shortfall_scope = "supplier_inbound"` (F-10) so Recovery cannot mistake it for channel loss.
- Why: the received, damage and identity columns are the answers the agent is meant to find from photos. Reading them would make the agent a stub. We assume `unit_id` is the PO line; revisit when the organisers rule on F-08.
- Consequences: `captured_at` is the sample row's value (or `context.order.captured_at`), not a photo timestamp; the README says so.

### D-V05 · The organiser's orchestration tests run Receiving on the organiser stub
- Date / Owner: 2026-10-07 / @ANSHUL-REAL
- Context: Receiving runs in every workflow. A real Receiving with no photos and no key answers `pending`, so eight organiser tests (workflow state, examples, HTTP, contract) that were written for a stage that always answers started failing. They test the plumbing, not Receiving.
- Options considered: A) leave them failing; B) a CSV-replay mode inside the real agent; C) keep the organiser stub as a test fixture and route those tests to it.
- Decision: C. `tests/stubs/receiving_stub.py` is the organiser's stub verbatim; `tests/conftest.py` (`STUB_STAGES`) routes plumbing tests to it, and a module with `REAL_AGENTS = {"receiving"}` tests the real agent. `test_agent_level_override_is_append_only` now picks a record that has checks. `agent.json` stays honest: it describes the real agent.
- Why: B would put a stub inside the product, which the contract forbids ("do not call a stub an agent"). A hides real regressions behind known failures.
- Consequences: every stage that becomes real will need the same line in `STUB_STAGES`. Receiving's real behaviour in a workflow is covered by `test_receiving_agent.py` (scripted model).

### D-V06 · Receiving: model time budget and fail-open codes
- Date / Owner: 2026-10-07 / @ANSHUL-REAL
- Decision: 2 attempts x 12 s plus the 2 s back-off (26 s worst case, enforced by a test) inside the orchestrator's 30 s stage timeout; retry only on 429/5xx and connection errors, never on a malformed answer. `model.calls` counts requests actually sent. Missing capture, altered capture, no key, model failure and bad PO data each return a pending record with a distinct `error.code`, the photos kept, and no invented checks.
- Why: a slow model should produce a retryable pending record that keeps the capture, not an orchestrator timeout that loses it. Same reasoning as D-P05.
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

### D-RT01 · Returns: the model observes, rules decide, and only the engine and its data are carried over
- Date / Owner: 2026-10-07 / @krishnababuprodduturu (Round 2 author), integrated by @ANSHUL-REAL
- Context: the Round 2 Returns Manager (RTN-0038) is a full product: web console, Postgres with row-level security, job queue, hash-chained ledger, tool-calling Gemini sessions. Round 3 needs one agent behind `handle()`.
- Options considered: (A) keep the stub's CSV replay; (B) run the Round 2 service and call it over HTTP; (C) copy the decision core and its reference data, and let the pod's orchestrator, store and contract replace the rest.
- Decision: C. `agents/returns/core/` holds the Round 2 validation, identity fusion, completeness, condition and disposition engine with only import lines changed (checked file by file, see PROVENANCE.md). The model fills a schema with no disposition field; code decides.
- Why: a disposition that a model could change by what it says in a photo is not auditable; Round 2's separation keeps every outcome traceable to a named rule (`payload.disposition.rule_id`, `inputs_sha256`). The pod already has an orchestrator, an evidence store and tenancy, so Round 2's copies of those are redundant.
- Consequences: Round 2's database-backed checks (photo reuse across returns, near-duplicate photos), its queue and its console are not here. Its evaluation numbers describe the Round 2 path, not this one.

### D-RT02 · Returns: one model call, no tools, bounded to fit the stage timeout
- Date / Owner: 2026-10-07 / @ANSHUL-REAL
- Context: Round 2 ran a stateful Interactions API session with a crop tool and up to 2 round trips, a 180 s timeout and a 45 s p95 target. The orchestrator gives a stage 30 s, and engineering rule 2 says one batched call per unit.
- Options considered: (A) keep the session loop and raise the stage timeout; (B) one `generateContent` call, no tools, no repair turn, 24 s, and a pending record on any failure.
- Decision: B. The system prompt is used unchanged (it is hash-locked and mentions tools); the user text says no tools are available. Thinking defaults to `low` (Round 2: `medium`). Output mode defaults to JSON in the prompt (`RETURNS_OUTPUT_MODE=json_prompted`), with constrained output available as `json_schema`.
- Why: a slow answer that is cut off by the orchestrator loses the capture's result; a retryable pending record keeps it.
- Consequences: the crop tool is gone, so small print and part identification rely on the photos as given. None of this is measured through the pod: no live run was made, the model id is Round 2's default and unchecked, and `low` thinking may grade worse than Round 2's `medium`. Revisit after a live run; raising `timeout_s` for this step in `flow.json` would allow `medium`.

### D-RT03 · Returns: the condition scale is Amazon's published one, from an unverified substitute source
- Date / Owner: 2026-10-07 / @krishnababuprodduturu (source choice), @ANSHUL-REAL (recording it)
- Context: the contract says to look the scale up, not invent it. Round 2 could not retrieve amazon.in's guidelines (login) and used the public Amazon UK Condition Guidelines PDF (16 Dec 2020) for amazon.in.
- Decision: keep Round 2's rubric snapshots as they are, stamped `unverified_substitute`, and write `payload.rule_source` (snapshot id and hash, URL, retrieval date, document SHA-256 as recorded in Round 2) into every record. A unit that cannot be graded from the photos is UNCERTAIN (`insufficient_evidence`) with `amazon_condition: null`, never given a grade by default. A missing or hash-failing rubric means no judgment (`no_product_reference`).
- Why: grading against a scale we could not check, silently, would be a model "remembering" a rule; labelling it keeps the record honest and the data easy to swap (`reference/rubrics/active.yaml`).
- Consequences: this pod did not retrieve the PDF or re-check its hash. The owner should confirm the marketplace and replace the snapshots if the organisers publish theirs.

### D-RT04 · Returns: how Round 2's checks map onto the contract's keys
- Date / Owner: 2026-10-07 / @ANSHUL-REAL
- Context: Round 2 emitted `identity`, `completeness`, `component:<id>`, `condition_grade`, `relistable_as_is`, `category_policy`, `unit_presence`, `photo_quality`; the contract recommends `identity_match`, `completeness`, `condition` and requires snake_case keys.
- Decision: `identity_match` (first, because the organiser Recovery stub reads `checks[0]`), `completeness`, `condition`, plus `unit_presence` and `photo_quality`. Per-part results stay whole in `payload.components`. `condition` is PASS when graded and nothing physically unacceptable was seen, FAIL for severe damage, dirt, a used consumable or an opened item in a New-only category, UNCERTAIN when ungradable; the function is never tested. `photo_quality` is never FAIL (nobody can retake the photo): fewer than 2 usable photos is UNCERTAIN and the unit is a pending review.
- Why: Round 2's `relistable_as_is` and `category_policy` are consequences of the same facts, so they live in the disposition block rather than as extra checks that would double-count a FAIL.
- Consequences: a consumer that wants Round 2's exact checks reads `payload`. A missing non-essential part makes `completeness` FAIL (as in Round 2) even when the engine would still route the item.

### D-RT05 · Returns: a route is reported only when nothing asks for a person
- Date / Owner: 2026-10-07 / @ANSHUL-REAL
- Context: Round 2 treats "needs review" as a flag next to a route (review is "never a fifth disposition"); the contract has `pending_review` as an outcome and `needs_human` as an explicit request.
- Decision: the outcome is the engine's route only if the verdict is not UNCERTAIN, the engine and the escalation rules ask for no review, and the earlier records do not conflict. Otherwise the outcome is `pending_review`, a PASS is lifted to UNCERTAIN, and the engine's route stays in `payload.disposition.engine_recommendation`. `dispose` and high-value routes (Round 2 sign-off rules S01, S02) set `needs_human`. `_assert_consistent` raises if a route is reported with identity or presence not PASS, or `restock` with any check not PASS.
- Why: handing a warehouse a "restock" that a rule also flagged for review is how a wrong item re-enters stock.
- Consequences: the pod's workflow is BLOCKED more often than Round 2's queue would be; that is the cost of not deciding for a person.

### D-RT06 · Returns: what Pack and Receiving change, and what they do not (finding F-08)
- Date / Owner: 2026-10-07 / @ANSHUL-REAL
- Context: a return is only meaningful against what was sold and sent. `unit_id` means different things in different stages (F-08).
- Decision: Pack's order id, order lines and box count are compared with the returned order and SKU. A different order, a SKU not on the order, or (Pack FAIL) a SKU Pack counted none of, make `identity_match` UNCERTAIN (`conflicting_evidence`) and the unit a pending review. Everything else from Pack and Receiving (flags, box mismatch, supplier-side findings) is context in the check detail and `evidence_refs`; no verdict changes. The latest workflow override of each record is its effective verdict. A missing or pending upstream record gives no conflict.
- Why: two records that disagree about scope are UNCERTAIN under the contract, and a guess in either direction could produce a wrong claim. Receiving's flags say what the supplier shipped, not who damaged the item, so they inform but do not decide.
- Consequences: Pack counts are a model's counts, so a `not_packed` conflict asks a person and does not assert the item was never sent. Pack counts per SKU cannot speak to missing parts of a unit.

### D-RT07 · Returns: no verified product reference, no judgment (and what that means for the sample)
- Date / Owner: 2026-10-07 / @ANSHUL-REAL
- Context: Round 2's gate (section 11.2a) refuses to call the model when the card, parts list, reference image, category entry, rubric or policy is missing. The organisers' ten sample SKUs have placeholder cards (invented features and values) and no reference images.
- Decision: keep the gate. The record is `pending_review` with `no_product_reference`, `status: error`, not retryable, naming what is missing, with the captures kept; the model is not called. Every reference document is hash-checked, and cards are read only under the caller's organisation.
- Why: judging a real photo against an invented description of the product would manufacture evidence.
- Consequences: on the organisers' sample, Returns ends in an error record until a product is onboarded (card with reference images, hashes updated) and photos are taken. A card with one critical body feature can never reach identity `yes` without a barcode. Open for the owner: relax the gate to judge completeness and condition without reference images (identity then stays UNCERTAIN)?

### D-RT08 · Returns: FBA-routed returns are judged like any other (finding F-11)
- Date / Owner: 2026-10-07 / @ANSHUL-REAL
- Context: whether FBA returns come back to the seller or the channel's warehouse decides whether Returns evidence can contradict `refund_issued_item_not_returned`.
- Decision: Returns judges whatever photos it is given, records `payload.route`, and reports `claim_signals` (`item_not_returned`, `wrong_item_returned`, `returned_damaged`) as `yes`/`no`/`uncertain` observations with their basis. It does not decide who received the return or whether a charge is contradicted.
- Why: that is Recovery's call, and the organisers have not ruled. If photos exist the item was at least seen by the seller.
- Consequences: nothing here blocks Recovery from using or ignoring these signals.

### D-RT09 · Returns: the starter's stub-only tests skip once a stub is replaced
- Date / Owner: 2026-10-07 / @ANSHUL-REAL
- Context: `test_examples.py::test_example_cases_still_produce_the_documented_outcome` and `test_http.py::test_full_workflow_over_http_matches_in_process` replay the stock stubs and expect every stage to complete. The starter already skips its golden-outcome test under the same condition ("not the stock stubs").
- Decision: the same skip (`implementation != "organiser-stub"`) on those two tests. No assertion was changed or removed, and the real behaviour is covered by `tests/integration/test_returns_agent.py`.
- Consequences: other members replacing stubs will make the same edit; the hunks are identical, so the merge should be trivial. Pod-level: decide whether to rewrite those tests against fixtures.
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

### D-O01 · Orchestrator: ids are file names, verdicts are a closed set, a misbehaving agent is a recorded failure
- Date / Owner: 2026-10-08 / @ANSHUL-REAL
- Context: a review of the orchestrator, reproduced before fixing, found: `GET /workflows/..%5C..%5Csecret` returned a JSON file outside the store on Windows (the API has no auth); an override with any verdict ("banana") was accepted and stored; an agent that returned nothing, a missing agent folder, a non-text unit id and a null actor all gave a 500 and could leave a workflow stuck; a `KeyError` inside an agent was reported as a refusal or a 404; in-process agents ignored `timeout_s`.
- Decision: workflow and record ids must match `^[A-Za-z0-9][A-Za-z0-9._-]*$` with no `..` (store, API, `discover_inputs`); `new_verdict` must be PASS, FAIL or UNCERTAIN and actor and reason must be text; every one of those failures becomes a recorded stage error (`agent_invalid_output`, or a stage that "could not be started") instead of an exception; `KeyError` and `IndexError` are agent bugs (`agent_exception`), only other `LookupError`s are refusals; in-process agents run in a daemon thread and time out like HTTP ones; the run counter is saved before the agent is called, so a crash replays under a new request id instead of raising `EvidenceConflict`.
- Why: the rubric scores reliability and tenancy; a failure must be recorded and visible, never a crash or a silent success.
- Consequences: a timed-out in-process agent keeps running until it returns (a Python thread cannot be killed) and its answer is discarded. Not fixed here: `put_evidence` still raises `EvidenceConflict` if the same request id is replayed with different content (see the open items in the build log).

### D-O02 · Tenancy: captures stay in the unit's folder, evidence stays with its subject, workflow ids are checked
- Date / Owner: 2026-10-08 / @ANSHUL-REAL
- Context: a security review, reproduced before fixing, found: (1) a capture ref like `UNIT-1/pack/../../UNIT-2/pack/x.jpg` passed all four photo loaders (the root check used the resolved path, the unit check only the raw text), so a direct caller could make an agent read another unit's photo; (2) Receiving, Prep, Pack and Returns used the latest earlier record without checking whose it was (only Recovery refused foreign evidence); (3) the workflow id `WF-<org>-<unit>` is not unique (`org "a-b" + unit "c"` and `org "a" + unit "b-c"` collide), so one organisation could be handed another's stored workflow; (4) Pack used `org_id` as a catalogue folder name unchecked; (5) a bad order or work order crashed the agent.
- Decision: one shared rule (`shared/utils/captures.py`) resolves the ref and requires it to lie inside `<root>/<unit>/<stage>/`, refusing `..`, absolute paths, drive letters and backslashes; every agent calls `require_same_subject()` first and refuses (404) a request carrying a record about another org or unit; `run_workflow` refuses to reuse a stored workflow whose (org, subject) differs (API: 409) because the id format is fixed by the organisers' tests; Pack requires plain-name org and unit ids; a bad order or work order becomes a pending `order_invalid` record.
- Why: the rubric asks for tenancy refused at every agent and at the storage layer, and a bad input must be recorded, not crash.
- Consequences / NOT fixed: **there is still no authentication.** The org is whatever the caller says it is, so these checks make agents consistent about the org they were told, not safe against a malicious caller. Any public deployment needs authentication in front of the orchestrator and agents. Evidence content hashes are unkeyed: a record can be forged by anyone who can write to the store, and only Recovery re-verifies them on read.
