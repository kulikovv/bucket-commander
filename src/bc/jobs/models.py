"""Durable batch job domain models."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum

from bc.core import EntryType, Location
from bc.jobs.planner import OperationKind, OperationPlan


class JobStatus(StrEnum):
    """Durable job lifecycle states."""

    QUEUED = "queued"
    RUNNING = "running"
    PAUSED = "paused"
    CANCELLING = "cancelling"
    CANCELLED = "cancelled"
    FAILED = "failed"
    COMPLETED = "completed"

    @property
    def is_terminal(self) -> bool:
        return self in {JobStatus.CANCELLED, JobStatus.FAILED, JobStatus.COMPLETED}


class JobItemStatus(StrEnum):
    """Lifecycle states for one durable job item."""

    PENDING = "pending"
    RUNNING = "running"
    SKIPPED = "skipped"
    FAILED = "failed"
    COMPLETED = "completed"

    @property
    def is_terminal(self) -> bool:
        return self in {JobItemStatus.SKIPPED, JobItemStatus.FAILED, JobItemStatus.COMPLETED}


class JobPhase(StrEnum):
    """Coarse phase for multi-step durable jobs."""

    PLANNING = "planning"
    COPYING = "copying"
    VERIFYING = "verifying"
    DELETING = "deleting"
    COMPLETE = "complete"


@dataclass(frozen=True, slots=True)
class JobItem:
    """One planned source entry in a durable job."""

    item_id: str
    job_id: str
    item_index: int
    name: str
    location: Location
    entry_type: EntryType
    size: int | None = None
    status: JobItemStatus = JobItemStatus.PENDING
    attempts: int = 0
    latest_error: str = ""

    def __post_init__(self) -> None:
        if self.item_index < 0:
            msg = "item_index must be non-negative"
            raise ValueError(msg)
        if self.size is not None and self.size < 0:
            msg = "size must be non-negative"
            raise ValueError(msg)
        if self.attempts < 0:
            msg = "attempts must be non-negative"
            raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class JobRecord:
    """Durable metadata for a planned batch job."""

    job_id: str
    plan: OperationPlan
    status: JobStatus = JobStatus.QUEUED
    phase: JobPhase = JobPhase.PLANNING
    created_at: datetime | None = None
    updated_at: datetime | None = None
    started_at: datetime | None = None
    ended_at: datetime | None = None
    latest_error: str = ""

    def __post_init__(self) -> None:
        if not self.job_id:
            msg = "job_id must not be empty"
            raise ValueError(msg)
        if self.created_at is not None:
            object.__setattr__(self, "created_at", _normalize_datetime(self.created_at))
        if self.updated_at is not None:
            object.__setattr__(self, "updated_at", _normalize_datetime(self.updated_at))
        if self.started_at is not None:
            object.__setattr__(self, "started_at", _normalize_datetime(self.started_at))
        if self.ended_at is not None:
            object.__setattr__(self, "ended_at", _normalize_datetime(self.ended_at))

    @property
    def kind(self) -> OperationKind:
        return self.plan.kind


VALID_JOB_TRANSITIONS: dict[JobStatus, frozenset[JobStatus]] = {
    JobStatus.QUEUED: frozenset({JobStatus.RUNNING, JobStatus.PAUSED, JobStatus.CANCELLED}),
    JobStatus.RUNNING: frozenset(
        {
            JobStatus.PAUSED,
            JobStatus.CANCELLING,
            JobStatus.FAILED,
            JobStatus.COMPLETED,
        }
    ),
    JobStatus.PAUSED: frozenset({JobStatus.QUEUED, JobStatus.CANCELLED}),
    JobStatus.CANCELLING: frozenset({JobStatus.CANCELLED, JobStatus.FAILED}),
    JobStatus.CANCELLED: frozenset(),
    JobStatus.FAILED: frozenset({JobStatus.QUEUED}),
    JobStatus.COMPLETED: frozenset(),
}


VALID_ITEM_TRANSITIONS: dict[JobItemStatus, frozenset[JobItemStatus]] = {
    JobItemStatus.PENDING: frozenset(
        {JobItemStatus.RUNNING, JobItemStatus.SKIPPED, JobItemStatus.FAILED}
    ),
    JobItemStatus.RUNNING: frozenset({JobItemStatus.COMPLETED, JobItemStatus.FAILED}),
    JobItemStatus.SKIPPED: frozenset(),
    JobItemStatus.FAILED: frozenset({JobItemStatus.PENDING}),
    JobItemStatus.COMPLETED: frozenset(),
}


def transition_job_status(
    record: JobRecord,
    status: JobStatus,
    *,
    phase: JobPhase | None = None,
    latest_error: str | None = None,
    now: datetime | None = None,
) -> JobRecord:
    """Return a job record with a validated status transition."""

    if status != record.status and status not in VALID_JOB_TRANSITIONS[record.status]:
        msg = f"Invalid job transition: {record.status.value} -> {status.value}"
        raise ValueError(msg)
    timestamp = _normalize_datetime(now or datetime.now(UTC))
    return replace(
        record,
        status=status,
        phase=record.phase if phase is None else phase,
        updated_at=timestamp,
        started_at=_started_at(record, status, timestamp),
        ended_at=timestamp if status.is_terminal else record.ended_at,
        latest_error=record.latest_error if latest_error is None else latest_error,
    )


def transition_item_status(
    item: JobItem,
    status: JobItemStatus,
    *,
    latest_error: str | None = None,
) -> JobItem:
    """Return a job item with a validated status transition."""

    if status != item.status and status not in VALID_ITEM_TRANSITIONS[item.status]:
        msg = f"Invalid job item transition: {item.status.value} -> {status.value}"
        raise ValueError(msg)
    attempts = item.attempts + 1 if status is JobItemStatus.RUNNING else item.attempts
    return replace(
        item,
        status=status,
        attempts=attempts,
        latest_error=item.latest_error if latest_error is None else latest_error,
    )


def _normalize_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _started_at(record: JobRecord, status: JobStatus, timestamp: datetime) -> datetime | None:
    if status is JobStatus.RUNNING and record.started_at is None:
        return timestamp
    return record.started_at
