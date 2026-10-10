"""Receiving Manager - Round 3 Pod implementation.

Owner: B Sai Charan

This module adapts the Receiving Manager implementation from the
Round 2 repository to the shared Round 3 Pod evidence contract.

Responsibilities:
- Read the PO line and receiving inputs.
- Perform one batched vision extraction per receiving request.
- Preserve photo evidence references and SHA-256 hashes.
- Run deterministic Receiving checks.
- Emit the six shared Receiving check keys.
- Fail open to pending_review when vision extraction fails.
- Keep Receiving limited to physical receipt facts.
- Do not infer supplier/channel liability; downstream agents handle that.
"""

from __future__ import annotations

import hashlib
import json
import time
from functools import lru_cache
from pathlib import Path

from shared.utils.records import build_output, build_record, check
from shared.utils.server import make_app

from .core.comparison_engine import evaluate_shared_checks
from .core.models import (
    CheckVerdict,
    DamageType,
    POExpected,
    POHeader,
    SharedCheckKey,
    VisualObservation,
)
from .core.vision_extractor import (
    GeminiVisionExtractor,
    MockVisionExtractor,
    VisionExtractor,
    calculate_file_sha256,
)


STAGE = "receiving"
AGENT_ID = "receiving-manager@3.0"

MODEL_VERSION = "gemini-2.5-flash"

SHARED_CHECK_KEYS = [
    SharedCheckKey.IDENTITY_MATCH.value,
    SharedCheckKey.CARTON_COUNT.value,
    SharedCheckKey.QUANTITY.value,
    SharedCheckKey.CARTON_DAMAGE.value,
    SharedCheckKey.UNIT_DAMAGE.value,
    SharedCheckKey.QUALITY_FLAGS.value,
]


ROOT = Path(__file__).resolve().parents[2]
SAMPLE_CASES_PATH = ROOT / "data" / "sample" / "cases.json"


@lru_cache(maxsize=1)
def _sample_tenant_map() -> dict[str, str]:
    """Load the shared sample unit-to-organization ownership map."""

    try:
        cases = json.loads(
            SAMPLE_CASES_PATH.read_text(
                encoding="utf-8"
            )
        )
    except Exception as exc:
        raise LookupError(
            "Receiving tenancy data could not be loaded."
        ) from exc

    tenant_map = {}

    for case in cases:
        unit_id = case.get("unit_id")
        org_id = case.get("org_id")

        if unit_id and org_id:
            tenant_map[str(unit_id)] = str(org_id)

    return tenant_map


def _validate_tenancy(request: dict) -> None:
    """Reject requests where the subject belongs to another organization.

    The Round 3 Pod sample data defines the authoritative relationship
    between unit IDs and organizations in data/sample/cases.json.

    The request's subject.org_id must match the organization that owns
    subject.subject_id.

    InProcClient converts LookupError into AgentRejected.
    """

    subject = request.get("subject") or {}

    requested_org = subject.get("org_id")
    requested_subject = subject.get("subject_id")

    if not requested_org:
        raise LookupError(
            "Receiving request is missing subject.org_id."
        )

    if not requested_subject:
        raise LookupError(
            "Receiving request is missing subject.subject_id."
        )

    tenant_map = _sample_tenant_map()

    actual_org = tenant_map.get(
        str(requested_subject)
    )

    if actual_org is None:
        raise LookupError(
            "Receiving request rejected: unknown receiving subject."
        )

    if str(requested_org) != actual_org:
        raise LookupError(
            "Receiving request rejected: subject does not belong "
            "to the requested organization."
        )


def _record_id(request: dict) -> str:
    """Create a deterministic Receiving evidence record ID."""

    request_id = request.get("request_id")

    subject = request.get("subject", {})
    org_id = subject.get("org_id", "")
    subject_id = subject.get("subject_id", "")

    if request_id:
        raw = f"{org_id}:{request_id}"
    else:
        raw = f"{org_id}:{subject_id}"

    digest = hashlib.sha256(
        raw.encode("utf-8")
    ).hexdigest()[:8].upper()

    return f"RCV-{digest}"


