from pathlib import Path

import urwid

from bc.app import AppConfig, BucketCommanderApp
from bc.config import (
    AppSettings,
    IndexSettings,
    KnownSource,
    OperationSettings,
    ProviderProfiles,
    ProviderSettings,
    S3ProviderProfile,
    SourcesConfig,
    UISettings,
)
from bc.core import Entry, EntryType, PanelState, S3Location, parse_location
from bc.jobs import JobItemStatus, SQLiteJobStore, plan_delete, plan_move
from bc.ui.commands import PanelId, TwoPanelState
from bc.ui.panels import (
    PanelListBox,
    UiCommand,
    render_app,
    render_bucket_menu_overlay,
    render_help_overlay,
    render_jobs_overlay,
    render_location_picker_overlay,
    render_message_overlay,
    render_name_prompt_overlay,
    render_operation_plan_overlay,
    render_search_overlay,
    render_settings_overlay,
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
    assert "< Sort" in rendered
    assert "< Copy" in rendered
    assert "< Move" in rendered
    assert "< Delete" in rendered
    assert "< Search" not in rendered
    assert "< Index" not in rendered
    assert "< Cancel" not in rendered


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
    assert "< Bucket" in rendered
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


def test_render_message_overlay_shows_title_and_content(tmp_path: Path) -> None:
    state = TwoPanelState(
        left=PanelState(location=parse_location(tmp_path)),
        right=PanelState(location=parse_location(tmp_path)),
    )

    rendered = render_text(
        render_message_overlay(
            render_app(state),
            title="Startup Warning",
            content="Bucket discovery failed: access denied",
        )
    )

    assert "Startup Warning" in rendered
    assert "Bucket discovery failed: access denied" in rendered
    assert "< Close" in rendered


def test_render_name_prompt_overlay_shows_name(tmp_path: Path) -> None:
    state = TwoPanelState(
        left=PanelState(location=parse_location(tmp_path)),
        right=PanelState(location=parse_location(tmp_path)),
    )

    rendered = render_text(
        render_name_prompt_overlay(render_app(state), title="New File", name="notes.txt")
    )

    assert "New File" in rendered
    assert "Name: notes.txt" in rendered
    assert "< Create" in rendered
    assert "< Close" in rendered


def test_render_bucket_menu_overlay_lists_bucket_commands(tmp_path: Path) -> None:
    state = TwoPanelState(
        left=PanelState(location=parse_location(tmp_path)),
        right=PanelState(location=parse_location(tmp_path)),
    )

    rendered = render_text(render_bucket_menu_overlay(render_app(state)))

    assert "Bucket" in rendered
    assert "< Search" in rendered
    assert "< Index" in rendered
    assert "< Cancel" in rendered
    assert "< Close" in rendered


def test_render_settings_overlay_shows_resolved_settings(tmp_path: Path) -> None:
    cache_root = Path("/tmp/bc-cache")
    settings = AppSettings(
        cache_root=cache_root,
        profiles=ProviderProfiles(
            default_s3=S3ProviderProfile(
                profile_name="dev",
                region_name="us-east-1",
                endpoint_url="http://127.0.0.1:9000/",
            ),
            s3_profiles=(S3ProviderProfile(name="archive", profile_name="archive-profile"),),
        ),
        provider=ProviderSettings(default_provider="s3"),
        index=IndexSettings(ttl_seconds=600),
        operations=OperationSettings(
            max_listing_concurrency=2,
            max_transfer_concurrency=3,
            multipart_upload_threshold=16 * 1024 * 1024,
            multipart_download_threshold=32 * 1024 * 1024,
            default_conflict_behavior="fail if exists",
            confirm_destructive=False,
        ),
        ui=UISettings(
            theme="high-contrast",
            keymap="commander",
            show_hidden_files=True,
            show_advanced_metadata=True,
        ),
    )
    state = TwoPanelState(
        left=PanelState(location=parse_location(tmp_path)),
        right=PanelState(location=parse_location(tmp_path)),
    )

    rendered = render_text(render_settings_overlay(render_app(state), settings), size=(120, 60))

    assert "Settings" in rendered
    assert f"Cache root: {cache_root}" in rendered
    assert "Default provider: s3" in rendered
    assert "Index TTL: 600s" in rendered
    assert "profile: dev" in rendered
    assert "endpoint: http://127.0.0.1:9000/" in rendered
    assert "Named S3 profiles: archive" in rendered
    assert "multipart upload threshold: 16.0M" in rendered
    assert "confirm destructive: no" in rendered
    assert "show hidden files: yes" in rendered
    assert "< Close" in rendered


def test_render_operation_plan_overlay_shows_move_scope(tmp_path: Path) -> None:
    entry = Entry(
        location=parse_location(tmp_path / "document.txt"),
        name="document.txt",
        entry_type=EntryType.FILE,
        size=42,
    )
    state = TwoPanelState(
        left=PanelState(location=parse_location(tmp_path), entries=(entry,)),
        right=PanelState(location=parse_location(tmp_path / "target")),
    )
    plan = plan_move((entry,), source_panel="left", destination=state.right.location)

    rendered = render_text(render_operation_plan_overlay(render_app(state), plan))

    assert "Confirm Move" in rendered
    assert "document.txt" in rendered
    assert "Expanded entries: 1" in rendered
    assert "Estimated bytes: 42B" in rendered
    assert "delete source after verify" in rendered
    assert "< Confirm" in rendered
    assert "< Cancel" in rendered


def test_render_app_shows_durable_job_footer(tmp_path: Path) -> None:
    entry = Entry(
        location=parse_location(tmp_path / "document.txt"),
        name="document.txt",
        entry_type=EntryType.FILE,
    )
    store = SQLiteJobStore(tmp_path / "jobs.sqlite3")
    record = store.add_plan(plan_delete((entry,), source_panel="left"))
    state = TwoPanelState(
        left=PanelState(location=parse_location(tmp_path)),
        right=PanelState(location=parse_location(tmp_path)),
    )

    rendered = render_text(render_app(state, jobs=(record,)))

    assert "Jobs:" in rendered
    assert "delete queued" in rendered


def test_render_jobs_overlay_shows_details_and_controls(tmp_path: Path) -> None:
    source = tmp_path / "document.txt"
    source.write_text("content", encoding="utf-8")
    entry = Entry(
        location=parse_location(source),
        name="document.txt",
        entry_type=EntryType.FILE,
        size=source.stat().st_size,
    )
    store = SQLiteJobStore(tmp_path / "jobs.sqlite3")
    record = store.add_plan(plan_delete((entry,), source_panel="left"))
    item = store.list_items(record.job_id)[0]
    failed_item = item.__class__(
        item_id=item.item_id,
        job_id=item.job_id,
        item_index=item.item_index,
        name=item.name,
        location=item.location,
        entry_type=item.entry_type,
        size=item.size,
        status=JobItemStatus.FAILED,
        attempts=2,
        latest_error="permission denied",
    )
    state = TwoPanelState(
        left=PanelState(location=parse_location(tmp_path)),
        right=PanelState(location=parse_location(tmp_path)),
    )

    rendered = render_text(render_jobs_overlay(render_app(state), (record,), (failed_item,)))

    assert "Jobs" in rendered
    assert record.job_id in rendered
    assert "document.txt" in rendered
    assert "permission denied" in rendered
    assert "< Pause" in rendered
    assert "< Resume" in rendered
    assert "< Cancel" in rendered
    assert "< Retry" in rendered


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


def test_bucket_menu_command_opens_search_dialog(tmp_path: Path) -> None:
    app = BucketCommanderApp(AppConfig.from_paths(left=tmp_path, right=tmp_path))
    try:
        app._show_bucket_menu()
        assert app._is_bucket_menu_open

        app._handle_ui_command(UiCommand.SEARCH)

        assert not app._is_bucket_menu_open
        assert app._search_panel is PanelId.LEFT
    finally:
        app._tasks.close()


def test_jobs_command_opens_monitor(tmp_path: Path) -> None:
    app = BucketCommanderApp(AppConfig.from_paths(left=tmp_path, right=tmp_path))
    try:
        app._handle_ui_command(UiCommand.JOBS)

        assert app._is_jobs_open
    finally:
        app._tasks.close()


def test_settings_key_toggles_modal_without_running_loop(tmp_path: Path) -> None:
    app = BucketCommanderApp(AppConfig.from_paths(left=tmp_path, right=tmp_path))
    try:
        app._handle_key("g")
        assert app._is_settings_open

        app._handle_key("esc")
        assert not app._is_settings_open
    finally:
        app._tasks.close()


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


def render_text(widget: urwid.Widget, *, size: tuple[int, int] = (100, 30)) -> str:
    canvas = widget.render(size, focus=True)
    return "\n".join(line.decode("utf-8", errors="replace") for line in canvas.text)
