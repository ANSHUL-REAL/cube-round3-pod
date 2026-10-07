"""verify_box: photos + order in, sealed evidence record out.

Fail-open: if the vision model errors or times out, the photos and a record are still
produced, marked pending, so the operator can check by hand without losing the box.
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from . import __version__
from .catalogue import select_candidates
from .config import Settings
from .decision import LOCAL_GATE_VERSION, decide, reuse_check
from .evidence import new_record_id, seal, utcnow
from .models import (
    AGENT_NAME,
    Catalogue,
    Check,
    Decision,
    EvidenceRecord,
    ImageRef,
    Order,
    OrderLine,
    Outcome,
    QualityReport,
    RecordStatus,
    Verdict,
)
from .quality import PreparedImage, prepare_photo
from .vision.base import Perceiver
from .vision.prompt import PROMPT_VERSION

log = logging.getLogger(__name__)


class QualityRejected(Exception):
    def __init__(self, reports: list[QualityReport]):
        self.reports = reports
        super().__init__("One or more photos failed the quality gate.")


@dataclass
class Photo:
    data: bytes
    role: str = "top_down"


def prepare_photos(photos: list[Photo], settings: Settings) -> list[PreparedImage]:
    if not photos:
        raise ValueError("At least one photo of the open box is required.")
    if len(photos) > settings.max_box_photos:
        raise ValueError(f"At most {settings.max_box_photos} photos per box.")
    return [prepare_photo(p.data, settings, p.role) for p in photos]


def cost_usd(usage: dict, settings: Settings) -> float | None:
    if settings.cost_per_1m_input_usd is None or settings.cost_per_1m_output_usd is None:
        return None
    out_tokens = usage.get("output_tokens", 0) + usage.get("thinking_tokens", 0)
    return round(
        usage.get("input_tokens", 0) / 1e6 * settings.cost_per_1m_input_usd
        + out_tokens / 1e6 * settings.cost_per_1m_output_usd,
        6,
    )


def verify_box(
    order: Order,
    prepared: list[PreparedImage],
    catalogue: Catalogue,
    perceiver: Perceiver,
    settings: Settings,
    *,
    operator_label: str,
    catalogue_root: Path | None = None,
    force_quality: bool = False,
    captured_at: datetime | None = None,
    earlier_uses: list[dict] | None = None,
    extra_observations: dict | None = None,
) -> EvidenceRecord:
    if not prepared:
        raise ValueError("At least one photo of the box is needed.")
    if not order.lines:
        raise ValueError("The order has no lines to check the box against.")
    reports = [p.quality for p in prepared]
    if any(r.gate == "FAIL" for r in reports) and not force_quality:
        raise QualityRejected(reports)

    captured_at = captured_at or utcnow()
    candidates = select_candidates(order, catalogue, settings)
    images = [
        ImageRef(image_id=p.image_id, role=p.role, sha256=p.sha256, original_sha256=p.original_sha256,
                 mime=p.mime, width=p.width, height=p.height, quality=p.quality)
        for p in prepared
    ]
    subject = {
        "type": "outbound_box",
        "unit_id": order.unit_id,
        "order_id": order.order_id,
        "channel": order.channel,
        "shipment_id": order.shipment_id,
        "expected_lines": [line.model_dump() for line in order.lines],
    }
    agent = {"name": AGENT_NAME, "version": __version__, "prompt_version": PROMPT_VERSION}

    try:
        perception = perceiver.perceive(prepared, candidates, catalogue.allowed_inserts, catalogue_root)
    except Exception as exc:  # fail open on any model failure, never lose the capture
        log.warning("Perception failed for order %s: %s", order.order_id, exc)
        checks = [
            Check(check_key="image_quality",
                  verdict=Verdict.PASS if all(r.gate == "PASS" for r in reports) else Verdict.UNCERTAIN,
                  confidence=None, detail="Local photo checks ran.", model_version=LOCAL_GATE_VERSION),
            Check(check_key="vision", verdict=Verdict.NOT_CHECKED, confidence=None,
                  detail=f"Vision model unavailable: {exc}", model_version=settings.gemini_model),
        ]
        if earlier_uses is not None:
            checks.insert(1, reuse_check(order, earlier_uses)[0])
        record = EvidenceRecord(
            record_id=new_record_id(),
            organization_id=order.organization_id,
            client_id=order.client_id,
            agent=agent,
            subject=subject,
            captured_at=captured_at,
            operator_label=operator_label,
            images=images,
            observations={"error": str(exc), **(extra_observations or {})},
            checks=checks,
            outcome=Outcome(
                decision=Decision.PENDING, decided_by=None, decided_at=None,
                reasons=["The vision model did not answer. The photos are saved.", f"Cause: {exc}"],
                fix_instructions=["Check the box by hand against the order, then record your decision."],
            ),
            status=RecordStatus.PENDING,
        )
        return seal(record)

    result = decide(order, catalogue, perception, reports, settings, earlier_uses=earlier_uses)
    agent["model_version"] = perception.model_version
    agent["prompt_version"] = perception.prompt_version
    observations = {
        **result.observations,
        "usage": perception.usage,
        "cost_usd": cost_usd(perception.usage, settings),
        "cached_response": perception.cached,
        **(extra_observations or {}),
    }
    record = EvidenceRecord(
        record_id=new_record_id(),
        organization_id=order.organization_id,
        client_id=order.client_id,
        agent=agent,
        subject=subject,
        captured_at=captured_at,
        operator_label=operator_label,
        images=images,
        observations=observations,
        checks=result.checks,
        outcome=Outcome(
            decision=result.decision,
            decided_by="agent",
            decided_at=utcnow(),
            reasons=result.reasons,
            fix_instructions=result.fix_instructions,
        ),
        status=RecordStatus.PENDING_REVIEW if result.decision == Decision.UNCERTAIN else RecordStatus.FINAL,
    )
    return seal(record)


def rerun_box(
    record: EvidenceRecord,
    stored: list[bytes],
    catalogue: Catalogue,
    perceiver: Perceiver,
    settings: Settings,
    *,
    retried_by: str,
    catalogue_root: Path | None = None,
    earlier_uses: list[dict] | None = None,
) -> tuple[EvidenceRecord, list[PreparedImage]]:
    """Run the agent check again on a saved box, as a new record linked to the old one.

    Used when the model didn't answer the first time. The old record is never changed.
    The check runs against the order as it was when the photos were taken, and the photos
    are the exact stored bytes (their hashes are verified first).
    """
    if len(stored) != len(record.images):
        raise ValueError("Some photos of this box are missing.")
    prepared = []
    for ref, data in zip(record.images, stored):
        if hashlib.sha256(data).hexdigest() != ref.sha256:
            raise ValueError(f"Photo {ref.image_id} doesn't match its hash in the record.")
        prepared.append(PreparedImage(
            image_id=str(uuid.uuid4()), role=ref.role, jpeg=data, sha256=ref.sha256,
            original_sha256=ref.original_sha256, width=ref.width, height=ref.height, quality=ref.quality,
        ))
    subject = record.subject
    order = Order(
        order_id=subject["order_id"], organization_id=record.organization_id, client_id=record.client_id,
        unit_id=subject.get("unit_id"), channel=subject.get("channel"),
        lines=[OrderLine(**line) for line in subject["expected_lines"]],
    )
    link = {"retry_of": record.record_id, "retried_by": retried_by,
            "photos_taken_at": record.captured_at.isoformat()}
    new = verify_box(
        order, prepared, catalogue, perceiver, settings,
        operator_label=record.operator_label, catalogue_root=catalogue_root,
        force_quality=any(ref.quality.gate == "FAIL" for ref in record.images),
        earlier_uses=earlier_uses, extra_observations=link,
    )
    # If a person already decided the old box and the agent now disagrees, say so on the record.
    if record.box_overrides and new.outcome.decision != Decision.PENDING:
        human = record.box_overrides[-1]
        if human.new_decision != new.outcome.decision:
            new = seal(new.model_copy(update={"observations": {**new.observations, "disagreement": {
                "human_decision": human.new_decision.value, "human_operator": human.operator_label,
                "human_decided_at": human.at.isoformat(), "agent_decision": new.outcome.decision.value,
            }}}))
    return new, prepared
