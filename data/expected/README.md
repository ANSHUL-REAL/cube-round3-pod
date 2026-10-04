# data/expected/ — what the starter should produce

You should not have to invent test data before understanding the intended workflow. These files are the **canonical expected results for the organiser stub agents and the standard flow**:

| File | What |
|---|---|
| `canonical-workflow.json` | **The canonical sample workflow**: UNIT-0014 (FBA, returned). Its case, the verdict and state of every stage, the workflow status and the final outcome. The full walkthrough is in [`examples/end-to-end/`](../../examples/end-to-end/). |
| `final-outcomes.sample.json` | Workflow status, final outcome, `needs_human` and `claimable_usd` for all 100 sample workflows. |

`tests/e2e/test_end_to_end.py::test_matches_expected_outcomes_for_the_organiser_stubs` checks the stubs against them. **It skips itself once you replace a stub or change the flow**: at that point write your own expected results for your own units (and put them next to your captures in `data/input/`).

Regenerate with `make expected`. Do not edit these by hand to make a test pass.
