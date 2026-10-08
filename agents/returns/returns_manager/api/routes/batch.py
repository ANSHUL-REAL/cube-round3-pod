"""Batch-upload jobs API (§15): upload a before/returned CSV pair, run the real standalone
batch pipeline against it, poll status, read the resulting rows, download the output CSV,
or delete the job and its files. See `batch/jobs_service.py` for what a "job" is here and
why it deliberately isn't the durable DB job queue used elsewhere.

Every route is scoped to the caller's own org via the authenticated `Principal`, same as
every other route in this API - a batch job lives at `<root>/<org_id>/<job_id>/` and one
org can never see, download or delete another org's upload.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import APIRouter, File, Form, Query, Response, UploadFile
from pydantic import BaseModel, Field

from returns_manager.api.deps import PrincipalDep, ServicesDep
from returns_manager.batch.jobs_service import BatchJob
from returns_manager.errors import BadRequest, NotFound
from returns_manager.security.roles import Permission, require

router = APIRouter(prefix="/api/v1/batch", tags=["batch"])

# A CSV here is a handful of return records, not a bulk data export - this is a demo/import
# size guard, not a real-world bulk-upload limit.
MAX_UPLOAD_BYTES = 2 * 1024 * 1024
# Spend guard default (CLAUDE.md "Spend guards"): a caller must explicitly raise this to
# spend more than a small number of real Gemini requests in one job.
DEFAULT_MAX_REQUESTS = 15
MAX_MAX_REQUESTS = 50


class BatchJobResponse(BaseModel):
    job_id: str
    org_id: str
    status: str
    created_at: float
    before_filename: str
    returned_filename: str
    total_rows: int
    processed: int
    uncertain: int
    live_requests: int
    error: str | None
    notes: list[str]


def _to_response(job: BatchJob) -> BatchJobResponse:
    return BatchJobResponse(
        job_id=job.job_id,
        org_id=job.org_id,
        status=job.status,
        created_at=job.created_at,
        before_filename=job.before_filename,
        returned_filename=job.returned_filename,
        total_rows=job.total_rows,
        processed=job.processed,
        uncertain=job.uncertain,
        live_requests=job.live_requests,
        error=job.error,
        notes=job.notes,
    )


def _split_combined_csv(content: bytes, default_org_id: str) -> tuple[bytes, bytes]:
    import csv
    import io

    text = content.decode("utf-8-sig", errors="ignore")
    reader = csv.DictReader(io.StringIO(text))
    fieldnames = reader.fieldnames or []
    rows = list(reader)
    if not rows:
        raise BadRequest("Uploaded CSV has no data rows.")

    from returns_manager.batch.io_csv import REQUIRED_BEFORE_COLUMNS

    # If the file already contains the exact internal before.csv column set, pass through directly
    if REQUIRED_BEFORE_COLUMNS.issubset(set(fieldnames)):
        return content, content

    # Check if the CSV has any recognizable return or product columns
    known_return_keys = {
        "unit_id",
        "record_id",
        "order_id",
        "ordered_sku",
        "sku",
        "asin",
        "ordered_asin",
        "sold_sku",
        "sold_photo_url",
        "returned_photo_url",
        "photo_ref",
        "returned_photo_ref",
        "category",
        "parts_list",
        "item_id",
        "return_id",
        "product_name",
        "product_sku",
        "disposition",
        "reason",
        "condition",
        "returned_photo",
        "sold_photo",
        "image",
        "image_url",
    }
    fieldnames_lower = {k.strip().lower() for k in fieldnames if k}
    has_any_return_col = any(
        k in known_return_keys or k.startswith(("sold_", "returned_")) for k in fieldnames_lower
    )
    if not has_any_return_col:
        sample_cols = ", ".join(f"'{c}'" for c in fieldnames[:5])
        raise BadRequest(
            f"Invalid returns CSV: missing required return columns (found headers: {sample_cols}). "
            "Please upload a returns CSV containing columns like 'unit_id', 'ordered_sku', "
            "'category', or photo URLs."
        )

    b_out = io.StringIO()
    r_out = io.StringIO()
    bw = csv.DictWriter(
        b_out,
        fieldnames=[
            "record_id",
            "unit_id",
            "org_id",
            "order_id",
            "ordered_sku",
            "ordered_asin",
            "identity_match",
            "parts_list",
            "time",
            "photo_ref",
            "category",
            "scenario",
            "parts_missing",
        ],
    )
    rw = csv.DictWriter(
        r_out,
        fieldnames=[
            "record_id",
            "unit_id",
            "org_id",
            "order_id",
            "ordered_sku",
            "ordered_asin",
            "returned_photo_ref",
            "time",
            "scenario",
            "parts_missing",
        ],
    )
    bw.writeheader()
    rw.writeheader()

    def _get_val(row: dict[str, Any], *keys: str, default: str = "") -> str:
        for k in keys:
            for col_name, val in row.items():
                if col_name and col_name.strip().lower() == k.lower() and val is not None:
                    s = str(val).strip()
                    if s:
                        return s
        return default

    has_explicit_ret_col = any(
        col
        and col.strip().lower()
        in (
            "returned_photo_url",
            "returned_photo_ref",
            "returned_photo",
            "returned_image",
            "returned_image_url",
            "return_photo_url",
            "return_photo_ref",
            "return_photo",
            "return_image",
            "return_image_url",
            "after_photo",
            "after_photo_url",
            "received_photo",
        )
        for col in fieldnames
    )

    for idx, r in enumerate(rows, 1):
        unit_id = _get_val(r, "unit_id", "unit", "item_id", "return_id", default="")
        if not unit_id:
            raise BadRequest(f"Row {idx}: missing required column 'unit_id'.")

        order_id = _get_val(r, "sold_order_id", "returned_order_id", "order_id", "order", default="")
        if not order_id:
            raise BadRequest(f"Row {idx}: missing required column 'order_id'.")

        sku_val = _get_val(r, "sold_sku", "returned_sku", "ordered_sku", "sku", default="")
        if not sku_val:
            raise BadRequest(f"Row {idx}: missing required column 'ordered_sku'.")

        asin_val = _get_val(r, "sold_asin", "returned_asin", "ordered_asin", "asin", default="")
        category_val = _get_val(r, "category", "product_category", default="")
        parts_list_val = _get_val(r, "parts_list", "parts", default="")
        scenario_val = _get_val(r, "scenario", default="")
        parts_missing_val = _get_val(r, "parts_missing", default="")

        sold_photo = _get_val(
            r,
            "sold_photo_url",
            "sold_photo",
            "sold_photo_ref",
            "sold_image",
            "sold_image_url",
            "photo_ref",
            "photo_url",
            "photo",
            "image_url",
            "image",
            "product_photo",
            "product_image",
            "item_photo",
            "item_image",
            "photos",
            "images",
            "img",
            "picture",
            "pic",
            "url",
            "link",
            "reference_photo",
            "before_photo",
            default="",
        )
        returned_photo = _get_val(
            r,
            "returned_photo_url",
            "returned_photo_ref",
            "returned_photo",
            "returned_image",
            "returned_image_url",
            "return_photo_url",
            "return_photo_ref",
            "return_photo",
            "return_image",
            "return_image_url",
            "after_photo",
            "after_photo_url",
            "received_photo",
            default="" if has_explicit_ret_col else sold_photo,
        )

        sold_record_id = _get_val(r, "sold_record_id", "record_id", "id", default=f"REC-SOLD-{idx:04d}")
        returned_record_id = _get_val(
            r, "returned_record_id", "record_id", "id", default=f"REC-RTN-{idx:04d}"
        )
        sold_time = _get_val(r, "sold_time", "time", "date", default="2026-08-01T00:00:00Z")
        returned_time = _get_val(r, "returned_time", "time", "date", default="2026-09-01T00:00:00Z")

        bw.writerow(
            {
                "record_id": sold_record_id,
                "unit_id": unit_id,
                "org_id": default_org_id,
                "order_id": order_id,
                "ordered_sku": sku_val,
                "ordered_asin": asin_val,
                "identity_match": _get_val(r, "identity_match", default="uncertain"),
                "parts_list": parts_list_val,
                "time": sold_time,
                "photo_ref": sold_photo,
                "category": category_val,
                "scenario": scenario_val,
                "parts_missing": parts_missing_val,
            }
        )
        rw.writerow(
            {
                "record_id": returned_record_id,
                "unit_id": unit_id,
                "org_id": default_org_id,
                "order_id": order_id,
                "ordered_sku": sku_val,
                "ordered_asin": asin_val,
                "returned_photo_ref": returned_photo,
                "time": returned_time,
                "scenario": scenario_val,
                "parts_missing": parts_missing_val,
            }
        )

    return b_out.getvalue().encode("utf-8"), r_out.getvalue().encode("utf-8")


@router.post("/jobs", response_model=BatchJobResponse, status_code=202)
async def create_batch_job(
    principal: PrincipalDep,
    svc: ServicesDep,
    confirm_spend: Annotated[bool, Form()],
    file: Annotated[
        UploadFile | None, File(description="Single combined CSV (unit_id,sold_*,returned_*)")
    ] = None,
    before: Annotated[
        UploadFile | None, File(description="Before-sale CSV (record_id,unit_id,org_id,...)")
    ] = None,
    returned: Annotated[
        UploadFile | None, File(description="Returned-item CSV (record_id,unit_id,org_id,...)")
    ] = None,
    default_category: Annotated[str | None, Form()] = None,
    max_requests: Annotated[int, Form(ge=1, le=MAX_MAX_REQUESTS)] = DEFAULT_MAX_REQUESTS,
) -> BatchJobResponse:
    """202 Accepted: the job runs in the background (real Gemini calls). Poll
    GET /jobs/{job_id} for status.

    Accepts either:
    1. A single unified returns CSV (via `file` or `before`) containing sold & returned records
       (e.g. returns_input_30.csv).
    2. A pair of `before` and `returned` CSV files.
    """
    require(principal, Permission.RETURNS_WRITE)
    if svc.batch_jobs is None:
        raise BadRequest("batch processing is not configured on this deployment (no Gemini API key set)")
    if not confirm_spend:
        raise BadRequest("confirm_spend must be true - this run spends real Gemini API quota")

    upload_file = file or (before if returned is None else None)
    if upload_file is not None:
        # Single unified CSV mode
        content = await upload_file.read()
        if not content:
            raise BadRequest("uploaded file is empty")
        if len(content) > MAX_UPLOAD_BYTES:
            raise BadRequest(f"file must be under {MAX_UPLOAD_BYTES // (1024 * 1024)} MB")
        before_bytes, returned_bytes = _split_combined_csv(content, default_org_id=principal.org_id)
        before_fn = upload_file.filename or "returns.csv"
        returned_fn = upload_file.filename or "returns.csv"
    elif before is not None and returned is not None:
        # Two-file mode
        before_bytes = await before.read()
        returned_bytes = await returned.read()
        if not before_bytes or not returned_bytes:
            raise BadRequest("both a before CSV and a returned CSV are required")
        if len(before_bytes) > MAX_UPLOAD_BYTES or len(returned_bytes) > MAX_UPLOAD_BYTES:
            raise BadRequest(f"each file must be under {MAX_UPLOAD_BYTES // (1024 * 1024)} MB")
        before_fn = before.filename or "before.csv"
        returned_fn = returned.filename or "returned.csv"
    else:
        raise BadRequest(
            "either a single unified returns CSV or both before and returned CSV files are required"
        )

    job = await svc.batch_jobs.create_job(
        org_id=principal.org_id,
        before_bytes=before_bytes,
        before_filename=before_fn,
        returned_bytes=returned_bytes,
        returned_filename=returned_fn,
        default_category=default_category,
        max_requests=max_requests,
    )
    return _to_response(job)


@router.get("/jobs", response_model=list[BatchJobResponse])
async def list_batch_jobs(principal: PrincipalDep, svc: ServicesDep) -> list[BatchJobResponse]:
    require(principal, Permission.RETURNS_READ)
    if svc.batch_jobs is None:
        return []
    return [_to_response(job) for job in svc.batch_jobs.list_jobs(principal.org_id)]


@router.post("/cache/clear")
async def clear_batch_cache(principal: PrincipalDep, svc: ServicesDep) -> dict[str, int]:
    """Clears all stored batch job cache files and records for this organization."""
    require(principal, Permission.RETURNS_WRITE)
    if svc.batch_jobs is None:
        return {"cleared": 0}
    count = svc.batch_jobs.clear_all_jobs(principal.org_id)
    return {"cleared": count}


@router.delete("/jobs/{job_id}", status_code=204)
async def delete_batch_job(job_id: str, principal: PrincipalDep, svc: ServicesDep) -> None:
    require(principal, Permission.RETURNS_WRITE)
    if svc.batch_jobs is None or not svc.batch_jobs.delete_job(principal.org_id, job_id):
        raise NotFound(f"no batch job {job_id!r}")


@router.get("/jobs/{job_id}", response_model=BatchJobResponse)
async def get_batch_job(job_id: str, principal: PrincipalDep, svc: ServicesDep) -> BatchJobResponse:
    require(principal, Permission.RETURNS_READ)
    job = svc.batch_jobs.get_job(principal.org_id, job_id) if svc.batch_jobs else None
    if job is None:
        raise NotFound(f"no batch job {job_id!r}")
    return _to_response(job)


@router.get("/jobs/{job_id}/rows")
async def get_batch_job_rows(job_id: str, principal: PrincipalDep, svc: ServicesDep) -> list[dict[str, str]]:
    """The evidence rows this job produced - the same data `output.csv` holds, as JSON so
    the dashboard can render it directly without parsing a CSV client-side."""
    require(principal, Permission.RETURNS_READ)
    rows = svc.batch_jobs.output_rows(principal.org_id, job_id) if svc.batch_jobs else None
    if rows is None:
        return []
    return rows


@router.get("/jobs/{job_id}/output.csv")
async def download_batch_job_output(job_id: str, principal: PrincipalDep, svc: ServicesDep) -> Response:
    """Renders the output CSV fresh on every request, with `operator_disposition` reflecting the
    latest recorded `override` decision on top of the engine's own route (see
    `BatchJobsService.render_output_csv`) - what a reviewer downloads matches what they approved
    in the UI, not a stale snapshot from the moment the job finished."""
    require(principal, Permission.RETURNS_READ)
    csv_bytes = svc.batch_jobs.render_output_csv(principal.org_id, job_id) if svc.batch_jobs else None
    if csv_bytes is None:
        raise NotFound(f"no completed output for batch job {job_id!r} (it may still be processing)")
    return Response(
        content=csv_bytes,
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="returns_output_{job_id}.csv"'},
    )


@router.get("/jobs/{job_id}/rows/{record_id}/detail")
async def get_batch_job_row_detail(
    job_id: str, record_id: str, principal: PrincipalDep, svc: ServicesDep
) -> dict[str, Any]:
    """The rich per-row detail (§14.2 fixed check list, fused identity/completeness/condition,
    disposition reasoning, raw model output) for one row - everything `output.csv`'s flat columns
    leave out. 404 for a row that hasn't finished, doesn't exist, or fell back to fail-open (there
    is no real pipeline run to show for that row)."""
    require(principal, Permission.RETURNS_READ)
    detail = svc.batch_jobs.output_row_detail(principal.org_id, job_id, record_id) if svc.batch_jobs else None
    if detail is None:
        raise NotFound(f"no detail for batch job {job_id!r} row {record_id!r}")
    return detail


class RowDecisionRequest(BaseModel):
    action: Literal["accept", "override", "retake_request", "review_request"]
    new_disposition: str | None = None
    reason: str = Field(..., min_length=1, max_length=2000)


class RowDecisionEntry(BaseModel):
    record_id: str
    action: str
    new_disposition: str | None
    reason: str
    actor: str
    at: float


@router.post("/jobs/{job_id}/rows/{record_id}/decision", response_model=RowDecisionEntry, status_code=201)
async def record_batch_row_decision(
    job_id: str,
    record_id: str,
    body: RowDecisionRequest,
    principal: PrincipalDep,
    svc: ServicesDep,
) -> RowDecisionEntry:
    """Records one operator/reviewer decision against a row - accept, override (needs
    `review:write`, like overriding a DB-backed disposition does), or a retake/review request.
    Appended, never overwriting an earlier decision; the full history is always readable via
    GET .../decisions. The downloaded output CSV picks up an `override`'s `new_disposition`."""
    require(principal, Permission.RETURNS_WRITE)
    if body.action == "override":
        require(principal, Permission.REVIEW_WRITE)
        if not body.new_disposition:
            raise BadRequest("override requires new_disposition")
    if svc.batch_jobs is None:
        raise BadRequest("batch processing is not configured on this deployment (no Gemini API key set)")
    entry = svc.batch_jobs.record_decision(
        principal.org_id,
        job_id,
        record_id,
        action=body.action,
        new_disposition=body.new_disposition,
        reason=body.reason,
        actor=principal.actor_label,
    )
    if entry is None:
        raise NotFound(f"no batch job {job_id!r}")
    return RowDecisionEntry.model_validate(entry)


