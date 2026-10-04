# Evidence Contract (v1.0)

Every agent, whatever it does inside, speaks this one contract. It is **strict on what must be true** and **silent on how you make it true**.

> **Status: v1.0, drafted for Round 3.** Round 2 told every pod to build against "the official evidence contract provided by the organisers", and none was published. This is that contract. It keeps what the Round 2 rules already required (per-check verdicts, confidence, model and version, timestamps, overrides, a content hash). Schemas are the source of truth: [`shared/schemas/`](shared/schemas/). Worked examples (validated by the tests): [`examples/`](examples/). If this page and a schema disagree, the schema wins; please open an issue.
>
> **Field names.** The only organiser-authored field list in the Round 2 repos (the Returns README) uses `organization_id`, `operator_label`, `images`, `outcome`, `content_hash`, `check_key`, `detail`, `model_version`, `latency_ms`, `client_id`. v1.0 uses those names where the sample CSVs do not already name the thing: `check_key`, `detail`, `content_hash`, `latency_ms`, `client_id`. Where the CSVs already use a name, v1.0 follows the data: `org_id` (= `organization_id`), `operator_id` (= `operator_label`), `inputs` (= `images`, because not every input is an image), `model.version` (= `model_version`). Whether to rename these is an open organiser decision (finding F-15 in [`docs/decisions.md`](docs/decisions.md)).

## 1. Two objects, one flow

```text
 Agent Input ──▶  [ your agent ]  ──▶  Agent Output { …, evidence: Evidence Record }
 (what to judge,                       (the result + the stored evidence)
  previous evidence)                              │
                                                  ▼
                              Orchestrator records the evidence and decides the state transition
```

| Object | Who produces it | Schema | Purpose |
|---|---|---|---|
| **Agent Input** | Orchestrator | [`agent-input`](shared/schemas/agent-input.schema.json) | Workflow id, subject, current stage, this stage's captures, **all previous evidence**, overrides, context |
| **Agent Output** | Agent | [`agent-output`](shared/schemas/agent-output.schema.json) | Agent id, stage, status, verdict, confidence, timestamp, model/version, error, next-step recommendation, **the evidence** |
| **Evidence Record** | Agent (inside the output) | [`evidence`](shared/schemas/evidence.schema.json) | The immutable, check-by-check record of what was found and decided |
| Workflow State | **Orchestrator only** | [`workflow-state`](shared/schemas/workflow-state.schema.json) | The authoritative state of the workflow (see [`ORCHESTRATION-GUIDE.md`](ORCHESTRATION-GUIDE.md)) |
| Final Outcome | **Orchestrator only** | [`final-outcome`](shared/schemas/final-outcome.schema.json) | The end-to-end decision |
| Error | Agent or orchestrator | [`error`](shared/schemas/error.schema.json) | A failure, recorded instead of hidden |

An Agent Output **informs** a state transition. It never sets workflow state: `next_step_recommendation` is advice, and the orchestrator decides.

## 2. What every evidence record answers

```text
Who?                     agent_id                      (+ stage)
What?                    checks[].check_key, checks[].verdict, decision.outcome
For what?                workflow_id, subject.subject_id (the unit/order), subject.org_id
When?                    captured_at (the physical event), produced_at (this record)
How confident?           checks[].confidence, decision.confidence
Using what?              model {name, version, prompt_version}, inputs[] (with sha256), upstream_refs[]
What supports it?        checks[].evidence_refs[]  ->  inputs[].ref / upstream record_ids
What was decided?        decision {verdict: PASS | FAIL | UNCERTAIN, outcome, reason, needs_human}
Was anything overridden? overrides[] (agent level)  and  Workflow State overrides[] (workflow level)
```

## 3. The Evidence Record

