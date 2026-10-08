"""Eval data model (§21). One `EvaluatedUnit` per sealed unit: metadata, two independent
human labels, an adjudicated gold label, and the agent's own result (read from
`rm.inspection_results` / `rm.inspection_runs` for a real run, or hand-built for
`--dev-mini`). Every downstream computation (§21.1-21.9) is a pure function over a list of
these — nothing here touches the database directly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

IdentityMatch = Literal["yes", "no", "uncertain"]
Completeness = Literal["complete", "incomplete", "uncertain"]
Disposition = Literal["restock", "refurbish", "liquidate", "dispose"]
# §11 / llm/schemas.py UnitPresenceStatus. "The problem" (§21.1) is the item not being
# there: empty_packaging or non_product_contents; product_present is the non-problem case.
UnitPresence = Literal["product_present", "empty_packaging", "non_product_contents", "uncertain"]
UNIT_PRESENCE_EMPTY_VALUES: frozenset[str] = frozenset({"empty_packaging", "non_product_contents"})

# Best to worst. Source: reference/rubrics extraction (judgment/grading.py, extract.py);
# "uncertain" is not a grade, it is the absence of one, and is handled separately by
# callers (never included in this ordinal ladder itself).
CONDITION_GRADE_ORDER: tuple[str, ...] = (
    "New",
    "Used - Like New",
    "Used - Very Good",
    "Used - Good",
    "Used - Acceptable",
)

# §21.4: the fixed failure-mode taxonomy. A tag outside this set is a bug in the tagger,
# not a new category invented ad hoc.
FAILURE_MODES: frozenset[str] = frozenset(
    {
        "wrong_sku_similar_product",
        "barcode_failure",
        "ocr_failure",
        "visual_occlusion",
        "poor_lighting",
        "blur",
        "missing_angle",
        "accessory_not_visible",
        "false_missing_component",
        "condition_ambiguity",
        "overgrade",
        "undergrade",
        "policy_mismatch",
        "evidence_verdict_conflict",
        "wrong_disposition",
        "refusal",
        "schema_error",
    }
)

# §21.0: the 10 official scenarios, each needing >= 3 units in the sealed set.
OFFICIAL_SCENARIOS: tuple[str, ...] = tuple(f"S{i:02d}" for i in range(1, 11))


@dataclass(frozen=True)
class UnitMeta:
    """Sealing-time metadata used for coverage quotas (§21.0) and report slices."""

    unit_id: str
    scenario_codes: tuple[str, ...]
    lighting: str  # "normal" | "poor" | ...
    angle: str  # "square" | "oblique" | ...
    blur: str  # "none" | "slight" | ...
    ambiguity: str  # "clear" | "genuinely_ambiguous" | ...
    product_seen_in_dev: bool


@dataclass(frozen=True)
class HumanLabel:
    """One labeller's independent judgment for one unit (§8.10). No agent output inside."""

    labeller: str
    unit_presence: UnitPresence
    identity: IdentityMatch
    completeness: Completeness
    parts_missing: tuple[str, ...]
    condition: str  # a CONDITION_GRADE_ORDER value, or "uncertain"
    disposition: Disposition | None


@dataclass(frozen=True)
class GoldLabel:
    """The adjudicated label two humans (or a tie-break) agreed on for one unit."""

    unit_presence: UnitPresence
    identity: IdentityMatch
    completeness: Completeness
    parts_missing: tuple[str, ...]
    condition: str
    disposition: Disposition | None


@dataclass(frozen=True)
class AgentResult:
    """What the deterministic engine + judgment pipeline actually produced for one unit."""

    unit_presence: UnitPresence
    identity: IdentityMatch
    completeness: Completeness
    parts_missing: tuple[str, ...]
    condition: str  # a CONDITION_GRADE_ORDER value, or "uncertain"
    disposition: Disposition | None
    requires_review: bool
    uncertain_checks: tuple[str, ...]
    uncertainty_reasons: tuple[str, ...]
    latency_ms: int | None
    cost_usd: float | None


@dataclass(frozen=True)
class EvaluatedUnit:
    meta: UnitMeta
    human_a: HumanLabel
    human_b: HumanLabel
    gold: GoldLabel
    agent: AgentResult
    failure_mode: str | None = None  # filled by confusion.tag_failure_modes
    notes: str = ""  # a human writes this after reviewing a disagreement; never generated


@dataclass(frozen=True)
class RunManifest:
    """§18.4 + §21.8: what was requested, what was spent, so `eval run` is reproducible
    and auditable after the fact."""

    run_id: str
    dev_mini: bool
    units_requested: int
    units_evaluated: int
    seed: int
    spend_preflight: dict[str, object] = field(default_factory=dict)
    actual_cost_usd: float = 0.0
    actual_requests: int = 0
    started_at: str = ""
    completed_at: str = ""
