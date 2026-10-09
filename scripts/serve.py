"""Start the console and API on this machine.

    python scripts/serve.py                       # our agents; a stage with no photo or no model key answers "pending" and says so
    python scripts/serve.py --data D:/pod12-demo  # a separate folder for photos, workflows, evidence and the Pack photo ledger
    python scripts/serve.py --stubs               # the organisers' CSV-replay stubs, only to look at the screens (labelled)
    python scripts/serve.py --port 8100 --host 127.0.0.1
    python scripts/serve.py --lan --data D:/pod12-demo   # teammates clear their own step from their phones (same Wi-Fi)
    python scripts/serve.py --tunnel --data D:/pod12-demo   # the same, over a public Cloudflare link (any network)

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
    ap.add_argument("--lan", action="store_true",
                    help="let phones on the same Wi-Fi join with a 6-digit access code (phone stations at /ui/station)")
    ap.add_argument("--code", help="the access code for --lan (default: a new random 6-digit code each start)")
    ap.add_argument("--tunnel", action="store_true",
                    help="like --lan, but through a Cloudflare quick tunnel: a public https link that works on any network "
                         "(needs cloudflared; the access code still guards every page)")
    args = ap.parse_args()
    tunnel = None
    if args.tunnel:
        args.lan = True
    if args.lan:
        import secrets

        code = args.code or f"{secrets.randbelow(10**6):06d}"
        if not (code.isdigit() and len(code) == 6):
            ap.error("--code must be 6 digits")
        os.environ["POD_LAN_CODE"], os.environ["POD_LAN_PORT"] = code, str(args.port)
        if args.host == "127.0.0.1" and not args.tunnel:  # a tunnel reaches us on loopback; nothing else needs to
            args.host = "0.0.0.0"
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

    if args.tunnel:
        tunnel = start_tunnel(args.port)
        if tunnel is None:
            return 1
    if args.lan:
        from orchestration.web.station import lan_url

        url = lan_url()
        where = "Anyone with the link" if args.tunnel else "Phones on this Wi-Fi"
        print(f"\n{where}: open {url}\nAccess code: {os.environ['POD_LAN_CODE']}\n"
              "(The laptop's own pages show the QR code and the code. If Windows asks about the firewall, allow Private networks.)")
        try:
            import segno

            segno.make(url, error="m").terminal(compact=True)
        except Exception:  # a terminal that cannot draw the QR still has the address above
            pass

    try:
        uvicorn.run("orchestration.api:app", host=args.host, port=args.port, log_level="info")
    finally:
        if tunnel is not None:
            tunnel.terminate()
    return 0


def start_tunnel(port: int):
    """Start `cloudflared tunnel --url http://127.0.0.1:<port>` and wait for its trycloudflare.com address.

    A quick tunnel needs no Cloudflare account; the address changes every start. Returns the process (stopped when the
    server stops), or None with a message if cloudflared is missing or gives no address in 60 s."""
    import re
    import shutil
    import subprocess
    import threading

    exe = shutil.which("cloudflared") or next((str(p) for p in (
        Path("C:/Program Files (x86)/cloudflared/cloudflared.exe"), Path("C:/Program Files/cloudflared/cloudflared.exe"))
        if p.is_file()), None)
    if not exe:
        print("cloudflared is not installed (Windows: winget install --id Cloudflare.cloudflared)", file=sys.stderr)
        return None
    proc = subprocess.Popen([exe, "tunnel", "--no-autoupdate", "--url", f"http://127.0.0.1:{port}"],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")
    found = threading.Event()
    url = {}

    def read():  # keep draining cloudflared's output so its pipe never fills and blocks it
        for line in proc.stdout:
            m = re.search(r"https://[a-z0-9-]+\.trycloudflare\.com", line)
            if m and not found.is_set():
                url["u"] = m.group(0)
                found.set()

    threading.Thread(target=read, daemon=True).start()
    if not found.wait(60):
        proc.terminate()
        print("cloudflared gave no public address within 60 s (no internet, or Cloudflare refused).", file=sys.stderr)
        return None
    os.environ["POD_PUBLIC_URL"] = url["u"]
    return proc


if __name__ == "__main__":
    raise SystemExit(main())
