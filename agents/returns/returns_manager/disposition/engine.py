"""The disposition engine (§12): a pure function, `decide(inputs) -> DispositionDecision`. No I/O, no model.

Order: Step 1 no-recommendation gates (first match wins) → Step 2 review flags (never blank the route) →
Step 3 route rules (first match wins). Review is a separate flag, never a fifth disposition; a `null`
recommendation always carries a reason and always requires review. Rule IDs, titles and sources follow §12.2.

Deliberate resolutions (build log, finding F-012):
- R09 applies only when the complete item's own route would be `refurbish` or better. Taken literally, R09
  would route an item with a missing replaceable part to `refurbish` while the same item complete goes to
  `liquidate` (R14), which breaks the §12.3 monotonicity invariant. Appendix A (headphones) is unaffected:
  complete opened electronics already route to `refurbish` via R11.
- A used grade in `restock_used_grades` with non-essential parts missing that its rubric text does not permit
  is handled by R14 (liquidate), instead of falling into the R99 rule gap.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Literal

from returns_manager.canonical.hashing import sha256_jcs

Route = Literal["restock", "refurbish", "liquidate", "dispose"]
ROUTE_RANK: dict[str, int] = {"restock": 3, "refurbish": 2, "liquidate": 1, "dispose": 0}
DECIDING_BLOCKERS = frozenset(
    {
        "essential_component_missing",
        "damaged_difficult_to_use",
        "not_clean",
        "category_new_only_opened",
        "consumable_used",
        "functional_test_required",
    }
)
REVIEW_FLAGS = (
    "injection_attempt_suspected",
    "audit_disagreement",
    "model_disagreement",
    "possible_reused_photo",
)
# Rubric grades whose text allows non-essential material to be missing ("…non-essential instructions may be
# missing…", Used - Acceptable).
GRADES_ALLOWING_NONESSENTIAL_MISSING = frozenset({"used_acceptable"})


@dataclass(frozen=True)
class MissingPart:
    component_id: str
    replaceable: bool | None


@dataclass(frozen=True)
class DispositionInputs:
    """Everything the engine may look at. Canonicalised and hashed into `inputs_sha256` (no floats)."""

    inspection_state: Literal["complete", "incomplete", "skipped"]
    skip_reason: str | None
    usable_photo_count: int
    unit_presence: str
    identity: Literal["yes", "no", "uncertain"]
    actual_sku: str | None
    completeness_status: Literal["complete", "incomplete", "uncertain"]
    essential_missing: tuple[MissingPart, ...]
    nonessential_missing: tuple[str, ...]
    essential_uncertain: tuple[MissingPart, ...]
    nonessential_uncertain: tuple[str, ...]
    cosmetic_grade: str | None
    listing_blockers: tuple[str, ...]
    blockers_undetermined: tuple[str, ...]
    max_severity: Literal["none", "minor", "moderate", "severe"]
    new_only: bool
    opened_item_route: Route
    damaged_item_route: Route
    list_price_minor: int
    currency: str
    recovery_rate_bp: tuple[tuple[str, int], ...]
    refurbish_cost_minor: int
    restock_used_grades: tuple[str, ...]
    refurbish_min_net_gain_minor: int
    dispose_max_salvage_minor: int
    high_value_threshold_minor: int
    auto_disposition_enabled: bool = True
    flags: tuple[str, ...] = ()
    escalation_disagreement_resolved_by_reviewer: bool = False

    def canonical(self) -> dict[str, object]:
        """JSON-shaped (lists, not tuples) for RFC 8785 hashing and for storage/simulation."""

        def listify(v: object) -> object:
            if isinstance(v, tuple | list):
                return [listify(x) for x in v]
            if isinstance(v, dict):
                return {k: listify(x) for k, x in v.items()}
            return v

        out = listify(asdict(self))
        assert isinstance(out, dict)
        return out

    @classmethod
    def from_canonical(cls, data: dict[str, object]) -> DispositionInputs:
        """Rebuild from stored canonical JSON (used by what-if simulation, §12.6)."""
        d = dict(data)
        for key in ("essential_missing", "essential_uncertain"):
            d[key] = tuple(MissingPart(**p) for p in d[key])  # type: ignore[attr-defined]
        d["recovery_rate_bp"] = tuple((str(k), int(v)) for k, v in d["recovery_rate_bp"])  # type: ignore[attr-defined]
        for key in (
            "nonessential_missing",
            "nonessential_uncertain",
            "listing_blockers",
            "blockers_undetermined",
            "restock_used_grades",
            "flags",
        ):
            d[key] = tuple(d[key])  # type: ignore[arg-type]
        return cls(**d)  # type: ignore[arg-type]

    def sha256(self) -> str:
        return sha256_jcs(self.canonical())

    def recovery_minor(self, rate_key: str) -> int:
        bp = dict(self.recovery_rate_bp).get(rate_key, 0)
        return self.list_price_minor * bp // 10000


@dataclass(frozen=True)
class DispositionDecision:
    recommended_disposition: Route | None
    no_recommendation_reason: str | None
    provisional: bool
    assumptions: tuple[str, ...]
    requires_review: bool
    review_reasons: tuple[str, ...]
    listing_condition: str | None
    rule_id: str
    rules_version: str
    reasons: tuple[str, ...]
    requires_signoff: bool
    signoff_reasons: tuple[str, ...]
    expected_recovery_minor: tuple[tuple[str, int], ...]
    inputs_sha256: str
    decided_by: Literal["deterministic_engine"] = "deterministic_engine"
    synthetic_values: bool = True
    currency: str = "INR"
    extra: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class _Route:
    rule_id: str
    route: Route | None
    listing_condition: str | None
    reason: str


def _net_gain_refurbish(inp: DispositionInputs) -> int:
    return inp.recovery_minor("refurbish") - inp.refurbish_cost_minor - inp.recovery_minor("liquidate")


def _salvage_route(inp: DispositionInputs, rule_id: str, why: str) -> _Route:
    """Liquidate when the salvage value justifies it, otherwise dispose."""
    salvage = inp.recovery_minor("liquidate")
    if salvage > inp.dispose_max_salvage_minor:
        return _Route(
            rule_id, "liquidate", None, f"{why}; salvage {salvage} > {inp.dispose_max_salvage_minor}"
        )
    return _Route(rule_id, "dispose", None, f"{why}; salvage {salvage} <= {inp.dispose_max_salvage_minor}")


def _route(
    inp: DispositionInputs,
    blockers: frozenset[str],
    essential_missing: tuple[MissingPart, ...],
) -> _Route:
    grade = inp.cosmetic_grade
    damaged = bool(blockers & {"damaged_difficult_to_use", "not_clean"}) or inp.max_severity in (
        "moderate",
        "severe",
    )

    if inp.new_only:
        if grade == "new" and not blockers and not inp.blockers_undetermined:
            return _Route("R06", "restock", "new", "New-only category, factory-sealed and intact")
        policy_route = inp.damaged_item_route if damaged else inp.opened_item_route
        which = "damaged_item_route" if damaged else "opened_item_route"
        if policy_route in ("restock", "refurbish"):
            return _Route(
                "R99", None, None, f"policy {which}={policy_route} is not allowed for a New-only category"
            )
        if policy_route == "liquidate":
            return _salvage_route(inp, "R07", f"New-only category, not sealed-new; policy {which}=liquidate")
        return _Route("R07", "dispose", None, f"New-only category, not sealed-new; policy {which}=dispose")

    if "consumable_used" in blockers:
        return _Route("R08", "dispose", None, "consumable item shows use")

    if essential_missing:
        replaceable = all(p.replaceable is True for p in essential_missing)
        gain = _net_gain_refurbish(inp)
        if replaceable and gain >= inp.refurbish_min_net_gain_minor:
            base = _route(inp, blockers - {"essential_component_missing"}, ())
            if base.route is not None and ROUTE_RANK[base.route] >= ROUTE_RANK["refurbish"]:
                return _Route(
                    "R09", "refurbish", None, f"replaceable essential part(s) missing; net gain {gain}"
                )
        return _salvage_route(inp, "R10", "essential part(s) missing and not refurbishable")

    if blockers & {"damaged_difficult_to_use", "not_clean"}:
        return _salvage_route(inp, "R10", "damage or dirt makes the item unlistable as-is")

    if "functional_test_required" in blockers:
        gain = _net_gain_refurbish(inp)
        if gain >= inp.refurbish_min_net_gain_minor:
            return _Route(
                "R11", "refurbish", None, f"used electrical item needs a functional test; net gain {gain}"
            )
        return _Route("R11", "liquidate", None, f"functional test needed but net gain {gain} too small")

    if grade == "new" and not blockers:
        return _Route("R12", "restock", "new", "factory-sealed and intact")

    if grade in inp.restock_used_grades:
        if inp.completeness_status == "complete":
            return _Route("R13", "restock", grade, f"used grade {grade} is restockable by policy")
        if (
            inp.completeness_status == "incomplete"
            and not essential_missing
            and grade in GRADES_ALLOWING_NONESSENTIAL_MISSING
        ):
            return _Route(
                "R13", "restock", grade, f"{grade}: rubric allows non-essential material to be missing"
            )
        if inp.completeness_status == "incomplete" and not essential_missing:
            return _Route(
                "R14", "liquidate", None, f"non-essential parts missing; not permitted at grade {grade}"
            )

    if grade is not None and grade != "new" and grade not in inp.restock_used_grades:
        return _Route(
            "R14", "liquidate", None, f"used grade {grade} is outside restock_used_grades (business policy)"
        )

    return _Route("R99", None, None, "rule_gap")


def decide(inp: DispositionInputs, rules_version: str) -> DispositionDecision:
    inputs_sha = inp.sha256()
    review: list[str] = []
    assumptions: list[str] = []

    # ── Step 2 flags (computed first so a gated decision still lists them) ──
    if not inp.auto_disposition_enabled:
        review.append("assisted_mode")  # R00
    review += [f for f in REVIEW_FLAGS if f in inp.flags]  # R04
    provisional = bool(inp.essential_uncertain) and inp.inspection_state == "complete"
    if provisional:  # R05
        review.append("essential_component_uncertain")
        assumptions.append(
            "uncertain essential components are treated as missing: "
            + ", ".join(p.component_id for p in inp.essential_uncertain)
        )
    if inp.nonessential_uncertain:  # R05c
        review.append("nonessential_component_uncertain")
    if inp.blockers_undetermined:  # R05c
        review.append("listing_blockers_undetermined")

    # ── Step 1 gates: no honest recommendation possible ─────────────────────
    gate: tuple[str, str] | None = None
    if inp.inspection_state == "skipped":
        gate = ("R01b", inp.skip_reason or "no_product_reference")
    elif inp.inspection_state != "complete" or inp.usable_photo_count == 0:
        gate = ("R01", "inspection_incomplete")
    elif inp.unit_presence != "product_present":
        gate = ("R02", "item_not_present_or_unverified")
    elif inp.identity == "no":
        gate = ("R03", "wrong_item_returned")
    elif inp.identity == "uncertain":
        gate = ("R03b", "identity_unverified")

    essential_missing = inp.essential_missing + (inp.essential_uncertain if provisional else ())
    blockers = frozenset(inp.listing_blockers) | (
        {"essential_component_missing"} if essential_missing else frozenset()
    )
    if gate is None and inp.cosmetic_grade is None and not (blockers & DECIDING_BLOCKERS):
        gate = ("R05b", "condition_uncertain")

    if gate is not None:
        rule_id, reason = gate
        return DispositionDecision(
            recommended_disposition=None,
            no_recommendation_reason=reason,
            provisional=False,
            assumptions=(),
            requires_review=True,
            review_reasons=tuple(dict.fromkeys([reason, *review])),
            listing_condition=None,
            rule_id=rule_id,
            rules_version=rules_version,
            reasons=(reason,)
            + ((f"actual_sku:{inp.actual_sku}",) if rule_id == "R03" and inp.actual_sku else ()),
            requires_signoff=False,
            signoff_reasons=(),
            expected_recovery_minor=_expected(inp),
            inputs_sha256=inputs_sha,
            currency=inp.currency,
        )

    r = _route(inp, blockers, essential_missing)
    if r.route is None:
        review.append("rule_gap" if r.reason == "rule_gap" else "policy_conflict")

    signoff: list[str] = []
    if r.route == "dispose":
        signoff.append("S01_dispose_always")
    if r.route not in (None, "restock") and inp.list_price_minor >= inp.high_value_threshold_minor:
        signoff.append("S02_high_value")
    if inp.escalation_disagreement_resolved_by_reviewer:
        signoff.append("S03_escalation_disagreement_resolved")

    return DispositionDecision(
        recommended_disposition=r.route,
        no_recommendation_reason=None if r.route is not None else r.reason,
        provisional=provisional,
        assumptions=tuple(assumptions),
        requires_review=bool(review) or r.route is None or provisional,
        review_reasons=tuple(dict.fromkeys(review)),
        listing_condition=r.listing_condition,
        rule_id=r.rule_id,
        rules_version=rules_version,
        reasons=(r.reason,),
        requires_signoff=bool(signoff),
        signoff_reasons=tuple(signoff),
        expected_recovery_minor=_expected(inp),
        inputs_sha256=inputs_sha,
        currency=inp.currency,
    )


def _expected(inp: DispositionInputs) -> tuple[tuple[str, int], ...]:
    """Expected recovery per route (synthetic value data; labelled as such everywhere it is shown)."""
    return (
        ("restock_new", inp.recovery_minor("restock_new")),
        ("restock_used", inp.recovery_minor("restock_used")),
        ("refurbish", inp.recovery_minor("refurbish") - inp.refurbish_cost_minor),
        ("liquidate", inp.recovery_minor("liquidate")),
        ("dispose", 0),
    )
