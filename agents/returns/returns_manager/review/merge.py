"""Pure, table-testable P9 escalation and audit rules (§11.12, §11.13)."""

from __future__ import annotations

import dataclasses
import hashlib
from typing import Any


@dataclasses.dataclass(frozen=True)
class MergeOutcome:
    value: Any
    outcome: str
    requires_review: bool


def merge_area(primary: Any, escalation: Any, *, uncertain: Any = "uncertain") -> MergeOutcome:
    """Apply the four-row escalation merge table without hidden confidence thresholds."""
    if escalation == uncertain:
        return MergeOutcome(uncertain, "uncertain", True)
    if primary == uncertain:
        return MergeOutcome(escalation, "resolved_by_escalation", False)
    if primary == escalation:
        return MergeOutcome(primary, "kept_primary", False)
    return MergeOutcome(uncertain, "model_disagreement", True)


def audit_sampled(return_id: str, sample_rate: float, *, eval_run: bool = False) -> bool:
    """Stable SHA-256 sampling; eval runs are always audited and do not use randomness."""
    if eval_run:
        return True
    if sample_rate <= 0:
        return False
    if sample_rate >= 1:
        return True
    bucket = int.from_bytes(hashlib.sha256(return_id.encode("utf-8")).digest()[:8], "big") / 2**64
    return bucket < sample_rate


def audit_disagreements(primary: dict[str, Any], audit: dict[str, Any]) -> tuple[str, ...]:
    """Flag only P9's declared fields; audit is advisory and never mutates a record."""
    out: list[str] = []
    for key in ("identity_match", "completeness_status", "unit_presence"):
        if primary.get(key) != audit.get(key):
            out.append(key)
    grades = ["new", "used_like_new", "used_very_good", "used_good", "used_acceptable"]
    a, b = primary.get("cosmetic_grade"), audit.get("cosmetic_grade")
    far_apart = a in grades and b in grades and abs(grades.index(a) - grades.index(b)) > 1
    if far_apart or (a is None) != (b is None):
        out.append("cosmetic_grade")
    return tuple(out)
