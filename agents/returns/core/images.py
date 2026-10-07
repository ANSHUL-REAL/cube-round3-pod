"""Photo handling and the deterministic quality gate for return photos.

Adapted from the Round 2 `intake/images.py` (safe decoding, MIME sniffing, EXIF-aware downscale) and
`intake/quality.py` (sharpness, exposure and resolution checks, section 9.2), plus `vision/barcode.py` (zxing-cpp).
Check functions `evaluate_sharpness`, `evaluate_exposure` and `evaluate_resolution` are the Round 2 bodies.

Round 3 changes:
- no perceptual hashes, so no near-duplicate or cross-return photo-reuse check (those needed the Round 2 database);
- the quality thresholds are read from agents/returns/reference/quality/quality-gate.yaml, hash-checked. Round 2 states
  these are NOT calibrated placeholders (see that file); nothing here changes that;
- no retake-guidance text: the model's own retake requests (consistency rule C13) are kept instead;
- the barcode decoder is optional. If zxing-cpp is not installed no barcode is decoded, and identity then needs two
  matching product-body features (the conservative branch of the identity table).
"""
from __future__ import annotations

import hashlib
import io
from dataclasses import dataclass
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import cv2
import numpy as np
import yaml
from PIL import Image, ImageOps

from .errors import VerificationFailed
from .models import QualityGateV1
from .paths import REFERENCE_DIR
from .refhash import verify_reference_content_sha256

try:  # pillow-heif is in requirements; tolerate its absence rather than fail the import
    import pillow_heif

    pillow_heif.register_heif_opener()
except Exception:  # pragma: no cover
    pillow_heif = None

try:
    import zxingcpp
except Exception:  # pragma: no cover
    zxingcpp = None

# Safety cap against decompression bombs (Round 2, section 9.1)
Image.MAX_IMAGE_PIXELS = 60_000_000
MAX_PHOTO_BYTES = 20 * 1024 * 1024
QUALITY_GATE_FILE = REFERENCE_DIR / "quality" / "quality-gate.yaml"


class ImageDecodeError(ValueError):
    """The capture is not a photo this agent can read (wrong format, too big, corrupt, animated)."""


# ---------------------------------------------------------------- decoding
def sniff_mime(data: bytes) -> str:
    """Image type from magic bytes. A declared type or file extension is never trusted."""
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if len(data) >= 12 and data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    if len(data) >= 12 and data[4:8] == b"ftyp" and data[8:12] in (b"heic", b"heix", b"hevc", b"heim", b"heis", b"mif1", b"msf1"):
        return "image/heic"
    raise ImageDecodeError("unsupported image format (allowed: JPEG, PNG, WebP, HEIC)")


@dataclass(frozen=True)
class ProcessedPhoto:
    mime: str
    sha256_original: str
    original_width: int
    original_height: int
    analysis_bytes: bytes
    analysis_width: int
    analysis_height: int
    sha256_analysis: str
    quality: "QualityReport"
    barcodes: tuple[tuple[str, str], ...]  # (text, format)


