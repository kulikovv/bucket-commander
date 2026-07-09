import time
from datetime import UTC, datetime
from pathlib import Path

from bc.app import AppConfig, BucketCommanderApp
from bc.backends import Backend
from bc.backends.base import PreviewResult
from bc.core import (
    Entry,
    EntryType,
    Location,
    OperationResult,
    PanelState,
    S3Location,
    TaskManager,
    TaskRecord,
    TaskType,
    parse_location,
)
from bc.core.task_manager import ProgressSink
from bc.index import RecursiveIndexResult
from bc.index.cache_paths import account_id, index_dir
from bc.index.parquet_store import ObjectMetadata, ParquetIndexStore
from bc.jobs import JobStatus, plan_delete
from bc.ui.commands import PanelId, TwoPanelState

INDEXED_OBJECTS = 2
INDEXED_BYTES = 30


def indexed_object_row(
    key: str,
    *,
    parent_prefix: str,
    name: str,
    size: int,
    bucket: str,
) -> ObjectMetadata:
    timestamp = datetime(2026, 1, 1, tzinfo=UTC)
    return ObjectMetadata(
        provider="s3",
        account_id="default",
        bucket=bucket,
        key=key,
        parent_prefix=parent_prefix,
        name=name,
        size=size,
        last_modified=timestamp,
        discovered_at=timestamp,
        refreshed_at=timestamp,
        source_listing_id="test",
    )


def test_operation_entries_prefer_marked_entries_over_cursor(tmp_path: Path) -> None:
    current = Entry(
        location=parse_location(tmp_path / "current.txt"),
        name="current.txt",
        entry_type=EntryType.FILE,
    )
    selected = Entry(
        location=parse_location(tmp_path / "selected.txt"),
        name="selected.txt",
        entry_type=EntryType.FILE,
    )
    app = BucketCommanderApp(AppConfig.from_paths(left=tmp_path, right=tmp_path))
    app._backend = NoopBackend()  # type: ignore[assignment]
    try:
        app._state = TwoPanelState(
            left=PanelState(
                location=parse_location(tmp_path),
                entries=(current, selected),
                cursor_index=0,
                selected_uris=frozenset({selected.uri}),
            ),
            right=PanelState(location=parse_location(tmp_path)),
        )

        assert app._operation_entries() == (selected,)
    finally:
        app._tasks.close()


def wait_for_task(manager: TaskManager, task_id: str) -> TaskRecord:
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        record = manager.record(task_id)
        assert record is not None
        if record.status.is_terminal:
            return record
        time.sleep(0.01)
    raise AssertionError(f"Task {task_id} did not finish")


def test_operation_entries_ignore_marked_entries_in_other_panel(
    tmp_path: Path,
) -> None:
    current = Entry(
        location=parse_location(tmp_path / "current.txt"),
        name="current.txt",
        entry_type=EntryType.FILE,
    )
    selected = Entry(
        location=parse_location(tmp_path / "selected.txt"),
        name="selected.txt",
        entry_type=EntryType.FILE,
    )
    app = BucketCommanderApp(AppConfig.from_paths(left=tmp_path, right=tmp_path))
    try:
        app._state = TwoPanelState(
            left=PanelState(
                location=parse_location(tmp_path),
                entries=(selected,),
                selected_uris=frozenset({selected.uri}),
            ),
            right=PanelState(
                location=parse_location(tmp_path),
                entries=(current,),
            ),
            focused=PanelId.RIGHT,
        )

        assert app._operation_source_panel() is PanelId.RIGHT
        assert app._operation_entries() == (current,)
    finally:
        app._tasks.close()


