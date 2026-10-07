"""Where Receiving gets the purchase-order line it checks a delivery against, scoped to the caller's organisation.

Order of precedence:
  1. ``context.order`` (or ``context.case.order``) on the request, used only when its ``org_id`` is the subject's:
     {"org_id", "po_number", "po_line", "supplier", "sku", "asin", "product_title", "spec_colour", "spec_variant",
      "spec_components": "a;b" | ["a", "b"], "cartons_ordered", "units_per_carton_ordered", "qty_ordered",
      optional "operator_id", "captured_at"}
  2. The organisers' Round 2 receiving sample (``receiving_sample.csv``), looked up by (unit_id, org_id). Only its
     ORDERED / SPEC columns are read. The received, damage and identity columns are the answers the agent is meant to
     find by looking at photos, and are never used.

Anything else raises LookupError, which the server turns into a 404. A unit that belongs to another organisation is
never answered: that is the tenancy rule. Round 2 finding F-08: in Receiving, ``unit_id`` is a PO line.
"""
from __future__ import annotations

from shared.utils import sample_data

from .models import PurchaseOrderLine


def resolve_order(request: dict) -> tuple[PurchaseOrderLine, dict]:
    """The PO line plus record metadata (operator_id, captured_at). LookupError for another tenant's unit;
    OrderError for PO data that cannot be judged against."""
    subject = request["subject"]
    org_id, unit_id = subject["org_id"], subject["subject_id"]

    ctx = request.get("context") or {}
    given = ctx.get("order") or (ctx.get("case") or {}).get("order")
    if given and given.get("org_id") == org_id:
        return PurchaseOrderLine.parse(given), {"operator_id": given.get("operator_id"),
                                                "captured_at": given.get("captured_at")}

    row = sample_data.row("receiving", unit_id, org_id)  # LookupError for another org's unit, or an unknown unit
    return PurchaseOrderLine.parse(row), {"operator_id": row["operator_id"], "captured_at": row["captured_at"]}
