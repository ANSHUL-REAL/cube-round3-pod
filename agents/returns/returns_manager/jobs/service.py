"""Job administration (§20 `jobs list|retry|cancel`): list, requeue and cancel jobs of one org."""

from __future__ import annotations

from returns_manager.db.pool import Database
from returns_manager.errors import Conflict, NotFound
from returns_manager.jobs.queue import JobQueue, JobRecord
from returns_manager.jobs.statemachine import (
    JobStatus,
    ReturnStatus,
    transition_job_status,
    transition_return_status,
)


async def list_jobs(
    db: Database, org_id: str, status: JobStatus | None = None, limit: int = 50
) -> list[JobRecord]:
    async with db.transaction(org_id) as conn:
        cur = await conn.execute(
            """
            SELECT * FROM rm.inspection_jobs
            WHERE org_id = %s AND (%s::text IS NULL OR status = %s)
            ORDER BY created_at DESC LIMIT %s
            """,
            (org_id, status, status, limit),
        )
        return [JobRecord.from_row(r) for r in await cur.fetchall()]


async def retry_job(db: Database, org_id: str, job_id: str) -> JobRecord:
    """Admin requeue (§10.1): a `needs_attention` job starts again with fresh attempts; its return is queued.

    A `failed_retryable` job is simply made due now.
    """
    async with db.transaction(org_id) as conn:
        job = await JobQueue.get_job(conn, org_id, job_id)
        if job is None:
            raise NotFound(f"Job '{job_id}' not found")
        if job.status == JobStatus.FAILED_RETRYABLE:
            await conn.execute(
                "UPDATE rm.inspection_jobs SET next_attempt_at = now(), updated_at = now() "
                "WHERE org_id = %s AND job_id = %s",
                (org_id, job_id),
            )
        else:
            transition_job_status(job.status, JobStatus.PENDING, trigger="admin_requeue")
            await conn.execute(
                """
                UPDATE rm.inspection_jobs
                SET status = 'pending', attempts = 0, next_attempt_at = now(),
                    last_error_class = NULL, last_error_detail = NULL, updated_at = now()
                WHERE org_id = %s AND job_id = %s
                """,
                (org_id, job_id),
            )
            cur = await conn.execute(
                "SELECT status FROM rm.returns WHERE org_id = %s AND return_id = %s FOR UPDATE",
                (org_id, job.return_id),
            )
            ret = await cur.fetchone()
            if ret is not None and ret["status"] == ReturnStatus.NEEDS_ATTENTION:
                transition_return_status(ret["status"], ReturnStatus.QUEUED, trigger="admin_requeue")
                await conn.execute(
                    "UPDATE rm.returns SET status = 'queued' WHERE org_id = %s AND return_id = %s",
                    (org_id, job.return_id),
                )
        updated = await JobQueue.get_job(conn, org_id, job_id)
    assert updated is not None
    return updated


async def cancel_job(db: Database, org_id: str, job_id: str) -> JobRecord:
    async with db.transaction(org_id) as conn:
        job = await JobQueue.get_job(conn, org_id, job_id)
        if job is None:
            raise NotFound(f"Job '{job_id}' not found")
        if job.status not in (JobStatus.PENDING, JobStatus.IN_PROGRESS, JobStatus.FAILED_RETRYABLE):
            raise Conflict(
                f"Job is '{job.status}'; only pending, in-progress or retryable jobs can be cancelled"
            )
        await JobQueue.mark_job_cancelled(conn, org_id, job_id)
        updated = await JobQueue.get_job(conn, org_id, job_id)
    assert updated is not None
    return updated
