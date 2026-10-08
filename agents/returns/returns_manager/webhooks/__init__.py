"""Webhooks module (§17 / P14)."""

from returns_manager.webhooks.models import (
    WebhookDeliveryRecord,
    WebhookEvent,
    WebhookPayload,
    WebhookSubscription,
)
from returns_manager.webhooks.service import (
    WebhookService,
    get_webhook_service,
    is_url_allowed,
)
from returns_manager.webhooks.signer import compute_signature, verify_signature

__all__ = [
    "WebhookDeliveryRecord",
    "WebhookEvent",
    "WebhookPayload",
    "WebhookService",
    "WebhookSubscription",
    "compute_signature",
    "get_webhook_service",
    "is_url_allowed",
    "verify_signature",
]
