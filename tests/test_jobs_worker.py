import asyncio
from pathlib import Path

from bc.backends import Backend, BackendError, BackendErrorKind, LocalBackend
from bc.backends.base import PreviewResult
from bc.core import Entry, EntryType, Location, OperationResult, parse_location
from bc.core.task_manager import ProgressSink
from bc.jobs import (
    BatchJobWorker,
    JobItemStatus,
    JobStatus,
    RetryPolicy,
    SQLiteJobStore,
    plan_copy,
    plan_delete,
    plan_move,
)

RETRY_ATTEMPTS = 2


def test_worker_executes_local_copy_job(tmp_path: Path) -> None:
    source = tmp_path / "source.txt"
    source.write_text("copy", encoding="utf-8")
    destination = tmp_path / "destination"
    destination.mkdir()
    store = SQLiteJobStore(tmp_path / "jobs.sqlite3")
    record = store.add_plan(
        plan_copy(
            (_entry(source),),
            source_panel="left",
            destination=parse_location(destination),
        )
    )
    worker = BatchJobWorker(store=store, backend=LocalBackend())

    result = asyncio.run(worker.run_next())

    assert result is not None
    assert result.status is JobStatus.COMPLETED
    assert (destination / "source.txt").read_text(encoding="utf-8") == "copy"
    stored = store.get_job(record.job_id)
    assert stored is not None
    assert stored.status is JobStatus.COMPLETED
    assert store.list_items(record.job_id)[0].status is JobItemStatus.COMPLETED


def test_worker_moves_only_after_copy_verifies(tmp_path: Path) -> None:
    source = tmp_path / "source.txt"
    source.write_text("move", encoding="utf-8")
    destination = tmp_path / "destination"
    destination.mkdir()
    store = SQLiteJobStore(tmp_path / "jobs.sqlite3")
    store.add_plan(
        plan_move(
            (_entry(source),),
            source_panel="left",
            destination=parse_location(destination),
        )
    )
    worker = BatchJobWorker(store=store, backend=LocalBackend())

    result = asyncio.run(worker.run_next())

    assert result is not None
    assert result.status is JobStatus.COMPLETED
    assert not source.exists()
    assert (destination / "source.txt").read_text(encoding="utf-8") == "move"


def test_worker_executes_delete_job(tmp_path: Path) -> None:
    source = tmp_path / "delete.txt"
    source.write_text("delete", encoding="utf-8")
    store = SQLiteJobStore(tmp_path / "jobs.sqlite3")
    store.add_plan(plan_delete((_entry(source),), source_panel="left"))
    worker = BatchJobWorker(store=store, backend=LocalBackend())

    result = asyncio.run(worker.run_next())

    assert result is not None
    assert result.status is JobStatus.COMPLETED
    assert not source.exists()


def test_worker_retries_failed_item(tmp_path: Path) -> None:
    source = tmp_path / "source.txt"
    source.write_text("retry", encoding="utf-8")
    destination = tmp_path / "destination"
    destination.mkdir()
    store = SQLiteJobStore(tmp_path / "jobs.sqlite3")
    record = store.add_plan(
        plan_copy(
            (_entry(source),),
            source_panel="left",
            destination=parse_location(destination),
        )
    )
    backend = FlakyCopyBackend()
    worker = BatchJobWorker(
        store=store,
        backend=backend,
        retry_policy=RetryPolicy(max_attempts=RETRY_ATTEMPTS),
    )

    result = asyncio.run(worker.run_next())
    item = store.list_items(record.job_id)[0]

    assert result is not None
    assert result.status is JobStatus.COMPLETED
    assert item.status is JobItemStatus.COMPLETED
    assert item.attempts == RETRY_ATTEMPTS


def test_worker_records_permanent_item_failure(tmp_path: Path) -> None:
    source = tmp_path / "source.txt"
    source.write_text("fail", encoding="utf-8")
    destination = tmp_path / "destination"
    destination.mkdir()
    store = SQLiteJobStore(tmp_path / "jobs.sqlite3")
    record = store.add_plan(
        plan_copy(
            (_entry(source),),
            source_panel="left",
            destination=parse_location(destination),
        )
    )
    backend = AlwaysFailCopyBackend()
    worker = BatchJobWorker(
        store=store,
        backend=backend,
        retry_policy=RetryPolicy(max_attempts=RETRY_ATTEMPTS),
    )

    result = asyncio.run(worker.run_next())
    item = store.list_items(record.job_id)[0]

    assert result is not None
    assert result.status is JobStatus.FAILED
    assert result.failed_items == 1
    assert item.status is JobItemStatus.FAILED
    assert item.attempts == RETRY_ATTEMPTS
    assert "copy failed" in item.latest_error


def _entry(path: Path) -> Entry:
    return Entry(
        location=parse_location(path),
        name=path.name,
        entry_type=EntryType.FILE,
        size=path.stat().st_size,
    )


class FlakyCopyBackend(LocalBackend):
    def __init__(self) -> None:
        self.calls = 0

    async def copy(
        self,
        source: Location,
        destination: Location,
        *,
        progress: ProgressSink | None = None,
    ) -> OperationResult:
        self.calls += 1
        if self.calls == 1:
            raise BackendError(BackendErrorKind.IO_ERROR, "copy failed", location=source)
        return await super().copy(source, destination, progress=progress)


class AlwaysFailCopyBackend(Backend):
    provider = "always-fail"

    def supports(self, location: Location) -> bool:
        _ = location
        return True

    async def list(self, location: Location) -> tuple[Entry, ...]:
        _ = location
        return ()

    async def stat(self, location: Location) -> Entry:
        return Entry(location=location, name=location.name, entry_type=EntryType.FILE)

    async def preview(self, location: Location, *, max_bytes: int) -> PreviewResult:
        _ = location, max_bytes
        return PreviewResult(b"")

    async def mkdir(self, location: Location, *, parents: bool = True) -> OperationResult:
        _ = parents
        return OperationResult.success(destination=location)

    async def copy(
        self,
        source: Location,
        destination: Location,
        *,
        progress: ProgressSink | None = None,
    ) -> OperationResult:
        _ = destination, progress
        raise BackendError(BackendErrorKind.IO_ERROR, "copy failed", location=source)

    async def move(self, source: Location, destination: Location) -> OperationResult:
        _ = destination
        raise BackendError(BackendErrorKind.IO_ERROR, "move failed", location=source)

    async def rename(self, source: Location, new_name: str) -> OperationResult:
        _ = new_name
        raise BackendError(BackendErrorKind.IO_ERROR, "rename failed", location=source)

    async def delete(
        self,
        location: Location,
        *,
        recursive: bool = False,
        progress: ProgressSink | None = None,
    ) -> OperationResult:
        _ = recursive, progress
        raise BackendError(BackendErrorKind.IO_ERROR, "delete failed", location=location)
