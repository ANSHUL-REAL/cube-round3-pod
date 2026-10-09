"""Units added from the app: a seller's order, typed in on /ui/units/new, becomes a unit the agents can check.

The sample units come from data/sample (cases.json and the organisers' CSVs). A unit added here carries its own order
on the case: every agent already takes ``context.case.order`` (Receiving's purchase-order line, Pack's order lines,
Prep's work order) and ``context.case.return`` (Returns) before it looks at the sample files, and only when the
order's org_id is the unit's own. So an added unit needs no CSV row.

Stored in the database when there is one (every worker sees them, and they survive a restart), else in this process.
A product that is not in the catalogue yet is added to the seller's own Pack catalogue
(agents/pack/catalogue/<org>/catalogue.json), which is copied into the database like the other files agents read.
"""
from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CATALOGUE_DIR = ROOT / "agents" / "pack" / "catalogue"
_MEM: dict[str, dict] = {}  # unit id -> case, while running without a database
_LOCK = threading.Lock()
_CACHE: list = [0.0, None]  # [read at (monotonic), [case, ...]]
_TTL_S = 15.0
MAX_QTY = 10_000
MARKS = ("fragile", "this_way_up", "keep_dry")  # handling marks a work order may ask for


class UnitError(ValueError):
    """What was typed cannot make a unit; the message says what to change."""


def stored() -> list[dict]:
    """Every added unit, oldest first. A failed read shows none, never an error page."""
    from shared.utils import db

    if not db.enabled():
        with _LOCK:
            return list(_MEM.values())
    if _CACHE[1] is not None and time.monotonic() - _CACHE[0] < _TTL_S:
        return _CACHE[1]
    try:
        rows = db.fetchall(f"select data from {db.SCHEMA}.units order by created_at, unit_id")
        cases = [r[0] if isinstance(r[0], dict) else json.loads(r[0]) for r in rows]
    except Exception:
        cases = []
    _CACHE[0], _CACHE[1] = time.monotonic(), cases
    return cases


def next_unit_id(taken: set[str]) -> str:
    """UNIT-0101 after UNIT-0100: one past the highest number in use. Photo folders are named by unit id alone, so
    an id is never reused, whichever seller had it."""
    nums = [int(m.group(1)) for t in taken if (m := re.fullmatch(r"UNIT-(\d+)", t))]
    return f"UNIT-{max(nums, default=0) + 1:04d}"


# ---------------------------------------------------------------- products
def _load(folder: Path) -> list[dict]:
    try:
        return json.loads((folder / "catalogue.json").read_text(encoding="utf-8"))["items"]
    except (OSError, ValueError, KeyError):
        return []


def products(org_id: str) -> list[dict]:
    """The products a seller can add a unit of: the shared sample catalogue plus the seller's own, by title."""
    items = {i["sku"]: i for i in _load(CATALOGUE_DIR / "sample")}
    if re.fullmatch(r"[A-Za-z0-9_-]{1,80}", org_id or ""):
        items |= {i["sku"]: i for i in _load(CATALOGUE_DIR / org_id)}
    return sorted(items.values(), key=lambda i: i["title"].lower())


def _variant(item: dict) -> str:
    a = item.get("attributes") or {}
    return a.get("variant") or a.get("size") or a.get("length") or "standard"


def _words(raw: str) -> list[str]:
    return [p.strip() for p in re.split(r"[;,\n]", raw or "") if p.strip()]


def add_product(org_id: str, title: str, colour: str, variant: str, components: str, looks: str) -> dict:
    """Add a product to the seller's own catalogue and return it. Its SKU comes from its title."""
    title = " ".join((title or "").split())
    if not 2 <= len(title) <= 80:
        raise UnitError("A product name is 2 to 80 characters.")
    parts = _words(components) or [title.lower()]
    if len(parts) > 12 or any(len(p) > 60 for p in parts):
        raise UnitError("List at most 12 parts, each under 60 characters.")
    folder = CATALOGUE_DIR / org_id
    own = _load(folder)
    taken = {i["sku"] for i in products(org_id)}
    base = "SKU-" + (re.sub(r"[^A-Z0-9]+", "-", title.upper()).strip("-")[:24] or "ITEM")
    sku, n = base, 2
    while sku in taken:
        sku, n = f"{base}-{n}", n + 1
    item = {"sku": sku, "title": title,
            "attributes": {"colour": (colour or "n/a").strip()[:40] or "n/a", "variant": (variant or "standard").strip()[:40] or "standard"},
            "components": parts,
            "sellable_unit": (looks or "").strip()[:200] or f"one {title.lower()}"}
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "catalogue.json"
    path.write_text(json.dumps({"organization_id": org_id, "items": own + [item]}, indent=1), encoding="utf-8")
    from shared.utils import db

    db.save_file("catalogue", path, org_id)
    return item