class NoopBackend(Backend):
    provider = "noop"

    def __init__(self) -> None:
        self.create_file_calls: list[Location] = []
        self.mkdir_calls: list[Location] = []
        self.delete_calls: list[tuple[Location, bool]] = []
        self.list_entries: tuple[Entry, ...] = ()
        self.list_calls: list[Location] = []

    def supports(self, location: Location) -> bool:
        _ = location
        return True

    async def list(self, location: Location) -> tuple[Entry, ...]:
        self.list_calls.append(location)
        return self.list_entries

    async def stat(self, location: Location) -> Entry:
        return Entry(location=location, name=location.name, entry_type=EntryType.FILE)

    async def preview(self, location: Location, *, max_bytes: int) -> PreviewResult:
        _ = location, max_bytes
        return PreviewResult(b"")

    async def mkdir(self, location: Location, *, parents: bool = True) -> OperationResult:
        _ = parents
        self.mkdir_calls.append(location)
        return OperationResult.success("created", destination=location)

    async def create_file(self, location: Location) -> OperationResult:
        self.create_file_calls.append(location)
        return OperationResult.success("created", destination=location)

    async def copy(
        self,
        source: Location,
        destination: Location,
        *,
        progress: ProgressSink | None = None,
    ) -> OperationResult:
        _ = progress
        return OperationResult.success("copied", source=source, destination=destination)

    async def move(self, source: Location, destination: Location) -> OperationResult:
        return OperationResult.success("moved", source=source, destination=destination)

    async def rename(self, source: Location, new_name: str) -> OperationResult:
        _ = new_name
        return OperationResult.success("renamed", source=source)

    async def delete(
        self,
        location: Location,
        *,
        recursive: bool = False,
        progress: ProgressSink | None = None,
    ) -> OperationResult:
        _ = progress
        self.delete_calls.append((location, recursive))
        return OperationResult.success("deleted", source=location)


class FakeIndexer:
    def __init__(self) -> None:
        self.calls: list[S3Location] = []

    async def index(
        self,
        location: S3Location,
        *,
        progress: ProgressSink | None = None,
        resume: bool = True,
    ) -> RecursiveIndexResult:
        _ = progress, resume
        self.calls.append(location)
        return RecursiveIndexResult(
            root=location,
            prefixes_indexed=1,
            objects_indexed=INDEXED_OBJECTS,
            bytes_indexed=INDEXED_BYTES,
            checkpoint_path=Path("/tmp/checkpoint.json"),
        )


def test_copy_task_ignores_marked_entries_when_other_panel_is_focused(
    tmp_path: Path,
) -> None:
    source_panel_path = tmp_path / "source-panel"
    source_panel_path.mkdir()
    current_path = source_panel_path / "current.txt"
    selected_path = tmp_path / "selected.txt"
    destination_path = tmp_path / "destination"
    current_path.write_text("current", encoding="utf-8")
    selected_path.write_text("selected", encoding="utf-8")
    destination_path.mkdir()
    current = Entry(
        location=parse_location(current_path),
        name="current.txt",
        entry_type=EntryType.FILE,
    )
    selected = Entry(
        location=parse_location(selected_path),
        name="selected.txt",
        entry_type=EntryType.FILE,
    )
    app = BucketCommanderApp(AppConfig.from_paths(left=destination_path, right=source_panel_path))
    try:
        app._state = TwoPanelState(
            left=PanelState(
                location=parse_location(destination_path),
                entries=(selected,),
                selected_uris=frozenset({selected.uri}),
            ),
            right=PanelState(
                location=parse_location(source_panel_path),
                entries=(current,),
            ),
            focused=PanelId.RIGHT,
        )

        app._start_copy_tasks()

        records = app._tasks.records()
        assert len(records) == 1
        assert records[0].task_type is TaskType.COPY
        assert records[0].source == current.location
        assert records[0].destination == parse_location(destination_path)
        wait_for_task(app._tasks, records[0].task_id)
        assert (destination_path / "current.txt").exists()
        assert not (destination_path / "selected.txt").exists()
    finally:
        app._tasks.close()


