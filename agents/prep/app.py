"""Prep Manager: the pod's Round 3 agent entry point.

One batched vision call per unit looks at the photos of the prepped unit and reports what is VISIBLE (is a bag
sealed, is the label flat, what does the label read, which handling marks are there). The model never decides.
Fixed rules (rules.py) compare those observations with what the unit's work order required and return
compliant / non_compliant / pending_review, each backed by named checks.

Fail open: a missing capture, an altered or unreadable photo, no API key, a model error or an unusable model answer
returns a *pending* record (UNCERTAIN, with the reason and the photos kept), never an exception or an invented verdict.
Only FBA units reach Prep (route == "fba"); the flow's routing decides that, and we refuse anything else.

Run:  uvicorn agents.prep.app:app --port 8102
"""
from __future__ import annotations

from functools import lru_cache

from shared.utils.log import get_logger
from shared.utils.records import build_output
from shared.utils.server import make_app
from shared.utils.stubs import effective_verdict, previous

from . import adapter, captures, rules, workorders
from .settings import Settings, get_settings
from .vision import GeminiObserver, Observer, PerceptionError

STAGE = adapter.STAGE
log = get_logger("prep")


@lru_cache
def settings() -> Settings:
    """GEMINI_API_KEY and friends come from the environment or the pod's git-ignored .env."""
    return get_settings()


def get_observer(st: Settings) -> Observer:
    """The vision model. Raises PerceptionError when no API key is configured. Tests replace this function."""
    return GeminiObserver(st)


def _upstream(request: dict) -> dict:
    """What Receiving said about this unit, with the latest human override applied.

    Recorded for traceability and shown in the decision reason when it is not a PASS. It does NOT change any prep
    check: Prep judges the prepped unit against its work order, which is a different question from what arrived.
    """
    rec = previous(request, "receiving")
    if not rec:
        return {}
    effective = effective_verdict(request, rec)
    return {"receiving": {
        "record_id": rec["record_id"], "status": rec["status"], "verdict": rec["decision"]["verdict"],
        "effective_verdict": effective, "overridden": effective != rec["decision"]["verdict"],
        "failed_checks": [c["check_key"] for c in rec["checks"] if c["verdict"] == "FAIL"]}}


def handle(request: dict) -> dict:
    subject = request["subject"]
    org_id, unit_id = subject["org_id"], subject["subject_id"]
    if subject.get("route") != "fba":
        raise LookupError(f"Prep handles FBA units only; {unit_id} has route {subject.get('route')!r}")
    order = workorders.resolve(request)  # LookupError: unknown unit or another organisation's unit -> 404
    st = settings()
    upstream = _upstream(request)
    fail = dict(order=order, upstream=upstream)

    inputs = [i for i in request.get("inputs") or [] if i.get("kind", "image") == "image"]
    if not inputs:
        return adapter.pending(request, code="no_capture", retryable=True,
                               message="No photo of the prepped unit was provided, so nothing was checked.", **fail)
    try:
        loaded = captures.load(inputs, unit_id)
        used, extra = loaded[: st.prep_max_photos], loaded[st.prep_max_photos:]
        prepared = [captures.prepare(c, st.prep_send_max_side_px) for c in used]
    except captures.CaptureError as exc:
        return adapter.pending(request, code="capture_unreadable", message=str(exc), retryable=False, **fail)
    captured_at, source = (order.captured_at, "context.order") if order.captured_at else (
        min(c.mtime for c in used), "file_mtime")
    fail.update(photos=prepared, extra=extra, captured_at=captured_at)

    try:
        observer = get_observer(st)
    except PerceptionError as exc:
        return adapter.pending(request, code=exc.code, message=str(exc), retryable=True, **fail)
    pack = order.requirements
    try:
        observed = observer.observe([p.jpeg for p in prepared], pack.observable())
    except PerceptionError as exc:
        return adapter.pending(request, code=exc.code, message=str(exc), retryable=True, calls=exc.attempts, **fail)
    except Exception as exc:  # any other model-side failure still must not stop the line
        log.warning("prep_model_error", extra={"ctx": {"org_id": org_id, "subject_id": unit_id,
                                                       "error": type(exc).__name__}})
        return adapter.pending(request, code="model_unavailable", retryable=True,
                               message=f"{type(exc).__name__}: {exc}", **fail)

    rule_checks = rules.evaluate(pack, observed.response, [p.ref for p in prepared])
    record = adapter.build(request, order, pack, rule_checks, observed, prepared, extra, captured_at, source, upstream)
    log.info("prep_checked", extra={"ctx": {"org_id": org_id, "subject_id": unit_id,
                                            "outcome": record["decision"]["outcome"]}})
    return build_output(record)


app = make_app(STAGE, handle, version=adapter.VERSION)
