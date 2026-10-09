"""Check one delivery from the command line, through the same code the orchestrator runs.

    python -m agents.receiving.check --unit UNIT-0003 --org org_demo_bravo pallet.jpg carton.jpg unit.jpg

Photos are copied to data/input/<unit>/receiving/ (where the orchestrator looks for them), then Receiving runs on the
unit's PO line from the organisers' sample. Name the files after what they show (pallet, carton, unit, barcode): the
file name is passed to the model as the operator's label for the view. Needs GEMINI_API_KEY in the environment or
.env; without one it prints the pending record it would store, which says so.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from pathlib import Path

from orchestration.orchestrator import discover_inputs

from . import app as receiving
from .captures import capture_root
from .orders import resolve_order
from shared.utils.console import utf8_console


def main(argv: list[str] | None = None) -> int:
    utf8_console()
    ap = argparse.ArgumentParser(prog="python -m agents.receiving.check", description=__doc__.split("\n")[0])
    ap.add_argument("photos", nargs="+", help="1 to 6 photos of the delivery (pallet, carton, unit, barcode)")
    ap.add_argument("--unit", required=True, help="unit id (a PO line in Receiving), e.g. UNIT-0003")
    ap.add_argument("--org", required=True, help="organisation id, e.g. org_demo_bravo")
    ap.add_argument("--json", action="store_true", help="print the full Agent Output instead of a summary")
    args = ap.parse_args(argv)

    missing = [p for p in args.photos if not Path(p).is_file()]
    if missing or not 1 <= len(args.photos) <= 6:
        print("Give 1 to 6 photo files that exist. " + (f"Not found: {', '.join(missing)}" if missing else ""),
              file=sys.stderr)
        return 2
    try:  # refuse another organisation's unit before anything is written
        resolve_order({"subject": {"org_id": args.org, "subject_id": args.unit}})
    except LookupError as exc:
        print(f"Refused: {exc}", file=sys.stderr)
        return 2
    folder = capture_root() / args.unit / "receiving"
    folder.mkdir(parents=True, exist_ok=True)
    for src in args.photos:
        name = re.sub(r"[^A-Za-z0-9._-]", "_", Path(src).name)
        shutil.copyfile(src, folder / name)
    request = {
        "schema_version": "1.0", "request_id": f"CLI-{args.org}-{args.unit}", "workflow_id": f"WF-{args.org}-{args.unit}",
        "stage": "receiving", "subject": {"org_id": args.org, "subject_id": args.unit, "route": "unknown"},
        "inputs": discover_inputs(args.unit, "receiving"), "previous_evidence": [],
        "context": {"overrides": [], "case": {}},
    }
    out = receiving.handle(request)
    if args.json:
        print(json.dumps(out, indent=2))
        return 0
    ev, d = out["evidence"], out["evidence"]["decision"]
    print(f"{args.unit} ({args.org})  ->  {d['outcome'].upper()}   [{d['verdict']}, status {out['status']}]")
    print(f"  {d['reason']}")
    for c in ev["checks"]:
        print(f"  {c['verdict']:<9} {c['check_key']:<15} {c.get('detail', '')}")
    if ev.get("error"):
        print(f"  ERROR {ev['error']['code']}: {ev['error']['message']}")
    print(f"  record {ev['record_id']}  model {ev['model']['name']}  calls {ev['model']['calls']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
