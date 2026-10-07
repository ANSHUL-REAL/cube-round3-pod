"""Where Pack gets the order it checks the box against, scoped to the caller's organisation.

Order of precedence:
  1. ``context.order`` on the request ({"org_id", "order_id", "lines": "SKU:qty;..." | [{"sku","qty"}], ...}),
     used only when its org_id is the subject's org_id.
  2. The organisers' Round 2 pack sample (``pack_sample.csv``), looked up by (unit_id, org_id).

Anything else raises LookupError, which the server turns into a 404. A unit that belongs to another
organisation is never answered: that is the tenancy rule.
"""
from __future__ import annotations

from shared.utils import sample_data

from .core.models import Order, OrderLine, parse_lines


def _lines(raw) -> list[OrderLine]:
    if isinstance(raw, str):
        return parse_lines(raw)
    return [OrderLine(sku=item["sku"], qty=int(item["qty"])) for item in raw]


def resolve_order(request: dict) -> tuple[Order, dict]:
    """The Order plus metadata (channel, operator_id, captured_at, operator_verdict) for the record."""
    subject = request["subject"]
    org_id, unit_id = subject["org_id"], subject["subject_id"]

    ctx = request.get("context") or {}
    given = ctx.get("order") or (ctx.get("case") or {}).get("order")
    if given and given.get("org_id") == org_id:
        order = Order(order_id=given["order_id"], organization_id=org_id, client_id=given.get("client_id"),
                      unit_id=unit_id, channel=given.get("channel"), shipment_id=given.get("shipment_id"),
                      lines=_lines(given["lines"]))
        return order, {"channel": given.get("channel"), "operator_id": given.get("operator_id"),
                       "captured_at": given.get("captured_at"), "operator_verdict": given.get("operator_verdict")}

    row = sample_data.row("pack", unit_id, org_id)  # LookupError for another org's unit, or an unknown unit
    order = Order(order_id=row["order_id"], organization_id=org_id, unit_id=unit_id, channel=row["channel"],
                  lines=parse_lines(row["order_lines"]))
    return order, {"channel": row["channel"], "operator_id": row["operator_id"], "captured_at": row["captured_at"],
                   "operator_verdict": row["operator_verdict"]}
