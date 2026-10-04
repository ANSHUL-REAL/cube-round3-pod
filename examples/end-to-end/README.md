# End-to-end: UNIT-0014 (FBA, returned) -> claim recommended

The canonical sample workflow. Run it yourself: `make case UNIT=UNIT-0014 ORG=org_demo_alpha`
(or `python -m orchestration.run --case examples/end-to-end/case.json`).

| Stage | Result | Evidence record |
|---|---|---|
| receiving | accept / PASS | RCV-0014 |
| prep | compliant / PASS | PRP-0014 |
| pack | skipped: route='fba' not in ['mfn'] | - |
| returns | liquidate / PASS | RTN-0014 |
| recovery | claim_recommended / FAIL | RCY-UNIT-0014 |

**Final:** status `COMPLETED`, outcome `CLAIM_RECOMMENDED`, claimable **$2.00**.

## How the evidence moved

1. The orchestrator created workflow `WF-org_demo_alpha-UNIT-0014` (`workflow-state.json`) and decided routing: FBA, so Prep runs and Pack is skipped; a return happened, so Returns runs.
2. For each stage it sent an **Agent Input** (see `agent-input.prep.json`: note `previous_evidence` carries the Receiving record) and received an **Agent Output** (`agent-output.<stage>.json`) whose `evidence` is the stored Evidence Record.
3. Recovery received all earlier evidence. It found that Prep says the unit was compliant, so the channel's inbound-defect fee is **contradicted**, and it cites `PRP-0014` as the supporting evidence.
4. 3 other charge(s) have no evidence (lost_inbound, fulfilment_fee_weight_tier, refund_issued_item_not_returned) and are **left unclaimed with the reason recorded**: silence never becomes a claim.
5. The orchestrator **derived** the status and `final-outcome.json` from the evidence chain. Recovery recommended the claim; the orchestrator owns the decision.

Every link is traceable: `final-outcome.contributing_records` -> `agent-output.*.evidence` -> `checks[].evidence_refs` -> `inputs[].sha256`; `workflow-state.transitions` is the audit trail.

(The stub agents replay the Round 2 CSV and their claim rules are illustrative. Your agents decide for real.)
