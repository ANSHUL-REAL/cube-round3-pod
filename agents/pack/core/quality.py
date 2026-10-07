"""Photo preparation and the local quality gate.

The gate runs before any model call. A confident verdict from an unusable photo is worse
than no verdict, and rejecting a bad photo here costs nothing.
"""

from __future__ import annotations

import hashlib
import io
import uuid
from dataclasses import dataclass

import cv2
import numpy as np
from PIL import Image, ImageOps

from .config import Settings
from .models import QualityReport

try:  # iPhone photos (HEIC). Optional: without it, HEIC uploads get a clear error.
    from pillow_heif import register_heif_opener

    register_heif_opener()
except ImportError:  # pragma: no cover
    pass


class ImageDecodeError(ValueError):
    pass


# Phones take 12-24 MP photos by default; 30 MP leaves room and keeps decoding within a small
# server's memory. Checked from the header, before any pixels are decoded, so a tiny file that
# claims to be 20000 x 20000 can't exhaust memory.
MAX_PIXELS = 30_000_000
TOO_LARGE = "This image is too large ({}). Send a normal phone photo."


@dataclass
class PreparedImage:
    image_id: str
    role: str
    jpeg: bytes  # upright, resized; stored as evidence and sent to the model
    sha256: str
    original_sha256: str
    width: int
    height: int
    quality: QualityReport
    mime: str = "image/jpeg"


def _load_upright(data: bytes) -> Image.Image:
    try:
        img = Image.open(io.BytesIO(data))
        w, h = img.size
    except Image.DecompressionBombError as exc:
        raise ImageDecodeError(TOO_LARGE.format("over 170 megapixels")) from exc
    except Exception as exc:  # Pillow raises many types for bad files (OSError, SyntaxError, ...)
        raise ImageDecodeError("Could not read this file as an image.") from exc
    if w * h > MAX_PIXELS:
        raise ImageDecodeError(TOO_LARGE.format(f"{w}x{h}"))
    if min(w, h) < 1:
        raise ImageDecodeError("Could not read this file as an image.")
    try:
        img = ImageOps.exif_transpose(img).convert("RGB")
    except Exception as exc:
        raise ImageDecodeError("Could not read this file as an image.") from exc
    img.info = {}  # drop EXIF (GPS), comments and other metadata before anything is saved
    return img


def _resize(img: Image.Image, max_side: int) -> Image.Image:
    w, h = img.size
    scale = max_side / max(w, h)
    if scale >= 1:
        return img
    return img.resize((max(1, round(w * scale)), max(1, round(h * scale))), Image.LANCZOS)


def to_jpeg(img: Image.Image, quality: int = 88) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality, optimize=True)
    return buf.getvalue()


def assess(rgb: np.ndarray, settings: Settings, original_size: tuple[int, int]) -> QualityReport:
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    # Measure blur at a fixed working size so the threshold means the same for every photo.
    h, w = gray.shape
    scale = 1024 / max(h, w)
    work = cv2.resize(gray, (max(1, round(w * scale)), max(1, round(h * scale))), interpolation=cv2.INTER_AREA)
    blur_var = float(cv2.Laplacian(work, cv2.CV_64F).var())
    mean_luma = float(gray.mean())
    clipped_pct = float((gray >= 250).mean() * 100)
    dark_pct = float((gray <= 20).mean() * 100)

    reasons = []
    ow, oh = original_size
    if min(ow, oh) < settings.min_side_px:
        reasons.append(f"Photo is too small ({ow}x{oh}). Use the phone camera at full resolution.")
    if blur_var < settings.blur_min_var:
        reasons.append("Photo looks blurry. Hold the phone steady and tap to focus.")
    if mean_luma < settings.luma_min or dark_pct > settings.max_dark_pct:
        reasons.append("Photo is too dark. Add light or move closer to a window.")
    if mean_luma > settings.luma_max:
        reasons.append("Photo is overexposed. Reduce direct light.")
    if clipped_pct > settings.max_clipped_pct:
        reasons.append("Strong glare. Tilt the phone slightly so shiny wrapping doesn't reflect.")

    return QualityReport(
        gate="FAIL" if reasons else "PASS",
        reasons=reasons,
        width=ow,
        height=oh,
        blur_var=round(blur_var, 1),
        mean_luma=round(mean_luma, 1),
        clipped_pct=round(clipped_pct, 2),
        dark_pct=round(dark_pct, 2),
    )


def prepare_photo(data: bytes, settings: Settings, role: str = "top_down") -> PreparedImage:
    original_sha = hashlib.sha256(data).hexdigest()
    img = _load_upright(data)
    original_size = img.size
    report = assess(np.asarray(img), settings, original_size)
    small = _resize(img, settings.send_max_side_px)
    jpeg = to_jpeg(small)
    return PreparedImage(
        image_id=str(uuid.uuid4()),
        role=role,
        jpeg=jpeg,
        sha256=hashlib.sha256(jpeg).hexdigest(),
        original_sha256=original_sha,
        width=small.size[0],
        height=small.size[1],
        quality=report,
    )


def prepare_reference(data: bytes, max_side: int) -> bytes:
    return to_jpeg(_resize(_load_upright(data), max_side), quality=85)
