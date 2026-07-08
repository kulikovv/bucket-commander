from pathlib import Path

import urwid

from bc.app import AppConfig, BucketCommanderApp
from bc.config import KnownSource, SourcesConfig
from bc.core import Entry, EntryType, PanelState, S3Location, parse_location
from bc.ui.commands import PanelId, TwoPanelState
from bc.ui.panels import (
    PanelListBox,
    render_app,
    render_help_overlay,
    render_location_picker_overlay,
    render_search_overlay,
    render_view_overlay,
)


def test_render_app_includes_help_button(tmp_path: Path) -> None:
    state = TwoPanelState(
        left=PanelState(location=parse_location(tmp_path)),
        right=PanelState(location=parse_location(tmp_path)),
    )

    rendered = render_text(render_app(state))

    assert "< Help" in rendered
    assert "< View" in rendered
    assert "< Search" in rendered
    assert "< Sort" in rendered
    assert "< Copy" in rendered
    assert "< Move" in rendered
    assert "< Delete" in rendered


def test_render_app_includes_location_panel(tmp_path: Path) -> None:
    source = KnownSource(
        name="Local MinIO",
        location=S3Location(bucket="bucket-commander", prefix="logs/"),
    )
    state = TwoPanelState(
        left=PanelState(location=parse_location(tmp_path)),
        right=PanelState(location=parse_location(tmp_path)),
    )

    rendered = render_text(render_app(state, sources=(source,)))

    assert "Locations" in rendered
    assert "< Left" in rendered
    assert "< Right" in rendered
    assert "1 known location" in rendered


def test_app_adds_local_file_system_source(tmp_path: Path) -> None:
    source = KnownSource(
        name="Local MinIO",
        location=S3Location(bucket="bucket-commander", prefix="logs/"),
    )
    app = BucketCommanderApp(
        AppConfig(
            left=parse_location(tmp_path),
            right=parse_location(tmp_path),
            sources=SourcesConfig((source,)),
        )
    )
    try:
        assert [source.name for source in app._sources][:2] == [
            "Local file system",
            "Local MinIO",
        ]
    finally:
        app._tasks.close()


def test_panel_mouse_focuses_item_and_panel(tmp_path: Path) -> None:
    first = Entry(location=parse_location(tmp_path / "a"), name="a", entry_type=EntryType.FILE)
    second = Entry(location=parse_location(tmp_path / "b"), name="b", entry_type=EntryType.FILE)
    app = BucketCommanderApp(AppConfig.from_paths(left=tmp_path, right=tmp_path))
    try:
        app._state = TwoPanelState(
            left=PanelState(location=parse_location(tmp_path), entries=(first, second)),
            right=PanelState(location=parse_location(tmp_path), entries=(first, second)),
        )

        app._handle_panel_mouse(PanelId.RIGHT, 1)

        assert app._state.focused is PanelId.RIGHT
        assert app._state.right.cursor_index == 1
    finally:
        app._tasks.close()


def test_panel_mouse_can_toggle_item_selection(tmp_path: Path) -> None:
    first = Entry(location=parse_location(tmp_path / "a"), name="a", entry_type=EntryType.FILE)
    second = Entry(location=parse_location(tmp_path / "b"), name="b", entry_type=EntryType.FILE)
    app = BucketCommanderApp(AppConfig.from_paths(left=tmp_path, right=tmp_path))
    try:
        app._state = TwoPanelState(
            left=PanelState(location=parse_location(tmp_path), entries=(first, second)),
            right=PanelState(location=parse_location(tmp_path), entries=(first, second)),
        )

        app._handle_panel_mouse(PanelId.RIGHT, 1, toggle_mark=True)

        assert app._state.focused is PanelId.RIGHT
        assert app._state.right.selected_entries == (second,)
    finally:
        app._tasks.close()


def test_panel_mouse_double_click_opens_directory(tmp_path: Path) -> None:
    directory = tmp_path / "folder"
    directory.mkdir()
    (directory / "inside.txt").write_text("content", encoding="utf-8")
    entry = Entry(
        location=parse_location(directory),
        name="folder",
        entry_type=EntryType.DIRECTORY,
    )
    app = BucketCommanderApp(AppConfig.from_paths(left=tmp_path, right=tmp_path))
    try:
        app._state = TwoPanelState(
            left=PanelState(location=parse_location(tmp_path), entries=(entry,)),
            right=PanelState(location=parse_location(tmp_path)),
        )

        app._handle_panel_mouse(PanelId.LEFT, 0)
        app._handle_panel_mouse(PanelId.LEFT, 0)

        assert app._state.left.location == parse_location(directory)
        assert [entry.name for entry in app._state.left.entries] == ["..", "inside.txt"]
    finally:
        app._tasks.close()


def test_panel_mouse_double_click_file_does_not_open_directory(tmp_path: Path) -> None:
    file_path = tmp_path / "file.txt"
    file_path.write_text("content", encoding="utf-8")
    entry = Entry(
        location=parse_location(file_path),
        name="file.txt",
        entry_type=EntryType.FILE,
    )
    app = BucketCommanderApp(AppConfig.from_paths(left=tmp_path, right=tmp_path))
    try:
        app._state = TwoPanelState(
            left=PanelState(location=parse_location(tmp_path), entries=(entry,)),
            right=PanelState(location=parse_location(tmp_path)),
        )

        app._handle_panel_mouse(PanelId.LEFT, 0)
        app._handle_panel_mouse(PanelId.LEFT, 0)

        assert app._state.left.location == parse_location(tmp_path)
        assert app._state.left.current_entry == entry
    finally:
        app._tasks.close()


