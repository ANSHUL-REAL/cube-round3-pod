"""What the earlier stages said about this unit, read for the Returns judgment (what was sent vs what came back).

Reads ``request["previous_evidence"]`` only (read-only; it never changes a previous record) and honours the LATEST
workflow override of each record (``context.overrides``; ``shared.utils.stubs.effective_verdict``).

What it uses, and how:

Pack  (merchant-fulfilled units; its record id may be absent for FBA units, which skip Pack)
  payload.order_id, payload.order_lines {sku: qty}, payload.observed_in_box {sku: count}
  * `order_mismatch`     Pack's order is not the order that was returned          -> CONFLICT
  * `sku_not_in_order`   the returned SKU is not on Pack's order lines            -> CONFLICT
  * `not_packed`         Pack's record says none of the returned SKU was in the box (effective Pack verdict FAIL and
                         the SKU absent from observed_in_box)                      -> CONFLICT
  A CONFLICT means two records disagree about what was sold or sent. EVIDENCE-CONTRACT section 9 says UNCERTAIN, do not
  guess: the identity check becomes UNCERTAIN (`conflicting_evidence`) and a person decides.
  What was sent: if Pack's effective verdict is PASS (the box matched the order, as judged or as overridden by a
  person), what was sent is the order lines; if it is FAIL and Pack counted the box, what was sent is that count;
  otherwise it is unknown and nothing is concluded.

Receiving (every route)
  effective verdict, payload.quality_flags, payload.shortfall_units, its identity_match / unit_damage checks
  * Only context. When Receiving's EFFECTIVE verdict is FAIL and it flagged `missing_components`, `wrong_variant`,
    `wrong_colour` or `obvious_defect` (or a failed unit_damage check), a matching finding here is annotated "may
    predate the sale" in the check detail, with Receiving's record id in that check's evidence_refs. No verdict changes:
    who is at fault is Recovery's question. An override of Receiving to PASS removes the annotation.

Everything is optional. A missing or unusable upstream record gives no conflict and says so in ``notes``.
"""
from __future__ import annotations

from shared.utils.stubs import effective_verdict, previous

from .returned import ReturnedItem

SUPPLIER_FLAGS = {
    "completeness": {"missing_components"},
    "identity": {"wrong_variant", "wrong_colour"},
    "condition": {"obvious_defect"},
}


def _counts(value) -> dict[str, int] | None:
    """`{sku: count}` from a payload value, or None if it is not that shape."""
    if not isinstance(value, dict):
        return None
    try:
        return {str(k): int(v) for k, v in value.items()}
    except (TypeError, ValueError):
        return None


def _overridden(request: dict, record: dict) -> bool:
    return any(o["supersedes"]["record_id"] == record["record_id"]
               for o in (request.get("context") or {}).get("overrides", []))


def read_pack(request: dict, item: ReturnedItem) -> dict | None:
    rec = previous(request, "pack")
    if rec is None:
        return None
    payload = rec.get("payload") or {}
    effective = effective_verdict(request, rec)
    order_lines = _counts(payload.get("order_lines"))
    observed = _counts(payload.get("observed_in_box")) if rec["status"] == "completed" else None
    if effective == "PASS" and order_lines:
        sent, basis = dict(order_lines), "pack_passed_so_sent_equals_order"
    elif effective == "FAIL" and observed is not None:
        sent, basis = dict(observed), "pack_counted_the_box"
    else:
        sent, basis = None, "unknown"
    return {"record_id": rec["record_id"], "status": rec["status"], "verdict": rec["decision"]["verdict"],
            "effective_verdict": effective, "overridden": _overridden(request, rec),
            "outcome": rec["decision"]["outcome"], "order_id": payload.get("order_id"), "order_lines": order_lines,
            "observed_in_box": observed, "sent": sent, "sent_basis": basis}


