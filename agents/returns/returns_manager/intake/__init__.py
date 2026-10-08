"""Intake module: photo pipeline, quality gate, and intake service (§9)."""

from returns_manager.intake.images import ProcessedImage, process_photo, sniff_mime, validate_photo_bytes
from returns_manager.intake.quality import QualityReport, QualityStatus, assess_photo_quality
from returns_manager.intake.retake import RetakeInstruction, guidance_for_issues
from returns_manager.intake.service import (
    IntakeService,
    ObservationRecord,
    PhotoRecord,
    PhotoUploadResult,
    ReturnDetails,
    ReturnRecord,
    SubmitResult,
)

__all__ = [
    "IntakeService",
    "ObservationRecord",
    "PhotoRecord",
    "PhotoUploadResult",
    "ProcessedImage",
    "QualityReport",
    "QualityStatus",
    "RetakeInstruction",
    "ReturnDetails",
    "ReturnRecord",
    "SubmitResult",
    "assess_photo_quality",
    "guidance_for_issues",
    "process_photo",
    "sniff_mime",
    "validate_photo_bytes",
]