def _get_expected(request: dict) -> POExpected:
    """Convert Pod request inputs into the Round 2 POExpected model."""

    inputs = request.get("inputs") or {}

    expected_raw = (
        inputs.get("expected")
        or inputs.get("expected_values")
        or {}
    )

    cartons = int(
        expected_raw.get("cartons_ordered", 1)
    )

    units_per_carton = int(
        expected_raw.get(
            "units_per_carton_ordered",
            24,
        )
    )

    quantity = int(
        expected_raw.get("quantity_ordered")
        or expected_raw.get("qty_ordered")
        or cartons * units_per_carton
    )

    return POExpected(
        sku=expected_raw.get(
            "sku",
            "SKU-DEFAULT",
        ),
        asin=expected_raw.get(
            "asin",
            "B0DEFAULT",
        ),
        product_title=expected_raw.get(
            "product_title",
            "General Product",
        ),
        expected_colour=(
            expected_raw.get("expected_colour")
            or expected_raw.get(
                "spec_colour",
                "n/a",
            )
        ),
        expected_variant=(
            expected_raw.get("expected_variant")
            or expected_raw.get(
                "spec_variant",
                "standard",
            )
        ),
        expected_components=(
            expected_raw.get("expected_components")
            or expected_raw.get(
                "spec_components",
                [],
            )
        ),
        cartons_ordered=cartons,
        units_per_carton_ordered=units_per_carton,
        quantity_ordered=quantity,
    )


def _get_po_header(request: dict) -> POHeader:
    """Convert Pod request inputs into the Round 2 POHeader model."""

    inputs = request.get("inputs") or {}
    subject = request.get("subject") or {}

    po_raw = inputs.get("po_header") or {}

    return POHeader(
        po_number=(
            po_raw.get("po_number")
            or inputs.get("po_number")
            or subject.get("refs", {}).get("po_number")
            or "PO-AUTO"
        ),
        po_line=int(
            po_raw.get("po_line")
            or inputs.get("po_line")
            or subject.get("refs", {}).get("po_line")
            or 1
        ),
        supplier=(
            po_raw.get("supplier")
            or inputs.get("supplier")
            or "Supplier Standard"
        ),
    )


def _unit_id(request: dict) -> str:
    """Resolve the Receiving subject/unit identifier."""

    subject = request.get("subject") or {}
    inputs = request.get("inputs") or {}

    return (
        inputs.get("unit_id")
        or subject.get("unit_id")
        or subject.get("subject_id")
        or "UNIT-UNKNOWN"
    )


def _photo_inputs(request: dict) -> dict:
    """Normalize Pod photo inputs for the vision extractor."""

    inputs = request.get("inputs") or {}

    photos = (
        inputs.get("photos")
        or inputs.get("photo_captures")
        or inputs.get("images")
        or inputs.get("evidence")
        or {}
    )

    if isinstance(photos, dict):
        return {
            str(view_type): str(photo)
            for view_type, photo in photos.items()
        }

    if isinstance(photos, list):
        result = {}

        for index, item in enumerate(photos):
            if isinstance(item, dict):
                reference = (
                    item.get("path")
                    or item.get("photo_reference")
                    or item.get("uri")
                )

                if reference:
                    view_type = (
                        item.get("view_type")
                        or f"view_{index}"
                    )

                    result[str(view_type)] = str(
                        reference
                    )
            else:
                result[f"view_{index}"] = str(item)

        return result

    return {}


def _evidence_refs(images: dict) -> list[str]:
    """Return content hashes for supplied photo evidence."""

    refs = []

    for reference in images.values():
        try:
            refs.append(
                calculate_file_sha256(reference)
            )
        except Exception:
            # A URI/reference may not be locally readable.
            # Preserve a deterministic reference rather than
            # inventing a random hash.
            refs.append(
                hashlib.sha256(
                    str(reference).encode("utf-8")
                ).hexdigest()
            )

    return refs


