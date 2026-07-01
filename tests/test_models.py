from datetime import UTC, datetime
from pathlib import Path

import pytest

from bc.core.locations import parse_location
from bc.core.models import Entry, EntryType, OperationResult, PanelState


def test_entry_exposes_location_uri_and_container_state(tmp_path: Path) -> None:
    location = parse_location(tmp_path / "alpha")
    entry = Entry(
        location=location,
        name="alpha",
        entry_type=EntryType.DIRECTORY,
        modified_at=datetime(2026, 1, 1, tzinfo=UTC),
        metadata={"owner": "local"},
    )

    assert entry.uri == location.uri
    assert entry.is_container
    assert entry.metadata["owner"] == "local"

    with pytest.raises(TypeError):
        entry.metadata["owner"] = "other"  # type: ignore[index]


def test_entry_rejects_empty_name_and_negative_size(tmp_path: Path) -> None:
    location = parse_location(tmp_path / "alpha")

    with pytest.raises(ValueError, match="name"):
        Entry(location=location, name="", entry_type=EntryType.FILE)

    with pytest.raises(ValueError, match="size"):
        Entry(location=location, name="alpha", entry_type=EntryType.FILE, size=-1)


def test_panel_state_clamps_cursor_and_discards_stale_selections(tmp_path: Path) -> None:
    first = Entry(
        location=parse_location(tmp_path / "first"),
        name="first",
        entry_type=EntryType.FILE,
    )
    second = Entry(
        location=parse_location(tmp_path / "second"),
        name="second",
        entry_type=EntryType.FILE,
    )

    panel = PanelState(
        location=parse_location(tmp_path),
        entries=(first, second),
        cursor_index=10,
        selected_uris=frozenset({first.uri, "file:///missing"}),
    )

    assert panel.cursor_index == 1
    assert panel.selected_uris == frozenset({first.uri})
    assert panel.current_entry == second


def test_panel_state_toggles_selection_for_current_entry(tmp_path: Path) -> None:
    entry = Entry(
        location=parse_location(tmp_path / "first"),
        name="first",
        entry_type=EntryType.FILE,
    )
    panel = PanelState(location=parse_location(tmp_path), entries=(entry,))

    selected = panel.toggle_selection()
    unselected = selected.toggle_selection()

    assert selected.selected_entries == (entry,)
    assert unselected.selected_entries == ()


def test_operation_result_validates_counts(tmp_path: Path) -> None:
    source = parse_location(tmp_path / "source")
    destination = parse_location(tmp_path / "destination")

    result = OperationResult.success(
        "copied",
        source=source,
        destination=destination,
        entries_affected=1,
        bytes_affected=42,
    )

    assert result.ok
    assert result.source == source
    assert result.destination == destination

    with pytest.raises(ValueError, match="bytes_affected"):
        OperationResult.failure("failed", bytes_affected=-1)