def read_receiving(request: dict) -> dict | None:
    rec = previous(request, "receiving")
    if rec is None:
        return None
    payload = rec.get("payload") or {}
    effective = effective_verdict(request, rec)
    flags = [str(f) for f in (payload.get("quality_flags") or [])]
    checks = {c["check_key"]: c["verdict"] for c in rec.get("checks", [])}
    return {"record_id": rec["record_id"], "status": rec["status"], "verdict": rec["decision"]["verdict"],
            "effective_verdict": effective, "overridden": _overridden(request, rec),
            "outcome": rec["decision"]["outcome"], "unit_scope": (rec.get("subject") or {}).get("unit_scope"),
            "quality_flags": flags, "shortfall_units": payload.get("shortfall_units"),
            "unit_damage": checks.get("unit_damage"), "identity_match": checks.get("identity_match")}


def read(request: dict, item: ReturnedItem) -> dict:
    """Everything the adapter needs from upstream evidence, as plain data."""
    pack, receiving = read_pack(request, item), read_receiving(request)
    conflicts: list[dict] = []
    notes: list[str] = []
    sent_vs_returned: dict = {"ordered_sku": item.ordered_sku, "ordered_qty": None, "sent_qty": None, "basis": "unknown",
                              "pack_record_id": None}

    if pack is None:
        notes.append("no Pack record (FBA units skip Pack): what was sent is not known from evidence")
    else:
        rid = pack["record_id"]
        sent_vs_returned.update(pack_record_id=rid, basis=pack["sent_basis"])
        lines = pack["order_lines"]
        if lines is not None:
            sent_vs_returned["ordered_qty"] = lines.get(item.ordered_sku)
        if pack["order_id"] and pack["order_id"] != item.order_id:
            conflicts.append({"code": "order_mismatch", "record_ids": [rid],
                              "detail": f"Pack judged order {pack['order_id']} but the return is for order {item.order_id}"})
        if lines and item.ordered_sku not in lines:
            conflicts.append({"code": "sku_not_in_order", "record_ids": [rid],
                              "detail": f"the returned SKU {item.ordered_sku} is not on Pack's order lines {sorted(lines)}"})
        if pack["sent"] is not None:
            sent_vs_returned["sent_qty"] = pack["sent"].get(item.ordered_sku, 0)
            if sent_vs_returned["sent_qty"] == 0 and (sent_vs_returned["ordered_qty"] or 0) >= 1:
                conflicts.append({"code": "not_packed", "record_ids": [rid],
                                  "detail": f"Pack's record says no {item.ordered_sku} was in the box (it counted "
                                            f"{pack['sent'] or 'nothing'}), yet one came back"})
        else:
            notes.append(f"Pack {rid} (effective {pack['effective_verdict']}, status {pack['status']}) does not say "
                         "what was sent")
        if pack["overridden"]:
            notes.append(f"Pack {rid} was overridden by a person: its effective verdict {pack['effective_verdict']} is used")

    causes: dict[str, list[dict]] = {"identity": [], "completeness": [], "condition": []}
    if receiving is None:
        notes.append("no Receiving record")
    else:
        rid = receiving["record_id"]
        if receiving["overridden"]:
            notes.append(f"Receiving {rid} was overridden by a person: its effective verdict "
                         f"{receiving['effective_verdict']} is used")
        if receiving["effective_verdict"] == "FAIL":
            flags = set(receiving["quality_flags"])
            for topic, wanted in SUPPLIER_FLAGS.items():
                hit = sorted(flags & wanted)
                if topic == "condition" and receiving["unit_damage"] == "FAIL":
                    hit = sorted({*hit, "unit_damage"})
                if hit:
                    causes[topic].append({"record_id": rid, "flags": hit,
                                          "text": f"Receiving {rid} flagged {', '.join(hit)} at receipt, so this may predate the sale"})
        if receiving["unit_scope"] and receiving["unit_scope"] != "unit":
            notes.append(f"Receiving counts per {receiving['unit_scope']} (finding F-08): its quantities are not a per-unit fact")

    used = [r for r in (pack and pack["record_id"], receiving and receiving["record_id"]) if r]
    return {"pack": pack, "receiving": receiving, "conflicts": conflicts, "supplier_side": causes,
            "sent_vs_returned": sent_vs_returned, "notes": notes, "record_ids": used}
