"""Quality gate evaluation (§9.2).

Deterministic checks against the thresholds in `reference/quality/quality-gate.yaml` (to be calibrated on
dev fixtures, never eval data; the file states its calibration status):
- Sharpness: Laplacian variance on resolution-normalized grayscale (1024px long edge)
- Exposure: Mean luminance, clipped highlights (>=250), crushed shadows (<=5)
- Resolution: Short edge of EXIF-transposed image
- Near-duplicate: Hamming distance of perceptual hash against other photos in this return
- Reused photo: Hamming distance against other photos in the org from the last 30 days
"""

from __future__ import annotations

import io
from dataclasses import asdict, dataclass
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import cv2
import numpy as np
import yaml
from PIL import Image

from returns_manager.config import REPO_ROOT
from returns_manager.intake.retake import guidance_for_issues
from returns_manager.reference.models import QualityGateV1

QUALITY_GATE_FILE = REPO_ROOT / "reference" / "quality" / "quality-gate.yaml"


@dataclass(frozen=True)
class GateThresholds:
    version: str
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
    near_dup_flag_at_or_below: int
    near_dup_ok_above: int
    reused_at_or_below: int
    reused_window_days: int


@lru_cache(maxsize=4)
def load_thresholds(path: Path = QUALITY_GATE_FILE) -> GateThresholds:
    """Load and validate the quality-gate config (§8.7). A missing key is an error, never a silent default."""
    doc = QualityGateV1.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
    t = doc.thresholds

    def num(key: str) -> float:
        return float(Decimal(str(t[key])))

    return GateThresholds(
        version=doc.version,
        sharpness_long_edge_px=int(t["sharpness_long_edge_px"]),
        sharpness_pass_min=num("sharpness_laplacian_var_pass_min"),
        sharpness_fail_below=num("sharpness_laplacian_var_fail_below"),
        mean_fail_below=num("exposure_mean_fail_below"),
        mean_fail_above=num("exposure_mean_fail_above"),
        mean_warn_below=num("exposure_mean_warn_below"),
        mean_warn_above=num("exposure_mean_warn_above"),
        clipped_warn_above=num("exposure_clipped_pct_warn_above"),
        clipped_fail_above=num("exposure_clipped_pct_fail_above"),
        crushed_warn_above=num("exposure_crushed_pct_warn_above"),
        crushed_fail_above=num("exposure_crushed_pct_fail_above"),
        short_edge_pass_min=int(t["resolution_short_edge_pass_min"]),
        short_edge_fail_below=int(t["resolution_short_edge_fail_below"]),
        near_dup_flag_at_or_below=int(t["near_duplicate_hamming_flag_at_or_below"]),
        near_dup_ok_above=int(t["near_duplicate_hamming_ok_above"]),
        reused_at_or_below=int(t["reused_photo_hamming_at_or_below"]),
        reused_window_days=int(t["reused_photo_window_days"]),
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
    guidance: list[dict[str, str]]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def evaluate_sharpness(img_gray_1024: np.ndarray, th: GateThresholds | None = None) -> CheckResult:
    """Evaluate sharpness via variance of Laplacian on 1024px long-edge grayscale."""
    th = th or load_thresholds()
    lap = cv2.Laplacian(img_gray_1024, cv2.CV_64F)
    variance = float(lap.var())
    score = round(variance, 2)

    if variance < th.sharpness_fail_below:
        return CheckResult(status="fail", details={"variance": score}, issues=["blur"])
    if variance < th.sharpness_pass_min:
        return CheckResult(status="warn", details={"variance": score}, issues=["blur"])
    return CheckResult(status="pass", details={"variance": score}, issues=[])


def evaluate_exposure(img_gray: np.ndarray, th: GateThresholds | None = None) -> CheckResult:
    """Evaluate exposure: mean luminance (0-255), clipped highlights (>=250), crushed shadows (<=5)."""
    th = th or load_thresholds()
    mean_lum = float(np.mean(img_gray))
    clipped_high_pct = float(np.mean(img_gray >= 250) * 100.0)
    crushed_shadow_pct = float(np.mean(img_gray <= 5) * 100.0)

    details = {
        "mean_luminance": round(mean_lum, 2),
        "clipped_highlights_pct": round(clipped_high_pct, 2),
        "crushed_shadows_pct": round(crushed_shadow_pct, 2),
    }

    # Fail conditions
    if mean_lum < th.mean_fail_below or crushed_shadow_pct > th.crushed_fail_above:
        return CheckResult(status="fail", details=details, issues=["too_dark"])
    if mean_lum > th.mean_fail_above or clipped_high_pct > th.clipped_fail_above:
        return CheckResult(status="fail", details=details, issues=["too_bright"])

    # Warn conditions
    issues: list[str] = []
    if clipped_high_pct > th.clipped_warn_above or mean_lum > th.mean_warn_above:
        issues.append("too_bright")
    if crushed_shadow_pct > th.crushed_warn_above or mean_lum < th.mean_warn_below:
        issues.append("too_dark")

    if issues:
        return CheckResult(status="warn", details=details, issues=issues)
    return CheckResult(status="pass", details=details, issues=[])


def evaluate_resolution(orig_width: int, orig_height: int, th: GateThresholds | None = None) -> CheckResult:
    """Evaluate resolution using short edge of original EXIF-transposed image."""
    th = th or load_thresholds()
    short_edge = min(orig_width, orig_height)
    details = {"short_edge": short_edge, "width": orig_width, "height": orig_height}

    if short_edge < th.short_edge_fail_below:
        return CheckResult(status="fail", details=details, issues=["low_resolution"])
    if short_edge < th.short_edge_pass_min:
        return CheckResult(status="warn", details=details, issues=["low_resolution"])
    return CheckResult(status="pass", details=details, issues=[])


def evaluate_near_duplicate(
    current_phash: str, other_phashes_in_set: list[str], th: GateThresholds | None = None
) -> CheckResult:
    """Evaluate Hamming distance against other photos in this return set."""
    th = th or load_thresholds()
    if not other_phashes_in_set:
        return CheckResult(status="pass", details={"min_hamming_distance": None}, issues=[])

    c_int = int(current_phash, 2)
    min_dist = min((c_int ^ int(p, 2)).bit_count() for p in other_phashes_in_set)
    details = {"min_hamming_distance": min_dist}

    if min_dist <= th.near_dup_flag_at_or_below:
        return CheckResult(status="warn", details=details, issues=["near_duplicate_in_set"])
    if min_dist <= th.near_dup_ok_above:
        return CheckResult(status="warn", details=details, issues=[])
    return CheckResult(status="pass", details=details, issues=[])


def evaluate_reused_photo(
    current_phash: str, org_recent_phashes: list[str], th: GateThresholds | None = None
) -> CheckResult:
    """Flag possible reused photos across the org's recent returns (window and distance from config)."""
    th = th or load_thresholds()
    if not org_recent_phashes:
        return CheckResult(status="pass", details={"min_hamming_distance": None, "reused": False}, issues=[])

    c_int = int(current_phash, 2)
    min_dist = min((c_int ^ int(p, 2)).bit_count() for p in org_recent_phashes)
    reused = min_dist <= th.reused_at_or_below
    details = {"min_hamming_distance": min_dist, "reused": reused}

    if reused:
        # Integrity signal for reviewer; never auto-reject per §9.2
        return CheckResult(status="warn", details=details, issues=["possible_reused_photo"])
    return CheckResult(status="pass", details=details, issues=[])


def assess_photo_quality(
    image_bytes: bytes,
    phash: str,
    orig_width: int,
    orig_height: int,
    other_set_phashes: list[str] | None = None,
    org_recent_phashes: list[str] | None = None,
) -> QualityReport:
    """Run all deterministic quality checks on a photo (§9.2)."""
    th = load_thresholds()
    other_set = other_set_phashes or []
    recent_org = org_recent_phashes or []

    # Safe PIL decode to numpy grayscale
    with Image.open(io.BytesIO(image_bytes)) as pil_img:
        rgb_img = pil_img.convert("RGB")

    # Resolution normalized 1024px long edge for sharpness
    long_edge = max(rgb_img.width, rgb_img.height)
    if long_edge > th.sharpness_long_edge_px:
        scale = th.sharpness_long_edge_px / float(long_edge)
        target_w = max(1, round(rgb_img.width * scale))
        target_h = max(1, round(rgb_img.height * scale))
        img_1024 = rgb_img.resize((target_w, target_h), Image.Resampling.LANCZOS)
    else:
        img_1024 = rgb_img

    gray_1024 = np.array(img_1024.convert("L"))
    gray_full = np.array(rgb_img.convert("L"))

    # Run checks
    sharpness_res = evaluate_sharpness(gray_1024, th)
    exposure_res = evaluate_exposure(gray_full, th)
    resolution_res = evaluate_resolution(orig_width, orig_height, th)
    duplicate_res = evaluate_near_duplicate(phash, other_set, th)
    reused_res = evaluate_reused_photo(phash, recent_org, th)

    all_issues: list[str] = []
    statuses: list[QualityStatus] = [
        sharpness_res.status,
        exposure_res.status,
        resolution_res.status,
        duplicate_res.status,
        reused_res.status,
    ]

    for res in (sharpness_res, exposure_res, resolution_res, duplicate_res, reused_res):
        for issue in res.issues:
            if issue not in all_issues:
                all_issues.append(issue)

    if "fail" in statuses:
        overall_status: QualityStatus = "fail"
    elif "warn" in statuses:
        overall_status = "warn"
    else:
        overall_status = "pass"

    guidance = [g.to_dict() for g in guidance_for_issues(all_issues)]

    metrics: dict[str, Any] = {
        "quality_gate_version": th.version,
        "sharpness": sharpness_res.details,
        "exposure": exposure_res.details,
        "resolution": resolution_res.details,
        "near_duplicate": duplicate_res.details,
        "reused_photo": reused_res.details,
    }

    return QualityReport(
        status=overall_status,
        issues=all_issues,
        metrics=metrics,
        guidance=guidance,
    )
