"""Start the console and API on this machine.

    python scripts/serve.py                # our agents; a stage with no photo or no model key answers "pending" and says so
    python scripts/serve.py --stubs        # the organisers' CSV-replay stubs, only to look at the screens (clearly labelled)
    python scripts/serve.py --port 8100 --host 127.0.0.1

Then open http://localhost:8100/. There is no sign-in: keep --host 127.0.0.1 unless you know who can reach the port.
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
    args = ap.parse_args()
    if args.stubs:
        os.environ["POD_UI_STUBS"] = "1"
    os.chdir(ROOT)
    sys.path.insert(0, str(ROOT))
    import uvicorn

    uvicorn.run("orchestration.api:app", host=args.host, port=args.port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
