"""Returns Manager: the pod's Round 3 agent entry point.

Round 2 agent by Prodduturu Krishna Babu (@krishnababuprodduturu), RTN-0038, brought into the pod. One batched Gemini
call per returned unit looks at 1 to 3 photos plus the seller's catalogue card and the condition rubric, and REPORTS
what it sees (identity features, parts present, defects, a proposed rubric grade). It never decides. Deterministic code
(`core/`) validates the report, fuses identity, does the parts arithmetic, grades the condition against the rubric and
runs the disposition engine: restock | refurbish | liquidate | dispose, or pending_review when a person must decide.

Round 3 adds what the pod needs: the Agent Input / Agent Output contract, tenancy by LookupError, hash-checked
captures, idempotent record ids, a pending record whenever it cannot judge, and the use of earlier evidence (Pack's
order and box count, Receiving's flags) with the latest overrides honoured (upstream.py).

Fail open: a missing capture, an unreadable photo, a product with no reference, no API key or a model error returns a
*pending* record (UNCERTAIN, with the reason and the captures kept) rather than an exception or an invented verdict.
Returns only runs for returned units (the flow's routing decides that).

Run:  uvicorn agents.returns.app:app --port 8104
"""
from __future__ import annotations

import logging
from functools import lru_cache

from shared.utils.log import get_logger
from shared.utils.records import build_output
from shared.utils.server import make_app
from shared.utils.stubs import require_same_subject

from . import adapter, captures, upstream
from .config import Settings
from .core import __version__
from .core.context import ReturnPhoto, assemble
from .core.errors import MissingReference
from .core.images import ImageDecodeError, barcode_decoder_available, process_photo
from .core.params import rules_version
from .core.pipeline import PhotoGate, run_pipeline
from .core.refs import load_references, reference_image_bytes
from .core.types import BarcodeDecode
from .judge import GeminiJudge, JudgeError
from .returned import resolve_return

STAGE = adapter.STAGE
log = get_logger("returns")


@lru_cache
def settings() -> Settings:
    """GEMINI_API_KEY and friends come from the environment or the pod's .env (git-ignored)."""
    return Settings()


def get_judge(st: Settings):
    """The model. Raises JudgeError(model_not_configured) when there is no key. Tests replace this function."""
    return GeminiJudge(st)


def _provider(judge) -> str | None:
    return "google" if isinstance(judge, GeminiJudge) else None


def handle(request: dict) -> dict:
    subject = request["subject"]
    org_id, unit_id = subject["org_id"], subject["subject_id"]
    require_same_subject(request)
    item = resolve_return(request)  # LookupError: unknown unit or another organisation's unit -> 404
    st = settings()

    inputs = request.get("inputs") or []
    if not any(i.get("kind", "image") == "image" for i in inputs):
        return adapter.pending(request, item, code="no_capture", retryable=True,
                               message="No photo of the returned item was provided, so nothing was checked.")
    try:
        loaded = captures.load(inputs, unit_id)
    except captures.CaptureError as exc:
        return adapter.pending(request, item, code="capture_unreadable", message=str(exc), retryable=False)

    used, extra = loaded[: st.returns_max_photos], loaded[st.returns_max_photos:]
    not_used = [i["ref"] for i, _ in extra]
    kept = [{"ref": i["ref"], "sha256_original": i["sha256"]} for i, _ in used]  # what a pending record keeps

    try:
        card, others, rubric, policy, rule_source = load_references(org_id, item.ordered_sku)
        refs = reference_image_bytes(card)
    except MissingReference as exc:
        return adapter.pending(request, item, code="no_product_reference", retryable=False, photos=kept,
                               message=("No judgment is possible without a verified product reference: "
                                        + "; ".join(exc.missing)),
                               payload={"missing_reference": exc.missing, "reasons": exc.reasons,
                                        "photos_not_used": not_used})

    try:
        processed = [process_photo(data) for _, data in used]
    except ImageDecodeError as exc:
        return adapter.pending(request, item, code="capture_unreadable", retryable=False,
                               message=f"A photo could not be read: {exc}")
    photos, rphotos = [], []
    for n, ((inp, _), p) in enumerate(zip(used, processed), 1):
        alias = f"P{n}"
        photos.append({"alias": alias, "ref": inp["ref"], "sha256_original": p.sha256_original,
                       "sha256_analysis": p.sha256_analysis, "width": p.original_width, "height": p.original_height,
                       "quality_status": p.quality.status, "quality_issues": p.quality.issues,
                       "quality_metrics": p.quality.metrics})
        rphotos.append(ReturnPhoto(
            alias=alias, ref=inp["ref"], sha256_original=p.sha256_original, sha256_analysis=p.sha256_analysis,
            analysis_bytes=p.analysis_bytes, quality_status=p.quality.status, gate_issues=tuple(p.quality.issues),
            barcodes=tuple(BarcodeDecode(alias, text, fmt) for text, fmt in p.barcodes),
            width=p.original_width, height=p.original_height))
    common = {"photos_not_used": not_used, "barcode_decoder": "zxing-cpp" if barcode_decoder_available() else "unavailable"}

    try:
        bundle = assemble(order_id=item.order_id, card=card, other_cards=others, rubric=rubric, policy=policy,
                          photos=rphotos, refs=refs, output_mode=st.returns_output_mode)
    except MissingReference as exc:
        return adapter.pending(request, item, code="no_product_reference", retryable=False, photos=kept,
                               message="; ".join(exc.missing), payload={"reasons": exc.reasons, **common})
    try:
        judge = get_judge(st)
        answer = judge.judge(bundle)
    except JudgeError as exc:
        calls = getattr(exc, "calls", 0) if exc.code != "model_not_configured" else 0
        return adapter.pending(request, item, code=exc.code, message=str(exc), retryable=exc.retryable, photos=kept,
                               payload=common, model_calls=calls, model_name=st.returns_model if calls else None)

    up = upstream.read(request, item)
    result = run_pipeline(
        answer.judgment, bundle.ctx,
        # Nobody is there to retake a photo mid-workflow: fewer than 2 usable photos is acknowledged here, which makes
        # the unit a pending review with the reason, not a refusal and not a guess.
        photo_gate=PhotoGate(non_fail_photos=sum(1 for p in photos if p["quality_status"] != "fail"),
                             acknowledged_warnings=True),
        rules_version=rules_version(), operator_state=item.operator_state)
    record = adapter.build(request, item, result, bundle, photos, not_used, answer, rule_source=rule_source, card=card,
                           up=up, provider=_provider(judge), manifest={**bundle.manifest, **common})
    log.info("returns_checked", extra={"ctx": {"org_id": org_id, "subject_id": unit_id,
                                              "outcome": record["decision"]["outcome"]}})
    return build_output(record)


app = make_app(STAGE, handle, version=__version__)
logging.getLogger("httpx").setLevel(logging.WARNING)
