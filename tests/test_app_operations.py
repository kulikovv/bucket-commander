import time
from pathlib import Path

from bc.app import AppConfig, BucketCommanderApp
from bc.core import Entry, EntryType, PanelState, TaskManager, TaskRecord, TaskType, parse_location
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