def _vision_engine() -> VisionExtractor:
    """Select Gemini when configured, otherwise deterministic mock vision."""

    import os

    api_key = os.getenv("GEMINI_API_KEY")

    if api_key:
        return GeminiVisionExtractor(
            api_key=api_key
        )

    return MockVisionExtractor()


def _uncertain_checks(
    expected: POExpected,
    evidence_refs: list[str],
    model_name: str,
    reason: str,
) -> list[dict]:
    """Create fail-open UNCERTAIN checks for a vision failure."""

    return [
        check(
            SharedCheckKey.IDENTITY_MATCH.value,
            "UNCERTAIN",
            0.0,
            expected={
                "sku": expected.sku,
                "asin": expected.asin,
                "title": expected.product_title,
            },
            observed=None,
            evidence_refs=evidence_refs,
            uncertain_reason=reason,
        ),
        check(
            SharedCheckKey.CARTON_COUNT.value,
            "UNCERTAIN",
            0.0,
            expected=expected.cartons_ordered,
            observed=None,
            evidence_refs=evidence_refs,
            uncertain_reason=reason,
        ),
        check(
            SharedCheckKey.QUANTITY.value,
            "UNCERTAIN",
            0.0,
            expected=expected.quantity_ordered,
            observed=None,
            evidence_refs=evidence_refs,
            uncertain_reason=reason,
        ),
        check(
            SharedCheckKey.CARTON_DAMAGE.value,
            "UNCERTAIN",
            0.0,
            expected="none",
            observed="uncertain",
            evidence_refs=evidence_refs,
            uncertain_reason=reason,
        ),
        check(
            SharedCheckKey.UNIT_DAMAGE.value,
            "UNCERTAIN",
            0.0,
            expected="none",
            observed="uncertain",
            evidence_refs=evidence_refs,
            uncertain_reason=reason,
        ),
        check(
            SharedCheckKey.QUALITY_FLAGS.value,
            "UNCERTAIN",
            0.0,
            expected={
                "expected_colour": expected.expected_colour,
                "expected_variant": expected.expected_variant,
                "expected_components": expected.expected_components,
            },
            observed=None,
            evidence_refs=evidence_refs,
            uncertain_reason=reason,
        ),
    ]


