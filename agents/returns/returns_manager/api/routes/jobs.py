"""Jobs API route (§15). The handler only translates HTTP; the lookup runs under the caller's tenant."""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter
from pydantic import BaseModel

from returns_manager.api.deps import PrincipalDep, ServicesDep
from returns_manager.errors import NotFound
from returns_manager.jobs.queue import JobQueue
from returns_manager.security.roles import Permission, require

router = APIRouter(prefix="/api/v1")


class JobOut(BaseModel):
    job_id: str
    return_id: str
    kind: str
    status: str
    priority: int
    attempts: int
    max_attempts: int
    next_attempt_at: datetime
    lease_expires_at: datetime | None
    last_error_class: str | None
    last_error_detail: str | None
    created_at: datetime
    updated_at: datetime


@router.get("/jobs/{job_id}", response_model=JobOut)
async def get_job(job_id: str, principal: PrincipalDep, svc: ServicesDep) -> JobOut:
    require(principal, Permission.RETURNS_READ)
    async with svc.db.transaction(principal.org_id) as conn:
        job = await JobQueue.get_job(conn, principal.org_id, job_id)
    if job is None:
        raise NotFound("job not found")
    return JobOut(
        job_id=job.job_id,
        return_id=job.return_id,
        kind=job.kind,
        status=str(job.status),
        priority=job.priority,
        attempts=job.attempts,
        max_attempts=job.max_attempts,
        next_attempt_at=job.next_attempt_at,
        lease_expires_at=job.lease_expires_at,
        last_error_class=job.last_error_class,
        last_error_detail=job.last_error_detail,
        created_at=job.created_at,
        updated_at=job.updated_at,
    )