def test_move_task_ignores_marked_entries_when_other_panel_is_focused(
    tmp_path: Path,
) -> None:
    source_panel_path = tmp_path / "source-panel"
    source_panel_path.mkdir()
    current_path = source_panel_path / "current.txt"
    selected_path = tmp_path / "selected.txt"
    destination_path = tmp_path / "destination"
    current_path.write_text("current", encoding="utf-8")
    selected_path.write_text("selected", encoding="utf-8")
    destination_path.mkdir()
    current = Entry(
        location=parse_location(current_path),
        name="current.txt",
        entry_type=EntryType.FILE,
    )
    selected = Entry(
        location=parse_location(selected_path),
        name="selected.txt",
        entry_type=EntryType.FILE,
    )
    app = BucketCommanderApp(AppConfig.from_paths(left=destination_path, right=source_panel_path))
    try:
        app._state = TwoPanelState(
            left=PanelState(
                location=parse_location(destination_path),
                entries=(selected,),
                selected_uris=frozenset({selected.uri}),
            ),
            right=PanelState(
                location=parse_location(source_panel_path),
                entries=(current,),
            ),
            focused=PanelId.RIGHT,
        )

        app._start_move_tasks()

        assert app._pending_operation_plan is not None
        assert app._pending_operation_plan.direct_count == 1
        assert app._tasks.records() == ()
        app._confirm_operation_plan()

        records = app._tasks.records()
        assert len(records) == 1
        assert records[0].task_type is TaskType.MOVE
        assert records[0].source == current.location
        wait_for_task(app._tasks, records[0].task_id)
        assert (destination_path / "current.txt").exists()
        assert not current_path.exists()
        assert selected_path.exists()
    finally:
        app._tasks.close()


def test_delete_task_ignores_marked_entries_when_other_panel_is_focused(
    tmp_path: Path,
) -> None:
    current_path = tmp_path / "current.txt"
    selected_path = tmp_path / "selected.txt"
    current_path.write_text("current", encoding="utf-8")
    selected_path.write_text("selected", encoding="utf-8")
    current = Entry(
        location=parse_location(current_path),
        name="current.txt",
        entry_type=EntryType.FILE,
    )
    selected = Entry(
        location=parse_location(selected_path),
        name="selected.txt",
        entry_type=EntryType.FILE,
    )
    app = BucketCommanderApp(AppConfig.from_paths(left=tmp_path, right=tmp_path))
    try:
        app._state = TwoPanelState(
            left=PanelState(
                location=parse_location(tmp_path),
                entries=(selected,),
                selected_uris=frozenset({selected.uri}),
            ),
            right=PanelState(
                location=parse_location(tmp_path),
                entries=(current,),
            ),
            focused=PanelId.RIGHT,
        )

        app._start_delete_tasks()

        assert app._pending_operation_plan is not None
        assert app._pending_operation_plan.direct_count == 1
        assert app._tasks.records() == ()
        app._confirm_operation_plan()

        records = app._tasks.records()
        assert len(records) == 1
        assert records[0].task_type is TaskType.DELETE
        assert records[0].source == current.location
        wait_for_task(app._tasks, records[0].task_id)
        assert not current_path.exists()
        assert selected_path.exists()
    finally:
        app._tasks.close()


def test_delete_task_starts_for_s3_entries(tmp_path: Path) -> None:
    entry = Entry(
        location=S3Location(bucket="bucket-commander", prefix="logs/a.txt"),
        name="a.txt",
        entry_type=EntryType.OBJECT,
    )
    app = BucketCommanderApp(AppConfig.from_paths(left=tmp_path, right=tmp_path))
    backend = NoopBackend()
    app._backend = backend  # type: ignore[assignment]
    try:
        app._state = TwoPanelState(
            left=PanelState(
                location=S3Location(bucket="bucket-commander", prefix="logs/"),
                entries=(entry,),
            ),
            right=PanelState(location=parse_location(tmp_path)),
        )

        app._start_delete_tasks()

        assert app._pending_operation_plan is not None
        app._confirm_operation_plan()

        records = app._tasks.records()
        assert len(records) == 1
        assert records[0].task_type is TaskType.DELETE
        assert records[0].source == entry.location
        wait_for_task(app._tasks, records[0].task_id)
        assert backend.delete_calls == [(entry.location, False)]
    finally:
        app._tasks.close()


