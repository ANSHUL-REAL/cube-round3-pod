"""Where Prep gets the work order (what the unit had to be prepped to), scoped to the caller's organisation.

Order of precedence:
  1. ``context.order`` (or ``context.case.order``) on the request: {"org_id", "sku", "fnsku", "work_order_id",
     "fba_shipment_id", "asin", "operator_id", "captured_at", "prep_price_usd",
     "requirements": {"polybag", "suffocation_warning", "expiry_date", "handling_marks", "cover_original_barcode"}}.
     It must carry the subject's own org_id; one for another organisation is refused.
  2. The organisers' Round 2 prep sample (``prep_sample.csv``) looked up by (unit_id, org_id).

Anything else raises LookupError, which the server turns into a 404. A unit that belongs to another organisation is
never answered: that is the tenancy rule.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from shared.utils import sample_data

from .requirements import RequirementPack, pack_from_context, pack_from_csv_row


@dataclass
class WorkOrder:
    requirements: RequirementPack
    refs: dict = field(default_factory=dict)
    operator_id: str | None = None
    captured_at: str | None = None   # only when the caller states it; the sample's timestamp is for photos that do not exist here
    prep_price_usd: float | None = None


def resolve(request: dict) -> WorkOrder:
    subject = request["subject"]
    org_id, unit_id = subject["org_id"], subject["subject_id"]
    ctx = request.get("context") or {}
    given = ctx.get("order") or (ctx.get("case") or {}).get("order")
    if given:
        if given.get("org_id") != org_id:
            raise LookupError(f"the work order in the request does not belong to {org_id}")
        if given.get("unit_id") not in (None, unit_id):
            raise LookupError(f"the work order in the request is for {given.get('unit_id')}, not {unit_id}")
        price = given.get("prep_price_usd")
        return WorkOrder(
            requirements=pack_from_context(given),
            refs={"work_order_id": given.get("work_order_id"), "fba_shipment_id": given.get("fba_shipment_id"),
                  "sku": given.get("sku"), "asin": given.get("asin"), "fnsku": given.get("fnsku")},
            operator_id=given.get("operator_id"), captured_at=given.get("captured_at"),
            prep_price_usd=float(price) if price not in (None, "") else None)

    row = sample_data.row("prep", unit_id, org_id)  # LookupError for another org's unit, or an unknown unit
    return WorkOrder(
        requirements=pack_from_csv_row(row),
        refs={"work_order_id": row["work_order_id"], "fba_shipment_id": row["fba_shipment_id"], "sku": row["sku"],
              "asin": row["asin"], "fnsku": row["fnsku"]},
        prep_price_usd=float(row["prep_price_usd"]))  # the sample's operator is not ours: not attributed
