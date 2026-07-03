"""Application entry point for the interactive Bucket Commander UI."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import urwid

from bc.backends import BackendError, LocalBackend
from bc.core import LocalLocation, PanelState
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
from bc.ui.panels import render_app, render_help_overlay

PENDING_COMMAND_KEYS = {
    "f3": "View",
    "f4": "New file",
    "f5": "Copy",
    "f6": "Move",
    "f7": "New folder",
}


@dataclass(frozen=True, slots=True)
class AppConfig:
    """Startup configuration for the terminal application."""

    left: Path
    right: Path


class BucketCommanderApp:
    """Minimal two-panel local file manager."""

    def __init__(self, config: AppConfig) -> None:
        self._backend = LocalBackend()
        self._state = TwoPanelState(
            left=PanelState(location=LocalLocation(config.left.resolve())),
            right=PanelState(location=LocalLocation(config.right.resolve())),
        )
        self._loop: urwid.MainLoop | None = None
        self._is_help_open = False

    def run(self) -> int:
        self._refresh_panel(PanelId.LEFT)
        self._refresh_panel(PanelId.RIGHT)
        self._loop = urwid.MainLoop(
            self._render(),
            palette=_palette(),
            unhandled_input=self._handle_key,
        )
        self._loop.run()
        return 0

    def _handle_key(self, key: str | tuple[str, int, int, int]) -> None:
        if not isinstance(key, str):
            return
        if self._handle_help_key(key):
            return
        if key in {"q", "Q", "esc"}:
            raise urwid.ExitMainLoop()
        if key in {"f1", "?"}:
            self._show_help_dialog()
            return
        if key == "tab":
            self._update(switch_focus(self._state))
        elif key == "up":
            self._update(move_cursor(self._state, -1))
        elif key == "down":
            self._update(move_cursor(self._state, 1))
        elif key in {"enter", " ", "right"}:
            self._run_command(lambda: enter(self._state, self._backend))
        elif key in {"s", "S"}:
            self._update(toggle_selection(self._state))
        elif key in {"backspace", "left"}:
            self._run_command(lambda: go_parent(self._state, self._backend))
        elif key in {"r", "R", "ctrl r"}:
            self._refresh_panel(self._state.focused)
        elif key in PENDING_COMMAND_KEYS:
            self._show_pending_command(PENDING_COMMAND_KEYS[key])

    def _handle_help_key(self, key: str) -> bool:
        if not self._is_help_open:
            return False
        if key in {"q", "Q", "esc", "enter", "f1", "?"}:
            self._close_help_dialog()
        return True

    def _refresh_panel(self, panel_id: PanelId) -> None:
        self._run_command(lambda: refresh(self._state, panel_id, self._backend))

    def _show_pending_command(self, command_name: str) -> None:
        self._update(self._state.with_status(f"{command_name} is not implemented yet"))

    def _show_help_dialog(self, _button: urwid.Button | None = None) -> None:
        self._is_help_open = True
        self._redraw()

    def _close_help_dialog(self, _button: urwid.Button | None = None) -> None:
        self._is_help_open = False
        self._redraw()

    def _run_command(self, command: Callable[[], Coroutine[Any, Any, TwoPanelState]]) -> None:
        try:
            state: TwoPanelState = asyncio.run(command())
        except BackendError as error:
            self._update(self._state.with_status(str(error)))
            return
        self._update(state)

    def _update(self, state: TwoPanelState) -> None:
        self._state = state
        self._redraw()

    def _redraw(self) -> None:
        if self._loop is not None:
            self._loop.widget = self._render()
            self._loop.draw_screen()

    def _render(self) -> urwid.Widget:
        app = render_app(self._state, on_help=self._show_help_dialog)
        if self._is_help_open:
            return render_help_overlay(app, on_close=self._close_help_dialog)
        return app


def run_app(config: AppConfig) -> int:
    """Run the interactive application."""

    return BucketCommanderApp(config).run()


def _palette() -> list[tuple[str, str, str]]:
    return [
        ("panel_header", "black", "light gray"),
        ("panel_border", "dark gray", "black"),
        ("panel_border_focus", "yellow", "black"),
        ("panel_body", "light gray", "black"),
        ("entry_current", "black", "light cyan"),
        ("footer_commands", "black", "light cyan"),
        ("footer", "black", "light gray"),
        ("error", "light red", "black"),
        ("dialog", "light gray", "black"),
    ]