def handle(request: dict) -> dict:
    """Receive a Pod Agent Input and return a shared Agent Output."""

    started = time.perf_counter()

    subject = request["subject"]
    inputs = request.get("inputs") or {}

    org_id = subject.get("org_id")
    subject_id = subject.get("subject_id")

    if not org_id:
        raise LookupError(
            "Receiving request is missing subject.org_id."
        )

    if not subject_id:
        raise LookupError(
            "Receiving request is missing subject.subject_id."
        )

    # Enforce tenant isolation before doing any Receiving work.
    _validate_tenancy(request)

    unit_id = _unit_id(request)

    expected = _get_expected(request)
    po_header = _get_po_header(request)

    images = _photo_inputs(request)
    evidence_refs = _evidence_refs(images)

    engine = _vision_engine()

    model_name = getattr(
        engine,
        "model_name",
        MODEL_VERSION,
    )

    extraction_failed = False
    failure_reason = ""

    try:
        # IMPORTANT:
        # This is the single batched multimodal call
        # for the receiving request.
        extraction = engine.extract(
            po=expected,
            unit_id=unit_id,
            images=images,
        )

        if (
            extraction.status != "success"
            or extraction.observation is None
        ):
            extraction_failed = True

            failure_reason = (
                extraction.error_message
                or (
                    "Vision extractor returned status "
                    f"'{extraction.status}'."
                )
            )

            observation = VisualObservation(
                carton_damage=DamageType.UNCERTAIN,
                unit_damage=DamageType.UNCERTAIN,
                image_clarity=None,
            )

        else:
            observation = extraction.observation

    except Exception as exc:
        extraction_failed = True

        failure_reason = (
            f"Vision extraction failed: {exc}"
        )

        observation = VisualObservation(
            carton_damage=DamageType.UNCERTAIN,
            unit_damage=DamageType.UNCERTAIN,
            image_clarity=None,
        )

    latency_ms = int(
        round(
            (time.perf_counter() - started) * 1000
        )
    )

    if extraction_failed:
        checks = _uncertain_checks(
            expected=expected,
            evidence_refs=evidence_refs,
            model_name=model_name,
            reason=failure_reason,
        )

        outcome = "pending_review"

        reason = (
            "Receiving placed in pending_review because "
            "the vision extraction failed: "
            f"{failure_reason}"
        )

        verdict = "UNCERTAIN"
        record_status = "pending"

    else:
        (
            shared_checks,
            receiving_outcome,
            reason,
            _,
            _,
        ) = evaluate_shared_checks(
            expected=expected,
            observation=observation,
            evidence_refs=evidence_refs,
            model_version=model_name,
            latency_ms=latency_ms,
        )

        checks = []

        for item in shared_checks:
            check_item = {
                "check_key": (
                    item.check_key.value
                    if hasattr(
                        item.check_key,
                        "value",
                    )
                    else item.check_key
                ),
                "verdict": (
                    item.verdict.value
                    if hasattr(
                        item.verdict,
                        "value",
                    )
                    else item.verdict
                ),
                "confidence": item.confidence,
                "expected": item.expected,
                "observed": item.observed,
                "detail": item.detail,
                "evidence_refs": item.evidence_refs,
            }

            if check_item["verdict"] == "UNCERTAIN":
                check_item["uncertain_reason"] = getattr(
                    item,
                    "uncertain_reason",
                    "insufficient_evidence",
                )

            checks.append(check_item)

        outcome = receiving_outcome.value

        verdict = (
            "UNCERTAIN"
            if any(
                c["verdict"]
                == CheckVerdict.UNCERTAIN.value
                for c in checks
            )
            else (
                "FAIL"
                if any(
                    c["verdict"]
                    == CheckVerdict.FAIL.value
                    for c in checks
                )
                else "PASS"
            )
        )

        record_status = "completed"

    record = build_record(
        request,
        agent_id=AGENT_ID,
        record_id=_record_id(request),
        captured_at=(
            request.get("timestamp")
            or time.strftime(
                "%Y-%m-%dT%H:%M:%SZ",
                time.gmtime(),
            )
        ),
        operator_id=(
            inputs.get("operator_id")
            or "op_dock"
        ),
        unit_scope="po_line",
        refs={
            "po_number": po_header.po_number,
            "po_line": po_header.po_line,
            "sku": expected.sku,
            "asin": expected.asin,
        },
        checks=checks,
        outcome=outcome,
        status=record_status,
        model={
            "name": model_name,
            "version": model_name,
            "calls": 1,
        },
        inputs=[
            {
                "ref": str(reference),
                "sha256": evidence_refs[index],
                "kind": "photo",
            }
            for index, reference in enumerate(
                images.values()
            )
            if index < len(evidence_refs)
        ],
        reason=reason,
        payload={
            "supplier": po_header.supplier,
            "unit_id": unit_id,
            "quantity_ordered": (
                expected.quantity_ordered
            ),
            "cartons_ordered": (
                expected.cartons_ordered
            ),
            "shortfall_scope": (
                "physical_receipt_only"
            ),
            "liability_inference": (
                "not_performed_by_receiving"
            ),
            "model_verdict": (
                "observation_only"
            ),
        },
        verdict=verdict,
        confidence=(
            min(
                [
                    float(
                        c.get("confidence")
                        or 0.0
                    )
                    for c in checks
                ]
            )
            if checks
            else 0.0
        ),
        needs_human=(
            outcome == "pending_review"
        ),
        latency_ms=latency_ms,
    )

    return build_output(record)


app = make_app(STAGE, handle)