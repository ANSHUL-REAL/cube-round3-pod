"""Read the return photos named in ``request["inputs"]``, safely.

``inputs[].ref`` is relative to the capture root (``INPUT_DIR`` or ``data/input``) and starts with the
subject id (``<subject_id>/returns/<file>``), which is how the orchestrator writes it. We refuse anything
that points outside the root or at another subject, and anything whose bytes don't match the declared hash.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


class CaptureError(Exception):
    """A capture could not be read or does not match what the request says it is."""


def capture_root() -> Path:
    return Path(os.environ.get("INPUT_DIR", ROOT / "data" / "input")).resolve()


def load(inputs: list[dict], subject_id: str) -> list[tuple[dict, bytes]]:
    """(input, bytes) for each image input. The returned input always carries the sha256 of the bytes actually read."""
    root = capture_root()
    out: list[tuple[dict, bytes]] = []
    for item in inputs:
        if item.get("kind", "image") != "image":
            continue
        ref = item["ref"]
        path = (root / ref).resolve()
        if root not in path.parents:
            raise CaptureError(f"capture {ref!r} is outside the capture root")
        segments = ref.split("/")
        if segments[0] != subject_id:
            raise CaptureError(f"capture {ref!r} does not belong to subject {subject_id!r}")
        if len(segments) < 3 or segments[1] != "returns":
            raise CaptureError(f"capture {ref!r} is not in this subject's returns folder")
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise CaptureError(f"capture {ref!r} could not be read: {exc.strerror or exc}") from exc
        digest = hashlib.sha256(data).hexdigest()
        declared = item.get("sha256")
        if declared and digest != declared:
            raise CaptureError(f"capture {ref!r} does not match its declared sha256")
        out.append(({**item, "sha256": digest}, data))
    return out
