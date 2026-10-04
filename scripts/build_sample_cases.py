"""Derive data/sample/cases.json from the sample CSVs (one case per unit in receiving).

route:    fba if the unit has a Prep record, mfn if it has a Pack record, else unknown.
returned: unit has a Returns record.
Re-run after you add your own data:  python scripts/build_sample_cases.py
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from shared.utils import sample_data  # noqa: E402

cases = [
    {"org_id": r["org_id"], "unit_id": r["unit_id"],
     "route": sample_data.route(r["unit_id"], r["org_id"]),
     "returned": sample_data.has("returns", r["unit_id"], r["org_id"])}
    for r in sample_data.rows("receiving")
]
out = Path(__file__).resolve().parents[1] / "data/sample/cases.json"
out.write_text(json.dumps(cases, indent=2) + "\n")
print(f"wrote {len(cases)} cases -> {out}")
