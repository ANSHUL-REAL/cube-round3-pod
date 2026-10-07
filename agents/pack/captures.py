"""Read the box photos named in ``request["inputs"]``, safely.

``inputs[].ref`` is relative to the capture root (``INPUT_DIR`` or ``data/input``) and starts with the
subject id (``<subject_id>/pack/<file>``), which is how the orchestrator writes it. We refuse anything
that points outside the root or at another subject, and anything whose bytes don't match the declared hash.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path

from shared.utils.captures import CapturePathError, resolve_capture

ROOT = Path(__file__).resolve().parents[2]


class CaptureError(Exception):
    """A capture could not be read or does not match what the request says it is."""


def capture_root() -> Path:
    return Path(os.environ.get("INPUT_DIR", ROOT / "data" / "input")).resolve()


def load(inputs: list[dict], subject_id: str) -> list[tuple[dict, bytes]]:
    root = capture_root()
    out: list[tuple[dict, bytes]] = []
    for item in inputs:
        if item.get("kind", "image") != "image":
            continue
        ref = item["ref"]
        try:
            path = resolve_capture(root, ref, subject_id, "pack")
        except CapturePathError as exc:
            raise CaptureError(str(exc)) from exc
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise CaptureError(f"capture {ref!r} could not be read: {exc.strerror or exc}") from exc
        declared = item.get("sha256")
        if declared and hashlib.sha256(data).hexdigest() != declared:
            raise CaptureError(f"capture {ref!r} does not match its declared sha256")
        out.append((item, data))
    return out
