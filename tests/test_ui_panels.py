from pathlib import Path

import urwid

from bc.app import AppConfig, BucketCommanderApp
from bc.core import PanelState, parse_location
from bc.ui.commands import TwoPanelState
from bc.ui.panels import render_app, render_help_overlay


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
    app = BucketCommanderApp(AppConfig(left=tmp_path, right=tmp_path))

    app._handle_key("?")
    assert app._is_help_open

    app._handle_key("esc")
    assert not app._is_help_open


def render_text(widget: urwid.Widget) -> str:
    canvas = widget.render((100, 30), focus=True)
    return "\n".join(line.decode("utf-8", errors="replace") for line in canvas.text)
