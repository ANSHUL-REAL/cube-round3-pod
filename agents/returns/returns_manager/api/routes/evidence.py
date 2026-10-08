"""P10 evidence record REST routes (§15).

Endpoints served:
  GET /api/v1/units/{unit_id}/return-evidence
  GET /api/v1/units/{unit_id}/return-evidence/history
  GET /api/v1/evidence/export

All routes require EVIDENCE_READ permission (scope evidence:read or reviewer/admin role).
Cross-org resources return 404, not 403 (§6.5).
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Query, Response

from returns_manager.api.deps import PrincipalDep, ServicesDep
from returns_manager.contract.flat import build_flat_row, flat_rows_to_csv
from returns_manager.contract.service import (
    export_evidence_stream,
    get_evidence_document,
    list_evidence_history,
)
from returns_manager.security.roles import Permission, require

router = APIRouter(prefix="/api/v1", tags=["evidence"])


@router.get(
    "/units/{unit_id}/return-evidence",
    summary="Get evidence record for a unit (§15, §14.2)",
)
async def get_return_evidence(
    unit_id: str,
    principal: PrincipalDep,
    svc: ServicesDep,
    version: int | None = Query(None, ge=1, description="Specific version; latest if omitted."),
    include_pending: bool = Query(False, description="Include pre-finalization state."),
    include_photo_urls: bool = Query(False, description="Include short-TTL signed photo URLs."),
) -> dict[str, Any]:
    """Return the evidence record for `unit_id` in the caller's org.

    Returns 404 when no record exists or the unit belongs to another org.
    """
    require(principal, Permission.EVIDENCE_READ)
    doc = await get_evidence_document(
        svc.db.pool,
        org_id=principal.org_id,
        unit_id=unit_id,
        version=version,
        include_pending=include_pending,
    )
    if doc is None:
        from returns_manager.errors import NotFound

        raise NotFound(f"No evidence record for unit {unit_id!r}")

    if include_photo_urls and svc.storage:
        # Inject signed URLs for each photo in images[]
        # The storage key is stored in the photo record in the DB (not in the evidence document).
        # For safety, we only inject URLs that we can resolve; missing URLs remain null.
        images = doc.get("images") or []
        signed: dict[str, str] = {}
        for img in images:
            photo_id = img.get("image_id") or img.get("photo_id", "")
            storage_key = img.get("storage_key", "")
            if photo_id and storage_key:
                try:
                    url = await svc.storage.signed_url(
                        svc.settings.rm_storage_bucket_photos, storage_key, 300
                    )
                    if url:
                        signed[photo_id] = url
                except Exception:  # noqa: S110 - signed URLs are optional; a failure just skips the URL
                    pass  # log silently; signed URLs are best-effort
        if signed:
            from returns_manager.contract.service import add_signed_photo_urls

            doc = add_signed_photo_urls(doc, signed_urls=signed)

    return doc


@router.get(
    "/units/{unit_id}/return-evidence/history",
    summary="All evidence record versions for a unit (§15)",
)
async def get_return_evidence_history(
    unit_id: str,
    principal: PrincipalDep,
    svc: ServicesDep,
) -> list[dict[str, Any]]:
    """Return all record versions (v1 … vN) for `unit_id`, in ascending order."""
    require(principal, Permission.EVIDENCE_READ)
    return await list_evidence_history(
        svc.db.pool,
        org_id=principal.org_id,
        unit_id=unit_id,
    )


@router.get(
    "/evidence/export",
    summary="Bulk export evidence records (§15)",
)
async def export_evidence(
    principal: PrincipalDep,
    svc: ServicesDep,
    since: str | None = Query(None, description="ISO 8601 UTC timestamp filter."),
    format: str = Query("jsonl", description="jsonl | csv (flat view)"),
) -> Response:
    """Bulk export finalized evidence records as JSONL or flat CSV.

    JSONL: one record per line (RFC 8259).
    CSV: flat view with official column order (§14.3).
    """
    require(principal, Permission.EVIDENCE_READ)

    since_dt: datetime | None = None
    if since:
        try:
            since_dt = datetime.fromisoformat(since.replace("Z", "+00:00"))
        except ValueError as exc:
            from returns_manager.errors import BadRequest

            raise BadRequest("'since' must be a valid ISO 8601 timestamp") from exc

    records = await export_evidence_stream(
        svc.db.pool,
        org_id=principal.org_id,
        since=since_dt,
    )

    if format == "csv":
        rows = [build_flat_row(doc) for doc in records]
        content = flat_rows_to_csv(rows)
        return Response(
            content=content,
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": "attachment; filename=evidence-export.csv"},
        )
    else:
        # JSONL
        lines = "\n".join(json.dumps(doc, ensure_ascii=False) for doc in records)
        return Response(
            content=lines,
            media_type="application/x-ndjson",
            headers={"Content-Disposition": "attachment; filename=evidence-export.jsonl"},
        )
