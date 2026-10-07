"""The deterministic rules: what an Observation means against a PurchaseOrderLine.

The model only reports what it saw. Every verdict below is arithmetic or a string comparison, so every verdict is
reproducible and testable without a model. Ported from the Round 2 comparison engine (@cherryy-x23) and changed
where it was weak (PROVENANCE.md): identity now weighs ALL its evidence and refuses to pick a side when it
conflicts; image clarity and the model's own confidence can only make a verdict UNCERTAIN; quantity cross-checks the
model's total against cartons x units-per-carton; the carton-count check (written but never called in Round 2) is
wired in; colour, variant, components and an obvious-defect flag roll up into ``quality_flags``.

Verdicts are PASS / FAIL / UNCERTAIN. An UNCERTAIN check always carries an ``uncertain_reason`` from the schema enum.
Check ``confidence`` is null: these are deterministic checks, and the model's self-reported confidence is not
calibrated (see README), so it is kept in the payload and used only as a gate.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from shared.utils.records import check, rollup

from .models import Observation, PurchaseOrderLine, norm_id, tokens

ENGINE = "receiving-rules/1"


@dataclass(frozen=True)
class Thresholds:
    min_clarity: float = 0.5
    min_confidence: float = 0.6


@dataclass
class Result:
    checks: list[dict]
    verdict: str
    outcome: str
    needs_human: bool
    reason: str
    derived: dict = field(default_factory=dict)


def _gate(clarity: float, confidence: float, th: Thresholds) -> str | None:
    """Why a model-read fact cannot be trusted, or None. Clarity first: a blurry photo is a retake, not a doubt."""
    if clarity < th.min_clarity:
        return "poor_image"
    if confidence < th.min_confidence:
        return "insufficient_evidence"
    return None


def _uncertain(key: str, reason: str, why: str, *, expected, refs: list[str], observed=None) -> dict:
    return check(key, "UNCERTAIN", None, expected=expected, observed=observed, detail=why, evidence_refs=refs,
                 uncertain_reason=reason)


def _gate_check(key: str, reason: str, ob: Observation, confidence: float, th: Thresholds, *, expected, refs) -> dict:
    if reason == "poor_image":
        text = f"The photos are too unclear to rely on (clarity {ob.image_clarity:.2f}, minimum {th.min_clarity:.2f})."
    else:
        text = f"The reading was not confident enough to rely on ({confidence:.2f}, minimum {th.min_confidence:.2f})."
    return _uncertain(key, reason, text, expected=expected, refs=refs)


# ---------------------------------------------------------------- identity
_CATALOGUE_ID = re.compile(r"^(SKU[A-Z0-9]+|B0[A-Z0-9]{8})$")  # normalised: looks like a SKU or an ASIN


def identity_match(po: PurchaseOrderLine, ob: Observation, th: Thresholds, refs: list[str]) -> dict:
    expected = po.sku + (f" / {po.asin}" if po.asin else "")
    gate = _gate(ob.image_clarity, ob.identity_confidence, th)
    if gate:
        return _gate_check("identity_match", gate, ob, ob.identity_confidence, th, expected=expected, refs=refs)
    want_sku, want_asin = norm_id(po.sku), norm_id(po.asin)
    read: dict = {}
    yes: list[str] = []
    no: list[str] = []
    if norm_id(ob.identified_sku):
        read["sku_text"] = ob.identified_sku
        (yes if norm_id(ob.identified_sku) == want_sku else no).append("the SKU printed on the goods")
    b = norm_id(ob.barcode)
    if b:
        read["barcode"] = ob.barcode
        if b in {want_sku, want_asin}:
            yes.append("the barcode")
        elif _CATALOGUE_ID.match(b):  # clearly a different SKU/ASIN. Any other format (FNSKU, UPC) says nothing.
            no.append("the barcode")
    ocr = norm_id(ob.ocr_text)
    if ocr:
        read["ocr_text"] = ob.ocr_text
        if want_sku in ocr or (want_asin and want_asin in ocr):  # text can confirm but never contradict
            yes.append("the label text")
    if yes and no:
        return _uncertain("identity_match", "conflicting_evidence",
                          f"{' and '.join(yes).capitalize()} match the order but {' and '.join(no)} do not.",
                          expected=expected, observed=read, refs=refs)
    if no:
        return check("identity_match", "FAIL", None, expected=expected, observed=read, evidence_refs=refs,
                     detail=f"{' and '.join(no).capitalize()} do not match the ordered SKU {po.sku}.")
    if yes:
        return check("identity_match", "PASS", None, expected=expected, observed=read, evidence_refs=refs,
                     detail=f"{' and '.join(yes).capitalize()} match the ordered SKU {po.sku}.")
    return _uncertain("identity_match", "insufficient_evidence",
                      "No SKU, barcode or label text could be read to confirm the product.",
                      expected=expected, observed=read or None, refs=refs)


# ---------------------------------------------------------------- counts
def carton_count(po: PurchaseOrderLine, ob: Observation, th: Thresholds, refs: list[str]) -> dict:
    gate = _gate(ob.image_clarity, ob.count_confidence, th)
    if gate:
        return _gate_check("carton_count", gate, ob, ob.count_confidence, th, expected=po.cartons_ordered, refs=refs)
    if ob.cartons_counted is None:
        return _uncertain("carton_count", "insufficient_evidence", "The cartons could not be counted from the photos.",
                          expected=po.cartons_ordered, refs=refs)
    diff = ob.cartons_counted - po.cartons_ordered
    if diff == 0:
        return check("carton_count", "PASS", None, expected=po.cartons_ordered, observed=ob.cartons_counted,
                     evidence_refs=refs, detail=f"{po.cartons_ordered} carton(s), as ordered.")
    word = f"short by {-diff}" if diff < 0 else f"over by {diff}"
    return check("carton_count", "FAIL", None, expected=po.cartons_ordered, observed=ob.cartons_counted,
                 evidence_refs=refs,
                 detail=f"{ob.cartons_counted} carton(s) counted, {po.cartons_ordered} ordered: {word}.")


def received_quantity(ob: Observation) -> tuple[int | None, str]:
    """(units received, how we know), or (None, why not). A direct count and cartons x units-per-carton must agree."""
    product = (ob.cartons_counted * ob.units_per_carton_counted
               if ob.cartons_counted is not None and ob.units_per_carton_counted is not None else None)
    if ob.quantity_counted is not None and product is not None and ob.quantity_counted != product:
        return None, (f"conflict: counted {ob.quantity_counted} units directly but {ob.cartons_counted} cartons x "
                      f"{ob.units_per_carton_counted} units is {product}")
    if ob.quantity_counted is not None:
        return ob.quantity_counted, "counted directly"
    if product is not None:
        return product, f"{ob.cartons_counted} cartons x {ob.units_per_carton_counted} units"
    return None, "no count"


def quantity(po: PurchaseOrderLine, ob: Observation, th: Thresholds, refs: list[str]) -> dict:
    gate = _gate(ob.image_clarity, ob.count_confidence, th)
    if gate:
        return _gate_check("quantity", gate, ob, ob.count_confidence, th, expected=po.qty_ordered, refs=refs)
    received, how = received_quantity(ob)
    if received is None and how.startswith("conflict"):
        return _uncertain("quantity", "conflicting_evidence", f"The two counts disagree ({how}).",
                          expected=po.qty_ordered, refs=refs)
    if received is None:
        return _uncertain("quantity", "insufficient_evidence", "The units could not be counted from the photos.",
                          expected=po.qty_ordered, refs=refs)
    diff = received - po.qty_ordered
    if diff == 0:
        return check("quantity", "PASS", None, expected=po.qty_ordered, observed=received, evidence_refs=refs,
                     detail=f"{received} units ({how}), as ordered.")
    word = f"a shortfall of {-diff}" if diff < 0 else f"an overage of {diff}"
    return check("quantity", "FAIL", None, expected=po.qty_ordered, observed=received, evidence_refs=refs,
                 detail=f"{received} units ({how}) against {po.qty_ordered} ordered: {word}.")


# ---------------------------------------------------------------- damage
def damage(key: str, what: str, value: str, ob: Observation, th: Thresholds, refs: list[str]) -> dict:
    gate = _gate(ob.image_clarity, ob.damage_confidence, th)
    if gate:
        return _gate_check(key, gate, ob, ob.damage_confidence, th, expected="none", refs=refs)
    if value == "uncertain":
        return _uncertain(key, "insufficient_evidence", f"The {what} condition cannot be told from the photos.",
                          expected="none", refs=refs)
    if value == "none":
        return check(key, "PASS", None, expected="none", observed="none", evidence_refs=refs,
                     detail=f"No {what} damage seen.")
    return check(key, "FAIL", None, expected="none", observed=value, evidence_refs=refs,
                 detail=f"{what.capitalize()} damage seen: {value}.")


# ---------------------------------------------------------------- quality flags
def _constrained(spec: str, free: set[str]) -> bool:
    """Does the PO actually constrain this attribute? 'n/a' and 'standard' do not."""
    return bool(spec) and spec.strip().lower() not in free


def quality_flags(po: PurchaseOrderLine, ob: Observation, th: Thresholds, refs: list[str]) -> tuple[dict, dict]:
    """Colour, variant, required components and an obvious defect, as one check. Returns (check, details)."""
    gate = _gate(ob.image_clarity, ob.spec_confidence, th)
    if gate:
        return _gate_check("quality_flags", gate, ob, ob.spec_confidence, th, expected=[], refs=refs), {}
    raised: list[str] = []
    unclear: list[str] = []
    notes: list[str] = []
    missing: list[str] = []

    if _constrained(po.spec_colour, {"n/a", "none"}):
        if not ob.observed_colour:
            unclear.append("colour")
        elif tokens(ob.observed_colour) != tokens(po.spec_colour):
            raised.append("wrong_colour")
            notes.append(f"colour {ob.observed_colour!r}, spec {po.spec_colour!r}")
    if _constrained(po.spec_variant, {"n/a", "standard"}):
        if not ob.observed_variant:
            unclear.append("variant")
        elif tokens(ob.observed_variant) != tokens(po.spec_variant):
            raised.append("wrong_variant")
            notes.append(f"variant {ob.observed_variant!r}, spec {po.spec_variant!r}")
    if po.spec_components:
        if ob.observed_components is None:
            unclear.append("components")
        else:
            seen = [tokens(c) for c in ob.observed_components]
            missing = [c for c in po.spec_components if not any(tokens(c) <= s for s in seen)]
            if missing:
                raised.append("missing_components")
                notes.append(f"missing {', '.join(missing)}")
    if ob.obvious_defect == "yes":
        raised.append("obvious_defect")
        notes.append("an obvious defect is visible")
    elif ob.obvious_defect == "unsure":
        unclear.append("defect")

    details = {"quality_flags": raised, "missing_components": missing}
    if raised:  # a confirmed problem is reported even if something else could not be read
        return check("quality_flags", "FAIL", None, expected=[], observed=raised, evidence_refs=refs,
                     detail="; ".join(notes).capitalize() + "."), details
    if unclear:
        return _uncertain("quality_flags", "insufficient_evidence", f"Could not confirm: {', '.join(unclear)}.",
                          expected=[], refs=refs), details
    return check("quality_flags", "PASS", None, expected=[], observed=[], evidence_refs=refs,
                 detail="Colour, variant, components and finish match the spec; no obvious defect."), details


# ---------------------------------------------------------------- the whole inspection
def evaluate(po: PurchaseOrderLine, ob: Observation, th: Thresholds, refs: list[str]) -> Result:
    qf, flag_details = quality_flags(po, ob, th, refs)
    checks = [
        identity_match(po, ob, th, refs),
        carton_count(po, ob, th, refs),
        quantity(po, ob, th, refs),
        damage("carton_damage", "carton", ob.carton_damage, ob, th, refs),
        damage("unit_damage", "unit", ob.unit_damage, ob, th, refs),
        qf,
    ]
    by = {c["check_key"]: c for c in checks}
    verdict = rollup(checks)
    unclear = [c for c in checks if c["verdict"] == "UNCERTAIN"]
    failed = [c for c in checks if c["verdict"] == "FAIL"]
    if verdict == "PASS":
        outcome, reason = "accept", "Everything checked matches the purchase order; no damage seen."
    elif verdict == "UNCERTAIN":
        outcome = "pending_review"
        reason = "A person needs to settle: " + " ".join(f"{c['check_key']}: {c['detail']}" for c in unclear)
    else:
        # Wrong goods are rejected. Every other exception (short, over, damaged, off-spec) is accepted with the
        # exception recorded: the stock is usable, and the evidence is what a supplier claim needs.
        outcome = "reject" if by["identity_match"]["verdict"] == "FAIL" else "accept_with_exceptions"
        reason = " ".join(f"{c['check_key']}: {c['detail']}" for c in failed)
        if unclear:
            reason += f" Also unresolved: {', '.join(c['check_key'] for c in unclear)}."
    received, _ = received_quantity(ob)
    qty_known = by["quantity"]["verdict"] != "UNCERTAIN"  # a gated or conflicting count is never reported as received
    derived = {
        "qty_received": received if qty_known else None,
        "shortfall_units": max(po.qty_ordered - received, 0) if qty_known and received is not None else None,
        "overage_units": max(received - po.qty_ordered, 0) if qty_known and received is not None else None,
        "cartons_received": ob.cartons_counted if by["carton_count"]["verdict"] != "UNCERTAIN" else None,
        "units_per_carton_counted": ob.units_per_carton_counted if qty_known else None,
        **flag_details,
    }
    return Result(checks, verdict, outcome, needs_human=bool(unclear), reason=reason[:600], derived=derived)