```jsonc
{
  "schema_version": "1.0",
  "record_id": "PRP-0014",                 // stage prefix + id: RCV | PRP | PCK | RTN | RCY. Same request in, same record_id out.
  "workflow_id": "WF-org_demo_alpha-UNIT-0014",
  "stage": "prep",                         // receiving | prep | pack | returns | recovery
  "agent_id": "prep-manager@1.4.0",        // who produced it
  "subject": { "org_id": "org_demo_alpha", "subject_id": "UNIT-0014", "unit_id": "UNIT-0014",
               "unit_scope": "unit", "refs": { "work_order_id": "WO-3002", "fba_shipment_id": "…", "sku": "…", "fnsku": "…" } },
  "status": "completed",                   // completed | pending | error
  "captured_at": "2026-06-04T12:25:00Z", "produced_at": "2026-10-04T11:20:31Z", "latency_ms": 1840,
  "operator_id": "op_amira",
  "model": { "name": "your-model", "version": "2026-09-30", "provider": "…", "prompt_version": "v3", "calls": 1, "cost_usd": 0.004 },
  "inputs": [ { "ref": "UNIT-0014/prep/front.jpg", "sha256": "…", "kind": "image" } ],
  "checks": [ { "check_key": "fnsku_label_placement", "verdict": "FAIL", "confidence": 0.84,
                "expected": "flat", "observed": "on_seam", "detail": "Label crosses the bottom seam.",
                "evidence_refs": ["UNIT-0014/prep/label.jpg"] } ],
  "decision": { "verdict": "FAIL", "outcome": "non_compliant", "confidence": 0.88, "reason": "…", "needs_human": false },
  "payload": { },                          // agent-specific (section 8)
  "upstream_refs": ["RCV-0014"],           // record_ids you consumed
  "overrides": [],                         // optional agent-level overrides (section 6)
  "error": null,
  "content_hash": "…64 hex…"
}
```

**Required:** `schema_version`, `record_id`, `workflow_id`, `stage`, `agent_id`, `subject` (`org_id`, `subject_id`), `status`, `captured_at`, `produced_at`, `model` (`name`, `version`), `checks`, `decision`, `upstream_refs`, `content_hash`.

Rules: **Same request in, same `record_id` out** (idempotent). Never overwrite `captured_at` with "now". If no model ran, say what did: `"rules"`, `"csv-replay-stub"`, `"human"`. Do not call a rules engine a model, and do not call a stub an agent.

## 4. Checks: verdicts and confidence

```jsonc
{ "check_key": "fnsku_label_placement",  // snake_case, stable, listed in section 8
  "verdict": "FAIL",                     // PASS | FAIL | UNCERTAIN
  "confidence": 0.84,                    // 0..1 in THIS verdict, or null for deterministic checks. Not a guess dressed as a number.
  "expected": "flat", "observed": "on_seam", "detail": "…",
  "evidence_refs": ["…"],                // which inputs[].ref or previous record_ids support it
  "uncertain_reason": null }             // REQUIRED when UNCERTAIN: poor_image | occluded | insufficient_evidence | model_error | rule_unavailable | conflicting_evidence | other
```

| Verdict | Meaning | Not |
|---|---|---|
| **PASS** | The evidence supports the condition. | "Nothing looked wrong." |
| **FAIL** | The evidence shows the condition is **not** met. | A low-confidence PASS. |
| **UNCERTAIN** | The evidence is insufficient for a reliable judgment. | **A low-confidence PASS.** It is a first-class outcome. |

A check that does not apply (`not_required`) is **omitted**, not marked PASS.

## 5. Outcome states (fixed meanings: do not invent others)

**Verdicts** (agents and the orchestrator): `PASS`, `FAIL`, `UNCERTAIN`.

**`decision.verdict`** is the roll-up of the checks. Default (`shared/utils/records.py: rollup`): any FAIL → FAIL; else any UNCERTAIN → UNCERTAIN; else PASS; an empty check list is UNCERTAIN.

