"""What a prepped unit must satisfy: the per-unit requirement pack and the catalogue of checks.

Ported from the Round 2 reference (Manvith111, see PROVENANCE.md), where each product carried flags such as
``requiresPolybag`` and ``hasExpiry`` and a fixed list of eleven checks. Here the flags come from the unit's
**work order** (the organisers' ``wo_*`` columns, or ``context.order``), so the requirement pack is per unit/SKU.

HONESTY ABOUT THE RULES. The reference quoted "§2.1 Poly-bagging ..." style clauses and said they "model the
standard marketplace rules". Those clause numbers and quotes were invented, so they are NOT carried over.
The requirement texts below are our own plain-language wording of what each check looks for. We could not browse
or retrieve Amazon's published prep requirements (the starter says to look them up, not infer them), so every
rule's source is recorded as **unverified** and every check detail says so. The thresholds in the reference
(a 5 inch opening, 1.5 mil) are also not used. Which requirements apply to a unit is decided by the work order, not
by a rule we claim to know.
"""
from __future__ import annotations

from dataclasses import dataclass

DEMO_TAG = "[demo rule, source unverified]"

RULE_SOURCE = {
    "status": "unverified",
    "publisher": "Amazon FBA prep requirements (intended source; NOT retrieved)",
    "url": None,
    "retrieved_at": None,
    "note": ("No published rule text was retrieved or checked. The requirement wording is ours and the flags that "
             "decide which requirements apply come from the work order. The organisers' sample flags are dummy "
             "values, not Amazon's rules. Replace this with a URL and retrieval date once the rules are looked up."),
}


@dataclass(frozen=True)
class CheckDef:
    """One thing the model is asked to observe. ``contract_key`` is the Round 3 check_key it rolls up into."""

    check_id: str
    contract_key: str | None  # None: reported in the payload only (not photo-verifiable)
    requirement: str          # what must be true (our wording)
    question: str             # what the model is asked to look for (no verdict words)
    verifiable: bool = True


# Order matters: it is the order of the prompt and of payload.rule_checks.
CHECKS: tuple[CheckDef, ...] = (
    CheckDef("polybag_present", "polybag_sealed",
             "The unit is enclosed in a transparent poly bag.",
             "met = the unit is visibly enclosed in a transparent poly bag. not_met = it is visibly not in a bag."),
    CheckDef("polybag_sealed", "polybag_sealed",
             "The poly bag is fully closed, with no open side.",
             "met = the bag is closed all round, no open end or gap is visible. not_met = an open end, gap or "
             "unsealed flap is visible."),
    CheckDef("suffocation_warning_present", "suffocation_warning",
             "A suffocation warning is printed on the bag or on an attached label.",
             "met = suffocation-warning text or symbol is visible on the bag or a label on it. not_met = the bag is "
             "visible and carries no such warning."),
    CheckDef("suffocation_warning_legible", "suffocation_warning",
             "The suffocation warning is fully visible and readable (not folded or covered).",
             "met = the warning is fully visible and readable. not_met = it is partly hidden (for example by a "
             "fold) or unreadable."),
    CheckDef("fnsku_present", "fnsku_label_placement",
             "A barcode label (FNSKU) is applied to the unit or its bag.",
             "met = a printed barcode label is visible on the unit or bag. not_met = no label is visible on the "
             "unit or bag."),
    CheckDef("fnsku_placement", "fnsku_label_placement",
             "The label lies flat on a flat surface, not across a seam, curve, corner or edge.",
             "met = the label lies flat on a flat surface. not_met = it crosses a seam, curve, corner or edge, or "
             "is creased so it cannot be scanned cleanly."),
    CheckDef("fnsku_text_match", "fnsku_text_match",
             "The code printed on the label equals the FNSKU expected for this unit.",
             "Not an observation: derived from label_text_read (the model never sees the expected code)."),
    CheckDef("original_barcode_covered", "original_barcode_covered",
             "Any original manufacturer barcode on the exterior is covered or unscannable.",
             "met = no manufacturer barcode (UPC/EAN) is exposed on the exterior (it is covered, or there is none). "
             "not_met = a manufacturer barcode is visible and uncovered."),
    CheckDef("expiry_visible", "expiry_legible",
             "The expiry date is printed on the unit and still legible after prep.",
             "met = an expiry date is visible and legible. not_met = it is hidden, or illegible (for example "
             "blurred by the bag or wrap)."),
    CheckDef("handling_marks_present", "handling_marks",
             "Every handling mark the work order requires is visible on the exterior.",
             "List every handling mark you can see on the exterior in marks_visible (for example fragile, "
             "this_way_up, liquid; use an empty list if there are none). met = you can see enough of the exterior "
             "to be sure which marks it carries. cant_tell = parts of the exterior are not shown."),
    CheckDef("bag_thickness_material", None,
             "The bag is a durable material of an adequate thickness.",
             "Not observed: thickness cannot be judged from a photograph.", verifiable=False),
)
BY_ID = {c.check_id: c for c in CHECKS}

