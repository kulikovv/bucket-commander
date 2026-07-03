"""Short-lived task tracking and progress reporting."""

from __future__ import annotations

import asyncio
import itertools
import threading
from collections.abc import Awaitable, Callable
from concurrent.futures import Future
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum

from bc.core.locations import Location
from bc.core.models import OperationResult


class TaskState(StrEnum):
    """Lifecycle states for short-lived UI tasks."""

    PENDING = "pending"
    RUNNING = "running"
    CANCELLING = "cancelling"
    CANCELLED = "cancelled"
    FAILED = "failed"
    COMPLETED = "completed"

    @property
    def is_terminal(self) -> bool:
        return self in {TaskState.CANCELLED, TaskState.FAILED, TaskState.COMPLETED}


class TaskType(StrEnum):
    """Task categories shown by the UI."""

    COPY = "copy"
    MOVE = "move"
    DELETE = "delete"
    LISTING = "listing"
    INDEXING = "indexing"
    SEARCH = "search"


class OperationCancelledError(Exception):
    """Raised by cooperative task workers when cancellation is requested."""


@dataclass(frozen=True, slots=True)
class ProgressSnapshot:
    """Immutable progress values for a task."""

    items_total: int = 0
    items_done: int = 0
    bytes_total: int = 0
    bytes_done: int = 0
    current_item: str = ""
    message: str = ""

    def __post_init__(self) -> None:
        for name in ("items_total", "items_done", "bytes_total", "bytes_done"):
            value = getattr(self, name)
            if value < 0:
                msg = f"{name} must be non-negative"
                raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class TaskRecord:
    """Current state of one task."""

    task_id: str
    task_type: TaskType
    status: TaskState = TaskState.PENDING
    source: Location | None = None
    destination: Location | None = None
    progress: ProgressSnapshot = ProgressSnapshot()
    started_at: datetime | None = None
    ended_at: datetime | None = None
    latest_error: str = ""
    result: OperationResult | None = None


class CancellationToken:
    """Thread-safe cancellation flag shared with task workers."""

    def __init__(self) -> None:
        self._event = threading.Event()

    def cancel(self) -> None:
        self._event.set()

    @property
    def is_cancelled(self) -> bool:
        return self._event.is_set()

    def raise_if_cancelled(self) -> None:
        if self.is_cancelled:
            raise OperationCancelledError


class ProgressSink:
    """Thread-safe progress reporter passed to backends and task workers."""

    def __init__(self, manager: TaskManager, task_id: str, token: CancellationToken) -> None:
        self._manager = manager
        self._task_id = task_id
        self._token = token

    def update(
        self,
        *,
        items_total: int | None = None,
        items_done: int | None = None,
        bytes_total: int | None = None,
        bytes_done: int | None = None,
        current_item: str | None = None,
        message: str | None = None,
    ) -> None:
        self._token.raise_if_cancelled()
        self._manager.update_progress(
            self._task_id,
            items_total=items_total,
            items_done=items_done,
            bytes_total=bytes_total,
            bytes_done=bytes_done,
            current_item=current_item,
            message=message,
        )

    def advance(
        self,
        *,
        items: int = 0,
        bytes_count: int = 0,
        current_item: str | None = None,
        message: str | None = None,
    ) -> None:
        self._token.raise_if_cancelled()
        self._manager.advance_progress(
            self._task_id,
            items=items,
            bytes_count=bytes_count,
            current_item=current_item,
            message=message,
        )

    def raise_if_cancelled(self) -> None:
        self._token.raise_if_cancelled()


@dataclass(frozen=True, slots=True)
class TaskContext:
    """Values supplied to a task coroutine."""

    task_id: str
    progress: ProgressSink
    cancellation: CancellationToken

    def raise_if_cancelled(self) -> None:
        self.cancellation.raise_if_cancelled()


TaskRunner = Callable[[TaskContext], Awaitable[OperationResult | None]]