def test_cancel_delete_plan_does_not_start_task(tmp_path: Path) -> None:
    file_path = tmp_path / "current.txt"
    file_path.write_text("current", encoding="utf-8")
    entry = Entry(
        location=parse_location(file_path),
        name="current.txt",
        entry_type=EntryType.FILE,
    )
    app = BucketCommanderApp(AppConfig.from_paths(left=tmp_path, right=tmp_path))
    try:
        app._state = TwoPanelState(
            left=PanelState(location=parse_location(tmp_path), entries=(entry,)),
            right=PanelState(location=parse_location(tmp_path)),
        )

        app._start_delete_tasks()
        app._cancel_operation_plan()

        assert app._pending_operation_plan is None
        assert app._tasks.records() == ()
        assert file_path.exists()
        assert app._state.status_message == "Cancelled delete"
    finally:
        app._tasks.close()


def test_create_local_file_from_prompt(tmp_path: Path) -> None:
    app = BucketCommanderApp(AppConfig.from_paths(left=tmp_path, right=tmp_path))
    try:
        app._state = TwoPanelState(
            left=PanelState(location=parse_location(tmp_path)),
            right=PanelState(location=parse_location(tmp_path)),
        )

        app._show_create_prompt("file")
        app._create_prompt = ("file", "notes.txt")
        app._apply_create_prompt()

        assert (tmp_path / "notes.txt").is_file()
        assert [entry.name for entry in app._state.left.entries] == ["..", "notes.txt"]
    finally:
        app._tasks.close()


def test_create_local_folder_from_prompt(tmp_path: Path) -> None:
    app = BucketCommanderApp(AppConfig.from_paths(left=tmp_path, right=tmp_path))
    try:
        app._state = TwoPanelState(
            left=PanelState(location=parse_location(tmp_path)),
            right=PanelState(location=parse_location(tmp_path)),
        )

        app._show_create_prompt("folder")
        app._create_prompt = ("folder", "docs")
        app._apply_create_prompt()

        assert (tmp_path / "docs").is_dir()
        assert [entry.name for entry in app._state.left.entries] == ["..", "docs"]
    finally:
        app._tasks.close()


def test_create_s3_file_and_folder_targets_active_prefix(tmp_path: Path) -> None:
    backend = NoopBackend()
    app = BucketCommanderApp(AppConfig.from_paths(left=tmp_path, right=tmp_path))
    app._backend = backend  # type: ignore[assignment]
    app._refresh_panel = lambda _panel_id: None  # type: ignore[assignment]
    try:
        app._state = TwoPanelState(
            left=PanelState(location=S3Location(bucket="bucket-commander", prefix="logs/")),
            right=PanelState(location=parse_location(tmp_path)),
        )

        app._create_prompt = ("file", "a.txt")
        app._apply_create_prompt()
        app._create_prompt = ("folder", "archive")
        app._apply_create_prompt()

        assert backend.create_file_calls == [
            S3Location(bucket="bucket-commander", prefix="logs/a.txt")
        ]
        assert backend.mkdir_calls == [
            S3Location(bucket="bucket-commander", prefix="logs/archive/")
        ]
    finally:
        app._tasks.close()


def test_job_monitor_actions_update_durable_job_state(tmp_path: Path) -> None:
    file_path = tmp_path / "current.txt"
    file_path.write_text("current", encoding="utf-8")
    entry = Entry(
        location=parse_location(file_path),
        name="current.txt",
        entry_type=EntryType.FILE,
    )
    app = BucketCommanderApp(
        AppConfig(
            left=parse_location(tmp_path),
            right=parse_location(tmp_path),
            job_store_path=tmp_path / "jobs.sqlite3",
        )
    )
    try:
        record = app._ensure_job_queue().enqueue(plan_delete((entry,), source_panel="left"))

        app._pause_latest_job()
        paused = app._ensure_job_store().get_job(record.job_id)
        assert paused is not None
        assert paused.status is JobStatus.PAUSED

        app._resume_latest_job()
        resumed = app._ensure_job_store().get_job(record.job_id)
        assert resumed is not None
        assert resumed.status is JobStatus.QUEUED

        app._cancel_latest_job()
        cancelled = app._ensure_job_store().get_job(record.job_id)
        assert cancelled is not None
        assert cancelled.status is JobStatus.CANCELLED
    finally:
        app._tasks.close()