# The check_keys the contract recommends for Prep, plus the one we add (allowed: contract section 11).
CONTRACT_KEYS = ("polybag_sealed", "suffocation_warning", "fnsku_label_placement", "original_barcode_covered",
                 "expiry_legible", "handling_marks")
EXTRA_KEYS = ("fnsku_text_match",)


@dataclass(frozen=True)
class RequirementPack:
    """What this unit must satisfy. Built from the work order; never from the model."""

    sku: str | None = None
    fnsku: str | None = None                  # the code that must be on the label (None: not known, no text check)
    polybag: bool = True
    suffocation_warning: bool = True
    expiry_date: bool = False
    handling_marks: tuple[str, ...] = ()
    cover_original_barcode: bool = True       # the CSV has no 'not_required' for it, so it applies unless said otherwise
    label_required: bool = True
    source: str = "work_order"

    def applies(self, c: CheckDef) -> bool:
        i = c.check_id
        if i in ("polybag_present", "polybag_sealed", "bag_thickness_material"):
            return self.polybag
        if i in ("suffocation_warning_present", "suffocation_warning_legible"):
            return self.suffocation_warning
        if i in ("fnsku_present", "fnsku_placement"):
            return self.label_required
        if i == "fnsku_text_match":
            return self.label_required and bool(self.fnsku)
        if i == "original_barcode_covered":
            return self.cover_original_barcode
        if i == "expiry_visible":
            return self.expiry_date
        if i == "handling_marks_present":
            return bool(self.handling_marks)
        return False

    def active(self) -> list[CheckDef]:
        return [c for c in CHECKS if self.applies(c)]

    def observable(self) -> list[CheckDef]:
        """The checks the model is asked about: applicable, photo-verifiable, and not derived from the label text."""
        return [c for c in self.active() if c.verifiable and c.check_id != "fnsku_text_match"]

    def not_verifiable(self) -> list[CheckDef]:
        return [c for c in self.active() if not c.verifiable]

    def describe(self) -> dict:
        return {"sku": self.sku, "fnsku_expected": self.fnsku, "polybag": self.polybag,
                "suffocation_warning": self.suffocation_warning, "expiry_date": self.expiry_date,
                "handling_marks": list(self.handling_marks), "cover_original_barcode": self.cover_original_barcode,
                "source": self.source}


def norm_mark(value: str) -> str:
    return "_".join(str(value).strip().lower().replace("-", " ").replace("_", " ").split())


def _flag(value, name: str) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in ("true", "yes", "1"):
        return True
    if text in ("false", "no", "0", ""):
        return False
    raise ValueError(f"work order flag {name} has an unreadable value {value!r}")


def pack_from_csv_row(row: dict) -> RequirementPack:
    """The organisers' prep_sample.csv work-order columns (wo_*)."""
    marks = tuple(m for m in (norm_mark(x) for x in str(row.get("wo_handling_marks", "")).split(";")) if m)
    return RequirementPack(
        sku=row.get("sku") or None, fnsku=row.get("fnsku") or None,
        polybag=_flag(row["wo_polybag"], "wo_polybag"),
        suffocation_warning=_flag(row["wo_suffocation_warning"], "wo_suffocation_warning"),
        expiry_date=_flag(row["wo_expiry_date"], "wo_expiry_date"), handling_marks=marks, source="prep_sample.csv")


def pack_from_context(order: dict) -> RequirementPack:
    """``context.order``: {"sku", "fnsku", "requirements": {polybag, suffocation_warning, expiry_date, handling_marks,
    cover_original_barcode}}. A missing flag takes the pack's default; an unreadable one raises ValueError."""
    req = order.get("requirements") or {}
    marks = req.get("handling_marks", ())
    if isinstance(marks, str):
        marks = marks.split(";")
    polybag = _flag(req.get("polybag", True), "polybag")
    return RequirementPack(
        sku=order.get("sku"), fnsku=order.get("fnsku"), polybag=polybag,
        suffocation_warning=_flag(req.get("suffocation_warning", polybag), "suffocation_warning"),
        expiry_date=_flag(req.get("expiry_date", False), "expiry_date"),
        handling_marks=tuple(m for m in (norm_mark(x) for x in marks) if m),
        cover_original_barcode=_flag(req.get("cover_original_barcode", True), "cover_original_barcode"),
        source="context.order")
