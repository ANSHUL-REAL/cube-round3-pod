"""Put a folder of capture photos into the deployment's database, so the live site has them after its next start.

    DATABASE_URL=postgresql://... python scripts/seed_photos.py D:/pod12-rehearsal-2/input
    DATABASE_URL=postgresql://... python scripts/seed_photos.py D:/pod12-rehearsal-2/input --units UNIT-0014 UNIT-0044

The folder is laid out like data/input: <unit>/<stage>/<photo>. Only units of the sample cases and stages of that unit
are taken; anything else is listed and skipped. Each photo is stored unchanged (its SHA-256 is what evidence cites),
with its original modification time. The deployed server copies the files back to disk when it starts, so restart it
(Render: Manual Deploy > Restart service) after seeding. A unit's stage that already has photos in the database is
replaced by this folder's photos for that stage.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
PHOTO_EXT = {".jpg", ".jpeg", ".png", ".webp"}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("folder", help="a folder laid out as <unit>/<stage>/<photo>")
    ap.add_argument("--units", nargs="*", help="only these units")
    args = ap.parse_args()
    if not os.environ.get("DATABASE_URL"):
        ap.error("set DATABASE_URL (the deployment's Postgres) first")
    folder = Path(args.folder).resolve()
    os.environ["INPUT_DIR"] = str(folder)  # the files are recorded relative to this folder, as data/input on the server

    from shared.utils import db

    cases = {c["unit_id"]: c for c in json.loads((ROOT / "data" / "sample" / "cases.json").read_text())}
    db.migrate()
    done = 0
    for unit_dir in sorted(p for p in folder.iterdir() if p.is_dir()):
        unit = unit_dir.name
        if args.units and unit not in args.units:
            continue
        case = cases.get(unit)
        if case is None:
            print(f"skip {unit}: not a sample unit")
            continue
        stages = {"receiving", "prep" if case["route"] == "fba" else "pack", *(["returns"] if case["returned"] else [])}
        for stage_dir in sorted(p for p in unit_dir.iterdir() if p.is_dir()):
            if stage_dir.name not in stages:
                print(f"skip {unit}/{stage_dir.name}: not a stage of this unit ({', '.join(sorted(stages))})")
                continue
            photos = [p for p in stage_dir.iterdir() if p.is_file() and p.suffix.lower() in PHOTO_EXT]
            if not photos:
                continue
            db.sync_folder("input", stage_dir, case["org_id"])
            done += len(photos)
            print(f"{case['org_id']} {unit}/{stage_dir.name}: {len(photos)} photo(s)")
    print(f"{done} photo(s) in the database. Restart the deployed server to load them.")
    db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