def test_view_current_entry_opens_preview_dialog(tmp_path: Path) -> None:
    file_path = tmp_path / "document.txt"
    file_path.write_text("hello", encoding="utf-8")
    entry = Entry(
        location=parse_location(file_path),
        name="document.txt",
        entry_type=EntryType.FILE,
    )
    app = BucketCommanderApp(AppConfig.from_paths(left=tmp_path, right=tmp_path))
    try:
        app._state = TwoPanelState(
            left=PanelState(location=parse_location(tmp_path), entries=(entry,)),
            right=PanelState(location=parse_location(tmp_path)),
        )

        app._view_current_entry()

        assert app._view_dialog == ("document.txt", "hello")
    finally:
        app._tasks.close()


def test_s3_refresh_writes_live_listing_to_panel_cache(tmp_path: Path) -> None:
    location = S3Location(
        bucket="bucket-commander",
        prefix="logs/",
        profile="dev",
        region="us-east-1",
        endpoint_url="http://localhost:9000",
    )
    live_entry = Entry(
        location=S3Location(
            bucket="bucket-commander",
            prefix="logs/a.txt",
            profile="dev",
            region="us-east-1",
            endpoint_url="http://localhost:9000",
        ),
        name="a.txt",
        entry_type=EntryType.OBJECT,
        size=12,
    )
    app = BucketCommanderApp(
        AppConfig(left=location, right=parse_location(tmp_path), cache_root=tmp_path / "cache")
    )
    backend = NoopBackend()
    backend.list_entries = (live_entry,)
    app._backend = backend  # type: ignore[assignment]
    try:
        app._refresh_panel(PanelId.LEFT)

        assert [entry.name for entry in app._state.left.entries] == ["..", "a.txt"]
        assert "live entries" in app._state.status_message

        backend.list_entries = ()
        app._refresh_panel(PanelId.LEFT)

        assert [entry.name for entry in app._state.left.entries] == [".."]
        assert any((tmp_path / "cache" / "indexes").rglob("*.parquet"))
    finally:
        app._tasks.close()


def test_index_task_starts_for_current_s3_prefix(tmp_path: Path) -> None:
    entry = Entry(
        location=S3Location(bucket="bucket-commander", prefix="logs/archive/"),
        name="archive",
        entry_type=EntryType.PREFIX,
    )
    app = BucketCommanderApp(AppConfig.from_paths(left=tmp_path, right=tmp_path))
    indexer = FakeIndexer()
    app._indexer = indexer  # type: ignore[assignment]
    try:
        app._state = TwoPanelState(
            left=PanelState(
                location=S3Location(bucket="bucket-commander", prefix="logs/"),
                entries=(entry,),
            ),
            right=PanelState(location=parse_location(tmp_path)),
        )

        app._start_index_task()

        records = app._tasks.records()
        assert len(records) == 1
        assert records[0].task_type is TaskType.INDEXING
        assert records[0].source == entry.location
        record = wait_for_task(app._tasks, records[0].task_id)
        assert record.result is not None
        assert record.result.entries_affected == INDEXED_OBJECTS
        assert indexer.calls == [entry.location]
    finally:
        app._tasks.close()


