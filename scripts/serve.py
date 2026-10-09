"""Start the console and API on this machine.

    python scripts/serve.py                       # our agents; a stage with no photo or no model key answers "pending" and says so
    python scripts/serve.py --data D:/pod12-demo  # a separate folder for photos, workflows, evidence and the Pack photo ledger
    python scripts/serve.py --stubs               # the organisers' CSV-replay stubs, only to look at the screens (labelled)
    python scripts/serve.py --port 8100 --host 127.0.0.1

Then open http://localhost:8100/. There is no sign-in: keep --host 127.0.0.1 unless you know who can reach the port.

`--data DIR` keeps a demo apart from everything else: photos go to DIR/input/<unit>/<stage>/, workflows and evidence to
DIR/out/, the Pack photo-reuse ledger to DIR/out/pack-ledger.json. Start a demo on a new, empty folder and nothing from an
earlier run (or from the stubs) can show up on screen. Product reference photos for Returns are product data, not demo
data: they are written into agents/returns/reference/ (see agents/returns/onboard.py).
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8100)
    ap.add_argument("--stubs", action="store_true", help="use the organiser stubs instead of our agents (labelled on every page)")
    ap.add_argument("--data", help="folder for this session's photos (input/) and results (out/); created if missing")
    args = ap.parse_args()
    if args.stubs:
        os.environ["POD_UI_STUBS"] = "1"
    if args.data:
        data = Path(args.data).resolve()
        (data / "input").mkdir(parents=True, exist_ok=True)
        (data / "out").mkdir(parents=True, exist_ok=True)
        os.environ["INPUT_DIR"] = str(data / "input")
        os.environ["OUT_DIR"] = str(data / "out")
        os.environ["PACK_LEDGER_PATH"] = str(data / "out" / "pack-ledger.json")
        print(f"photos: {data / 'input'}\nresults: {data / 'out'}")
    os.chdir(ROOT)
    sys.path.insert(0, str(ROOT))
    import uvicorn

    uvicorn.run("orchestration.api:app", host=args.host, port=args.port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
