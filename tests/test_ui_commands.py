import asyncio
from collections.abc import Coroutine
from pathlib import Path
from typing import Any, TypeVar

import pytest

from bc.backends import LocalBackend
from bc.core import Entry, EntryType, PanelState, parse_location
from bc.ui.commands import (
    PanelId,
    TwoPanelState,
    enter,
    go_parent,
    move_cursor,
    refresh,
    switch_focus,
    toggle_selection,
)

T = TypeVar("T")


@pytest.mark.parametrize("panel_id", [PanelId.LEFT, PanelId.RIGHT])
def test_refresh_loads_entries_for_panel(tmp_path: Path, panel_id: PanelId) -> None:
    (tmp_path / "alpha").mkdir()
    (tmp_path / "bravo.txt").write_text("content", encoding="utf-8")
    state = TwoPanelState(
        left=PanelState(location=parse_location(tmp_path)),
        right=PanelState(location=parse_location(tmp_path)),
    )

    refreshed = run_async(refresh(state, panel_id, LocalBackend()))

    panel = refreshed.panel(panel_id)
    assert [entry.name for entry in panel.entries] == ["..", "alpha", "bravo.txt"]
    assert not panel.is_loading
    assert "3 entries" in refreshed.status_message


def test_switch_focus_toggles_active_panel(tmp_path: Path) -> None:
    state = TwoPanelState(
        left=PanelState(location=parse_location(tmp_path)),
        right=PanelState(location=parse_location(tmp_path)),
    )

    assert switch_focus(state).focused is PanelId.RIGHT
    assert switch_focus(switch_focus(state)).focused is PanelId.LEFT


def test_move_cursor_updates_focused_panel_only(tmp_path: Path) -> None:
    first = Entry(location=parse_location(tmp_path / "a"), name="a", entry_type=EntryType.FILE)
    second = Entry(location=parse_location(tmp_path / "b"), name="b", entry_type=EntryType.FILE)
    state = TwoPanelState(
        left=PanelState(location=parse_location(tmp_path), entries=(first, second)),
        right=PanelState(location=parse_location(tmp_path), entries=(first, second)),
    )

    moved = move_cursor(state, 1)

    assert moved.left.cursor_index == 1
    assert moved.right.cursor_index == 0


def test_toggle_selection_marks_focused_panel_current_entry(tmp_path: Path) -> None:
    first = Entry(location=parse_location(tmp_path / "a"), name="a", entry_type=EntryType.FILE)
    second = Entry(location=parse_location(tmp_path / "b"), name="b", entry_type=EntryType.FILE)
    state = TwoPanelState(
        left=PanelState(location=parse_location(tmp_path), entries=(first, second), cursor_index=1),
        right=PanelState(location=parse_location(tmp_path), entries=(first, second)),
    )

    selected = toggle_selection(state)
    unselected = toggle_selection(selected)

    assert selected.left.selected_entries == (second,)
    assert selected.right.selected_entries == ()
    assert selected.status_message == "Selected b"
    assert unselected.left.selected_entries == ()


def test_enter_directory_refreshes_active_panel(tmp_path: Path) -> None:
    directory = tmp_path / "directory"
    directory.mkdir()
    (directory / "inside.txt").write_text("content", encoding="utf-8")
    entry = Entry(
        location=parse_location(directory),
        name="directory",
        entry_type=EntryType.DIRECTORY,
    )
    state = TwoPanelState(
        left=PanelState(location=parse_location(tmp_path), entries=(entry,)),
        right=PanelState(location=parse_location(tmp_path)),
    )

    entered = run_async(enter(state, LocalBackend()))

    assert entered.left.location == parse_location(directory)
    assert [entry.name for entry in entered.left.entries] == ["..", "inside.txt"]


def test_enter_parent_entry_navigates_up(tmp_path: Path) -> None:
    child = tmp_path / "child"
    child.mkdir()
    parent_entry = Entry(
        location=parse_location(tmp_path),
        name="..",
        entry_type=EntryType.DIRECTORY,
    )
    state = TwoPanelState(
        left=PanelState(location=parse_location(child), entries=(parent_entry,)),
        right=PanelState(location=parse_location(child)),
    )

    entered = run_async(enter(state, LocalBackend()))

    assert entered.left.location == parse_location(tmp_path)
    assert "child" in [entry.name for entry in entered.left.entries]


def test_enter_file_reports_status_without_navigation(tmp_path: Path) -> None:
    file_path = tmp_path / "file.txt"
    file_path.write_text("content", encoding="utf-8")
    entry = Entry(location=parse_location(file_path), name="file.txt", entry_type=EntryType.FILE)
    state = TwoPanelState(
        left=PanelState(location=parse_location(tmp_path), entries=(entry,)),
        right=PanelState(location=parse_location(tmp_path)),
    )

    entered = run_async(enter(state, LocalBackend()))

    assert entered.left.location == parse_location(tmp_path)
    assert "not a directory" in entered.status_message


def test_go_parent_refreshes_active_panel(tmp_path: Path) -> None:
    child = tmp_path / "child"
    child.mkdir()

    state = TwoPanelState(
        left=PanelState(location=parse_location(child)),
        right=PanelState(location=parse_location(child)),
    )

    parent = run_async(go_parent(state, LocalBackend()))

    assert parent.left.location == parse_location(tmp_path)
    assert "child" in [entry.name for entry in parent.left.entries]


def run_async(coro: Coroutine[Any, Any, T]) -> T:
    return asyncio.run(coro)
