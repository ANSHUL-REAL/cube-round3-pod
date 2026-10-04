# Uncertain path: Prep says UNCERTAIN (UNIT-0012)

UNCERTAIN means "the evidence is insufficient for a reliable judgment". It is not a low-confidence PASS, and the system never turns it into one.

| File | Policy | What happened |
|---|---|---|
| `agent-output.prep.json` | | Prep returned `UNCERTAIN` with an `uncertain_reason` on the failing checks, and `needs_human: true`. The evidence is stored as is. |
| `workflow-state.continue.json` | `on_uncertain: continue` (default) | Later stages still ran. Final: **BLOCKED / NEEDS_REVIEW**, `provisional: true`. A person must decide. |
| `workflow-state.blocked.json` | `on_uncertain: block` (`flow.block.json`) | The orchestrator **halted** at Prep (`halted` is set); Returns and Recovery are still `pending`. Status **BLOCKED**. |
| `workflow-state.resolved.json` | | `op_amira` overrode Prep's decision to PASS with a reason (`overrides[0]`, which references Prep's record and the previous verdict), then the workflow was resumed. Status **COMPLETED**, outcome **CLEAN**. |

Things to notice:
- Prep's original evidence record is **unchanged**. The override is a new entry in the workflow state, with actor, timestamp, reason, `original_verdict`, `previous_verdict`, `new_verdict`.
- `final_outcome.effective_verdicts.prep` shows the verdict after the override; the stage still reports the agent's own verdict.
- `transitions` records the halt, the override, the resume and every status change.

Try it: `python -m orchestration.run --case examples/uncertain-path/case.json --flow examples/uncertain-path/flow.block.json`
then `--override WF-org_demo_bravo-UNIT-0012 --record PRP-0012 --verdict PASS --actor you --reason "retook the photo" --and-resume`.
