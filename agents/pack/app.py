"""Pack Manager: the pod's Round 3 agent entry point.

One batched Gemini call per box looks at the photo(s) and reports what is in the box. The model never sees
the order and never decides. Fixed rules (core/decision.py) compare what it saw with the order and return
seal / stop_and_fix / pending_review, each backed by named checks.

Fail open: a missing capture, an unreadable photo, no API key or a model error returns a *pending* record
(UNCERTAIN, with the reason and the photos kept) rather than an exception or an invented verdict.
Only merchant-fulfilled / 3PL units reach Pack (route == "mfn"); the flow's routing decides that.

Run:  uvicorn agents.pack.app:app --port 8103
"""
from __future__ import annotations

import logging
from functools import lru_cache
from pathlib import Path

from shared.utils.log import get_logger
from shared.utils.records import build_output
from shared.utils.server import make_app
from shared.utils.stubs import effective_verdict, previous, require_same_subject

from . import adapter, captures, ledger
from .core import __version__
from .core.catalogue import load_org_catalogue
from .core.config import Settings
from .core.models import Decision
from .core.pipeline import verify_box
from .core.quality import ImageDecodeError, prepare_photo
from .core.vision.base import PerceptionError
from .core.vision.gemini import GeminiPerceiver
from .orders import resolve_order

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
STAGE = adapter.STAGE
log = get_logger("pack")

# The orchestrator gives a stage 75 s (flow.json defaults.timeout_s, decision D-O07). The model call is bounded inside
# that: 2 attempts x 28 s + the 2 s back-off = 58 s worst case. Measured p95 latency was 10.4 s, but a live rehearsal
# on 2026-10-09 saw a real call pass 12 s, so the old 12 s bound failed a demo step that would have answered. A call that fails here becomes a pending
# record the orchestrator can retry, not a timeout that loses the capture.
MODEL_TIMEOUT_S = 28.0
MODEL_RETRIES = 1


@lru_cache
def settings() -> Settings:
    """Round 2 settings. GEMINI_API_KEY and friends come from the environment or the pod's .env."""
    return Settings(catalogue_dir=str(HERE / "catalogue"), cache_dir=str(ROOT / ".cache" / "pack-vlm"),
                    gemini_timeout_s=MODEL_TIMEOUT_S, gemini_max_retries=MODEL_RETRIES)


def get_perceiver(st: Settings):
    """The vision model. Raises PerceptionError when no API key is configured. Tests replace this function."""
    return GeminiPerceiver(st)


def _provider(perceiver) -> str | None:
    return "google" if isinstance(perceiver, GeminiPerceiver) else None


def _upstream(request: dict) -> dict:
    """What Receiving said about this unit, using the latest human override. Informational: Pack judges the box
    against the order, so a Receiving exception is recorded here but does not change the box verdict."""
    rec = previous(request, "receiving")
    if not rec:
        return {}
    return {"receiving": {"record_id": rec["record_id"], "verdict": rec["decision"]["verdict"],
                          "effective_verdict": effective_verdict(request, rec)}}


def handle(request: dict) -> dict:
    subject = request["subject"]
    org_id, unit_id = subject["org_id"], subject["subject_id"]
    require_same_subject(request)
    try:
        order, meta = resolve_order(request)  # LookupError: unknown unit or another organisation's unit -> 404
        if not order.lines:
            raise ValueError("the order has no lines to check the box against")
    except ValueError as exc:  # a bad order line (quantity 0, "A:1,B:2", no lines) is the order's fault, not a crash
        return adapter.pending(request, None, None, code="order_invalid", retryable=False,
                               message=f"The order cannot be checked: {exc}")
    st = settings()

    inputs = request.get("inputs") or []
    if not any(i.get("kind", "image") == "image" for i in inputs):
        return adapter.pending(request, order, meta, code="no_capture", retryable=True,
                               message="No photo of the open box was provided, so nothing was checked.")
    try:
        loaded = captures.load(inputs, unit_id)
    except captures.CaptureError as exc:
        return adapter.pending(request, order, meta, code="capture_unreadable", message=str(exc), retryable=False)

    used, extra = loaded[: st.max_box_photos], loaded[st.max_box_photos:]
    not_used = [item["ref"] for item, _ in extra]
    try:
        prepared = [prepare_photo(data, st) for _, data in used]
    except (ImageDecodeError, ValueError) as exc:
        return adapter.pending(request, order, meta, code="capture_unreadable", retryable=False,
                               message=f"A photo could not be decoded: {exc}")
    photos = [{"ref": item["ref"], "original_sha256": p.original_sha256, "analysed_sha256": p.sha256,
               "width": p.width, "height": p.height, "quality_gate": p.quality.gate}
              for (item, _), p in zip(used, prepared)]

    try:
        perceiver = get_perceiver(st)
    except PerceptionError as exc:
        return adapter.pending(request, order, meta, code="model_not_configured", message=str(exc), retryable=True,
                               photos=photos, payload={"photos_not_used": not_used})

    catalogue, root = load_org_catalogue(st.catalogue_dir, org_id)
    hashes = [p.sha256 for p in prepared]
    rec = verify_box(
        order, prepared, catalogue, perceiver, st, operator_label=meta.get("operator_id") or "pod-agent",
        catalogue_root=root,
        force_quality=True,  # nobody to retake it mid-workflow: a bad photo becomes UNCERTAIN, with the reason
        earlier_uses=ledger.earlier_uses(org_id, hashes),
    )
    if rec.outcome.decision == Decision.PENDING:  # the model did not answer; verify_box kept the capture
        return adapter.pending(request, order, meta, code="model_unavailable", retryable=True, photos=photos,
                               message=str(rec.observations.get("error", "the vision model did not answer")),
                               checks=adapter.map_checks(rec, [p["ref"] for p in photos]),
                               payload={"photos_not_used": not_used})

    out_record = adapter.build(request, order, meta, rec, photos, not_used, st, provider=_provider(perceiver),
                               upstream=_upstream(request))
    ledger.remember(org_id, hashes, order.order_id, out_record["record_id"])
    log.info("pack_checked", extra={"ctx": {"org_id": org_id, "subject_id": unit_id,
                                            "outcome": out_record["decision"]["outcome"]}})
    return build_output(out_record)


app = make_app(STAGE, handle, version=__version__)
logging.getLogger("httpx").setLevel(logging.WARNING)
