"""One position per fee line. Deterministic: no model decides anything here.

The condition every charge is judged against is "this charge is supported by evidence":

    CONTRADICTED / DUPLICATE   evidence contradicts the charge      position CONTRADICTS   check FAIL       claim
    SUPPORTED / ALREADY_REIMBURSED / CREDIT                         position SUPPORTS      check PASS       no claim
    SILENT                     evidence is absent or insufficient   position SILENT        check UNCERTAIN  never a claim

Precision first: every rule below that can produce a claim also has the guards that stop it. A guard that fires
costs a missed claim (money). A missing guard costs a wrong claim (standing).

Not encoded anywhere: Amazon's policies, fee schedules or tier tables. The only fee schedule that can be used is one
the operator supplies (`RECOVERY_TIER_TABLE`) with its source and retrieval date; the repository ships none.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

from .fees import FeeLine
from .policy import POLICY, Policy
from .upstream import Evidence, Upstream, ref_conflicts

POSITION = {"CONTRADICTED": "CONTRADICTS", "DUPLICATE": "CONTRADICTS", "SUPPORTED": "SUPPORTS",
            "ALREADY_REIMBURSED": "SUPPORTS", "CREDIT": "SUPPORTS", "SILENT": "SILENT"}
VERDICT = {"CONTRADICTS": "FAIL", "SUPPORTS": "PASS", "SILENT": "UNCERTAIN"}
CENT = Decimal("0.01")
# Receiving checks that can say "this unit is defective".
DEFECT_CHECKS = ("carton_damage", "unit_damage", "quality_flags")


@dataclass
class Position:
    assessment: str
    detail: str
    codes: list[str] = field(default_factory=list)
    refs: list[str] = field(default_factory=list)          # upstream record ids consulted (for a claim: relied on)
    basis: list[dict] = field(default_factory=list)        # record, check, verdicts, and the role it played
    fee_refs: list[str] = field(default_factory=list)      # other fee-report rows this rests on (duplicates, credits)
    claim: Decimal = Decimal("0")
    conflict: bool = False                                  # a person must look (records disagree, or no Receiving)
    uncertain_reason: str = "insufficient_evidence"
    underlying: str | None = None                           # what the evidence said before the amount guard
    settle: str | None = None                               # what would turn this into a decision

    @property
    def position(self) -> str:
        return POSITION[self.assessment]

    @property
    def verdict(self) -> str:
        return VERDICT[self.position]


def silent(detail: str, *codes: str, settle: str | None = None, refs=(), basis=(), conflict=False,
           reason: str | None = None) -> Position:
    return Position("SILENT", detail, list(codes), list(refs), list(basis), conflict=conflict, settle=settle,
                    uncertain_reason=reason or ("conflicting_evidence" if conflict else "insufficient_evidence"))


def _basis(up: Upstream, role: str, check: str = "decision", verdict: str | None = None) -> dict:
    if check == "decision":
        original = up.original
    else:
        original = (up.check(check) or {}).get("verdict")
    return {"record_id": up.record_id, "stage": up.stage, "check_key": check, "role": role, "verdict": original,
            "effective_verdict": verdict or up.verdict, "overridden": up.overridden,
            "model": (up.record.get("model") or {}).get("name")}


def _override_note(up: Upstream) -> str:
    if not up.overridden:
        return ""
    o = up.override
    return (f" (the record said {up.original}; {o.get('actor')} overrode it to {up.verdict} as {o.get('override_id')}: "
            f"{o.get('reason')})")


def _money(d: Decimal) -> str:
    return f"${d.quantize(CENT)}"


# ---------------------------------------------------------------- the fee schedule an operator may supply
def load_tier_table() -> tuple[dict | None, str | None]:
    """(table, problem). The table must say where it came from; one that does not is not used."""
    path = os.environ.get("RECOVERY_TIER_TABLE")
    if not path:
        return None, None
    try:
        t = json.loads(Path(path).read_text(encoding="utf-8"))
        tiers = t["tiers"]
        assert isinstance(t.get("source_url"), str) and t["source_url"].strip(), "source_url is required"
        assert isinstance(t.get("retrieved"), str) and t["retrieved"].strip(), "retrieved (date) is required"
        assert isinstance(t.get("tolerance_g"), (int, float)) and t["tolerance_g"] >= 0, "tolerance_g is required"
        assert tiers and all(isinstance(x.get("fee_usd"), (int, float)) for x in tiers), "tiers need fee_usd"
        bounds = [x.get("up_to_g") for x in tiers]
        assert all(b is None or isinstance(b, (int, float)) for b in bounds), "up_to_g must be a number or null"
        nums = [b for b in bounds if b is not None]
        assert nums == sorted(nums) and (None not in bounds[:-1]), "tiers must ascend; only the last may be open-ended"
        return t, None
    except (OSError, ValueError, KeyError, AssertionError, TypeError, AttributeError) as exc:
        return None, f"the supplied fee schedule was not used ({exc})"


# ---------------------------------------------------------------- context
@dataclass
class Ctx:
    request: dict
    ev: Evidence
    lines: list[FeeLine]
    policy: Policy = POLICY
    flow: list[str] | None = None            # stages in this pod's flow, if it can be read
    tiers: dict | None = None
    tiers_problem: str | None = None
    duplicate_of: dict[str, FeeLine] = field(default_factory=dict)
    rule_sources: list[dict] = field(default_factory=list)

    @property
    def route(self) -> str:
        return self.request["subject"].get("route") or "unknown"

    def why_no(self, stage: str) -> str:
        if self.flow is not None and stage not in self.flow:
            return f"this pod's flow has no {stage.title()} stage (Specialist flow)"
        if self.flow is not None:
            return f"the {stage.title()} stage produced no record for this workflow (route={self.route})"
        return f"no {stage.title()} record was supplied"

    def credits_for(self, line: FeeLine) -> list[FeeLine]:
        """Credits that settle this charge: an explicit link, or the same unit, SKU and exact amount posted after it."""
        out = []
        for c in self.lines:
            if not c.is_credit or c.amount is None or c.amount <= 0:
                continue
            linked = line.line_id in (c.raw.get("related_line_id"), c.raw.get("original_line_id"))
            same = (bool(line.sku) and c.sku == line.sku and c.amount == line.amount and c.posted_date >= line.posted_date
                    and not (c.order_id and line.order_id and c.order_id != line.order_id))
            if linked or same:
                out.append(c)
        return out

    def measurement(self, line: FeeLine) -> tuple[Upstream, dict] | None:
        for up in reversed(self.ev.every()):
            m = (up.record.get("payload") or {}).get("measurements")
            w = m.get("weight_g") if isinstance(m, dict) else None
            if up.completed and up.verdict != "UNCERTAIN" and not up.needs_person \
                    and isinstance(w, (int, float)) and not isinstance(w, bool) and w > 0 \
                    and not ref_conflicts(line.refs(), up):
                return up, m
        return None


def _captured_day(up: Upstream) -> str:
    return str(up.record.get("captured_at") or "")[:10]


def _after_charge(up: Upstream, line: FeeLine, ctx: Ctx) -> bool:
    """Evidence captured after the charge was posted cannot show the state the charge was based on."""
    captured = _captured_day(up)
    return bool(ctx.policy.evidence_must_precede_charge and captured and line.posted_date and captured > line.posted_date)


def _units(line: FeeLine) -> str:
    return f"{line.quantity} units" if line.quantity is not None else "an unknown number of units"


def find_duplicates(lines: list[FeeLine]) -> dict[str, FeeLine]:
    """line_id of each later repeat -> the earlier line it repeats. Same SKU, shipment, order, type, amount and date."""
    groups: dict[tuple, list[FeeLine]] = {}
    for ln in lines:
        if ln.is_credit or ln.amount is None or ln.amount <= 0:
            continue
        key = (ln.sku, ln.fnsku, ln.fba_shipment_id, ln.order_id, ln.charge_type, ln.amount, ln.posted_date)
        groups.setdefault(key, []).append(ln)
    out: dict[str, FeeLine] = {}
    for g in groups.values():
        g.sort(key=lambda x: x.line_id)
        out.update({later.line_id: g[0] for later in g[1:]})
    return out


# ---------------------------------------------------------------- the rules
def _receiving_signal(ctx: Ctx) -> tuple[str, Upstream | None, list[str]]:
    """What Receiving says about defects on this unit: clean | absent | incomplete | fail | uncertain."""
    rcv = ctx.ev.latest("receiving")
    if rcv is None:
        return "absent", None, []
    if not rcv.completed:
        return "incomplete", rcv, []
    if rcv.overridden:  # a person's decision about the record outranks its individual checks
        return {"FAIL": "fail", "UNCERTAIN": "uncertain"}.get(rcv.verdict, "clean"), rcv, ["decision"]
    seen = [(k, (rcv.check(k) or {}).get("verdict")) for k in DEFECT_CHECKS]
    bad = [k for k, v in seen if v == "FAIL"]
    unsure = [k for k, v in seen if v == "UNCERTAIN"]
    return ("fail" if bad else "uncertain" if unsure else "clean"), rcv, bad or unsure


def inbound_defect_fee(line: FeeLine, ctx: Ctx) -> Position:
    prep = ctx.ev.latest("prep")
    if prep is None:
        return silent(f"No Prep evidence: {ctx.why_no('prep')}. An inbound-defect charge is never guessed.",
                      "no_prep_evidence", settle="a completed Prep record for this unit")
    if not prep.completed:
        return silent(f"Prep record {prep.record_id} is {prep.record.get('status')}: it is not a judgment.",
                      "prep_not_completed", refs=[prep.record_id], basis=[_basis(prep, "consulted")],
                      settle="Prep re-run so that it completes")
    clash = ref_conflicts(line.refs(), prep)
    if clash:
        return silent(f"Prep record {prep.record_id} names a different {', '.join(clash)} than the fee line: it may be "
                      "about another item, so it cannot speak for this charge (F-08).", "evidence_identity_mismatch",
                      refs=[prep.record_id], basis=[_basis(prep, "conflicts")], conflict=True,
                      settle="confirm which item the fee line and the Prep record are about")
    verdict, note = prep.verdict, _override_note(prep)
    if verdict == "FAIL":
        failed = [c["check_key"] for c in prep.checks if c["verdict"] == "FAIL"]
        why = f" (failed: {', '.join(failed)})" if failed and not prep.overridden else ""
        return Position("SUPPORTED", f"Prep record {prep.record_id} found the unit non-compliant{why}{note}; "
                        "the channel's defect charge is supported.", refs=[prep.record_id],
                        basis=[_basis(prep, "supports_charge")])
    if verdict != "PASS":
        return silent(f"Prep record {prep.record_id} is UNCERTAIN{note}: it can neither support nor contradict the charge.",
                      "prep_uncertain", refs=[prep.record_id], basis=[_basis(prep, "consulted")],
                      settle="a person resolving the Prep record, or a re-check of the unit")
    # Prep says compliant. Each guard below can still stop a claim.
    if not prep.checks and not prep.overridden:
        return silent(f"Prep record {prep.record_id} says PASS but lists no checks, so there is nothing to cite.",
                      "prep_no_checks", refs=[prep.record_id], basis=[_basis(prep, "consulted")])
    if ctx.policy.single_unit_lines_only and line.quantity != 1:
        return silent(f"The fee line covers {_units(line)} but Prep inspected one (F-08).", "quantity_scope",
                      refs=[prep.record_id], basis=[_basis(prep, "consulted")],
                      settle="Prep evidence for each unit on the line")
    captured = _captured_day(prep)
    if _after_charge(prep, line, ctx):
        return silent(f"Prep record {prep.record_id} was captured {captured}, after the charge was posted "
                      f"({line.posted_date}), so it cannot show the unit's state when it was inspected.",
                      "evidence_after_charge", refs=[prep.record_id], basis=[_basis(prep, "consulted")])
    if ctx.ev.latest("receiving") is None:
        # A claim rests on proof the unit arrived in good order. Prep's pass alone is not that proof, so a person checks.
        return silent(f"No Receiving record: cannot show the unit arrived in good condition ({ctx.why_no('receiving')}). "
                      f"Prep record {prep.record_id} found it compliant{note}, but that alone does not show the inbound "
                      f"defect fee of {_money(line.amount)} was wrong, so nothing is claimed and a person should check it.",
                      "no_receiving_evidence", refs=[prep.record_id], basis=[_basis(prep, "consulted")], conflict=True,
                      reason="insufficient_evidence",
                      settle="a completed Receiving record for this unit with no damage or quality flag, or a person "
                             "confirming the unit arrived in good condition")
    basis = [_basis(prep, "supports_claim")]
    refs = [prep.record_id]
    if ctx.policy.receiving_defect_blocks_claim:
        state, rcv, keys = _receiving_signal(ctx)
        if state == "fail":
            return silent(f"Prep record {prep.record_id} says compliant, but Receiving record {rcv.record_id} recorded "
                          f"{', '.join(keys)}{_override_note(rcv)}: the defect may be real, so this is not claimed.",
                          "receiving_defect_conflict", refs=[prep.record_id, rcv.record_id], conflict=True,
                          basis=basis + [_basis(rcv, "conflicts", keys[0])],
                          settle="a person deciding whether the Receiving defect explains the charge (override one record)")
        if state in ("uncertain", "incomplete"):
            what = "could not rule out" if state == "uncertain" else "did not complete, so it cannot rule out"
            return silent(f"Prep record {prep.record_id} says compliant, but Receiving record {rcv.record_id} {what} "
                          f"a defect on this unit{_override_note(rcv)}.", "receiving_defect_unresolved",
                          refs=[prep.record_id, rcv.record_id], basis=basis + [_basis(rcv, "consulted")],
                          settle="Receiving re-checking the unit")
        if rcv is not None:
            basis.append(_basis(rcv, "consulted"))
            refs.append(rcv.record_id)
    passed = [c["check_key"] for c in prep.checks if c["verdict"] == "PASS"]
    listed = f" ({len(passed)} checks passed: {', '.join(passed)})" if passed and not prep.overridden else ""
    return Position("CONTRADICTED", f"Prep record {prep.record_id} found the unit compliant{listed}{note}, so the "
                    f"inbound defect fee of {_money(line.amount)} is contradicted.", refs=refs, basis=basis,
                    claim=line.amount)


def refund_issued_item_not_returned(line: FeeLine, ctx: Ctx) -> Position:
    ret = ctx.ev.latest("returns")
    if ret is None:
        return silent(f"No Returns evidence: {ctx.why_no('returns')}.", "no_returns_evidence",
                      settle="a Returns record for this order")
    if not ret.completed:
        return silent(f"Returns record {ret.record_id} is {ret.record.get('status')}: it is not a judgment.",
                      "returns_not_completed", refs=[ret.record_id], basis=[_basis(ret, "consulted")])
    clash = ref_conflicts(line.refs(), ret)
    if clash:
        return silent(f"Returns record {ret.record_id} names a different {', '.join(clash)} than the fee line, so it may "
                      "be about another return (F-08).", "evidence_identity_mismatch", refs=[ret.record_id],
                      basis=[_basis(ret, "conflicts")], conflict=True,
                      settle="confirm which order the fee line and the Returns record are about")
    if ctx.route != "mfn" and not ctx.policy.fba_returns_can_contradict:
        code = "returns_route_unresolved" if ctx.route == "fba" else "route_unknown"
        why = ("the unit is FBA-routed and it is not settled whether FBA returns reach the seller (F-11)"
               if ctx.route == "fba" else "the unit has neither a Prep nor a Pack record, so its route is unknown (F-12)")
        return silent(f"Returns record {ret.record_id} exists, but {why}; a seller-side inspection is not shown to be "
                      "the item the channel refunded.", code, refs=[ret.record_id], basis=[_basis(ret, "consulted")],
                      settle="the organisers ruling on F-11/F-12, or channel-side return data")
    # The identity CHECK decides, not the record-level verdict: a person overriding the decision does not change what the
    # identity check found. A claim also needs the record to be a clean, settled PASS with the unit actually present.
    identity = (ret.check("identity_match") or {}).get("verdict", "UNCERTAIN")
    presence = (ret.check("unit_presence") or {}).get("verdict")
    note = _override_note(ret)
    clean = ret.verdict == "PASS" and not ret.needs_person and presence in (None, "PASS")
    if identity == "PASS" and clean:
        captured = _captured_day(ret)
        if _after_charge(ret, line, ctx):
            return silent(f"Returns record {ret.record_id} was captured {captured}, after the refund was posted "
                          f"({line.posted_date}), so it cannot show what came back at the time.", "evidence_after_charge",
                          refs=[ret.record_id], basis=[_basis(ret, "consulted")])
        return Position("CONTRADICTED", f"Returns record {ret.record_id} shows the item came back and is what was "
                        f"sold{note}, which contradicts a refund issued for an item not returned.", refs=[ret.record_id],
                        basis=[_basis(ret, "supports_claim", "identity_match", identity)], claim=line.amount)
    if identity == "FAIL" and ret.verdict == "FAIL":
        return Position("SUPPORTED", f"Returns record {ret.record_id} shows the item that came back is not what was sold"
                        f"{note}; the sold item was not returned.", refs=[ret.record_id],
                        basis=[_basis(ret, "supports_charge", "identity_match", identity)])
    if identity == "PASS":
        why = ("still asks for a person" if ret.needs_person else "is not a clean pass"
               if ret.verdict != "PASS" else "does not show the unit present")
        return silent(f"Returns record {ret.record_id} found the identity matched, but the record {why}{note}, so it is "
                      "not a basis for a claim.", "returns_not_cleared", refs=[ret.record_id],
                      basis=[_basis(ret, "consulted")], settle="a person resolving the Returns record")
    return silent(f"Returns record {ret.record_id} could not settle the item's identity{note}.", "returns_uncertain",
                  refs=[ret.record_id], basis=[_basis(ret, "consulted")])


def lost_inbound(line: FeeLine, ctx: Ctx) -> Position:
    """F-10: Receiving shortfalls are supplier-side and happen before the goods reach the channel. They are shown,
    kept apart, and never used for or against a channel loss."""
    rcv = ctx.ev.latest("receiving")
    basis, refs, extra = [], [], ""
    if rcv is not None and rcv.completed:
        refs, basis = [rcv.record_id], [_basis(rcv, "set_aside")]
        short = (rcv.record.get("payload") or {}).get("shortfall_units")
        if short:
            extra = (f" Receiving record {rcv.record_id} shows {short} unit(s) short of the PO; that is a supplier shortfall "
                     "(a Receiving claim), kept separate from channel loss, and is not used here.")
    return silent("The channel's inbound-loss adjustment cannot be tested: no record in this pod comes from the channel's "
                  f"side of the dock.{extra}", "channel_loss_unevidenced", refs=refs, basis=basis,
                  settle="a channel-side receipt or shipment reconciliation for this shipment")


def fulfilment_fee_weight_tier(line: FeeLine, ctx: Ctx) -> Position:
    """F-07: judged only against a measured weight an earlier stage recorded (payload.measurements) and a fee schedule
    the operator supplied with its source. The fee line itself carries neither the billed weight nor the tier."""
    found = ctx.measurement(line)
    if found is None:
        return silent("No earlier record carries a measured weight (payload.measurements), so the weight tier cannot be "
                      "checked (F-07).", "no_measurements",
                      settle="Prep (or another stage) recording measured weight in payload.measurements")
    up, m = found
    w = Decimal(str(m["weight_g"]))
    basis = [_basis(up, "consulted")]
    if ctx.policy.single_unit_lines_only and line.quantity != 1:
        return silent(f"The fee line covers {_units(line)} but {up.record_id} measured one unit, so the line total "
                      "cannot be compared with one unit's tier (F-08).", "quantity_scope", refs=[up.record_id],
                      basis=basis, settle="a measurement for each unit on the line, or a per-unit fee")
    if _after_charge(up, line, ctx):
        return silent(f"{up.record_id} was captured {_captured_day(up)}, after the charge was posted "
                      f"({line.posted_date}), so it cannot show the unit's weight when it was billed.",
                      "evidence_after_charge", refs=[up.record_id], basis=basis)
    if ctx.tiers is None:
        why = ctx.tiers_problem or "no fee schedule is configured (RECOVERY_TIER_TABLE); tiers are not guessed"
        return silent(f"{up.record_id} recorded {w} g, but {why}, so the charged tier cannot be compared (F-07).",
                      "measurements_not_comparable", refs=[up.record_id], basis=basis,
                      settle="a fee schedule with its source URL and retrieval date")
    tiers, tol = ctx.tiers["tiers"], Decimal(str(ctx.tiers["tolerance_g"]))
    ctx.rule_sources.append({"kind": "fee_schedule", "source_url": ctx.tiers["source_url"],
                             "retrieved": ctx.tiers["retrieved"]})
    near = [b for b in (t.get("up_to_g") for t in tiers) if b is not None and abs(w - Decimal(str(b))) <= tol]
    if near:
        return silent(f"{up.record_id} recorded {w} g, within {tol} g of a tier boundary ({near[0]} g): the scale's margin "
                      "could change the tier.", "near_tier_boundary", refs=[up.record_id], basis=basis,
                      settle="a re-weigh away from the boundary")
    tier = next((t for t in tiers if t.get("up_to_g") is None or w <= Decimal(str(t["up_to_g"]))), None)
    if tier is None:
        return silent(f"{up.record_id} recorded {w} g, above the last tier in the supplied schedule.", "no_tier",
                      refs=[up.record_id], basis=basis)
    expected = Decimal(str(tier["fee_usd"])).quantize(CENT)
    over = line.amount - expected
    if over > CENT / 2:
        return Position("CONTRADICTED", f"{up.record_id} recorded {w} g; the supplied schedule puts that at "
                        f"{_money(expected)} and the line charged {_money(line.amount)}: {_money(over)} more.",
                        ["tier_overcharge"], [up.record_id], [_basis(up, "supports_claim")], claim=over)
    return Position("SUPPORTED", f"{up.record_id} recorded {w} g; the supplied schedule puts that at {_money(expected)}, "
                    f"which the line's {_money(line.amount)} does not exceed.", ["tier_ok"], [up.record_id],
                    [_basis(up, "supports_charge")])


def no_rule(line: FeeLine, ctx: Ctx) -> Position:
    return silent(f"No rule for charge type '{line.charge_type}': it is left alone, not guessed.", "no_rule")


RULES = {"inbound_defect_fee": inbound_defect_fee, "refund_issued_item_not_returned": refund_issued_item_not_returned,
         "lost_inbound": lost_inbound, "fulfilment_fee_weight_tier": fulfilment_fee_weight_tier}


def assess(line: FeeLine, ctx: Ctx) -> Position:
    if line.is_credit:
        amount = _money(line.amount) if line.amount is not None else "an unreadable amount"
        return Position("CREDIT", f"A reimbursement of {amount} the channel already paid. It is not a charge, so there is "
                        "nothing to dispute.", ["credit"])
    if line.amount is None:
        return silent("The amount is missing or unreadable: nothing can be claimed (F-09).", "amount_missing",
                      settle="the line's amount from the report")
    if line.amount < 0:
        return silent("A negative amount: the sign convention of the report is not known, so it is not read as a charge.",
                      "amount_negative", settle="the report's sign convention")
    if line.amount > 0:
        paid = ctx.credits_for(line)
        if paid:
            return Position("ALREADY_REIMBURSED", f"Already credited by {', '.join(c.line_id for c in paid)} "
                            f"({_money(sum(c.amount for c in paid))}). A second recovery is not possible.",
                            ["already_reimbursed"], fee_refs=[c.ref for c in paid])
        orig = ctx.duplicate_of.get(line.line_id)
        if orig is not None:
            if line.order_id or not ctx.policy.duplicate_needs_order_id:
                return Position("DUPLICATE", f"Repeats {orig.line_id}: the same {line.charge_type} of {_money(line.amount)} "
                                f"on {line.posted_date} for the same SKU, shipment and order.", ["duplicate"],
                                fee_refs=[orig.ref], claim=line.amount)
            return silent(f"Looks like a repeat of {orig.line_id}, but with no order id it could be a second real unit (F-08).",
                          "possible_duplicate", settle="the order id for both lines")
    pos = RULES.get(line.charge_type, no_rule)(line, ctx)
    if pos.position == "CONTRADICTS" and line.amount <= 0:
        return Position("SILENT", f"{pos.detail} But the amount is {_money(line.amount)}: there is nothing to claim, or the "
                        "amount is missing (F-09, D-005).", ["amount_zero", *pos.codes], pos.refs, pos.basis,
                        underlying="CONTRADICTS", settle="the line's real amount from the report")
    if pos.position == "SILENT" and line.amount == 0:
        pos.codes.append("amount_zero")
        pos.detail += " The line's amount is $0.00, so nothing could be claimed even with evidence (F-09, D-005)."
    return pos
