"""What came back: the order, the SKU that was sold, and what the operator noted, scoped to the caller's organisation.

Order of precedence:
  1. ``context.return`` (or ``context.case.return``) on the request, used only when its ``org_id`` is the subject's:
     {"org_id", "order_id", "ordered_sku", "ordered_asin"?, "operator_id"?, "captured_at"?, "operator_state"?,
      "operator_disposition"?, "parts_list"?: "a;b" | [..]}
  2. The organisers' Round 2 returns sample (``returns_sample.csv``), looked up by (unit_id, org_id).

Anything else raises LookupError, which the server turns into a 404. A unit that belongs to another organisation is
never answered: that is the tenancy rule. (Same pattern as agents/pack/orders.py.)

``operator_state`` / ``operator_disposition`` are what a warehouse operator tapped. They are NEVER sent to the model
(Round 2 blinding, section 9.4). They are used after the judgment for the escalation rule "operator and model strongly
disagree", and are recorded so a reviewer can see where the agent and the operator differ.
"""
from __future__ import annotations

from dataclasses import dataclass

from shared.utils import sample_data


@dataclass(frozen=True)
class ReturnedItem:
    org_id: str
    unit_id: str
    order_id: str
    ordered_sku: str
    ordered_asin: str | None = None
    parts_list: tuple[str, ...] = ()
    operator_id: str | None = None
    captured_at: str | None = None
    operator_state: str | None = None
    operator_disposition: str | None = None
    source: str = "request"


def _parts(raw) -> tuple[str, ...]:
    items = raw.split(";") if isinstance(raw, str) else (raw or [])
    return tuple(p.strip() for p in items if p and p.strip())


def resolve_return(request: dict) -> ReturnedItem:
    subject = request["subject"]
    org_id, unit_id = subject["org_id"], subject["subject_id"]

    ctx = request.get("context") or {}
    given = ctx.get("return") or (ctx.get("case") or {}).get("return")
    if isinstance(given, dict) and given.get("org_id") == org_id:
        return ReturnedItem(
            org_id=org_id, unit_id=unit_id, order_id=given["order_id"], ordered_sku=given["ordered_sku"],
            ordered_asin=given.get("ordered_asin"), parts_list=_parts(given.get("parts_list")),
            operator_id=given.get("operator_id"), captured_at=given.get("captured_at"),
            operator_state=given.get("operator_state"), operator_disposition=given.get("operator_disposition"),
            source="request")

    row = sample_data.row("returns", unit_id, org_id)  # LookupError: another org's unit, or an unknown unit
    return ReturnedItem(
        org_id=org_id, unit_id=unit_id, order_id=row["order_id"], ordered_sku=row["ordered_sku"],
        ordered_asin=row["ordered_asin"] or None, parts_list=_parts(row["parts_list"]),
        operator_id=row["operator_id"] or None, captured_at=row["captured_at"] or None,
        operator_state=row["observed_state"] or None, operator_disposition=row["operator_disposition"] or None,
        source="returns_sample.csv")
