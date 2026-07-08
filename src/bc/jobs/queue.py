"""Durable job queue facade backed by the job store."""

from __future__ import annotations

from datetime import datetime

from bc.jobs.models import JobRecord, JobStatus, transition_job_status
from bc.jobs.planner import OperationPlan
from bc.jobs.store import SQLiteJobStore


class DurableJobQueue:
    """Queue planned jobs for later durable worker execution."""

    def __init__(self, store: SQLiteJobStore) -> None:
        self._store = store

    def enqueue(self, plan: OperationPlan, *, now: datetime | None = None) -> JobRecord:
        """Persist a plan as a queued durable job."""

        return self._store.add_plan(plan, now=now)

    def pause(self, job_id: str, *, now: datetime | None = None) -> JobRecord:
        """Pause a queued or running job."""

        return self._transition(job_id, JobStatus.PAUSED, now=now)

    def resume(self, job_id: str, *, now: datetime | None = None) -> JobRecord:
        """Return a paused or failed job to the queue."""

        return self._transition(job_id, JobStatus.QUEUED, now=now)

    def request_cancel(self, job_id: str, *, now: datetime | None = None) -> JobRecord:
        """Cancel queued/paused jobs or mark running jobs as cancelling."""

        record = self._require_job(job_id)
        target = JobStatus.CANCELLING if record.status is JobStatus.RUNNING else JobStatus.CANCELLED
        updated = transition_job_status(record, target, now=now)
        self._store.update_job_status(updated, now=now)
        return updated

    def list_jobs(self) -> tuple[JobRecord, ...]:
        """Return queued and historical jobs."""

        return self._store.list_jobs()

    def _transition(
        self,
        job_id: str,
        status: JobStatus,
        *,
        now: datetime | None = None,
    ) -> JobRecord:
        record = self._require_job(job_id)
        updated = transition_job_status(record, status, now=now)
        self._store.update_job_status(updated, now=now)
        return updated

    def _require_job(self, job_id: str) -> JobRecord:
        record = self._store.get_job(job_id)
        if record is None:
            msg = f"Unknown durable job: {job_id}"
            raise KeyError(msg)
        return record
