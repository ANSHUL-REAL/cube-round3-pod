"""Webhook service and delivery dispatcher (§17 / P14).

Subscriptions and delivery history are persisted in `rm.webhook_subscriptions` /
`rm.webhook_deliveries` (migration 0011) so every process (API server, worker) reads
the same subscription list - a subscription registered through the REST API used to be
invisible to the worker process that actually dispatches events, because the original
implementation kept everything in a process-local in-memory singleton.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import time
from urllib.parse import urlparse

import httpx

from returns_manager.db.pool import Database
from returns_manager.ids import new_id
from returns_manager.webhooks.models import (
    WebhookDeliveryRecord,
    WebhookEvent,
    WebhookPayload,
    WebhookSubscription,
)
from returns_manager.webhooks.signer import compute_signature

# Every network a webhook destination must never resolve to unless the hostname is one
# of _LOCAL_HOSTNAMES (local dev). Includes cloud metadata (169.254.169.254 falls under
# the link-local /16), RFC 1918 private space, loopback (any hostname resolving there,
# not just the literal "127.0.0.1"), and the IPv6 equivalents.
_BLOCKED_NETWORKS: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = [
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fe80::/10"),
    ipaddress.ip_network("fc00::/7"),
]
_LOCAL_HOSTNAMES = frozenset({"localhost", "127.0.0.1", "::1"})


DNS_RESOLVE_TIMEOUT_S = 5.0


async def resolve_all_ips(hostname: str) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    """Every A/AAAA record for hostname, via asyncio's own non-blocking resolver
    (`loop.getaddrinfo` runs the lookup in the default executor; it does not block the
    event loop the way `socket.gethostbyname` would). Empty list if resolution fails OR
    times out: a webhook registration must never hang on a slow/blackholed DNS query,
    and "could not resolve" is exactly as safe a default as "resolution failed" - both
    end in `is_url_allowed` refusing the URL (fail closed), never in it being let through
    unresolved."""
    loop = asyncio.get_running_loop()
    try:
        infos = await asyncio.wait_for(loop.getaddrinfo(hostname, None), timeout=DNS_RESOLVE_TIMEOUT_S)
    except (OSError, TimeoutError):
        return []
    ips: set[ipaddress.IPv4Address | ipaddress.IPv6Address] = set()
    for _family, _type, _proto, _canonname, sockaddr in infos:
        try:
            ips.add(ipaddress.ip_address(sockaddr[0]))
        except ValueError:
            continue
    return list(ips)


async def is_url_allowed(url: str, allowlist_str: str = "") -> tuple[bool, str]:
    """Validate a webhook destination URL against SSRF rules and the allowlist.

    Every IP the hostname actually resolves to is checked against `_BLOCKED_NETWORKS`,
    not just a literal IP written directly in the URL - checking only the latter is a
    classic DNS-rebinding SSRF bypass: an attacker-controlled domain name that resolves
    to 169.254.169.254 or 127.0.0.1 would sail straight through a literal-IP-only check.

    Rules:
    - HTTPS is required except for localhost/127.0.0.1/::1 (local dev).
    - Every resolved IP must fall outside every network in _BLOCKED_NETWORKS.
    - If allowlist_str is non-empty, the hostname must match an entry.
    """
    try:
        parsed = urlparse(url)
    except Exception as e:
        return False, f"Invalid URL: {e}"

    if not parsed.scheme or not parsed.hostname:
        return False, "Missing URL scheme or hostname"
    if parsed.scheme not in ("http", "https"):
        return False, f"Unsupported scheme: {parsed.scheme}"

    hostname = parsed.hostname
    is_local = hostname in _LOCAL_HOSTNAMES

    if parsed.scheme == "http" and not is_local:
        return False, "HTTPS required for external webhooks"

    if not is_local:
        try:
            candidate_ips: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = [
                ipaddress.ip_address(hostname)
            ]
        except ValueError:
            candidate_ips = await resolve_all_ips(hostname)
            if not candidate_ips:
                return False, f"Could not resolve hostname {hostname!r}"

        for ip in candidate_ips:
            for net in _BLOCKED_NETWORKS:
                if ip in net:
                    return False, f"Destination {hostname!r} resolves to {ip}, in restricted network {net}"

    if allowlist_str.strip():
        allowed_hosts = [h.strip().lower() for h in allowlist_str.split(",") if h.strip()]
        host_lower = hostname.lower()
        matched = any(
            allowed == "*" or host_lower == allowed or host_lower.endswith("." + allowed)
            for allowed in allowed_hosts
        )
        if not matched:
            return False, f"Hostname {hostname} is not in RM_WEBHOOK_ALLOWLIST"

    return True, "ok"


class WebhookService:
    """Manages webhook subscriptions and delivers signed event payloads.

    Every method opens its own tenant-scoped transaction (`db.transaction(org_id)`,
    matching `IntakeService`/`HumanReviewService`); there is no other state on this
    object besides the `Database` handle and the configured allowlist, so it is safe to
    construct fresh per request or share as a singleton - either way every instance,
    in every process, reads the same rows.
    """

    def __init__(self, db: Database, allowlist: str = "") -> None:
        self.db = db
        self.allowlist = allowlist

    async def register_subscription(
        self,
        *,
        org_id: str,
        url: str,
        secret: str,
        created_by: str,
        events: list[WebhookEvent] | None = None,
    ) -> WebhookSubscription:
        """Register a new webhook subscription for an organization."""
        allowed, reason = await is_url_allowed(url, self.allowlist)
        if not allowed:
            raise ValueError(f"Webhook URL not allowed: {reason}")

        sub = WebhookSubscription(
            subscription_id=new_id(),
            org_id=org_id,
            url=url,
            secret=secret,
            events=events or ["evidence.finalized", "evidence.superseded"],
            created_by=created_by,
        )
        async with self.db.transaction(org_id) as conn:
            await conn.execute(
                """INSERT INTO rm.webhook_subscriptions
                     (subscription_id, org_id, url, secret, events, is_active,
                      created_by, created_at)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
                (
                    sub.subscription_id,
                    org_id,
                    url,
                    secret,
                    list(sub.events),
                    sub.is_active,
                    created_by,
                    sub.created_at,
                ),
            )
        return sub

    async def list_subscriptions(self, org_id: str) -> list[WebhookSubscription]:
        """List active subscriptions for an organization."""
        async with self.db.transaction(org_id) as conn:
            cur = await conn.execute(
                """SELECT subscription_id, org_id, url, secret, events, is_active,
                          created_by, created_at
                   FROM rm.webhook_subscriptions
                   WHERE org_id = %s AND is_active
                   ORDER BY created_at""",
                (org_id,),
            )
            rows = await cur.fetchall()
        return [WebhookSubscription(**row) for row in rows]

    async def delete_subscription(self, org_id: str, subscription_id: str) -> bool:
        """Deactivate a webhook subscription (soft delete; deliveries stay append-only)."""
        async with self.db.transaction(org_id) as conn:
            cur = await conn.execute(
                """UPDATE rm.webhook_subscriptions SET is_active = false
                   WHERE org_id = %s AND subscription_id = %s AND is_active""",
                (org_id, subscription_id),
            )
            return cur.rowcount == 1

    async def list_deliveries(self, org_id: str, limit: int = 200) -> list[WebhookDeliveryRecord]:
        """Return history of delivery attempts for an org, most recent first."""
        async with self.db.transaction(org_id) as conn:
            cur = await conn.execute(
                """SELECT delivery_id, subscription_id, org_id, event, unit_id, url,
                          status_code, success, attempt, duration_ms, error, created_at
                   FROM rm.webhook_deliveries
                   WHERE org_id = %s
                   ORDER BY created_at DESC
                   LIMIT %s""",
                (org_id, limit),
            )
            rows = await cur.fetchall()
        return [WebhookDeliveryRecord(**row) for row in rows]

    async def dispatch(
        self,
        *,
        event: WebhookEvent,
        org_id: str,
        unit_id: str,
        record_id: str,
        record_version: int,
        document_sha256: str,
        links: dict[str, str] | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> list[WebhookDeliveryRecord]:
        """Dispatch event payload to every active, matching subscription for org_id."""
        async with self.db.transaction(org_id) as conn:
            cur = await conn.execute(
                """SELECT subscription_id, url, secret, events
                   FROM rm.webhook_subscriptions
                   WHERE org_id = %s AND is_active""",
                (org_id,),
            )
            rows = await cur.fetchall()
        subs = [r for r in rows if event in (r["events"] or [])]
        if not subs:
            return []

        payload = WebhookPayload(
            event=event,
            org_id=org_id,
            unit_id=unit_id,
            record_id=record_id,
            record_version=record_version,
            document_sha256=document_sha256,
            links=links
            or {
                "evidence": f"/api/v1/units/{unit_id}/return-evidence?version={record_version}",
                "chain": f"/api/v1/units/{unit_id}/chain/verification",
            },
        )
        body_bytes = json.dumps(payload.model_dump(), separators=(",", ":")).encode("utf-8")

        results: list[WebhookDeliveryRecord] = []
        owns_client = client is None
        http_client = client or httpx.AsyncClient(timeout=5.0)

        try:
            for sub in subs:
                sig_header = compute_signature(sub["secret"], body_bytes)
                headers = {
                    "Content-Type": "application/json",
                    "RM-Signature": sig_header,
                    "User-Agent": "Returns-Manager-Webhook/1.0",
                }

                t0 = time.perf_counter()
                delivery = WebhookDeliveryRecord(
                    delivery_id=new_id(),
                    subscription_id=sub["subscription_id"],
                    org_id=org_id,
                    event=event,
                    unit_id=unit_id,
                    url=sub["url"],
                    success=False,
                )

                try:
                    resp = await http_client.post(sub["url"], content=body_bytes, headers=headers)
                    delivery.duration_ms = round((time.perf_counter() - t0) * 1000.0, 2)
                    delivery.status_code = resp.status_code
                    delivery.success = 200 <= resp.status_code < 300
                except Exception as exc:
                    delivery.duration_ms = round((time.perf_counter() - t0) * 1000.0, 2)
                    delivery.error = str(exc)
                    delivery.success = False

                async with self.db.transaction(org_id) as conn:
                    await conn.execute(
                        """INSERT INTO rm.webhook_deliveries
                             (delivery_id, org_id, subscription_id, event, unit_id, url,
                              status_code, success, duration_ms, error, attempt, created_at)
                           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                        (
                            delivery.delivery_id,
                            org_id,
                            delivery.subscription_id,
                            event,
                            unit_id,
                            delivery.url,
                            delivery.status_code,
                            delivery.success,
                            delivery.duration_ms,
                            delivery.error,
                            delivery.attempt,
                            delivery.created_at,
                        ),
                    )
                results.append(delivery)
        finally:
            if owns_client:
                await http_client.aclose()

        return results


_default_webhook_service: WebhookService | None = None


def get_webhook_service(db: Database, allowlist: str = "") -> WebhookService:
    """A process-wide `WebhookService`. Safe now that all state lives in the database -
    every process's singleton reads and writes the same rows, unlike before."""
    global _default_webhook_service
    if _default_webhook_service is None:
        _default_webhook_service = WebhookService(db, allowlist=allowlist)
    return _default_webhook_service
