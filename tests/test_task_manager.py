import asyncio
import time
from pathlib import Path

from bc.backends import LocalBackend
from bc.core import (
    OperationResult,
    TaskContext,
    TaskManager,
    TaskRecord,
    TaskState,
    TaskType,
    parse_location,
)

EXPECTED_ITEMS = 2
EXPECTED_BYTES = 10


def test_task_manager_completes_task_with_progress() -> None:
    manager = TaskManager()
    try:

        async def runner(context: TaskContext) -> OperationResult:
            context.progress.update(items_total=2, bytes_total=10, message="Working")
            context.progress.advance(items=1, bytes_count=4, current_item="alpha")
            context.progress.advance(items=1, bytes_count=6, current_item="bravo")
            return OperationResult.success("done", entries_affected=2, bytes_affected=10)

        task_id = manager.start_task(TaskType.COPY, runner)
        record = wait_for_task(manager, task_id)

        assert record.status is TaskState.COMPLETED
        assert record.progress.items_done == EXPECTED_ITEMS
        assert record.progress.bytes_done == EXPECTED_BYTES
        assert record.progress.current_item == "bravo"
        assert record.result is not None
        assert record.result.ok
    finally:
        manager.close()


def test_task_manager_marks_failed_task() -> None:
    manager = TaskManager()
    try:

        async def runner(_context: TaskContext) -> OperationResult:
            msg = "boom"
            raise RuntimeError(msg)

        task_id = manager.start_task(TaskType.DELETE, runner)
        record = wait_for_task(manager, task_id)

        assert record.status is TaskState.FAILED
        assert record.latest_error == "boom"
        assert record.ended_at is not None
    finally:
        manager.close()


def test_task_manager_requests_cooperative_cancellation() -> None:
    manager = TaskManager()
    try:

        async def runner(context: TaskContext) -> OperationResult:
            while True:
                context.raise_if_cancelled()
                await asyncio.sleep(0.01)

        task_id = manager.start_task(TaskType.DELETE, runner)
        wait_for_status(manager, task_id, TaskState.RUNNING)

        assert manager.cancel(task_id)

        record = wait_for_task(manager, task_id)
        assert record.status is TaskState.CANCELLED
    finally:
        manager.close()


def test_local_copy_reports_real_progress(tmp_path: Path) -> None:
    manager = TaskManager()
    source = tmp_path / "source.txt"
    destination = tmp_path / "destination"
    payload = b"x" * 2048
    source.write_bytes(payload)
    destination.mkdir()
    backend = LocalBackend()
    try:

        async def runner(context: TaskContext) -> OperationResult:
            return await backend.copy(
                parse_location(source),
                parse_location(destination),
                progress=context.progress,
            )

        task_id = manager.start_task(TaskType.COPY, runner)
        record = wait_for_task(manager, task_id)

        assert record.status is TaskState.COMPLETED
        assert record.progress.items_total == 1
        assert record.progress.items_done == 1
        assert record.progress.bytes_total == len(payload)
        assert record.progress.bytes_done == len(payload)
        assert (destination / "source.txt").read_bytes() == payload
    finally:
        manager.close()


def wait_for_task(manager: TaskManager, task_id: str) -> TaskRecord:
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        record = manager.record(task_id)
        assert record is not None
        if record.status.is_terminal:
            return record
        time.sleep(0.01)
    raise AssertionError(f"Task {task_id} did not finish")


def wait_for_status(manager: TaskManager, task_id: str, status: TaskState) -> TaskRecord:
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        record = manager.record(task_id)
        assert record is not None
        if record.status is status:
            return record
        time.sleep(0.01)
    raise AssertionError(f"Task {task_id} did not reach {status}")
