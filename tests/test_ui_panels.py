from pathlib import Path

import urwid

from bc.app import AppConfig, BucketCommanderApp
from bc.config import KnownSource
from bc.core import PanelState, S3Location, parse_location
from bc.ui.commands import PanelId, TwoPanelState
from bc.ui.panels import render_app, render_help_overlay, render_location_picker_overlay


def test_render_app_includes_help_button(tmp_path: Path) -> None:
    state = TwoPanelState(
        left=PanelState(location=parse_location(tmp_path)),
        right=PanelState(location=parse_location(tmp_path)),
    )

    rendered = render_text(render_app(state))

    assert "< Help" in rendered
    assert "< View" in rendered
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
    assert "< Close" in rendered


def test_help_keys_toggle_modal_without_running_loop(tmp_path: Path) -> None:
    app = BucketCommanderApp(AppConfig.from_paths(left=tmp_path, right=tmp_path))

    app._handle_key("?")
    assert app._is_help_open

    app._handle_key("esc")
    assert not app._is_help_open


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
