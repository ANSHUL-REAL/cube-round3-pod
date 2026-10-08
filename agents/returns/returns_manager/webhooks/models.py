"""Webhook data models (§17 / P14)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, Field

from returns_manager.contract.models import SCHEMA_VERSION

WebhookEvent = Literal["evidence.finalized", "evidence.superseded"]


class WebhookPayload(BaseModel):
    event: WebhookEvent
    contract_version: str = SCHEMA_VERSION
    org_id: str
    unit_id: str
    record_id: str
    record_version: int
    document_sha256: str
    links: dict[str, str] = Field(default_factory=dict)


def _default_events() -> list[WebhookEvent]:
    return ["evidence.finalized", "evidence.superseded"]


class WebhookSubscription(BaseModel):
    subscription_id: str
    org_id: str
    url: str
    secret: str
    events: list[WebhookEvent] = Field(default_factory=_default_events)
    is_active: bool = True
    created_by: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class WebhookDeliveryRecord(BaseModel):
    delivery_id: str
    subscription_id: str
    org_id: str
    event: str
    unit_id: str
    url: str
    status_code: int | None = None
    success: bool
    attempt: int = 1
    duration_ms: float = 0.0
    error: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