**`decision.outcome`** is the agent's own recommendation code (section 8). **`needs_human`** is the agent's explicit request for a person: it defaults to true when UNCERTAIN, and an agent may set it false only when there is nothing for a person to decide (Recovery's SILENT charges).

**`status`** (evidence and agent output): did the agent manage to judge?

| `status` | Meaning | Required shape |
|---|---|---|
| `completed` | Judged. | Normal. |
| `pending` | Saved but not judged: model timeout, rate limit, operator must retry or review. | `verdict: UNCERTAIN`, `outcome: pending_review`, `error` set, `needs_human: true`. |
| `error` | Could not produce a judgment; a record exists anyway. | Same as `pending`, `retryable: false`. |

**Workflow statuses** (set **only by the orchestrator**): `PENDING`, `IN_PROGRESS`, `COMPLETED`, `FAILED`, `BLOCKED`, `RECOVERY_REQUIRED`. Exact definitions and the precedence between them are in [`ORCHESTRATION-GUIDE.md`](ORCHESTRATION-GUIDE.md) section 5. An agent never reports one.

**Fail open.** A model error or timeout still saves the capture and still produces a record. Nothing blocks the operator. `shared/utils/records.py: pending_output()` builds one for you. It is **not a judgment**: no checks, UNCERTAIN, and the reason.

## 6. Overrides

Two levels, one rule: **the original survives, the new decision references it, and the reason, actor and timestamp are recorded.**

| Level | Where | When | Shape |
|---|---|---|---|
| **Agent** | `evidence.overrides[]` (optional) | An operator disagrees with the agent at capture time (Round 2 style). | `{overridden_at, overridden_by, target (check_key | "decision"), original_verdict, new_verdict, reason}` |
| **Workflow** | Workflow State `overrides[]` | A person or rule changes the effective decision of any record after the fact. | `{override_id, supersedes {record_id, override_id}, actor, at, reason, original_verdict, previous_verdict, new_verdict, new_outcome}` |

- **Append-only.** Never edit, reorder or delete an entry. Never rewrite the original `checks`/`decision`.
- **Reference the previous decision.** A workflow override names the record it supersedes and, if there was an earlier override, that one too (`override_id`). `previous_verdict` is what was effective just before it.
- **Latest wins.** Downstream agents must use the latest override as the effective verdict (`context.overrides` in every Agent Input; helper `shared/utils/stubs.py: effective_verdict`) and may cite the original.
- Overrides are **data**: they are how you measure false positives and negatives in production. Report them.

## 7. Evidence rules

- Evidence is **traceable to a workflow**: `workflow_id` on every record.
- Evidence is associated with its **agent and stage**: `agent_id`, `stage`.
- Evidence **never silently disappears.** Records are immutable; a re-run that produces different content gets a new `record_id`; the store refuses to replace one.
- Previous evidence **remains available** to later stages (`previous_evidence`) and to reviewers.
- Overrides **reference the previous decision** (section 6).
- Together the records must let a reviewer **reconstruct the end-to-end decision**.
- Evidence is **never fabricated**. If you did not see it, say UNCERTAIN with a reason. Do not fill a field to make a record look complete.
- Media is passed **by reference and hash** (`inputs[].ref` + `sha256`), not by value.

## 8. Agents, check keys and outcomes

Recommended `check_key`s and allowed `decision.outcome` values per agent. Add checks as needed with a stable `snake_case` key.

| Agent | Prefix | Recommended `check_key`s | `outcome` values | Useful `payload` keys |
|---|---|---|---|---|
| **Receiving** | `RCV` | `identity_match`, `carton_count`, `quantity`, `carton_damage`, `unit_damage`, `quality_flags` | `accept`, `accept_with_exceptions`, `reject`, `pending_review` | `supplier`, `qty_ordered`, `qty_received`, `shortfall_units`, `quality_flags[]` |
| **Prep** | `PRP` | `polybag_sealed`, `suffocation_warning`, `fnsku_label_placement`, `original_barcode_covered`, `expiry_legible`, `handling_marks` | `compliant`, `non_compliant`, `pending_review` | `prep_price_usd`, **`measurements`** `{weight_g, length_mm, width_mm, height_mm}`, `rule_source` |
| **Pack** | `PCK` | `items_present`, `quantities_correct`, `no_extra_items` | `seal`, `stop_and_fix`, `pending_review` | `channel`, `order_lines`, `observed_in_box`, `operator_verdict` |
| **Returns** | `RTN` | `identity_match`, `completeness`, `condition` | `restock`, `refurbish`, `liquidate`, `dispose`, `pending_review` | `observed_state`, `amazon_condition`, `parts_missing[]` |
| **Recovery** | `RCY` | one `charge_<line_id>` per fee line | `claim_recommended`, `no_claim`, `insufficient_evidence`, `pending_review` | `charges[]`, `claimable_usd`, `unclaimable[]` |

**Prep measurements.** In Round 2, 42 of 61 sample fee lines were weight-tier fees that no upstream record could speak to. Prep is the only stage that physically touches every FBA unit, so Prep is asked to record measured weight and dimensions when it can. A recommendation, not a requirement.

**Look the rules up.** Where the channel publishes a requirement (Amazon prep requirements, the condition scale, fee schedules), retrieve it and record where it came from (`payload.rule_source`: URL and retrieval date). Do not let a model recall it from memory. The requirement flags and fee amounts in the sample CSVs are dummy values.

### Recovery's semantics (the one place verdicts read differently)

Recovery has no camera. Each charge is a check whose condition is **"this charge is supported by evidence"**:

| Check verdict | Position | Meaning | Claim? |
|---|---|---|---|
| `PASS` | **SUPPORTS** | Evidence supports the charge. | No |
| `FAIL` | **CONTRADICTS** | Evidence contradicts the charge. | **Yes**, attach `evidence_refs` |
| `UNCERTAIN` | **SILENT** | Evidence is absent or insufficient. | **Never.** List it in `payload.unclaimable` with the reason |

A wrongly filed claim costs a seller standing; a missed one costs only money. So SILENT never becomes a claim. Recovery **reads** the accumulated evidence and produces its own new record. It does not rewrite earlier evidence or the workflow state.

## 9. Subject, and "what is a `unit_id`?"

All Round 2 repos share the values `UNIT-0001` to `UNIT-0100`. In Round 3, `subject.subject_id` is **what the workflow is about** (here, the unit). But Round 2 found that `unit_id` does not mean the same thing everywhere: in Receiving a row covers a **PO line**, in the fee report quantity-1 **units** (finding F-08). So:

- Always set `subject.unit_scope` to what **your** agent means by `unit_id` (`unit`, `po_line`, `order`, `unknown`), and put the other keys you have in `subject.refs` (PO line, order id, work order, shipment, SKU, ASIN, FNSKU). Joins should prefer `refs` over a bare id where available.
- If two records disagree on scope, that is `conflicting_evidence`: say UNCERTAIN, do not guess.

## 10. Content hash and traceability

`content_hash` is the SHA-256 of the **canonical JSON** (sorted keys, no whitespace) of the record **excluding** `content_hash` and the optional agent-level `overrides`. Use `shared/utils/hashing.py`. The orchestrator re-computes it and **rejects** a record whose hash does not match.

**Say what this is.** It is a content hash: it lets anyone detect a change *if they kept the original hash*. It is **not** a tamper-evident, immutable or anchored record unless you built and can demonstrate something that makes it so.

```text
Final Outcome ── contributing_records[] ──▶ Evidence Record
                                              ├─ decision {verdict, outcome, reason}
                                              ├─ checks[] ── evidence_refs[] ──▶ inputs[].sha256  (the exact bytes examined)
                                              ├─ upstream_refs[] ──▶ the earlier records it relied on
                                              ├─ model {name, version, prompt_version}  ──▶ what produced it
                                              └─ overrides[]  (+ Workflow State overrides[])  ──▶ where a person disagreed
Workflow State: transitions[] is the audit trail of every skip, retry, halt, override and status change.
```

A broken link breaks traceability, and the [rubric](ROUND3-RUBRIC.md) scores that.

## 11. Who may change the contract

| You may | You may not |
|---|---|
| Add keys inside `payload` | Rename, remove or re-type envelope fields |
| Add new `check_key`s for your agent | Change what PASS / FAIL / UNCERTAIN, or any status, mean |
| Add richer `observed` / `detail` | Invent a fourth verdict or workflow status |
| Propose a change with a `contract` issue on the starter | Fork the contract inside your Pod and not tell anyone |

Within `1.x`, changes are additive only.

## 12. Validate your output

```sh
python -c "import json,sys; from shared.utils.schema import validate; validate('agent-output', json.load(open(sys.argv[1])))" my_output.json
pytest tests/integration/test_agent_contracts.py
```
