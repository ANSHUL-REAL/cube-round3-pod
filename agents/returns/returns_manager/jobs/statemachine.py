"""State machines for return and job lifecycles (§10.1).

Enforces strictly allowed state transitions, raising InvalidTransitionError on any
disallowed transition.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final

from returns_manager.errors import InvalidTransitionError


class ReturnStatus(StrEnum):
    CAPTURING = "capturing"
    QUEUED = "queued"
    INSPECTING = "inspecting"
    PENDING = "pending"
    AWAITING_OPERATOR = "awaiting_operator"
    AWAITING_REVIEW = "awaiting_review"
    AWAITING_SIGNOFF = "awaiting_signoff"
    FINALIZED = "finalized"
    NEEDS_ATTENTION = "needs_attention"


class JobStatus(StrEnum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    SUCCEEDED = "succeeded"
    FAILED_RETRYABLE = "failed_retryable"
    NEEDS_ATTENTION = "needs_attention"
    CANCELLED = "cancelled"


_VALID_RETURN_TRANSITIONS: Final[set[tuple[ReturnStatus | None, ReturnStatus]]] = {
    # initial creation
    (None, ReturnStatus.CAPTURING),
    # normal progress
    (ReturnStatus.CAPTURING, ReturnStatus.QUEUED),
    (ReturnStatus.QUEUED, ReturnStatus.INSPECTING),
    # fail-open retry scheduled
    (ReturnStatus.INSPECTING, ReturnStatus.PENDING),
    (ReturnStatus.PENDING, ReturnStatus.INSPECTING),
    # decision ready branches
    (ReturnStatus.INSPECTING, ReturnStatus.AWAITING_OPERATOR),
    (ReturnStatus.INSPECTING, ReturnStatus.AWAITING_REVIEW),
    (ReturnStatus.INSPECTING, ReturnStatus.AWAITING_SIGNOFF),
    # operator actions
    (ReturnStatus.AWAITING_OPERATOR, ReturnStatus.FINALIZED),
    (ReturnStatus.AWAITING_OPERATOR, ReturnStatus.AWAITING_REVIEW),
    (ReturnStatus.AWAITING_OPERATOR, ReturnStatus.AWAITING_SIGNOFF),
    # reviewer resolution
    (ReturnStatus.AWAITING_REVIEW, ReturnStatus.AWAITING_OPERATOR),
    (ReturnStatus.AWAITING_REVIEW, ReturnStatus.AWAITING_SIGNOFF),
    (ReturnStatus.AWAITING_REVIEW, ReturnStatus.FINALIZED),
    # signoff actions
    (ReturnStatus.AWAITING_SIGNOFF, ReturnStatus.FINALIZED),
    (ReturnStatus.AWAITING_SIGNOFF, ReturnStatus.AWAITING_REVIEW),
    # error branches
    (ReturnStatus.PENDING, ReturnStatus.NEEDS_ATTENTION),
    (ReturnStatus.INSPECTING, ReturnStatus.NEEDS_ATTENTION),
    # admin recovery
    (ReturnStatus.NEEDS_ATTENTION, ReturnStatus.QUEUED),
    # post-finalization override (vN+1)
    (ReturnStatus.FINALIZED, ReturnStatus.FINALIZED),
    # retake requested before finalization (from any non-finalized status to capturing)
    (ReturnStatus.CAPTURING, ReturnStatus.CAPTURING),
    (ReturnStatus.QUEUED, ReturnStatus.CAPTURING),
    (ReturnStatus.INSPECTING, ReturnStatus.CAPTURING),
    (ReturnStatus.PENDING, ReturnStatus.CAPTURING),
    (ReturnStatus.AWAITING_OPERATOR, ReturnStatus.CAPTURING),
    (ReturnStatus.AWAITING_REVIEW, ReturnStatus.CAPTURING),
    (ReturnStatus.AWAITING_SIGNOFF, ReturnStatus.CAPTURING),
    (ReturnStatus.NEEDS_ATTENTION, ReturnStatus.CAPTURING),
}


_VALID_JOB_TRANSITIONS: Final[set[tuple[JobStatus | None, JobStatus]]] = {
    # initial creation
    (None, JobStatus.PENDING),
    # claiming
    (JobStatus.PENDING, JobStatus.IN_PROGRESS),
    (JobStatus.FAILED_RETRYABLE, JobStatus.IN_PROGRESS),
    # completion
    (JobStatus.IN_PROGRESS, JobStatus.SUCCEEDED),
    (JobStatus.IN_PROGRESS, JobStatus.FAILED_RETRYABLE),
    (JobStatus.IN_PROGRESS, JobStatus.NEEDS_ATTENTION),
    # cancellation
    (JobStatus.IN_PROGRESS, JobStatus.CANCELLED),
    (JobStatus.PENDING, JobStatus.CANCELLED),
    (JobStatus.FAILED_RETRYABLE, JobStatus.CANCELLED),
    # retry / recovery
    (JobStatus.FAILED_RETRYABLE, JobStatus.NEEDS_ATTENTION),
    (JobStatus.NEEDS_ATTENTION, JobStatus.PENDING),
}


def transition_return_status(
    current: ReturnStatus | str | None,
    target: ReturnStatus | str,
    trigger: str | None = None,
) -> ReturnStatus:
    """Validate and perform a return status transition.

    Raises:
        InvalidTransitionError: If the transition is not allowed by the state machine.
    """
    curr_enum = ReturnStatus(current) if current is not None else None
    target_enum = ReturnStatus(target)

    if (curr_enum, target_enum) not in _VALID_RETURN_TRANSITIONS:
        raise InvalidTransitionError(
            entity="return",
            current=str(curr_enum) if curr_enum else None,
            target=str(target_enum),
            trigger=trigger,
        )
    return target_enum


def transition_job_status(
    current: JobStatus | str | None,
    target: JobStatus | str,
    trigger: str | None = None,
) -> JobStatus:
    """Validate and perform a job status transition.

    Raises:
        InvalidTransitionError: If the transition is not allowed by the state machine.
    """
    curr_enum = JobStatus(current) if current is not None else None
    target_enum = JobStatus(target)

    if (curr_enum, target_enum) not in _VALID_JOB_TRANSITIONS:
        raise InvalidTransitionError(
            entity="job",
            current=str(curr_enum) if curr_enum else None,
            target=str(target_enum),
            trigger=trigger,
        )
    return target_enum