@router.get("/jobs/{job_id}/rows/{record_id}/decisions", response_model=list[RowDecisionEntry])
async def list_batch_row_decisions(
    job_id: str, record_id: str, principal: PrincipalDep, svc: ServicesDep
) -> list[RowDecisionEntry]:
    require(principal, Permission.RETURNS_READ)
    decisions = svc.batch_jobs.get_decisions(principal.org_id, job_id, record_id) if svc.batch_jobs else None
    if decisions is None:
        raise NotFound(f"no batch job {job_id!r}")
    return [RowDecisionEntry.model_validate(d) for d in decisions]


@router.get("/photos/proxy")
async def proxy_photo(url: str = Query(..., description="External image URL to proxy")) -> Response:
    """Proxies an external online photo URL server-side to bypass browser CORS, mixed-content,
    and hotlinking / Referer blocks."""
    import httpx

    clean_url = url.strip()
    if not (clean_url.startswith("http://") or clean_url.startswith("https://")):
        return Response(status_code=400, content="Invalid photo URL: must start with http:// or https://")

    fetch_headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    import contextlib

    with contextlib.suppress(Exception):
        async with httpx.AsyncClient(timeout=15.0, headers=fetch_headers, follow_redirects=True) as client:
            resp = await client.get(clean_url)
            if resp.status_code == 200 and resp.content:
                content_type = resp.headers.get("content-type", "image/jpeg")
                if not content_type.startswith("image/"):
                    content_type = "image/jpeg"
                return Response(
                    content=resp.content,
                    media_type=content_type,
                    headers={
                        "Cache-Control": "public, max-age=86400",
                        "Access-Control-Allow-Origin": "*",
                    },
                )

    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="120" height="120" viewBox="0 0 120 120">'
        '<rect width="120" height="120" rx="8" fill="#18241e"/>'
        '<text x="60" y="65" font-family="sans-serif" font-size="11" fill="#4ade80" '
        'text-anchor="middle">PHOTO</text>'
        "</svg>"
    )
    return Response(
        content=svg.encode("utf-8"),
        media_type="image/svg+xml",
        headers={"Cache-Control": "public, max-age=3600", "Access-Control-Allow-Origin": "*"},
    )
