"""The organisers' fee report, as typed lines, scoped to one tenant.

Recovery has no camera. Its inputs are the channel's fee / reimbursement / inventory-adjustment lines for the unit
(`shared.utils.sample_data.fee_lines`, which is tenant-scoped) plus the evidence the earlier stages left.

Tenancy: a subject that is not under `subject.org_id` raises LookupError (HTTP 404). It is never answered with
"no claim", because "no claim" would be an answer about somebody else's unit.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from shared.utils import sample_data
from shared.utils.hashing import canonical_json

CREDIT_REPORT_TYPES = {"reimbursement_report"}  # money the channel paid out, not a charge to dispute
KNOWN_KINDS = ("receiving", "prep", "pack", "returns", "fees")


@dataclass(frozen=True)
class FeeLine:
    line_id: str
    report_type: str
    unit_id: str
    org_id: str
    sku: str
    fnsku: str
    fba_shipment_id: str
    order_id: str
    charge_type: str
    quantity: int | None
    amount: Decimal | None
    posted_date: str
    raw: dict = field(repr=False, compare=False)

    @property
    def is_credit(self) -> bool:
        return self.report_type in CREDIT_REPORT_TYPES

    @property
    def ref(self) -> str:
        """How the evidence record names this row in `inputs[]` and `evidence_refs`."""
        return f"fee_report/{self.line_id}"

    @property
    def sha256(self) -> str:
        """Hash of the row exactly as read, so a reviewer can see which bytes were examined."""
        return hashlib.sha256(canonical_json(self.raw)).hexdigest()

    def refs(self) -> dict:
        return {"sku": self.sku, "fnsku": self.fnsku, "fba_shipment_id": self.fba_shipment_id,
                "order_id": self.order_id}


def _decimal(value) -> Decimal | None:
    try:
        d = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return None
    return d if d.is_finite() else None


def _int(value) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def parse(row: dict) -> FeeLine:
    g = lambda k: (row.get(k) or "").strip()  # noqa: E731
    return FeeLine(line_id=g("line_id"), report_type=g("report_type"), unit_id=g("unit_id"), org_id=g("org_id"),
                   sku=g("sku"), fnsku=g("fnsku"), fba_shipment_id=g("fba_shipment_id"), order_id=g("order_id"),
                   charge_type=g("charge_type"), quantity=_int(row.get("quantity")), amount=_decimal(row.get("amount_usd")),
                   posted_date=g("posted_date"), raw=dict(row))


def known_subject(unit_id: str, org_id: str) -> bool:
    """True if any organiser dataset has this unit under this organisation. Another org's unit is not known."""
    for kind in KNOWN_KINDS:
        try:
            if any(r["unit_id"] == unit_id and r["org_id"] == org_id for r in sample_data.rows(kind)):
                return True
        except OSError:  # a dataset that is not there knows nothing; it does not make the unit unknown to the others
            continue
    return False


def lines_for(unit_id: str, org_id: str) -> list[FeeLine]:
    """The fee-report lines for this unit under this organisation, in report order. Re-checked, not trusted."""
    try:
        rows = sample_data.fee_lines(unit_id, org_id)
    except OSError:
        rows = []
    lines = [parse(r) for r in rows]
    return [ln for ln in lines if ln.org_id == org_id and ln.unit_id == unit_id]


_KEY = re.compile(r"[^a-z0-9]+")


def check_keys(lines: list[FeeLine]) -> dict[int, str]:
    """`charge_<line_id>` for each line (index -> key), matching ^[a-z][a-z0-9_]*$ and unique within the record."""
    taken: set[str] = set()
    keys: dict[int, str] = {}
    for i, ln in enumerate(lines):
        base = "charge_" + (_KEY.sub("_", ln.line_id.lower()).strip("_") or f"line{i + 1}")
        key, n = base, 2
        while key in taken:
            key, n = f"{base}_{n}", n + 1
        taken.add(key)
        keys[i] = key
    return keys
