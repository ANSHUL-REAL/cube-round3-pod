"""Check one prepped unit from the command line, through the same code the orchestrator runs.

    python -m agents.prep.check --unit UNIT-0002 --org org_demo_alpha path/to/front.jpg [more.jpg ...]

Photos are copied to data/input/<unit>/prep/ (where the orchestrator looks for them), then Prep runs on the unit's
work order from the organisers' sample. Needs GEMINI_API_KEY in the environment or .env; without one it prints the
pending record it would store, which says so. Prints the verdict, each check and where the record is.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

from orchestration.orchestrator import discover_inputs

from . import app as prep
from .captures import capture_root
from .workorders import resolve
from shared.utils.console import utf8_console


def main(argv: list[str] | None = None) -> int:
    utf8_console()
    ap = argparse.ArgumentParser(prog="python -m agents.prep.check", description=__doc__.split("\n")[0])
    ap.add_argument("photos", nargs="+", help="photos of the prepped unit (the first 6 are used)")
    ap.add_argument("--unit", required=True, help="unit id, e.g. UNIT-0002")
    ap.add_argument("--org", required=True, help="organisation id, e.g. org_demo_alpha")
    ap.add_argument("--json", action="store_true", help="print the full Agent Output instead of a summary")
    args = ap.parse_args(argv)

    missing = [p for p in args.photos if not Path(p).is_file()]
    if missing:
        print(f"Photo not found: {', '.join(missing)}", file=sys.stderr)
        return 2
    try:  # refuse another organisation's unit before anything is written
        resolve({"subject": {"org_id": args.org, "subject_id": args.unit}})
    except LookupError as exc:
        print(f"Refused: {exc}", file=sys.stderr)
        return 2
    folder = capture_root() / args.unit / "prep"
    folder.mkdir(parents=True, exist_ok=True)
    for i, src in enumerate(args.photos, 1):
        shutil.copyfile(src, folder / f"photo{i}{Path(src).suffix.lower() or '.jpg'}")
    request = {
        "schema_version": "1.0", "request_id": f"CLI-{args.org}-{args.unit}", "workflow_id": f"WF-{args.org}-{args.unit}",
        "stage": "prep", "subject": {"org_id": args.org, "subject_id": args.unit, "route": "fba"},
        "inputs": discover_inputs(args.unit, "prep"), "previous_evidence": [],
        "context": {"overrides": [], "case": {}},
    }
    try:
        out = prep.handle(request)
    except LookupError as exc:
        print(f"Refused: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(out, indent=2))
        return 0
    ev, d = out["evidence"], out["evidence"]["decision"]
    print(f"{args.unit} ({args.org})  ->  {d['outcome'].upper()}   [{d['verdict']}, status {out['status']}]")
    print(f"  {d['reason']}")
    for c in ev["checks"]:
        print(f"  {c['verdict']:<9} {c['check_key']:<26} {c['detail']}")
    if ev.get("error"):
        print(f"  ERROR {ev['error']['code']}: {ev['error']['message']}")
    print(f"  record {ev['record_id']}  model {ev['model']['name']}  calls {ev['model']['calls']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
