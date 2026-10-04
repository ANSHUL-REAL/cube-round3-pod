# data/input/ — your captures and your cases

Put each stage's captures here, one folder per subject and stage:

```text
data/input/
└── UNIT-0014/
    ├── receiving/   pallet.jpg  carton.jpg  unit.jpg
    ├── prep/        front.jpg   back.jpg    label.jpg
    ├── pack/        open_box.jpg
    └── returns/     1.jpg  2.jpg  3.jpg
```

The orchestrator finds these, hashes them (`sha256`), and passes them to the agent as `inputs`. The hash proves which bytes a verdict was based on.

- **No images ship with the starter.** The Round 2 CSVs reference `fixtures/...` paths that do not exist. Capture your own.
- **Never commit real customer data or anything you do not have the right to share.** Use synthetic or consented images.
- Keep the repo light: a handful of images per demo subject, not a dataset.
- Tenancy test: keep at least one subject under each demo org (`org_demo_alpha`, `org_demo_bravo`).
- To run your own subjects, list them in a cases file (same shape as `data/sample/cases.json`) and run `python -m orchestration.run --all --cases data/input/my_cases.json`.