def process_photo(data: bytes, *, max_long_edge: int = 1568, jpeg_quality: int = 88) -> ProcessedPhoto:
    """Decode safely, respect EXIF orientation, make the downscaled JPEG the model sees, assess its quality."""
    if len(data) > MAX_PHOTO_BYTES:
        raise ImageDecodeError(f"photo is {len(data)} bytes, over the 20 MB cap")
    mime = sniff_mime(data)
    try:
        raw = Image.open(io.BytesIO(data))
        if getattr(raw, "is_animated", False) or getattr(raw, "n_frames", 1) > 1:
            raise ImageDecodeError("multi-frame or animated images are not allowed")
        orig_w, orig_h = raw.size
        rgb = (ImageOps.exif_transpose(raw) or raw).convert("RGB")
    except ImageDecodeError:
        raise
    except Exception as exc:
        raise ImageDecodeError(f"image could not be decoded: {exc}") from exc

    long_edge = max(rgb.width, rgb.height)
    if long_edge > max_long_edge:
        scale = max_long_edge / float(long_edge)
        analysis = rgb.resize((max(1, round(rgb.width * scale)), max(1, round(rgb.height * scale))), Image.Resampling.LANCZOS)
    else:
        analysis = rgb
    buf = io.BytesIO()
    analysis.save(buf, format="JPEG", quality=jpeg_quality, optimize=True)  # metadata stripped
    analysis_bytes = buf.getvalue()
    return ProcessedPhoto(
        mime=mime, sha256_original=hashlib.sha256(data).hexdigest(), original_width=orig_w, original_height=orig_h,
        analysis_bytes=analysis_bytes, analysis_width=analysis.width, analysis_height=analysis.height,
        sha256_analysis=hashlib.sha256(analysis_bytes).hexdigest(),
        quality=assess_quality(rgb, orig_w, orig_h), barcodes=read_barcodes(rgb),
    )


def read_barcodes(image: Image.Image) -> tuple[tuple[str, str], ...]:
    """Barcodes decoded by code (zxing-cpp), as (text, format). Empty if the decoder is absent or finds none."""
    if zxingcpp is None:
        return ()
    try:
        return tuple((str(b.text), str(b.format)) for b in zxingcpp.read_barcodes(image) if b.text)
    except Exception:
        return ()


def barcode_decoder_available() -> bool:
    return zxingcpp is not None


# ---------------------------------------------------------------- quality gate
@dataclass(frozen=True)
class GateThresholds:
    version: str
    calibrated: bool
    sharpness_long_edge_px: int
    sharpness_pass_min: float
    sharpness_fail_below: float
    mean_fail_below: float
    mean_fail_above: float
    mean_warn_below: float
    mean_warn_above: float
    clipped_warn_above: float
    clipped_fail_above: float
    crushed_warn_above: float
    crushed_fail_above: float
    short_edge_pass_min: int
    short_edge_fail_below: int


@lru_cache(maxsize=4)
def load_thresholds(path: Path = QUALITY_GATE_FILE) -> GateThresholds:
    """Load the quality-gate config. A missing key or a bad content hash is an error, never a silent default."""
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not verify_reference_content_sha256(doc):
        raise VerificationFailed(f"{path.name}: content_sha256 does not match the file")
    gate = QualityGateV1.model_validate(doc)
    t = gate.thresholds

    def num(key: str) -> float:
        return float(Decimal(str(t[key])))

    return GateThresholds(
        version=gate.version, calibrated=gate.calibrated_at not in ("", "not calibrated"),
        sharpness_long_edge_px=int(t["sharpness_long_edge_px"]),
        sharpness_pass_min=num("sharpness_laplacian_var_pass_min"),
        sharpness_fail_below=num("sharpness_laplacian_var_fail_below"),
        mean_fail_below=num("exposure_mean_fail_below"), mean_fail_above=num("exposure_mean_fail_above"),
        mean_warn_below=num("exposure_mean_warn_below"), mean_warn_above=num("exposure_mean_warn_above"),
        clipped_warn_above=num("exposure_clipped_pct_warn_above"), clipped_fail_above=num("exposure_clipped_pct_fail_above"),
        crushed_warn_above=num("exposure_crushed_pct_warn_above"), crushed_fail_above=num("exposure_crushed_pct_fail_above"),
        short_edge_pass_min=int(t["resolution_short_edge_pass_min"]), short_edge_fail_below=int(t["resolution_short_edge_fail_below"]),
    )


QualityStatus = Literal["pass", "warn", "fail"]


@dataclass(frozen=True)
class CheckResult:
    status: QualityStatus
    details: dict[str, Any]
    issues: list[str]


@dataclass(frozen=True)
class QualityReport:
    status: QualityStatus
    issues: list[str]
    metrics: dict[str, Any]


