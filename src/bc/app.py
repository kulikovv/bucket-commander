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
)
from bc.ui.panels import render_app


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

    def run(self) -> int:
        self._refresh_panel(PanelId.LEFT)
        self._refresh_panel(PanelId.RIGHT)
        self._loop = urwid.MainLoop(
            render_app(self._state),
            palette=_palette(),
            unhandled_input=self._handle_key,
        )
        self._loop.run()
        return 0

    def _handle_key(self, key: str | tuple[str, int, int, int]) -> None:
        if not isinstance(key, str):
            return
        if key in {"q", "Q", "esc"}:
            raise urwid.ExitMainLoop()
        if key == "tab":
            self._update(switch_focus(self._state))
            return
        if key == "up":
            self._update(move_cursor(self._state, -1))
            return
        if key == "down":
            self._update(move_cursor(self._state, 1))
            return
        if key in {"enter", "right"}:
            self._run_command(lambda: enter(self._state, self._backend))
            return
        if key in {"backspace", "left"}:
            self._run_command(lambda: go_parent(self._state, self._backend))
            return
        if key in {"r", "R", "ctrl r"}:
            self._refresh_panel(self._state.focused)

    def _refresh_panel(self, panel_id: PanelId) -> None:
        self._run_command(lambda: refresh(self._state, panel_id, self._backend))

    def _run_command(self, command: Callable[[], Coroutine[Any, Any, TwoPanelState]]) -> None:
        try:
            state: TwoPanelState = asyncio.run(command())
        except BackendError as error:
            self._update(self._state.with_status(str(error)))
            return
        self._update(state)

    def _update(self, state: TwoPanelState) -> None:
        self._state = state
        if self._loop is not None:
            self._loop.widget = render_app(self._state)
            self._loop.draw_screen()


def run_app(config: AppConfig) -> int:
    """Run the interactive application."""

    return BucketCommanderApp(config).run()


def _palette() -> list[tuple[str, str, str]]:
    return [
        ("panel_header", "black", "light gray"),
        ("panel_header_focus", "black", "yellow"),
        ("panel_body", "light gray", "black"),
        ("entry_focus", "black", "light cyan"),
        ("footer", "black", "light gray"),
        ("error", "light red", "black"),
    ]
