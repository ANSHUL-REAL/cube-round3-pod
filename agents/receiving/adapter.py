"""Build the Round 3 Evidence Record for a Receiving inspection, judged or pending."""
from __future__ import annotations

import re

from shared.utils.records import build_output, build_record, error_obj, utcnow

from . import __version__
from .config import Settings
from .models import Perception, PurchaseOrderLine
from .rules import ENGINE, Result, Thresholds
from .vision import Photo, cost_usd

STAGE = "receiving"
AGENT_ID = f"receiving-manager@{__version__}"


def _safe(request: dict) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "-", request["request_id"])


def record_id_for(request: dict) -> str:
    """Same request in, same record_id out (the contract's idempotency rule)."""
    return "RCV-" + _safe(request)


def pending_record_id(request: dict) -> str:
    return "RCV-PENDING-" + _safe(request)


def _po_payload(po: PurchaseOrderLine | None) -> dict:
    if po is None:
        return {}
    return {"supplier": po.supplier, "po_number": po.po_number, "po_line": po.po_line, "sku": po.sku,
            "qty_ordered": po.qty_ordered, "cartons_ordered": po.cartons_ordered,
            "units_per_carton_ordered": po.units_per_carton_ordered}


def _refs(po: PurchaseOrderLine | None) -> dict:
    return {} if po is None else {"po_number": po.po_number, "po_line": po.po_line, "sku": po.sku, "asin": po.asin}


def _inputs(photos: list[Photo]) -> list[dict]:
    return [{"ref": p.ref, "sha256": p.original_sha256, "kind": "image"} for p in photos]


def photo_payload(photos: list[Photo]) -> list[dict]:
    return [{"ref": p.ref, "view": p.view, "original_sha256": p.original_sha256, "analysed_sha256": p.sha256,
             "width": p.width, "height": p.height} for p in photos]


def model_block(perception: Perception, settings: Settings) -> dict:
    return {"name": perception.model_name, "version": perception.model_version, "provider": perception.provider,
            "prompt_version": perception.prompt_version, "calls": perception.calls,
            "cost_usd": cost_usd(settings, perception.usage)}


def build(request: dict, po: PurchaseOrderLine, meta: dict, result: Result, perception: Perception,
          photos: list[Photo], not_used: list[str], settings: Settings, th: Thresholds, previous: dict) -> dict:
    d = result.derived
    payload = {
        **_po_payload(po),
        "qty_received": d["qty_received"], "shortfall_units": d["shortfall_units"], "overage_units": d["overage_units"],
        "cartons_received": d["cartons_received"], "units_per_carton_counted": d["units_per_carton_counted"],
        "quality_flags": d.get("quality_flags", []), "missing_components": d.get("missing_components", []),
        # Round 2 finding F-10: a shortfall here is supplier-side, before the goods reached any channel. It is evidence
        # for a supplier dispute, not for a channel inbound-loss claim.
        "shortfall_scope": "supplier_inbound",
        "observation": perception.observation.model_dump(),
        "photos": photo_payload(photos), "photos_not_used": not_used,
        "usage": perception.usage, "decision_engine": ENGINE,
        "thresholds": {"min_clarity": th.min_clarity, "min_confidence": th.min_confidence},
        "previous_receiving": previous,
    }
    return build_record(
        request, agent_id=AGENT_ID, record_id=record_id_for(request),
        captured_at=meta.get("captured_at") or utcnow(), operator_id=meta.get("operator_id"),
        unit_scope="po_line", refs=_refs(po), checks=result.checks, outcome=result.outcome,
        needs_human=result.needs_human, reason=result.reason, model=model_block(perception, settings),
        inputs=_inputs(photos), payload=payload, latency_ms=perception.latency_ms)


def pending(request: dict, po: PurchaseOrderLine | None, meta: dict | None, *, code: str, message: str,
            retryable: bool, photos: list[Photo] | None = None, model: dict | None = None,
            payload: dict | None = None) -> dict:
    """Fail open: a record exists, says UNCERTAIN and why, keeps the photos, and has no checks (nothing was judged,
    so nothing is invented)."""
    meta = meta or {}
    record = build_record(
        request, agent_id=AGENT_ID, record_id=pending_record_id(request),
        captured_at=meta.get("captured_at") or utcnow(), operator_id=meta.get("operator_id"), unit_scope="po_line",
        refs=_refs(po), checks=[], outcome="pending_review", verdict="UNCERTAIN", needs_human=True,
        reason=f"{code}: {message}", model=model or {"name": "none", "version": "0", "calls": 0, "cost_usd": None},
        inputs=_inputs(photos or []),
        payload={**_po_payload(po), "photos": photo_payload(photos or []), **(payload or {})},
        status="pending" if retryable else "error",
        error=error_obj(code, message, retryable=retryable, stage=STAGE, agent_id=AGENT_ID))
    return build_output(record, next_step="retry" if retryable else "review", reason=message)
