# Happy path: everything passes -> CLEAN

`case.json` is UNIT-0039 (FBA, returned). `case.mfn.json` is UNIT-0008 (merchant-fulfilled, so Pack runs instead of Prep).

| Stage | Result (UNIT-0039) |
|---|---|
| receiving | accept / PASS |
| prep | compliant / PASS |
| pack | skipped: route='fba' not in ['mfn'] |
| returns | liquidate / PASS |
| recovery | insufficient_evidence / UNCERTAIN |

Final: status `COMPLETED`, outcome `CLEAN`, `needs_human: False`.

UNIT-0008 (`workflow-state.mfn.json`): Receiving -> Pack -> Recovery, also `CLEAN`. Prep is skipped because Amazon packs FBA boxes and the seller packs the others: a unit takes one route.

A stage is `skipped` only when the flow's `when` rule says it does not apply, and the reason is recorded. It is never skipped because an agent was inconvenient.
