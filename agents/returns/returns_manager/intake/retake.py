"""Retake guidance generation (§9.3)."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Literal

IssueCode = Literal[
    "blur",
    "too_dark",
    "too_bright",
    "low_resolution",
    "near_duplicate_in_set",
    "possible_reused_photo",
    "decode_error",
    "unsupported_format",
]

_GATE_GUIDANCE: dict[str, str] = {
    "blur": "Hold the phone steady 20-30 cm from the item; tap to focus on the label.",
    "too_dark": "Increase lighting or move closer to a light source.",
    "too_bright": "Reduce glare or move away from direct reflections.",
    "low_resolution": "Capture with the main camera at full resolution without zooming.",
    "near_duplicate_in_set": (
        "Take photos from different angles (e.g., front, label closeup, open packaging)."
    ),
    "possible_reused_photo": "Ensure you are photographing the actual physical return in front of you.",
    "decode_error": "The photo could not be read. Please retake the photo.",
    "unsupported_format": "Please upload a photo in JPEG, PNG, WebP, or HEIC format.",
}


@dataclass(frozen=True)
class RetakeInstruction:
    target: str
    reason_code: str
    instruction: str
    source: Literal["gate", "model"] = "gate"

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


def guidance_for_issues(issues: list[str], target: str = "item") -> list[RetakeInstruction]:
    """Generate deterministic retake guidance instructions for quality gate issues."""
    guidance: list[RetakeInstruction] = []
    seen: set[str] = set()
    for code in issues:
        if code in seen:
            continue
        seen.add(code)
        instruction = _GATE_GUIDANCE.get(
            code, "Please retake the photo following the standard capture instructions."
        )
        guidance.append(
            RetakeInstruction(
                target=target,
                reason_code=code,
                instruction=instruction,
                source="gate",
            )
        )
    return guidance
