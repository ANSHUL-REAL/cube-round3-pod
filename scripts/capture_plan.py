"""Which demo photos do we still need to take?

    python scripts/capture_plan.py                  # the demo set: what to photograph, and what is already in place
    python scripts/capture_plan.py --unit UNIT-0044 # one unit
    python scripts/capture_plan.py --check          # exit 1 if any demo folder is empty (use before a demo run)

Every real agent answers `no_capture` until its folder data/input/<unit>/<stage>/ holds a photo, so the integrated demo
needs these. The instructions come from the *inputs* the agents read in the organisers' sample CSVs (what was ordered,
what the work order asks for), never from the answer columns. Recovery takes no photos.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from shared.utils import sample_data as sd  # noqa: E402
from shared.utils.console import utf8_console  # noqa: E402

PHOTO_EXT = {".jpg", ".jpeg", ".png", ".webp", ".heic"}

# The demo set: the stories the Round 3 handbook asks to see, each on a real sample unit.
DEMO = {
    "UNIT-0014": "FBA unit that was returned: Receiving -> Prep -> Returns -> Recovery. Recovery can recommend an inbound-defect claim.",
    "UNIT-0008": "Merchant-fulfilled, clean: Receiving -> Pack. Pack the right item and expect a seal.",
    "UNIT-0044": "Merchant-fulfilled, wrong box: the order is a candle trio; put something else in the box and expect stop_and_fix.",
    "UNIT-0016": "Merchant-fulfilled and returned: Receiving -> Pack -> Returns -> Recovery.",
    "UNIT-0023": "Uncertain on purpose: photograph the box with items stacked or hidden, then resolve it with a recorded override.",
}


def case_for(unit: str) -> dict:
    for c in json.loads((ROOT / "data/sample/cases.json").read_text()):
        if c["unit_id"] == unit:
            return c
    raise SystemExit(f"{unit} is not in data/sample/cases.json")


def input_root() -> Path:
    return Path(os.environ.get("INPUT_DIR", ROOT / "data" / "input"))


def count(unit: str, stage: str) -> int:
    folder = input_root() / unit / stage
    return sum(1 for p in folder.iterdir() if p.suffix.lower() in PHOTO_EXT) if folder.is_dir() else 0


def what_to_shoot(stage: str, unit: str, org: str) -> str:
    if stage == "receiving":
        r = sd.row("receiving", unit, org)
        return (f"{r['product_title']} ({r['sku']}), colour {r['spec_colour']}, variant {r['spec_variant']}: the delivery as it arrives. "
                f"PO {r['po_number']}: {r['cartons_ordered']} cartons of {r['units_per_carton_ordered']}, {r['qty_ordered']} units. "
                f"Show the cartons, and open one so the units are visible.")
    if stage == "prep":
        r = sd.row("prep", unit, org)
        names = {"wo_polybag": "a polybag", "wo_suffocation_warning": "a suffocation warning", "wo_expiry_date": "an expiry date"}
        asks = [names[k] for k in names if r[k] == "True"]
        marks = ", ".join(m for m in r["wo_handling_marks"].split(";") if m) or "none"
        return (f"{r['sku']} prepped for FBA, work order {r['work_order_id']} (FNSKU {r['fnsku']}). Front and back of the unit. "
                f"The work order requires {', '.join(asks) or 'no polybag, suffocation warning or expiry date'}; handling marks: {marks}.")
    if stage == "pack":
        r = sd.row("pack", unit, org)
        return (f"The open box before sealing, from above, whole interior in frame. Order {r['order_id']} ({r['channel']}): "
                f"{r['order_lines']}. Take 1 to 3 photos.")
    if stage == "returns":
        r = sd.row("returns", unit, org)
        return (f"The returned parcel for order {r['order_id']}, ordered {r['ordered_sku']}: everything that came back laid out. "
                f"Parts expected: {r['parts_list']}. Show any wear clearly.")
    return "No photos: Recovery reads the fee report and the earlier evidence."


def plan(unit: str) -> list[tuple[str, int, str]]:
    case = case_for(unit)
    org, route = case["org_id"], case["route"]
    stages = ["receiving", "prep" if route == "fba" else "pack"] + (["returns"] if case["returned"] else [])
    return [(s, count(unit, s), what_to_shoot(s, unit, org)) for s in stages]


def main() -> int:
    utf8_console()
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--unit", help="one unit instead of the whole demo set")
    ap.add_argument("--check", action="store_true", help="exit 1 if any needed folder is empty")
    args = ap.parse_args()
    units = [args.unit] if args.unit else list(DEMO)
    missing = 0
    for unit in units:
        case = case_for(unit)
        print(f"\n{unit}  ({case['org_id']}, route {case['route']}{', returned' if case['returned'] else ''})")
        if unit in DEMO:
            print(f"  Story: {DEMO[unit]}")
        for stage, n, text in plan(unit):
            missing += n == 0
            print(f"  [{'OK ' if n else 'TODO'}] data/input/{unit}/{stage}/  ({n} photo{'s' if n != 1 else ''})")
            print(f"         {text}")
    print(f"\n{missing} folder(s) still empty.")
    return 1 if (args.check and missing) else 0


if __name__ == "__main__":
    raise SystemExit(main())