def test_indexed_search_uses_cache_without_remote_listing(tmp_path: Path) -> None:
    cache_root = tmp_path / "cache"
    location = S3Location(bucket="bucket-commander", prefix="logs/", region="us-east-1")
    store = ParquetIndexStore.open(
        index_dir(cache_root, location),
        provider="s3",
        account_id=account_id(location),
        bucket=location.bucket,
        region=location.region,
    )
    store.append_objects(
        (
            indexed_object_row(
                "logs/error.txt",
                parent_prefix="logs/",
                name="error.txt",
                size=12,
                bucket=location.bucket,
            ),
            indexed_object_row(
                "logs/info.txt",
                parent_prefix="logs/",
                name="info.txt",
                size=8,
                bucket=location.bucket,
            ),
        ),
        covered_prefix="logs/",
    )
    app = BucketCommanderApp(
        AppConfig(left=location, right=parse_location(tmp_path), cache_root=cache_root)
    )
    backend = NoopBackend()
    app._backend = backend  # type: ignore[assignment]
    try:
        app._state = TwoPanelState(
            left=PanelState(location=location),
            right=PanelState(location=parse_location(tmp_path)),
        )

        app._apply_indexed_search(PanelId.LEFT, "error")

        assert [entry.name for entry in app._state.left.entries] == ["..", "error.txt"]
        assert "indexed result" in app._state.status_message
        assert backend.list_calls == []
    finally:
        app._tasks.close()


def test_indexed_search_rejects_empty_query(tmp_path: Path) -> None:
    location = S3Location(bucket="bucket-commander", prefix="logs/", region="us-east-1")
    app = BucketCommanderApp(
        AppConfig(left=location, right=parse_location(tmp_path), cache_root=tmp_path / "cache")
    )
    backend = NoopBackend()
    app._backend = backend  # type: ignore[assignment]
    try:
        app._state = TwoPanelState(
            left=PanelState(location=location),
            right=PanelState(location=parse_location(tmp_path)),
        )

        app._apply_indexed_search(PanelId.LEFT, "")

        assert app._state.status_message == "Search query must not be empty"
        assert app._state.left.entries == ()
        assert backend.list_calls == []
    finally:
        app._tasks.close()


def test_parent_entry_leaves_indexed_search_mode(tmp_path: Path) -> None:
    location = S3Location(bucket="bucket-commander", prefix="logs/", region="us-east-1")
    parent = Entry(
        location=S3Location(bucket="bucket-commander"),
        name="..",
        entry_type=EntryType.PREFIX,
    )
    search_result = Entry(
        location=S3Location(bucket="bucket-commander", prefix="logs/error.txt"),
        name="error.txt",
        entry_type=EntryType.OBJECT,
    )
    live_entry = Entry(
        location=S3Location(bucket="bucket-commander", prefix="logs/live.txt"),
        name="live.txt",
        entry_type=EntryType.OBJECT,
    )
    app = BucketCommanderApp(
        AppConfig(left=location, right=parse_location(tmp_path), cache_root=tmp_path / "cache")
    )
    backend = NoopBackend()
    backend.list_entries = (live_entry,)
    app._backend = backend  # type: ignore[assignment]
    try:
        app._state = TwoPanelState(
            left=PanelState(
                location=location,
                entries=(parent, search_result),
                filter_text="error",
            ),
            right=PanelState(location=parse_location(tmp_path)),
        )

        app._enter_active()

        assert app._state.left.location == location
        assert app._state.left.filter_text == ""
        assert [entry.name for entry in app._state.left.entries] == ["..", "live.txt"]
        assert backend.list_calls == [location]
    finally:
        app._tasks.close()


def test_backslash_leaves_indexed_search_mode(tmp_path: Path) -> None:
    location = S3Location(bucket="bucket-commander", prefix="logs/", region="us-east-1")
    live_entry = Entry(
        location=S3Location(bucket="bucket-commander", prefix="logs/live.txt"),
        name="live.txt",
        entry_type=EntryType.OBJECT,
    )
    app = BucketCommanderApp(
        AppConfig(left=location, right=parse_location(tmp_path), cache_root=tmp_path / "cache")
    )
    backend = NoopBackend()
    backend.list_entries = (live_entry,)
    app._backend = backend  # type: ignore[assignment]
    try:
        app._state = TwoPanelState(
            left=PanelState(
                location=location,
                entries=(live_entry,),
                filter_text="live",
            ),
            right=PanelState(location=parse_location(tmp_path)),
        )

        app._handle_key("\\")

        assert app._state.left.location == location
        assert app._state.left.filter_text == ""
        assert [entry.name for entry in app._state.left.entries] == ["..", "live.txt"]
        assert backend.list_calls == [location]
    finally:
        app._tasks.close()