def evaluate_sharpness(img_gray_1024: np.ndarray, th: GateThresholds | None = None) -> CheckResult:
    """Sharpness via variance of the Laplacian on a 1024 px long-edge grayscale copy."""
    th = th or load_thresholds()
    variance = float(cv2.Laplacian(img_gray_1024, cv2.CV_64F).var())
    score = round(variance, 2)
    if variance < th.sharpness_fail_below:
        return CheckResult(status="fail", details={"variance": score}, issues=["blur"])
    if variance < th.sharpness_pass_min:
        return CheckResult(status="warn", details={"variance": score}, issues=["blur"])
    return CheckResult(status="pass", details={"variance": score}, issues=[])


def evaluate_exposure(img_gray: np.ndarray, th: GateThresholds | None = None) -> CheckResult:
    """Exposure: mean luminance (0-255), clipped highlights (>=250), crushed shadows (<=5)."""
    th = th or load_thresholds()
    mean_lum = float(np.mean(img_gray))
    clipped_high_pct = float(np.mean(img_gray >= 250) * 100.0)
    crushed_shadow_pct = float(np.mean(img_gray <= 5) * 100.0)
    details = {"mean_luminance": round(mean_lum, 2), "clipped_highlights_pct": round(clipped_high_pct, 2),
               "crushed_shadows_pct": round(crushed_shadow_pct, 2)}
    if mean_lum < th.mean_fail_below or crushed_shadow_pct > th.crushed_fail_above:
        return CheckResult(status="fail", details=details, issues=["too_dark"])
    if mean_lum > th.mean_fail_above or clipped_high_pct > th.clipped_fail_above:
        return CheckResult(status="fail", details=details, issues=["too_bright"])
    issues: list[str] = []
    if clipped_high_pct > th.clipped_warn_above or mean_lum > th.mean_warn_above:
        issues.append("too_bright")
    if crushed_shadow_pct > th.crushed_warn_above or mean_lum < th.mean_warn_below:
        issues.append("too_dark")
    if issues:
        return CheckResult(status="warn", details=details, issues=issues)
    return CheckResult(status="pass", details=details, issues=[])


def evaluate_resolution(orig_width: int, orig_height: int, th: GateThresholds | None = None) -> CheckResult:
    """Resolution from the short edge of the original image."""
    th = th or load_thresholds()
    short_edge = min(orig_width, orig_height)
    details = {"short_edge": short_edge, "width": orig_width, "height": orig_height}
    if short_edge < th.short_edge_fail_below:
        return CheckResult(status="fail", details=details, issues=["low_resolution"])
    if short_edge < th.short_edge_pass_min:
        return CheckResult(status="warn", details=details, issues=["low_resolution"])
    return CheckResult(status="pass", details=details, issues=[])


def assess_quality(rgb_img: Image.Image, orig_width: int, orig_height: int) -> QualityReport:
    """Run the deterministic checks (Round 2, section 9.2) on a decoded RGB image."""
    th = load_thresholds()
    long_edge = max(rgb_img.width, rgb_img.height)
    if long_edge > th.sharpness_long_edge_px:
        scale = th.sharpness_long_edge_px / float(long_edge)
        small = rgb_img.resize((max(1, round(rgb_img.width * scale)), max(1, round(rgb_img.height * scale))),
                               Image.Resampling.LANCZOS)
    else:
        small = rgb_img
    results = (evaluate_sharpness(np.array(small.convert("L")), th),
               evaluate_exposure(np.array(rgb_img.convert("L")), th),
               evaluate_resolution(orig_width, orig_height, th))
    issues: list[str] = []
    for res in results:
        issues += [i for i in res.issues if i not in issues]
    statuses = [r.status for r in results]
    status: QualityStatus = "fail" if "fail" in statuses else ("warn" if "warn" in statuses else "pass")
    return QualityReport(
        status=status, issues=issues,
        metrics={"quality_gate_version": th.version, "quality_gate_calibrated": th.calibrated,
                 "sharpness": results[0].details, "exposure": results[1].details, "resolution": results[2].details})

