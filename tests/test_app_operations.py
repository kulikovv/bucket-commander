import time
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
from bc.ui.commands import PanelId, TwoPanelState


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
        self.delete_calls: list[tuple[Location, bool]] = []
        self.list_entries: tuple[Entry, ...] = ()

    def supports(self, location: Location) -> bool:
        _ = location
        return True

    async def list(self, location: Location) -> tuple[Entry, ...]:
        _ = location
        return self.list_entries

    async def stat(self, location: Location) -> Entry:
        return Entry(location=location, name=location.name, entry_type=EntryType.FILE)

    async def preview(self, location: Location, *, max_bytes: int) -> PreviewResult:
        _ = location, max_bytes
        return PreviewResult(b"")

    async def mkdir(self, location: Location, *, parents: bool = True) -> OperationResult:
        _ = parents
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

        records = app._tasks.records()
        assert len(records) == 1
        assert records[0].task_type is TaskType.DELETE
        assert records[0].source == entry.location
        wait_for_task(app._tasks, records[0].task_id)
        assert backend.delete_calls == [(entry.location, False)]
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