def test_panel_mouse_uses_visible_position_when_list_is_scrolled() -> None:
    clicked: list[tuple[PanelId, int | None, bool, int | None]] = []
    rows: list[urwid.Widget] = [urwid.Text(f"item-{index}") for index in range(30)]
    walker = urwid.SimpleFocusListWalker(rows)
    walker.set_focus(20)
    list_box = PanelListBox(
        walker,
        panel_id=PanelId.LEFT,
        row_count=len(rows),
        on_mouse=lambda panel_id, index, *, toggle_mark=False, viewport_row=None: clicked.append(
            (panel_id, index, toggle_mark, viewport_row)
        ),
    )

    list_box.render((80, 10), focus=True)
    assert list_box.mouse_event((80, 10), "mouse press", 1, 0, 0, True)

    assert clicked == [(PanelId.LEFT, 20, False, 0)]


def test_panel_focus_row_keeps_selected_item_in_place_after_redraw() -> None:
    rows: list[urwid.Widget] = [urwid.Text(f"item-{index}") for index in range(30)]
    walker = urwid.SimpleFocusListWalker(rows)
    walker.set_focus(20)
    list_box = PanelListBox(
        walker,
        panel_id=PanelId.LEFT,
        row_count=len(rows),
        focus_row=5,
    )

    rendered = list_box.render((80, 10), focus=True)
    lines = [line.decode("utf-8", errors="replace").strip() for line in rendered.text]

    assert lines[5] == "item-20"


def test_render_location_picker_overlay_lists_sources(tmp_path: Path) -> None:
    source = KnownSource(
        name="Local MinIO",
        location=S3Location(bucket="bucket-commander", prefix="logs/"),
    )
    state = TwoPanelState(
        left=PanelState(location=parse_location(tmp_path)),
        right=PanelState(location=parse_location(tmp_path)),
    )

    rendered = render_text(
        render_location_picker_overlay(render_app(state), (source,), PanelId.LEFT)
    )

    assert "Left Locations" in rendered
    assert "Local MinIO" in rendered
    assert "s3://bucket-commander/logs/" in rendered


def test_render_help_overlay_lists_commands(tmp_path: Path) -> None:
    state = TwoPanelState(
        left=PanelState(location=parse_location(tmp_path)),
        right=PanelState(location=parse_location(tmp_path)),
    )

    rendered = render_text(render_help_overlay(render_app(state)))

    assert "Commands" in rendered
    assert "F1 or ?" in rendered
    assert "Refresh active panel" in rendered
    assert "Search indexed bucket metadata" in rendered
    assert "< Close" in rendered


def test_render_search_overlay_shows_query(tmp_path: Path) -> None:
    state = TwoPanelState(
        left=PanelState(location=parse_location(tmp_path)),
        right=PanelState(location=parse_location(tmp_path)),
    )

    rendered = render_text(render_search_overlay(render_app(state), query="error"))

    assert "Indexed Search" in rendered
    assert "Query: error" in rendered
    assert "< Search" in rendered
    assert "< Close" in rendered


def test_render_view_overlay_shows_content(tmp_path: Path) -> None:
    state = TwoPanelState(
        left=PanelState(location=parse_location(tmp_path)),
        right=PanelState(location=parse_location(tmp_path)),
    )

    rendered = render_text(
        render_view_overlay(render_app(state), title="document.txt", content="hello\nworld")
    )

    assert "View: document.txt" in rendered
    assert "hello" in rendered
    assert "world" in rendered
    assert "< Close" in rendered


def test_help_keys_toggle_modal_without_running_loop(tmp_path: Path) -> None:
    app = BucketCommanderApp(AppConfig.from_paths(left=tmp_path, right=tmp_path))

    app._handle_key("?")
    assert app._is_help_open

    app._handle_key("esc")
    assert not app._is_help_open


def test_view_key_closes_modal_without_running_loop(tmp_path: Path) -> None:
    app = BucketCommanderApp(AppConfig.from_paths(left=tmp_path, right=tmp_path))
    app._view_dialog = ("document.txt", "content")

    app._handle_key("esc")

    assert app._view_dialog is None


def test_select_location_source_updates_target_panel(tmp_path: Path) -> None:
    source = KnownSource(
        name="Local MinIO",
        location=S3Location(
            bucket="bucket-commander",
            prefix="logs/",
            endpoint_url="http://127.0.0.1:9000",
        ),
    )
    app = BucketCommanderApp(AppConfig.from_paths(left=tmp_path, right=tmp_path))
    app._sources = (source,)
    app._location_picker_panel = PanelId.RIGHT
    app._refresh_panel = lambda _panel_id: None  # type: ignore[assignment]
    try:
        app._select_location_source(source)

        assert app._state.right.location == source.location
    finally:
        app._tasks.close()


def render_text(widget: urwid.Widget) -> str:
    canvas = widget.render((100, 30), focus=True)
    return "\n".join(line.decode("utf-8", errors="replace") for line in canvas.text)
