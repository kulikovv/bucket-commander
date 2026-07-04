"""Application entry point for the interactive Bucket Commander UI."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import urwid

from bc.backends import BackendError, BackendRouter, LocalBackend, S3Backend, TransferBackend
from bc.config import KnownSource, SourcesConfig
from bc.core import (
    Entry,
    LocalLocation,
    OperationResult,
    PanelState,
    TaskContext,
    TaskManager,
    TaskState,
    TaskType,
)
from bc.core.locations import Location, parse_location
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
from bc.ui.panels import UiCommand, render_app, render_help_overlay, render_location_picker_overlay

PENDING_COMMAND_KEYS = {
    "f3": "View",
    "f4": "New file",
    "f7": "New folder",
}
TASK_POLL_SECONDS = 0.1


@dataclass(frozen=True, slots=True)
class AppConfig:
    """Startup configuration for the terminal application."""

    left: Location
    right: Location
    sources: SourcesConfig = field(default_factory=SourcesConfig)

    @classmethod
    def from_paths(cls, *, left: Path, right: Path) -> AppConfig:
        return cls(left=parse_location(left), right=parse_location(right))


class BucketCommanderApp:
    """Minimal two-panel local file manager."""

    def __init__(self, config: AppConfig) -> None:
        s3_backend = S3Backend()
        self._backend = BackendRouter(
            (LocalBackend(), s3_backend, TransferBackend(s3_backend=s3_backend))
        )
        self._state = TwoPanelState(
            left=PanelState(location=config.left),
            right=PanelState(location=config.right),
        )
        self._loop: urwid.MainLoop | None = None
        self._is_help_open = False
        self._location_picker_panel: PanelId | None = None
        self._sources = config.sources.sources
        self._tasks = TaskManager()
        self._task_poll_scheduled = False
        self._handled_terminal_tasks: set[str] = set()

    def run(self) -> int:
        self._refresh_panel(PanelId.LEFT)
        self._refresh_panel(PanelId.RIGHT)
        try:
            self._loop = urwid.MainLoop(
                self._render(),
                palette=_palette(),
                unhandled_input=self._handle_key,
            )
            self._loop.run()
            return 0
        finally:
            self._tasks.close()

    def _handle_key(self, key: str | tuple[str, int, int, int]) -> None:
        if not isinstance(key, str):
            return
        if self._handle_help_key(key):
            return
        if self._handle_location_picker_key(key):
            return
        if key in {"q", "Q", "esc"}:
            raise urwid.ExitMainLoop()
        if key in {"f1", "?"}:
            self._show_help_dialog()
            return
        if self._handle_panel_key(key):
            return
        if self._handle_task_key(key):
            return
        if key in PENDING_COMMAND_KEYS:
            self._show_pending_command(PENDING_COMMAND_KEYS[key])

    def _handle_panel_key(self, key: str) -> bool:
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
        else:
            return False
        return True

    def _handle_help_key(self, key: str) -> bool:
        if not self._is_help_open:
            return False
        if key in {"q", "Q", "esc", "enter", "f1", "?"}:
            self._close_help_dialog()
        return True

    def _handle_location_picker_key(self, key: str) -> bool:
        if self._location_picker_panel is None:
            return False
        if key in {"q", "Q", "esc"}:
            self._close_location_picker()
        return True

    def _handle_task_key(self, key: str) -> bool:
        if key == "f5":
            self._start_copy_tasks()
        elif key == "f6":
            self._start_move_tasks()
        elif key in {"f8", "delete"}:
            self._start_delete_tasks()
        elif key in {"c", "C"}:
            self._cancel_latest_task()
        else:
            return False
        return True

    def _handle_ui_command(self, command: UiCommand) -> None:
        if command is UiCommand.HELP:
            self._show_help_dialog()
        elif command is UiCommand.VIEW:
            self._show_pending_command("View")
        elif command is UiCommand.NEW_FILE:
            self._show_pending_command("New file")
        elif command is UiCommand.SELECT:
            self._update(toggle_selection(self._state))
        elif command is UiCommand.REFRESH:
            self._refresh_panel(self._state.focused)
        elif command is UiCommand.COPY:
            self._start_copy_tasks()
        elif command is UiCommand.MOVE:
            self._start_move_tasks()
        elif command is UiCommand.NEW_FOLDER:
            self._show_pending_command("New folder")
        elif command is UiCommand.DELETE:
            self._start_delete_tasks()
        elif command is UiCommand.CANCEL:
            self._cancel_latest_task()
        elif command is UiCommand.QUIT:
            raise urwid.ExitMainLoop()

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

    def _show_location_picker(self, panel_id: PanelId) -> None:
        self._location_picker_panel = panel_id
        self._redraw()

    def _close_location_picker(self, _button: urwid.Button | None = None) -> None:
        self._location_picker_panel = None
        self._redraw()

    def _select_location_source(self, source: KnownSource) -> None:
        panel_id = self._location_picker_panel
        if panel_id is None:
            return
        self._location_picker_panel = None
        self._state = self._state.with_panel(panel_id, PanelState(location=source.location))
        self._refresh_panel(panel_id)

    def _start_copy_tasks(self) -> None:
        source_panel = self._operation_source_panel()
        entries = self._operation_entries(source_panel)
        destination = self._state.panel(source_panel.other).location
        if not entries:
            self._update(self._state.with_status("No entry selected"))
            return
        started = 0
        for entry in entries:
            if entry.name == "..":
                continue

            async def copy_task(
                context: TaskContext,
                source: Location = entry.location,
                target: Location = destination,
            ) -> OperationResult:
                return await self._backend.copy(source, target, progress=context.progress)

            self._tasks.start_task(
                TaskType.COPY,
                copy_task,
                source=entry.location,
                destination=destination,
            )
            started += 1
        self._after_task_start(started, "copy")

    def _start_move_tasks(self) -> None:
        source_panel = self._operation_source_panel()
        entries = self._operation_entries(source_panel)
        destination = self._state.panel(source_panel.other).location
        if not entries:
            self._update(self._state.with_status("No entry selected"))
            return
        started = 0
        for entry in entries:
            if entry.name == "..":
                continue

            async def move_task(
                _context: TaskContext,
                source: Location = entry.location,
                target: Location = destination,
            ) -> OperationResult:
                return await self._backend.move(source, target)

            self._tasks.start_task(
                TaskType.MOVE,
                move_task,
                source=entry.location,
                destination=destination,
            )
            started += 1
        self._after_task_start(started, "move")

    def _start_delete_tasks(self) -> None:
        source_panel = self._operation_source_panel()
        entries = self._operation_entries(source_panel)
        if not entries:
            self._update(self._state.with_status("No entry selected"))
            return
        started = 0
        for entry in entries:
            if not isinstance(entry.location, LocalLocation) or entry.name == "..":
                continue

            async def delete_task(
                context: TaskContext,
                source: Location = entry.location,
            ) -> OperationResult:
                return await self._backend.delete(source, recursive=True, progress=context.progress)

            self._tasks.start_task(TaskType.DELETE, delete_task, source=entry.location)
            started += 1
        self._after_task_start(started, "delete")

    def _operation_source_panel(self) -> PanelId:
        return self._state.focused

    def _operation_entries(self, panel_id: PanelId | None = None) -> tuple[Entry, ...]:
        panel = self._state.panel(panel_id or self._operation_source_panel())
        selected = panel.selected_entries
        if selected:
            return selected
        current = panel.current_entry
        return () if current is None else (current,)

    def _after_task_start(self, count: int, action: str) -> None:
        if count == 0:
            self._update(self._state.with_status(f"No local entries to {action}"))
            return
        plural = "" if count == 1 else "s"
        self._update(self._state.with_status(f"Started {count} {action} task{plural}"))
        self._schedule_task_poll()

    def _cancel_latest_task(self) -> None:
        active = self._tasks.active_records()
        if not active:
            self._update(self._state.with_status("No active task to cancel"))
            return
        task = active[-1]
        if self._tasks.cancel(task.task_id):
            self._update(self._state.with_status(f"Cancelling {task.task_type.value} task"))
            self._schedule_task_poll()

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
        app = render_app(
            self._state,
            sources=self._sources,
            tasks=self._tasks.records(),
            on_help=self._show_help_dialog,
            on_command=self._handle_ui_command,
            on_location_picker=self._show_location_picker,
        )
        if self._is_help_open:
            return render_help_overlay(app, on_close=self._close_help_dialog)
        if self._location_picker_panel is not None:
            return render_location_picker_overlay(
                app,
                self._sources,
                self._location_picker_panel,
                on_select=self._select_location_source,
                on_close=self._close_location_picker,
            )
        return app

    def _schedule_task_poll(self) -> None:
        if self._loop is None or self._task_poll_scheduled:
            return
        self._task_poll_scheduled = True
        self._loop.set_alarm_in(TASK_POLL_SECONDS, self._poll_tasks)

    def _poll_tasks(
        self,
        _loop: urwid.MainLoop,
        _user_data: object | None = None,
    ) -> None:
        self._task_poll_scheduled = False
        refresh_needed = False
        for task in self._tasks.records():
            if task.status.is_terminal and task.task_id not in self._handled_terminal_tasks:
                self._handled_terminal_tasks.add(task.task_id)
                refresh_needed = refresh_needed or task.status in {
                    TaskState.COMPLETED,
                    TaskState.CANCELLED,
                }
        if refresh_needed:
            self._refresh_panel(PanelId.LEFT)
            self._refresh_panel(PanelId.RIGHT)
        else:
            self._redraw()
        if self._tasks.active_records():
            self._schedule_task_poll()


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
        ("task_footer", "light gray", "black"),
        ("footer", "black", "light gray"),
        ("error", "light red", "black"),
        ("dialog", "light gray", "black"),
    ]