class TaskManager:
    """Own short-lived tasks and publish immutable task records."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._records: dict[str, TaskRecord] = {}
        self._tokens: dict[str, CancellationToken] = {}
        self._futures: dict[str, Future[OperationResult | None]] = {}
        self._ids = itertools.count(1)
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._run_loop,
            name="bucket-commander-tasks",
            daemon=True,
        )
        self._thread.start()

    def start_task(
        self,
        task_type: TaskType,
        runner: TaskRunner,
        *,
        source: Location | None = None,
        destination: Location | None = None,
    ) -> str:
        """Start a task on the manager's background event loop."""

        task_id = f"task-{next(self._ids)}"
        token = CancellationToken()
        record = TaskRecord(
            task_id=task_id,
            task_type=task_type,
            source=source,
            destination=destination,
        )
        with self._lock:
            self._records[task_id] = record
            self._tokens[task_id] = token
        sink = ProgressSink(self, task_id, token)
        context = TaskContext(task_id=task_id, progress=sink, cancellation=token)
        future = asyncio.run_coroutine_threadsafe(
            self._run_task(task_id, runner, context),
            self._loop,
        )
        with self._lock:
            self._futures[task_id] = future
        return task_id

    def cancel(self, task_id: str) -> bool:
        """Request cooperative cancellation for a running task."""

        with self._lock:
            token = self._tokens.get(task_id)
            record = self._records.get(task_id)
            if token is None or record is None or record.status.is_terminal:
                return False
            token.cancel()
            self._records[task_id] = replace(record, status=TaskState.CANCELLING)
            return True

    def record(self, task_id: str) -> TaskRecord | None:
        with self._lock:
            return self._records.get(task_id)

    def records(self) -> tuple[TaskRecord, ...]:
        with self._lock:
            return tuple(self._records.values())

    def active_records(self) -> tuple[TaskRecord, ...]:
        return tuple(record for record in self.records() if not record.status.is_terminal)

    def latest_record(self) -> TaskRecord | None:
        records = self.records()
        if not records:
            return None
        return records[-1]

    def update_progress(
        self,
        task_id: str,
        *,
        items_total: int | None = None,
        items_done: int | None = None,
        bytes_total: int | None = None,
        bytes_done: int | None = None,
        current_item: str | None = None,
        message: str | None = None,
    ) -> None:
        self._mutate_progress(
            task_id,
            lambda progress: replace(
                progress,
                items_total=progress.items_total if items_total is None else items_total,
                items_done=progress.items_done if items_done is None else items_done,
                bytes_total=progress.bytes_total if bytes_total is None else bytes_total,
                bytes_done=progress.bytes_done if bytes_done is None else bytes_done,
                current_item=progress.current_item if current_item is None else current_item,
                message=progress.message if message is None else message,
            ),
        )

    def advance_progress(
        self,
        task_id: str,
        *,
        items: int = 0,
        bytes_count: int = 0,
        current_item: str | None = None,
        message: str | None = None,
    ) -> None:
        self._mutate_progress(
            task_id,
            lambda progress: replace(
                progress,
                items_done=progress.items_done + items,
                bytes_done=progress.bytes_done + bytes_count,
                current_item=progress.current_item if current_item is None else current_item,
                message=progress.message if message is None else message,
            ),
        )

    def close(self) -> None:
        """Stop the background event loop."""

        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=1)

    async def _run_task(
        self,
        task_id: str,
        runner: TaskRunner,
        context: TaskContext,
    ) -> OperationResult | None:
        self._set_record(
            task_id,
            status=TaskState.RUNNING,
            started_at=datetime.now(tz=UTC),
        )
        try:
            context.raise_if_cancelled()
            result = await runner(context)
        except OperationCancelledError:
            self._set_record(
                task_id,
                status=TaskState.CANCELLED,
                ended_at=datetime.now(tz=UTC),
                latest_error="Cancelled",
            )
            return None
        except asyncio.CancelledError:
            self._set_record(
                task_id,
                status=TaskState.CANCELLED,
                ended_at=datetime.now(tz=UTC),
                latest_error="Cancelled",
            )
            raise
        except Exception as error:
            self._set_record(
                task_id,
                status=TaskState.FAILED,
                ended_at=datetime.now(tz=UTC),
                latest_error=str(error),
            )
            return None
        self._set_record(
            task_id,
            status=TaskState.COMPLETED,
            ended_at=datetime.now(tz=UTC),
            result=result,
        )
        return result

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def _set_record(
        self,
        task_id: str,
        *,
        status: TaskState | None = None,
        started_at: datetime | None = None,
        ended_at: datetime | None = None,
        latest_error: str | None = None,
        result: OperationResult | None = None,
    ) -> None:
        with self._lock:
            record = self._records[task_id]
            self._records[task_id] = replace(
                record,
                status=record.status if status is None else status,
                started_at=record.started_at if started_at is None else started_at,
                ended_at=record.ended_at if ended_at is None else ended_at,
                latest_error=record.latest_error if latest_error is None else latest_error,
                result=record.result if result is None else result,
            )

    def _mutate_progress(
        self,
        task_id: str,
        mutate: Callable[[ProgressSnapshot], ProgressSnapshot],
    ) -> None:
        with self._lock:
            record = self._records.get(task_id)
            if record is None or record.status.is_terminal:
                return
            self._records[task_id] = replace(record, progress=mutate(record.progress))