# ---------------------------------------------------------------- units
def _int(raw, name: str, lo: int = 1, hi: int = MAX_QTY) -> int:
    try:
        v = int(str(raw).strip())
    except (TypeError, ValueError):
        raise UnitError(f"{name} must be a whole number.") from None
    if not lo <= v <= hi:
        raise UnitError(f"{name} must be between {lo} and {hi}.")
    return v


def build_case(org_id: str, unit_id: str, item: dict, form: dict, actor: str) -> dict:
    """The case for a new unit, with the order every agent reads. `form` holds what was typed."""
    route = form.get("route")
    if route not in ("mfn", "fba"):
        raise UnitError("Choose where the unit goes: to a customer (Pack) or to Amazon FBA (Prep).")
    cartons = _int(form.get("cartons"), "Cartons", hi=500)
    per = _int(form.get("per_carton"), "Units per carton")
    if cartons * per > MAX_QTY:
        raise UnitError(f"At most {MAX_QTY} units on one delivery.")
    in_box = _int(form.get("in_box") or 1, "Units in the customer's box", hi=50)
    num = unit_id.split("-")[-1]
    sku = item["sku"]
    order = {
        "org_id": org_id, "unit_id": unit_id,
        # Receiving: the purchase-order line the delivery is checked against
        "po_number": (form.get("po_number") or "").strip()[:40] or f"PO-{num}", "po_line": 1,
        "supplier": (form.get("supplier") or "").strip()[:80] or "Supplier", "sku": sku,
        "asin": item.get("asin") or "", "product_title": item["title"],
        "spec_colour": (item.get("attributes") or {}).get("colour") or "n/a", "spec_variant": _variant(item),
        "spec_components": list(item.get("components") or []),
        "cartons_ordered": cartons, "units_per_carton_ordered": per, "qty_ordered": cartons * per,
    }
    if route == "mfn":  # Pack: the customer's order the open box is checked against
        order |= {"order_id": (form.get("order_id") or "").strip()[:40] or f"ORD-{num}",
                  "channel": (form.get("channel") or "shopify").strip()[:30] or "shopify", "lines": f"{sku}:{in_box}"}
    else:  # Prep: the work order the prepped unit is checked against
        marks = [m for m in MARKS if form.get(f"mark_{m}")]
        order |= {"fnsku": (form.get("fnsku") or "").strip()[:20] or f"X00{num.zfill(7)}",
                  "work_order_id": f"WO-{num}", "fba_shipment_id": (form.get("shipment") or "").strip()[:40] or f"FBA-{num}",
                  "requirements": {"polybag": bool(form.get("polybag")),
                                   "suffocation_warning": bool(form.get("polybag")),
                                   "expiry_date": bool(form.get("expiry")), "handling_marks": marks,
                                   "cover_original_barcode": True}}
    case = {"org_id": org_id, "unit_id": unit_id, "route": route, "returned": bool(form.get("returned")),
            "added": {"by": actor, "at": _now(), "product": item["title"], "sku": sku}, "order": order}
    if case["returned"]:  # Returns: what was sold, and what should come back with it
        case["return"] = {"org_id": org_id, "order_id": order.get("order_id") or f"ORD-{num}", "ordered_sku": sku,
                          "ordered_asin": item.get("asin") or None, "parts_list": list(item.get("components") or [])}
    return case


def save(case: dict, actor: str) -> bool:
    """Store a new unit. False if its id was taken meanwhile (another worker added one): pick the next id and retry."""
    from shared.utils import db

    if db.enabled():
        from psycopg.types.json import Jsonb

        row = db.fetchone(f"insert into {db.SCHEMA}.units (unit_id, org_id, data, created_by) values (%s,%s,%s,%s) "
                          f"on conflict (unit_id) do nothing returning unit_id",
                          (case["unit_id"], case["org_id"], Jsonb(case), actor))
        _CACHE[1] = None
        return row is not None
    with _LOCK:
        if case["unit_id"] in _MEM:
            return False
        _MEM[case["unit_id"]] = case
        return True


def _now() -> str:
    from shared.utils import db

    return db.now()
