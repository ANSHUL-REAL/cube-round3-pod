"""Receiving Manager: the pod's Round 3 agent entry point.

One batched Gemini call per delivery looks at the photos and reports what it sees. The model is not shown the
purchase order and decides nothing. Fixed rules (rules.py) compare what it saw with the PO line and return accept /
accept_with_exceptions / reject / pending_review, each backed by named checks.

Fail open: a missing capture, an unreadable or altered photo, no API key, bad PO data or a model error returns a
*pending* record (UNCERTAIN, with the reason and the photos kept) rather than an exception or an invented verdict.

Run:  uvicorn agents.receiving.app:app --port 8101
"""
from __future__ import annotations

import logging
from functools import lru_cache

from shared.utils.log import get_logger
from shared.utils.records import build_output
from shared.utils.server import make_app
from shared.utils.stubs import effective_verdict, previous

from . import __version__, adapter, captures
from .config import Settings
from .models import OrderError
from .orders import resolve_order
from .rules import Thresholds, evaluate
from .vision import GeminiPerceiver, PerceptionError

STAGE = adapter.STAGE
log = get_logger("receiving")


@lru_cache
def settings() -> Settings:
    """GEMINI_API_KEY and friends come from the environment or the pod's git-ignored .env."""
    return Settings()


def get_perceiver(st: Settings):
    """The vision model. Raises PerceptionError when no API key is configured. Tests replace this function."""
    return GeminiPerceiver(st)


def _previous_receiving(request: dict) -> dict:
    """An earlier Receiving record for this workflow (a re-run), with the latest human override applied. Recorded for
    traceability; the new judgment is made from the new photos, never from the old verdict."""
    rec = previous(request, "receiving")
    if not rec:
        return {}
    return {"record_id": rec["record_id"], "verdict": rec["decision"]["verdict"],
            "effective_verdict": effective_verdict(request, rec)}


def handle(request: dict) -> dict:
    subject = request["subject"]
    org_id, unit_id = subject["org_id"], subject["subject_id"]
    try:
        po, meta = resolve_order(request)  # LookupError: unknown unit or another organisation's unit -> 404
    except OrderError as exc:
        return adapter.pending(request, None, None, code="order_invalid", retryable=False,
                               message=f"The purchase-order data cannot be checked against: {exc}")
    st = settings()

    inputs = request.get("inputs") or []
    if not any(i.get("kind", "image") == "image" for i in inputs):
        return adapter.pending(request, po, meta, code="no_capture", retryable=True,
                               message="No photos of the delivery were provided, so nothing was checked.")
    try:
        loaded = captures.load(inputs, unit_id)
        used, extra = loaded[: st.max_photos], loaded[st.max_photos:]
        photos = [captures.prepare(item, data, st.send_max_side_px) for item, data in used]
    except captures.CaptureError as exc:
        return adapter.pending(request, po, meta, code="capture_unreadable", message=str(exc), retryable=False)
    not_used = [item["ref"] for item, _ in extra]

    try:
        perceiver = get_perceiver(st)
        perception = perceiver.perceive(photos)
    except PerceptionError as exc:
        model = ({"name": st.gemini_model, "version": "unknown", "provider": "google", "calls": exc.calls,
                  "cost_usd": None} if exc.calls else None)
        return adapter.pending(request, po, meta, photos=photos, model=model, retryable=True,
                               code="model_unavailable" if exc.calls else "model_not_configured", message=str(exc),
                               payload={"photos_not_used": not_used})
    except Exception as exc:  # fail open: whatever went wrong, the capture is kept and a person is asked
        return adapter.pending(request, po, meta, code="model_error", retryable=True, photos=photos,
                               message=f"{type(exc).__name__}: {exc}", payload={"photos_not_used": not_used})

    th = Thresholds(st.rcv_min_clarity, st.rcv_min_confidence)
    result = evaluate(po, perception.observation, th, [p.ref for p in photos])
    record = adapter.build(request, po, meta, result, perception, photos, not_used, st, th,
                           _previous_receiving(request))
    log.info("receiving_checked", extra={"ctx": {"org_id": org_id, "subject_id": unit_id,
                                                  "outcome": record["decision"]["outcome"]}})
    return build_output(record)


app = make_app(STAGE, handle, version=__version__)
logging.getLogger("httpx").setLevel(logging.WARNING)
