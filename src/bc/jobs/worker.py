"""Durable batch job worker."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace

from bc.backends import Backend, BackendError, BackendErrorKind
from bc.core import Location, OperationResult
from bc.core.locations import LocalLocation, S3Location
from bc.jobs.models import (
    JobItem,
    JobItemStatus,
    JobPhase,
    JobRecord,
    JobStatus,
    transition_item_status,
    transition_job_status,
)
from bc.jobs.planner import ConflictPolicy, OperationKind, OperationPlan
from bc.jobs.store import SQLiteJobStore


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """Retry limits for durable job items."""

    max_attempts: int = 3

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            msg = "max_attempts must be at least 1"
            raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class JobExecutionResult:
    """Summary returned after a worker attempts a job."""

    job_id: str
    status: JobStatus
    completed_items: int
    failed_items: int


class BatchJobWorker:
    """Execute queued durable jobs against storage backends."""

    def __init__(
        self,
        *,
        store: SQLiteJobStore,
        backend: Backend,
        retry_policy: RetryPolicy | None = None,
        concurrency: int = 4,
    ) -> None:
        if concurrency < 1:
            msg = "concurrency must be at least 1"
            raise ValueError(msg)
        self._store = store
        self._backend = backend
        self._retry_policy = retry_policy or RetryPolicy()
        self._semaphore = asyncio.Semaphore(concurrency)

    async def run_next(self) -> JobExecutionResult | None:
        """Run the oldest queued job, if present."""

        record = self._store.next_queued_job()
        if record is None:
            return None
        return await self.run_job(record.job_id)

    async def run_job(self, job_id: str) -> JobExecutionResult:
        """Run one durable job by id."""

        record = self._require_job(job_id)
        self._recover_interrupted_items(record)
        running = transition_job_status(record, JobStatus.RUNNING, phase=_phase_for(record.plan))
        self._store.update_job_status(running)
        try:
            await self._preflight(running.plan)
            await self._run_items(running)
        except Exception as error:
            latest = self._require_job(job_id)
            if latest.status is JobStatus.CANCELLED:
                return self._result(latest)
            failed = transition_job_status(
                latest,
                JobStatus.FAILED,
                latest_error=str(error),
            )
            self._store.update_job_status(failed)
            return self._result(failed)
        current = self._require_job(job_id)
        items = self._store.list_items(job_id)
        if any(item.status is JobItemStatus.FAILED for item in items):
            failed = transition_job_status(
                current,
                JobStatus.FAILED,
                latest_error="One or more job items failed",
            )
            self._store.update_job_status(failed)
            return self._result(failed)
        incomplete_items = any(
            item.status not in {JobItemStatus.COMPLETED, JobItemStatus.SKIPPED} for item in items
        )
        if incomplete_items:
            failed = transition_job_status(
                current,
                JobStatus.FAILED,
                latest_error="One or more job items did not reach a terminal state",
            )
            self._store.update_job_status(failed)
            return self._result(failed)
        completed = transition_job_status(
            current,
            JobStatus.COMPLETED,
            phase=JobPhase.COMPLETE,
        )
        self._store.update_job_status(completed)
        return self._result(completed)

    async def _run_items(self, record: JobRecord) -> None:
        items = tuple(item for item in self._store.list_items(record.job_id) if _is_runnable(item))
        await asyncio.gather(*(self._run_item(record, item) for item in items))

    async def _run_item(self, record: JobRecord, item: JobItem) -> None:
        async with self._semaphore:
            current = _reset_failed_item(item)
            while current.attempts < self._retry_policy.max_attempts:
                self._raise_if_cancelling(record.job_id)
                running = transition_item_status(current, JobItemStatus.RUNNING)
                self._store.update_item(running)
                try:
                    await self._execute_item(record.plan, running)
                except Exception as error:
                    failed = transition_item_status(
                        running,
                        JobItemStatus.FAILED,
                        latest_error=str(error),
                    )
                    self._store.update_item(failed)
                    if failed.attempts >= self._retry_policy.max_attempts:
                        return
                    current = transition_item_status(failed, JobItemStatus.PENDING)
                    self._store.update_item(current)
                    continue
                completed = transition_item_status(running, JobItemStatus.COMPLETED)
                self._store.update_item(completed)
                return

    async def _execute_item(self, plan: OperationPlan, item: JobItem) -> OperationResult:
        if plan.kind is OperationKind.COPY:
            return await self._copy_item(plan, item)
        if plan.kind is OperationKind.DELETE:
            return await self._delete_item(item)
        if plan.kind is OperationKind.MOVE:
            copied = await self._copy_item(plan, item)
            if not item.entry_type.is_container:
                await self._verify_copy(_verified_destination(plan.destination, item, copied))
            await self._delete_item(item)
            return copied
        msg = f"Unsupported durable job kind: {plan.kind.value}"
        raise ValueError(msg)

    async def _copy_item(self, plan: OperationPlan, item: JobItem) -> OperationResult:
        if plan.destination is None:
            msg = f"{plan.kind.value} job requires a destination"
            raise ValueError(msg)
        return await self._backend.copy(item.location, plan.destination)

    async def _delete_item(self, item: JobItem) -> OperationResult:
        return await self._backend.delete(
            item.location,
            recursive=item.entry_type.is_container,
        )

    async def _verify_copy(self, destination: Location | None) -> None:
        if destination is None:
            msg = "Move copy did not report a destination"
            raise ValueError(msg)
        await self._backend.stat(destination)

    async def _preflight(self, plan: OperationPlan) -> None:
        if plan.kind in {OperationKind.COPY, OperationKind.MOVE} and plan.destination is None:
            msg = f"{plan.kind.value} job requires a destination"
            raise ValueError(msg)
        if plan.conflict_policy is ConflictPolicy.FAIL_IF_EXISTS and plan.destination is not None:
            await self._fail_if_destination_exists(plan.destination)

    async def _fail_if_destination_exists(self, destination: Location) -> None:
        try:
            await self._backend.stat(destination)
        except BackendError as error:
            if error.kind is BackendErrorKind.NOT_FOUND:
                return
            raise
        msg = f"Destination already exists: {destination.label}"
        raise BackendError(BackendErrorKind.ALREADY_EXISTS, msg, destination=destination)

    def _raise_if_cancelling(self, job_id: str) -> None:
        record = self._require_job(job_id)
        if record.status in {JobStatus.CANCELLING, JobStatus.CANCELLED}:
            cancelled = transition_job_status(record, JobStatus.CANCELLED)
            self._store.update_job_status(cancelled)
            msg = f"Job cancelled: {job_id}"
            raise RuntimeError(msg)

    def _require_job(self, job_id: str) -> JobRecord:
        record = self._store.get_job(job_id)
        if record is None:
            msg = f"Unknown durable job: {job_id}"
            raise KeyError(msg)
        return record

    def _recover_interrupted_items(self, record: JobRecord) -> None:
        if record.status is not JobStatus.RUNNING:
            return
        for item in self._store.list_items(record.job_id):
            if item.status is JobItemStatus.RUNNING:
                self._store.update_item(
                    replace(
                        item,
                        status=JobItemStatus.PENDING,
                        latest_error="Recovered after interruption",
                    )
                )

    def _result(self, record: JobRecord) -> JobExecutionResult:
        items = self._store.list_items(record.job_id)
        return JobExecutionResult(
            job_id=record.job_id,
            status=record.status,
            completed_items=sum(item.status is JobItemStatus.COMPLETED for item in items),
            failed_items=sum(item.status is JobItemStatus.FAILED for item in items),
        )


def _phase_for(plan: OperationPlan) -> JobPhase:
    if plan.kind in {OperationKind.COPY, OperationKind.MOVE}:
        return JobPhase.COPYING
    if plan.kind is OperationKind.DELETE:
        return JobPhase.DELETING
    return JobPhase.PLANNING


def _is_runnable(item: JobItem) -> bool:
    return item.status in {JobItemStatus.PENDING, JobItemStatus.FAILED}


def _reset_failed_item(item: JobItem) -> JobItem:
    if item.status is JobItemStatus.FAILED:
        return transition_item_status(item, JobItemStatus.PENDING)
    return item


def _verified_destination(
    plan_destination: Location | None,
    item: JobItem,
    result: OperationResult,
) -> Location | None:
    if result.destination is not None and not (
        isinstance(plan_destination, S3Location) and result.destination == plan_destination
    ):
        return result.destination
    if plan_destination is None:
        return result.destination
    if isinstance(plan_destination, LocalLocation) and isinstance(item.location, S3Location):
        return LocalLocation(plan_destination.path / item.location.name)
    if isinstance(plan_destination, S3Location):
        return S3Location(
            bucket=plan_destination.bucket,
            prefix=f"{plan_destination.prefix}{item.name}".lstrip("/"),
            profile=plan_destination.profile,
            region=plan_destination.region,
            endpoint_url=plan_destination.endpoint_url,
        )
    return result.destination
