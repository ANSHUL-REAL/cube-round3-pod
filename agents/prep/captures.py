"""Read the prep photos named in ``request["inputs"]``, safely, and get them ready for the model.

``inputs[].ref`` is relative to the capture root (``INPUT_DIR`` or ``data/input``) and looks like
``<subject_id>/prep/<file>``, which is how the orchestrator writes it. We refuse anything that points outside the
root, at another subject or another stage's folder, and anything whose bytes do not match the declared sha256.
"""
from __future__ import annotations

import hashlib
import io
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError

from shared.utils.captures import CapturePathError, resolve_capture

try:  # phone photos are often HEIC; optional
    import pillow_heif

    pillow_heif.register_heif_opener()
except Exception:  # pragma: no cover - the package is optional
    pass

ROOT = Path(__file__).resolve().parents[2]
STAGE_DIR = "prep"


class CaptureError(Exception):
    """A capture could not be read or does not match what the request says it is."""


@dataclass
class Capture:
    ref: str
    sha256: str          # of the bytes on disk (the original)
    data: bytes
    mtime: str           # UTC, ISO 8601: when the file was last written (not proof of when it was shot)


@dataclass
class Prepared:
    ref: str
    sha256: str          # of the original
    analysed_sha256: str  # of the JPEG the model saw
    width: int
    height: int
    jpeg: bytes


def capture_root() -> Path:
    return Path(os.environ.get("INPUT_DIR", ROOT / "data" / "input")).resolve()


def load(inputs: list[dict], subject_id: str) -> list[Capture]:
    root = capture_root()
    out: list[Capture] = []
    for item in inputs:
        if item.get("kind", "image") != "image":
            continue
        ref = item["ref"]
        try:
            path = resolve_capture(root, ref, subject_id, STAGE_DIR)
        except CapturePathError as exc:
            raise CaptureError(str(exc)) from exc
        try:
            data = path.read_bytes()
            mtime = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        except OSError as exc:
            raise CaptureError(f"capture {ref!r} could not be read: {exc.strerror or exc}") from exc
        digest = hashlib.sha256(data).hexdigest()
        declared = item.get("sha256")
        if declared and digest != declared:
            raise CaptureError(f"capture {ref!r} does not match its declared sha256")
        out.append(Capture(ref=ref, sha256=digest, data=data, mtime=mtime))
    return out


def prepare(cap: Capture, max_side: int = 1600) -> Prepared:
    """Decode, fix orientation, shrink to ``max_side`` and re-encode as JPEG. A file that is not an image raises."""
    try:
        img = Image.open(io.BytesIO(cap.data))
        img.load()
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise CaptureError(f"capture {cap.ref!r} is not a readable image: {exc}") from exc
    img = ImageOps.exif_transpose(img).convert("RGB")
    if max(img.size) > max_side:
        img.thumbnail((max_side, max_side))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=88)
    jpeg = buf.getvalue()
    return Prepared(ref=cap.ref, sha256=cap.sha256, analysed_sha256=hashlib.sha256(jpeg).hexdigest(),
                    width=img.width, height=img.height, jpeg=jpeg)
