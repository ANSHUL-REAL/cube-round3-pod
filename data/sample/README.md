# data/sample/ — Round 2 reference data (synthetic)

> ⚠️ **DUMMY DATA.** The SKUs, ASINs, FNSKUs, orders, suppliers, operators, requirement flags and money amounts are invented. Nothing here is a real Amazon rule or fee. Do not treat it as ground truth, and do not train on it. Look authoritative rules up.

The same five files Round 2 participants worked from, copied unchanged:

| File | Rows | Used by |
|---|---|---|
| `receiving_sample.csv` | 100 | Receiving stub |
| `prep_sample.csv` | 62 | Prep stub |
| `pack_sample.csv` | 29 | Pack stub |
| `returns_sample.csv` | 24 | Returns stub |
| `fee_report_sample.csv` | 61 | Recovery stub |
| `cases.json` | 100 | the orchestrator: one case per subject, derived from the above |

Column meanings are in the Round 2 repos' `data/README.md`. Key facts:

- All five share `unit_id` `UNIT-0001`…`UNIT-0100` (in Round 3 this is the workflow's `subject_id`), and two orgs (`org_demo_alpha`, `org_demo_bravo`) for the tenancy test.
- A unit has a **Prep** record (FBA) **or** a **Pack** record (merchant-fulfilled / 3PL), never both. 9 units have neither (finding F-12).
- `uncertain` and `pending_review` appear on purpose.
- `photo_refs` are placeholders; no images exist.
- `cases.json`: `route` (`fba` / `mfn` / `unknown`, derived from which of Prep/Pack has a record) and `returned` (a Returns record exists). Rebuild with `make cases`.

**The stub agents replay these rows. Your real agents do not read them.**
