"""GET /api/v1/metrics/summary and /api/v1/metrics/economics (§15, §18.2, §18.3).

Both endpoints are scoped to the caller's own org via the authenticated `Principal` — see
build-log.md, 2026-09-26, for why that is non-negotiable on every route in this module.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query
from pydantic import BaseModel

from returns_manager.api.deps import PrincipalDep, ServicesDep
from returns_manager.errors import BadRequest
from returns_manager.observability.economics import EconomicsService
from returns_manager.observability.metrics import MetricsService, parse_window
from returns_manager.security.roles import Permission, require

router = APIRouter(prefix="/api/v1/metrics", tags=["metrics"])


class MetricEnvelope(BaseModel):
    """§18.2: every metric is `{value, n, window, method}`; `n == 0` renders `value` as
    the literal string "no data" — a rate of zero and an absence of data must never look
    the same."""

    value: Any
    n: int
    window: str
    method: str


class MetricsSummaryResponse(BaseModel):
    window: str
    metrics: dict[str, MetricEnvelope]


class EconomicsReportResponse(BaseModel):
    window: str
    volume: int
    cost_by_stage: dict[str, MetricEnvelope]
    cost_per_inspection: MetricEnvelope
    projected_monthly_cost: MetricEnvelope
    recovery_uplift: MetricEnvelope
    sanity_anchor: dict[str, Any]


def _validated_window(window: str) -> str:
    try:
        parse_window(window)
    except ValueError as exc:
        raise BadRequest(str(exc)) from exc
    return window


@router.get(
    "/summary",
    response_model=MetricsSummaryResponse,
    summary="Operational metrics for the caller's org (§18.2)",
)
async def get_metrics_summary(
    principal: PrincipalDep,
    svc: ServicesDep,
    window: str = Query("7d", description="'24h', '7d', '30d', or 'all'."),
) -> MetricsSummaryResponse:
    require(principal, Permission.METRICS_READ)
    window = _validated_window(window)
    if svc.db is None:
        return MetricsSummaryResponse(window=window, metrics={})
    async with svc.db.transaction(principal.org_id) as conn:
        service = MetricsService(conn, principal.org_id)
        summary = await service.summary(window)
    return MetricsSummaryResponse(window=window, metrics=summary)


@router.get(
    "/economics",
    response_model=EconomicsReportResponse,
    summary="Unit economics for the caller's org (§18.3)",
)
async def get_metrics_economics(
    principal: PrincipalDep,
    svc: ServicesDep,
    window: str = Query("7d", description="'24h', '7d', '30d', or 'all'."),
    volume: int = Query(1000, gt=0, description="Units/month for the cost projection."),
) -> EconomicsReportResponse:
    require(principal, Permission.METRICS_READ)
    window = _validated_window(window)
    if svc.db is None:
        empty = MetricEnvelope(value="no data", n=0, window=window, method="none")
        return EconomicsReportResponse(
            window=window,
            volume=volume,
            cost_by_stage={},
            cost_per_inspection=empty,
            projected_monthly_cost=empty,
            recovery_uplift=empty,
            sanity_anchor={},
        )
    async with svc.db.transaction(principal.org_id) as conn:
        service = EconomicsService(conn, principal.org_id, svc.settings)
        report = await service.report(window, volume)  # already carries "window"
    return EconomicsReportResponse(volume=volume, **report)

