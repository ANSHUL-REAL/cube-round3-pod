"""Job queue operations, priority calculation, and idempotency (§10.3, §10.5)."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from returns_manager.canonical.hashing import sha256_jcs
from returns_manager.errors import NotFound
from returns_manager.ids import new_id
from returns_manager.jobs.statemachine import JobStatus, ReturnStatus, transition_return_status

logger = logging.getLogger(__name__)


@dataclass
class JobRecord:
    job_id: str
    org_id: str
    return_id: str
    kind: str
    status: JobStatus
    priority: int
    idempotency_key: str
    attempts: int
    max_attempts: int
    next_attempt_at: datetime
    lease_owner: str | None
    lease_expires_at: datetime | None
    last_error_class: str | None
    last_error_detail: str | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> JobRecord:
        return cls(
            job_id=row["job_id"],
            org_id=row["org_id"],
            return_id=row["return_id"],
            kind=row["kind"],
            status=JobStatus(row["status"]),
            priority=row["priority"],
            idempotency_key=row["idempotency_key"],
            attempts=row["attempts"],
            max_attempts=row["max_attempts"],
            next_attempt_at=row["next_attempt_at"],
            lease_owner=row.get("lease_owner"),
            lease_expires_at=row.get("lease_expires_at"),
            last_error_class=row.get("last_error_class"),
            last_error_detail=row.get("last_error_detail"),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )


def compute_job_priority(
    kind: str = "judgment",
    item_value_minor: int | None = None,
    high_value_threshold_minor: int = 500000,
    observed_state: str | None = None,
) -> int:
    """Calculate enqueue priority based on item value, observed state, and kind (§10.3).

    - Base 0
    - +20 if item value >= high-value threshold
    - +30 if operator marked 'empty_box' or 'damaged'
    - +10 for escalations
    """
    priority = 0
    if item_value_minor is not None and item_value_minor >= high_value_threshold_minor:
        priority += 20
    if observed_state in ("empty_box", "damaged"):
        priority += 30
    if kind == "escalation":
        priority += 10
    return priority


def compute_inspection_idempotency_key(
    org_id: str,
    return_id: str,
    photo_analysis_hashes: list[str],
    inspection_config_version: str = "v1",
    kind: str = "judgment",
) -> str:
    """Compute the deterministic inspection idempotency key (§10.5).

    Formula: SHA-256(JCS({org_id, return_id, sorted(photo_hashes), config_version, kind})). The job kind is
    part of the key so an escalation or audit of the same photos is a new job, not the judgment job again.
    `inspection_config_version` is a placeholder until P5 derives it from prompt/schema/model/rules hashes.
    """
    payload = {
        "config_version": inspection_config_version,
        "kind": kind,
        "org_id": org_id,
        "photo_hashes": sorted(photo_analysis_hashes),
        "return_id": return_id,
    }
    return sha256_jcs(payload)


class JobQueue:
    """Service for enqueueing, claiming, renewing, and concluding inspection jobs."""

    @staticmethod
    async def enqueue_job(
        conn: Any,
        org_id: str,
        return_id: str,
        kind: str = "judgment",
        priority: int = 0,
        idempotency_key: str | None = None,
        max_attempts: int = 5,
    ) -> JobRecord:
        """Enqueue an inspection job idempotently (§10.5).

        A `judgment` job is the operator's submit: the return moves to `queued` in the same transaction.
        Escalation and audit jobs run on a return that is already being processed and leave its status alone.
        """
        # 1. Check current return status
        res_ret = await conn.execute(
            "SELECT status FROM rm.returns WHERE org_id = %s AND return_id = %s FOR UPDATE",
            (org_id, return_id),
        )
        ret_row = await res_ret.fetchone()
        if ret_row is None:
            raise NotFound(f"Return '{return_id}' not found")
        if kind == "judgment":
            curr_status = ret_row["status"]
            new_status = transition_return_status(curr_status, ReturnStatus.QUEUED, trigger="submit")
            await conn.execute(
                """
                UPDATE rm.returns
                SET status = %s, submitted_at = now()
                WHERE org_id = %s AND return_id = %s
                """,
                (str(new_status), org_id, return_id),
            )

        # 2. Determine idempotency key
        if not idempotency_key:
            res_p = await conn.execute(
                """
                SELECT sha256_analysis FROM rm.return_photos
                WHERE org_id = %s AND return_id = %s AND superseded = false
                """,
                (org_id, return_id),
            )
            rows = await res_p.fetchall()
            hashes = [r["sha256_analysis"] for r in rows if r["sha256_analysis"]]
            idempotency_key = compute_inspection_idempotency_key(org_id, return_id, hashes, kind=kind)

        # 3. Insert or update job
        job_id = new_id()
        res = await conn.execute(
            """
            INSERT INTO rm.inspection_jobs (
                job_id, org_id, return_id, kind, status, priority,
                idempotency_key, max_attempts, next_attempt_at
            ) VALUES (%s, %s, %s, %s, 'pending', %s, %s, %s, now())
            ON CONFLICT (org_id, idempotency_key) DO UPDATE
            SET priority = EXCLUDED.priority, updated_at = now()
            RETURNING *
            """,
            (job_id, org_id, return_id, kind, priority, idempotency_key, max_attempts),
        )
        row = await res.fetchone()
        assert row is not None
        return JobRecord.from_row(row)

    @staticmethod
    async def claim_next_job(
        conn: Any,
        worker_id: str,
        lease_s: int = 600,
        max_inflight_per_org: int = 4,
    ) -> JobRecord | None:
        """Claim the next eligible job across tenants using the security definer function (§10.3)."""
        res = await conn.execute(
            "SELECT * FROM rm.claim_next_job(%s, %s, %s)",
            (worker_id, lease_s, max_inflight_per_org),
        )
        row = await res.fetchone()
        if not row:
            return None
        return JobRecord.from_row(row)

    enqueue_inspection_job = enqueue_job

    @staticmethod
    async def claim_specific(
        conn: Any,
        org_id: str,
        return_id: str,
        worker_id: str,
        lease_s: int = 600,
        kinds: tuple[str, ...] | str | None = None,
    ) -> JobRecord | None:
        """Claim this return's due job (CLI `inspect` or `audit run`). Runs under caller's tenant context."""
        if kinds is None:
            kind_list = ["judgment", "reinspection"]
        elif isinstance(kinds, str):
            kind_list = [kinds]
        else:
            kind_list = list(kinds)
        res = await conn.execute(
            """
            UPDATE rm.inspection_jobs AS t
            SET status = 'in_progress', attempts = t.attempts + 1, lease_owner = %s,
                lease_expires_at = now() + make_interval(secs => %s), updated_at = now()
            WHERE t.job_id = (
                SELECT j.job_id FROM rm.inspection_jobs AS j
                WHERE j.org_id = %s AND j.return_id = %s AND j.kind = ANY(%s)
                  AND j.status IN ('pending', 'failed_retryable')
                ORDER BY j.created_at DESC
                FOR UPDATE SKIP LOCKED
                LIMIT 1
            )
            RETURNING t.*
            """,
            (worker_id, lease_s, org_id, return_id, kind_list),
        )
        row = await res.fetchone()
        return JobRecord.from_row(row) if row else None

    @staticmethod
    async def renew_lease(
        conn: Any,
        org_id: str,
        job_id: str,
        worker_id: str,
        lease_s: int = 600,
    ) -> bool:
        """Renew the lease of an actively processing job during heartbeat."""
        res = await conn.execute(
            """
            UPDATE rm.inspection_jobs
            SET lease_expires_at = now() + make_interval(secs => %s), updated_at = now()
            WHERE org_id = %s AND job_id = %s AND status = 'in_progress' AND lease_owner = %s
            RETURNING job_id
            """,
            (lease_s, org_id, job_id, worker_id),
        )
        row = await res.fetchone()
        return row is not None

    @staticmethod
    async def reap_expired_leases(conn: Any, max_rows: int = 100) -> int:
        """Return expired in-progress jobs to 'failed_retryable' (§10.2)."""
        res = await conn.execute("SELECT rm.reap_expired_leases(%s) as reaped", (max_rows,))
        row = await res.fetchone()
        return row["reaped"] if row else 0

    @staticmethod
    async def mark_job_succeeded(conn: Any, org_id: str, job_id: str) -> None:
        """Mark job as succeeded."""
        await conn.execute(
            """
            UPDATE rm.inspection_jobs
            SET status = 'succeeded', lease_owner = NULL, lease_expires_at = NULL, updated_at = now()
            WHERE org_id = %s AND job_id = %s
            """,
            (org_id, job_id),
        )

    @staticmethod
    async def mark_job_failed_retryable(
        conn: Any,
        org_id: str,
        job_id: str,
        error_class: str,
        error_detail: str,
        next_attempt_at: datetime,
    ) -> None:
        """Mark job as failed_retryable with backoff."""
        await conn.execute(
            """
            UPDATE rm.inspection_jobs
            SET status = 'failed_retryable', lease_owner = NULL, lease_expires_at = NULL,
                last_error_class = %s, last_error_detail = %s, next_attempt_at = %s, updated_at = now()
            WHERE org_id = %s AND job_id = %s
            """,
            (error_class, error_detail, next_attempt_at, org_id, job_id),
        )

    @staticmethod
    async def hold_job(
        conn: Any,
        org_id: str,
        job_id: str,
        hold_reason: str,
        detail: str,
        next_attempt_at: datetime,
    ) -> None:
        """Put a claimed job back for an operational hold (kill switch, open circuit, exhausted quota).

        The claim incremented `attempts`; a hold is not an attempt, so it is given back. Otherwise a long
        hold would silently burn the job's retries.
        """
        await conn.execute(
            """
            UPDATE rm.inspection_jobs
            SET status = 'failed_retryable', attempts = GREATEST(attempts - 1, 0),
                lease_owner = NULL, lease_expires_at = NULL,
                last_error_class = %s, last_error_detail = %s, next_attempt_at = %s, updated_at = now()
            WHERE org_id = %s AND job_id = %s AND status = 'in_progress'
            """,
            (hold_reason, detail, next_attempt_at, org_id, job_id),
        )

    @staticmethod
    async def mark_job_needs_attention(
        conn: Any,
        org_id: str,
        job_id: str,
        error_class: str,
        error_detail: str,
    ) -> None:
        """Mark job as needs_attention (non-retryable or retries exhausted)."""
        await conn.execute(
            """
            UPDATE rm.inspection_jobs
            SET status = 'needs_attention', lease_owner = NULL, lease_expires_at = NULL,
                last_error_class = %s, last_error_detail = %s, updated_at = now()
            WHERE org_id = %s AND job_id = %s
            """,
            (error_class, error_detail, org_id, job_id),
        )

    @staticmethod
    async def mark_job_cancelled(conn: Any, org_id: str, job_id: str) -> None:
        """Cancel an in-progress, pending, or retryable job."""
        await conn.execute(
            """
            UPDATE rm.inspection_jobs
            SET status = 'cancelled', lease_owner = NULL, lease_expires_at = NULL, updated_at = now()
            WHERE org_id = %s AND job_id = %s AND status IN ('pending', 'in_progress', 'failed_retryable')
            """,
            (org_id, job_id),
        )

    @staticmethod
    async def get_job(conn: Any, org_id: str, job_id: str) -> JobRecord | None:
        """Fetch a job record within an org."""
        res = await conn.execute(
            "SELECT * FROM rm.inspection_jobs WHERE org_id = %s AND job_id = %s",
            (org_id, job_id),
        )
        row = await res.fetchone()
        return JobRecord.from_row(row) if row else None
