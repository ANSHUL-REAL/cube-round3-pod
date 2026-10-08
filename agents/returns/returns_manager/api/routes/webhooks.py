"""Webhook subscription management REST routes (§17 / P14).

Endpoints:
  POST   /api/v1/webhooks/subscriptions
  GET    /api/v1/webhooks/subscriptions
  DELETE /api/v1/webhooks/subscriptions/{subscription_id}
  GET    /api/v1/webhooks/deliveries
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field

from returns_manager.api.deps import PrincipalDep, ServicesDep
from returns_manager.errors import BadRequest, NotFound
from returns_manager.security.roles import Permission, require
from returns_manager.webhooks.models import (
    WebhookDeliveryRecord,
    WebhookEvent,
    WebhookSubscription,
)
from returns_manager.webhooks.service import get_webhook_service

router = APIRouter(prefix="/api/v1/webhooks", tags=["webhooks"])


class CreateSubscriptionRequest(BaseModel):
    url: str = Field(..., description="Destination webhook URL (HTTPS required)")
    secret: str = Field(..., min_length=16, description="Shared secret for HMAC-SHA256 signing")
    events: list[WebhookEvent] = Field(
        default=["evidence.finalized", "evidence.superseded"],
        description="List of events to subscribe to",
    )


@router.post(
    "/subscriptions",
    response_model=WebhookSubscription,
    summary="Register a webhook subscription (§17)",
)
async def create_subscription(
    body: CreateSubscriptionRequest,
    principal: PrincipalDep,
    svc: ServicesDep,
) -> WebhookSubscription:
    """Register a new webhook subscription for the caller's organization."""
    require(principal, Permission.ADMIN)
    service = get_webhook_service(svc.db, svc.settings.rm_webhook_allowlist)

    try:
        return await service.register_subscription(
            org_id=principal.org_id,
            url=body.url,
            secret=body.secret,
            created_by=principal.actor_label,
            events=body.events,
        )
    except ValueError as exc:
        raise BadRequest(str(exc)) from exc


@router.get(
    "/subscriptions",
    response_model=list[WebhookSubscription],
    summary="List active webhook subscriptions (§17)",
)
async def list_subscriptions(
    principal: PrincipalDep,
    svc: ServicesDep,
) -> list[WebhookSubscription]:
    """List all active webhook subscriptions for the caller's organization."""
    require(principal, Permission.ADMIN)
    service = get_webhook_service(svc.db, svc.settings.rm_webhook_allowlist)
    return await service.list_subscriptions(principal.org_id)


@router.delete(
    "/subscriptions/{subscription_id}",
    summary="Delete a webhook subscription (§17)",
)
async def delete_subscription(
    subscription_id: str,
    principal: PrincipalDep,
    svc: ServicesDep,
) -> dict[str, Any]:
    """Deactivate a webhook subscription."""
    require(principal, Permission.ADMIN)
    service = get_webhook_service(svc.db, svc.settings.rm_webhook_allowlist)
    ok = await service.delete_subscription(principal.org_id, subscription_id)
    if not ok:
        raise NotFound(f"Webhook subscription {subscription_id} not found")
    return {"status": "deleted", "subscription_id": subscription_id}


@router.get(
    "/deliveries",
    response_model=list[WebhookDeliveryRecord],
    summary="List webhook deliveries (§17)",
)
async def list_deliveries(
    principal: PrincipalDep,
    svc: ServicesDep,
) -> list[WebhookDeliveryRecord]:
    """List recent webhook delivery attempts for the caller's organization."""
    require(principal, Permission.ADMIN)
    service = get_webhook_service(svc.db, svc.settings.rm_webhook_allowlist)
    return await service.list_deliveries(principal.org_id)
