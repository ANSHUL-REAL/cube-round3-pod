"""Read the photos named in ``request["inputs"]``, safely, and prepare them for the model.

``inputs[].ref`` is relative to the capture root (``INPUT_DIR`` or ``data/input``) and is
``<subject_id>/receiving/<file>``, which is how the orchestrator writes it. We refuse anything outside the root, at
another subject or another stage, and anything whose bytes do not match the declared hash.
"""
from __future__ import annotations

import hashlib
import io
import os
from pathlib import Path

from PIL import Image, ImageOps

from shared.utils.captures import CapturePathError, resolve_capture

from .vision import Photo

try:  # iPhone photos (HEIC). Optional: without it, a HEIC file is reported as unreadable.
    from pillow_heif import register_heif_opener

    register_heif_opener()
except ImportError:  # pragma: no cover
    pass

ROOT = Path(__file__).resolve().parents[2]
STAGE = "receiving"
MAX_PIXELS = 30_000_000  # checked from the header, before decoding, so a tiny file cannot claim a giant image
VIEWS = ("pallet", "carton", "unit", "barcode", "label", "overview")


class CaptureError(Exception):
    """A capture could not be read or does not match what the request says it is."""


def capture_root() -> Path:
    return Path(os.environ.get("INPUT_DIR", ROOT / "data" / "input")).resolve()


def view_of(ref: str) -> str:
    name = ref.rsplit("/", 1)[-1].lower()
    return next((v for v in VIEWS if v in name), "unlabelled")


def load(inputs: list[dict], subject_id: str) -> list[tuple[dict, bytes]]:
    """(input, bytes) for each image input. Raises CaptureError on anything that is not what it claims to be."""
    root = capture_root()
    out: list[tuple[dict, bytes]] = []
    for item in inputs:
        if item.get("kind", "image") != "image":
            continue
        ref = item["ref"]
        try:
            path = resolve_capture(root, ref, subject_id, STAGE)
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


def prepare(item: dict, data: bytes, max_side: int) -> Photo:
    """Decode, make upright, drop metadata (EXIF GPS etc.), downscale, and re-encode as JPEG for the model."""
    try:
        img = Image.open(io.BytesIO(data))
        w, h = img.size
        if w * h > MAX_PIXELS:
            raise CaptureError(f"capture {item['ref']!r} is too large ({w}x{h})")
        img = ImageOps.exif_transpose(img).convert("RGB")
    except CaptureError:
        raise
    except Exception as exc:  # Pillow raises many types for bad files
        raise CaptureError(f"capture {item['ref']!r} could not be read as an image") from exc
    img.info = {}
    scale = max_side / max(img.size)
    if scale < 1:
        img = img.resize((max(1, round(img.width * scale)), max(1, round(img.height * scale))), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=88)
    jpeg = buf.getvalue()
    return Photo(ref=item["ref"], view=view_of(item["ref"]), jpeg=jpeg,
                 original_sha256=hashlib.sha256(data).hexdigest(), sha256=hashlib.sha256(jpeg).hexdigest(),
                 width=img.width, height=img.height)
