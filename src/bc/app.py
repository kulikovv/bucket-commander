"""Application entry point for the interactive Bucket Commander UI."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from dataclasses import replace as field_replace
from datetime import datetime
from pathlib import Path
from typing import Any

import urwid

from bc.backends import (
    BackendError,
    BackendRouter,
    LocalBackend,
    S3Backend,
    S3BackendConfig,
    TransferBackend,
)
from bc.config import AppSettings, KnownSource, SourcesConfig
from bc.core import (
    Entry,
    EntryType,
    OperationResult,
    PanelState,
    S3Location,
    SortField,
    SortOrder,
    TaskContext,
    TaskManager,
    TaskState,
    TaskType,
)
from bc.core.locations import Location, parse_location
from bc.index import (
    DEFAULT_QUERY_LIMIT,
    CacheBackedBucketPanel,
    IndexedBucketQuery,
    RecursiveBucketIndexer,
    parse_indexed_search_query,
)
from bc.jobs import (
    DurableJobQueue,
    JobItem,
    JobRecord,
    JobStatus,
    OperationPlan,
    SQLiteJobStore,
    plan_delete,
    plan_move,
)
from bc.ui.commands import (
    PanelId,
    TwoPanelState,
    enter,
    focus_panel,
    focus_panel_item,
    go_parent,
    move_cursor,
    refresh,
    switch_focus,
    toggle_selection,
    with_parent_entry,
)
from bc.ui.panels import (
    UiCommand,
    render_app,
    render_bucket_menu_overlay,
    render_help_overlay,
    render_jobs_overlay,
    render_location_picker_overlay,
    render_name_prompt_overlay,
    render_operation_plan_overlay,
    render_search_overlay,
    render_settings_overlay,
    render_view_overlay,
)

TASK_POLL_SECONDS = 0.1
VIEW_MAX_BYTES = 64 * 1024
DOUBLE_CLICK_SECONDS = 0.5


@dataclass(frozen=True, slots=True)
class AppConfig:
    """Startup configuration for the terminal application."""

    left: Location
    right: Location
    sources: SourcesConfig = field(default_factory=SourcesConfig)
    settings: AppSettings = field(default_factory=AppSettings.defaults)
    cache_root: Path | None = None
    job_store_path: Path | None = None

    @classmethod
    def from_paths(cls, *, left: Path, right: Path) -> AppConfig:
        return cls(left=parse_location(left), right=parse_location(right))


class BucketCommanderApp:
    """Minimal two-panel local file manager."""

    def __init__(self, config: AppConfig) -> None:
        self._s3_backend = S3Backend(_s3_backend_config(config.settings))
        self._backend = BackendRouter(
            (
                LocalBackend(),
                self._s3_backend,
                TransferBackend(s3_backend=self._s3_backend),
            )
        )
        self._state = TwoPanelState(
            left=PanelState(location=config.left),
            right=PanelState(location=config.right),
        )
        self._loop: urwid.MainLoop | None = None
        self._settings = config.settings
        self._is_help_open = False
        self._view_dialog: tuple[str, str] | None = None
        self._is_jobs_open = False
        self._is_settings_open = False
        self._create_prompt: tuple[str, str] | None = None
        self._pending_operation_plan: OperationPlan | None = None
        self._pending_operation_executor: Callable[[], None] | None = None
        self._location_picker_panel: PanelId | None = None
        self._is_bucket_menu_open = False
        self._search_panel: PanelId | None = None
        self._search_text = ""
        self._sources = _with_builtin_sources(config.sources.sources)
        cache_root = config.cache_root or config.settings.cache_root
        self._bucket_cache = CacheBackedBucketPanel(cache_root)
        self._indexer = RecursiveBucketIndexer(
            backend=self._s3_backend,
            cache_root=self._bucket_cache.cache_root,
        )
        self._bucket_query = IndexedBucketQuery(self._bucket_cache.cache_root)
        self._job_store_path = config.job_store_path or (
            self._bucket_cache.cache_root / "jobs" / "jobs.sqlite3"
        )
        self._job_store: SQLiteJobStore | None = None
        self._job_queue: DurableJobQueue | None = None
        self._panel_focus_rows: dict[PanelId, int | None] = {
            PanelId.LEFT: None,
            PanelId.RIGHT: None,
        }
        self._last_mouse_click: tuple[PanelId, str, float] | None = None
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
        if self._handle_modal_key(key):
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

    def _handle_modal_key(self, key: str) -> bool:
        return (
            self._handle_help_key(key)
            or self._handle_view_key(key)
            or self._handle_jobs_key(key)
            or self._handle_settings_key(key)
            or self._handle_create_prompt_key(key)
            or self._handle_operation_plan_key(key)
            or self._handle_bucket_menu_key(key)
            or self._handle_search_key(key)
            or self._handle_location_picker_key(key)
        )

    def _handle_panel_key(self, key: str) -> bool:
        if key == "tab":
            self._update(switch_focus(self._state))
        elif key == "up":
            self._update(move_cursor(self._state, -1))
        elif key == "down":
            self._update(move_cursor(self._state, 1))
        elif key in {"enter", " ", "right"}:
            self._enter_active()
        elif key in {"s", "S"}:
            self._update(toggle_selection(self._state))
        elif key in {"backspace", "left", "\\"}:
            if self._leave_search_mode(self._state.focused):
                return True
            self._run_command(lambda: go_parent(self._state, self._backend))
        else:
            handler = {
                "r": lambda: self._refresh_panel(self._state.focused),
                "R": lambda: self._refresh_panel(self._state.focused),
                "ctrl r": lambda: self._refresh_panel(self._state.focused),
                "/": self._show_search_dialog,
                "o": self._cycle_sort,
                "O": self._cycle_sort,
                "j": self._show_jobs_dialog,
                "J": self._show_jobs_dialog,
                "g": self._show_settings_dialog,
                "G": self._show_settings_dialog,
            }.get(key)
            if handler is None:
                return False
            handler()
        return True

    def _handle_help_key(self, key: str) -> bool:
        if not self._is_help_open:
            return False
        if key in {"q", "Q", "esc", "enter", "f1", "?"}:
            self._close_help_dialog()
        return True

    def _handle_view_key(self, key: str) -> bool:
        if self._view_dialog is None:
            return False
        if key in {"q", "Q", "esc", "enter", "f3"}:
            self._close_view_dialog()
        return True

    def _handle_jobs_key(self, key: str) -> bool:
        if not self._is_jobs_open:
            return False
        if key in {"q", "Q", "esc", "j", "J"}:
            self._close_jobs_dialog()
        return True

    def _handle_settings_key(self, key: str) -> bool:
        if not self._is_settings_open:
            return False
        if key in {"q", "Q", "esc", "enter", "g", "G"}:
            self._close_settings_dialog()
        return True

    def _handle_create_prompt_key(self, key: str) -> bool:
        if self._create_prompt is None:
            return False
        kind, name = self._create_prompt
        if key in {"esc", "q"}:
            self._close_create_prompt()
        elif key == "enter":
            self._apply_create_prompt()
        elif key == "backspace":
            self._create_prompt = (kind, name[:-1])
            self._redraw()
        elif len(key) == 1 and key.isprintable():
            self._create_prompt = (kind, f"{name}{key}")
            self._redraw()
        return True

    def _handle_operation_plan_key(self, key: str) -> bool:
        if self._pending_operation_plan is None:
            return False
        if key in {"enter"}:
            self._confirm_operation_plan()
        elif key in {"q", "Q", "esc"}:
            self._cancel_operation_plan()
        return True

    def _handle_bucket_menu_key(self, key: str) -> bool:
        if not self._is_bucket_menu_open:
            return False
        if key in {"q", "Q", "esc"}:
            self._close_bucket_menu()
        return True

    def _handle_search_key(self, key: str) -> bool:
        if self._search_panel is None:
            return False
        if key in {"esc", "q"}:
            self._close_search_dialog()
        elif key == "enter":
            self._apply_search_dialog()
        elif key == "backspace":
            self._search_text = self._search_text[:-1]
            self._redraw()
        elif len(key) == 1 and key.isprintable():
            self._search_text = f"{self._search_text}{key}"
            self._redraw()
        return True

    def _handle_location_picker_key(self, key: str) -> bool:
        if self._location_picker_panel is None:
            return False
        if key in {"q", "Q", "esc"}:
            self._close_location_picker()
        return True

    def _handle_task_key(self, key: str) -> bool:
        if key == "f3":
            self._view_current_entry()
        elif key == "f5":
            self._start_copy_tasks()
        elif key == "f6":
            self._start_move_tasks()
        elif key == "f4":
            self._show_create_prompt("file")
        elif key == "f7":
            self._show_create_prompt("folder")
        elif key in {"f8", "delete"}:
            self._start_delete_tasks()
        elif key in {"i", "I"}:
            self._start_index_task()
        elif key in {"c", "C"}:
            self._cancel_latest_task()
        else:
            return False
        return True

    def _handle_ui_command(self, command: UiCommand) -> None:
        if command is UiCommand.QUIT:
            raise urwid.ExitMainLoop()
        self._is_bucket_menu_open = False
        handlers: dict[UiCommand, Callable[[], None]] = {
            UiCommand.HELP: self._show_help_dialog,
            UiCommand.VIEW: self._view_current_entry,
            UiCommand.SEARCH: self._show_search_dialog,
            UiCommand.SORT: self._cycle_sort,
            UiCommand.JOBS: self._show_jobs_dialog,
            UiCommand.NEW_FILE: lambda: self._show_create_prompt("file"),
            UiCommand.SELECT: lambda: self._update(toggle_selection(self._state)),
            UiCommand.REFRESH: lambda: self._refresh_panel(self._state.focused),
            UiCommand.COPY: self._start_copy_tasks,
            UiCommand.MOVE: self._start_move_tasks,
            UiCommand.NEW_FOLDER: lambda: self._show_create_prompt("folder"),
            UiCommand.DELETE: self._start_delete_tasks,
            UiCommand.INDEX: self._start_index_task,
            UiCommand.CANCEL: self._cancel_latest_task,
        }
        handlers[command]()

    def _handle_panel_mouse(
        self,
        panel_id: PanelId,
        index: int | None,
        *,
        toggle_mark: bool = False,
        viewport_row: int | None = None,
    ) -> None:
        self._panel_focus_rows[panel_id] = viewport_row
        if index is None:
            self._last_mouse_click = None
            self._update(focus_panel(self._state, panel_id))
            return
        clicked_entry = self._state.panel(panel_id).entries[index]
        is_double_click = self._is_double_click(panel_id, clicked_entry)
        self._update(focus_panel_item(self._state, panel_id, index, toggle_mark=toggle_mark))
        if toggle_mark:
            self._last_mouse_click = None
            return
        self._last_mouse_click = (panel_id, clicked_entry.uri, time.monotonic())
        if is_double_click and (clicked_entry.name == ".." or clicked_entry.is_container):
            self._last_mouse_click = None
            self._enter_active()

    def _is_double_click(self, panel_id: PanelId, entry: Entry) -> bool:
        last_click = self._last_mouse_click
        if last_click is None:
            return False
        last_panel_id, last_uri, last_time = last_click
        return (
            last_panel_id is panel_id
            and last_uri == entry.uri
            and time.monotonic() - last_time <= DOUBLE_CLICK_SECONDS
        )

    def _refresh_panel(self, panel_id: PanelId) -> None:
        panel = self._state.panel(panel_id)
        if isinstance(panel.location, S3Location):
            self._refresh_s3_panel(panel_id, panel.location)
            return
        self._run_command(lambda: refresh(self._state, panel_id, self._backend))

    def _refresh_s3_panel(self, panel_id: PanelId, location: S3Location) -> None:
        try:
            cached = asyncio.run(self._bucket_cache.load_cached(location))
        except BackendError as error:
            self._update(self._state.with_status(str(error)))
            return
        except (OSError, ValueError, TypeError) as error:
            cached = None
            self._update(
                self._state.with_status(f"Ignoring damaged cache for {location.label}: {error}")
            )
        if cached is not None and cached.has_cache:
            panel = self._state.panel(panel_id)
            cached_entries = with_parent_entry(location, cached.entries)
            cached_panel = panel.with_entries(cached_entries)
            cached_panel = field_replace(
                cached_panel,
                is_loading=True,
                status_message=cached.status,
                cursor_index=min(panel.cursor_index, max(0, len(cached_entries) - 1)),
            )
            self._update(
                self._state.with_panel(panel_id, cached_panel).with_status(
                    f"{location.label}: {cached.status}"
                )
            )
        self._run_command(lambda: self._refresh_s3_panel_live(panel_id, location))

    async def _refresh_s3_panel_live(
        self,
        panel_id: PanelId,
        location: S3Location,
    ) -> TwoPanelState:
        panel = self._state.panel(panel_id)
        loading_panel = field_replace(panel, is_loading=True, status_message="Loading live")
        loading_state = self._state.with_panel(panel_id, loading_panel)
        live_entries = await self._backend.list(location)
        cache_status = "cached fresh"
        try:
            await self._bucket_cache.store_live_listing(location, live_entries)
        except (OSError, ValueError, TypeError) as error:
            cache_status = f"cache update failed: {error}"
        entries = with_parent_entry(location, live_entries)
        refreshed_panel = field_replace(
            loading_panel,
            entries=entries,
            cursor_index=min(loading_panel.cursor_index, max(0, len(entries) - 1)),
            is_loading=False,
            status_message=f"{len(entries)} entries",
        )
        return loading_state.with_panel(panel_id, refreshed_panel).with_status(
            f"{location.label}: {len(entries)} live entries ({cache_status})"
        )

    def _show_help_dialog(self, _button: urwid.Button | None = None) -> None:
        self._is_help_open = True
        self._redraw()

    def _close_help_dialog(self, _button: urwid.Button | None = None) -> None:
        self._is_help_open = False
        self._redraw()

    def _close_view_dialog(self, _button: urwid.Button | None = None) -> None:
        self._view_dialog = None
        self._redraw()

    def _show_jobs_dialog(self, _button: urwid.Button | None = None) -> None:
        self._is_jobs_open = True
        self._redraw()

    def _close_jobs_dialog(self, _button: urwid.Button | None = None) -> None:
        self._is_jobs_open = False
        self._redraw()

    def _show_settings_dialog(self, _button: urwid.Button | None = None) -> None:
        self._is_settings_open = True
        self._redraw()

    def _close_settings_dialog(self, _button: urwid.Button | None = None) -> None:
        self._is_settings_open = False
        self._redraw()

    def _show_create_prompt(self, kind: str) -> None:
        self._create_prompt = (kind, "")
        self._redraw()

    def _close_create_prompt(self, _button: urwid.Button | None = None) -> None:
        self._create_prompt = None
        self._redraw()

    def _apply_create_prompt(self, _button: urwid.Button | None = None) -> None:
        prompt = self._create_prompt
        if prompt is None:
            return
        kind, name = prompt
        self._create_prompt = None
        self._create_entry(kind, name.strip())

    def _create_entry(self, kind: str, name: str) -> None:
        if not name:
            self._update(self._state.with_status("Name must not be empty"))
            return
        panel_id = self._state.focused
        panel = self._state.panel(panel_id)
        try:
            target = panel.location.child(name)
            if kind == "file" and isinstance(panel.location, S3Location):
                target = S3Location(
                    bucket=panel.location.bucket,
                    prefix=f"{panel.location.prefix}{name}".lstrip("/"),
                    profile=panel.location.profile,
                    region=panel.location.region,
                    endpoint_url=panel.location.endpoint_url,
                )
        except ValueError as error:
            self._update(self._state.with_status(str(error)))
            return
        try:
            if kind == "file":
                asyncio.run(self._backend.create_file(target))
            else:
                asyncio.run(self._backend.mkdir(target))
        except BackendError as error:
            self._update(self._state.with_status(str(error)))
            return
        self._refresh_panel(panel_id)
        self._update(self._state.with_status(f"Created {target.label}"))

    def _pause_latest_job(self, _button: urwid.Button | None = None) -> None:
        self._update_latest_job("pause", lambda queue, job_id: queue.pause(job_id))

    def _resume_latest_job(self, _button: urwid.Button | None = None) -> None:
        self._update_latest_job("resume", lambda queue, job_id: queue.resume(job_id))

    def _cancel_latest_job(self, _button: urwid.Button | None = None) -> None:
        self._update_latest_job("cancel", lambda queue, job_id: queue.request_cancel(job_id))

    def _retry_latest_job(self, _button: urwid.Button | None = None) -> None:
        self._update_latest_job("retry", lambda queue, job_id: queue.resume(job_id))

    def _update_latest_job(
        self,
        action: str,
        update: Callable[[DurableJobQueue, str], JobRecord],
    ) -> None:
        job = self._latest_visible_job()
        if job is None:
            self._update(self._state.with_status(f"No durable job to {action}"))
            return
        try:
            updated = update(self._ensure_job_queue(), job.job_id)
        except (KeyError, ValueError) as error:
            self._update(self._state.with_status(f"Cannot {action} job: {error}"))
            return
        self._update(self._state.with_status(f"{updated.job_id}: {updated.status.value}"))

    def _confirm_operation_plan(self, _button: urwid.Button | None = None) -> None:
        executor = self._pending_operation_executor
        self._pending_operation_plan = None
        self._pending_operation_executor = None
        if executor is None:
            self._update(self._state.with_status("No pending operation to confirm"))
            return
        executor()

    def _cancel_operation_plan(self, _button: urwid.Button | None = None) -> None:
        plan = self._pending_operation_plan
        self._pending_operation_plan = None
        self._pending_operation_executor = None
        action = "operation" if plan is None else plan.kind.value
        self._update(self._state.with_status(f"Cancelled {action}"))

    def _show_bucket_menu(self, _button: urwid.Button | None = None) -> None:
        self._is_bucket_menu_open = True
        self._redraw()

    def _close_bucket_menu(self, _button: urwid.Button | None = None) -> None:
        self._is_bucket_menu_open = False
        self._redraw()

    def _show_search_dialog(self, _button: urwid.Button | None = None) -> None:
        self._search_panel = self._state.focused
        self._search_text = self._state.active.filter_text
        self._redraw()

    def _close_search_dialog(self, _button: urwid.Button | None = None) -> None:
        self._search_panel = None
        self._redraw()

    def _apply_search_dialog(self, _button: urwid.Button | None = None) -> None:
        panel_id = self._search_panel
        if panel_id is None:
            return
        query = self._search_text.strip()
        self._search_panel = None
        self._apply_indexed_search(panel_id, query)

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

    def _view_current_entry(self) -> None:
        entry = self._state.active.current_entry
        if entry is None:
            self._update(self._state.with_status("No entry selected"))
            return
        if entry.name == "..":
            self._update(self._state.with_status("Cannot view parent entry"))
            return
        self._run_view_command(entry)

    def _run_view_command(self, entry: Entry) -> None:
        try:
            preview = asyncio.run(self._backend.preview(entry.location, max_bytes=VIEW_MAX_BYTES))
        except BackendError as error:
            self._update(self._state.with_status(str(error)))
            return
        text = preview.data.decode("utf-8", errors="replace")
        if preview.truncated:
            text = f"{text}\n\n[truncated at {VIEW_MAX_BYTES} bytes]"
        self._view_dialog = (entry.name, text)
        self._redraw()

    def _enter_active(self) -> None:
        panel = self._state.active
        if (
            panel.current_entry is not None
            and panel.current_entry.name == ".."
            and self._leave_search_mode(self._state.focused)
        ):
            return
        self._run_command(lambda: enter(self._state, self._backend))

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
        plan = plan_move(entries, source_panel=source_panel.value, destination=destination)
        if not plan.entries:
            self._update(self._state.with_status("No entries to move"))
            return
        def executor() -> None:
            self._execute_move_tasks(entries, destination)

        self._show_operation_plan(plan, executor)

    def _execute_move_tasks(self, entries: tuple[Entry, ...], destination: Location) -> None:
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
        plan = plan_delete(entries, source_panel=source_panel.value)
        if not plan.entries:
            self._update(self._state.with_status("No entries to delete"))
            return
        def executor() -> None:
            self._execute_delete_tasks(entries)

        self._show_operation_plan(plan, executor)

    def _execute_delete_tasks(self, entries: tuple[Entry, ...]) -> None:
        started = 0
        for entry in entries:
            if entry.name == "..":
                continue

            async def delete_task(
                context: TaskContext,
                source: Location = entry.location,
                recursive: bool = entry.is_container,
            ) -> OperationResult:
                return await self._backend.delete(
                    source,
                    recursive=recursive,
                    progress=context.progress,
                )

            self._tasks.start_task(TaskType.DELETE, delete_task, source=entry.location)
            started += 1
        self._after_task_start(started, "delete")

    def _show_operation_plan(
        self,
        plan: OperationPlan,
        executor: Callable[[], None],
    ) -> None:
        self._pending_operation_plan = plan
        self._pending_operation_executor = executor
        self._update(
            self._state.with_status(
                f"Confirm {plan.kind.value}: {plan.direct_count} selected entry"
                f"{'' if plan.direct_count == 1 else 's'}"
            )
        )

    def _start_index_task(self) -> None:
        target = self._index_target()
        if target is None:
            self._update(self._state.with_status("Recursive indexing is only available for S3"))
            return

        async def index_task(
            context: TaskContext,
            source: S3Location = target,
        ) -> OperationResult:
            result = await self._indexer.index(source, progress=context.progress, resume=True)
            return OperationResult.success(
                f"Indexed {result.objects_indexed} objects under {source.uri}",
                source=source,
                entries_affected=result.objects_indexed,
                bytes_affected=result.bytes_indexed,
            )

        self._tasks.start_task(TaskType.INDEXING, index_task, source=target)
        self._after_task_start(1, "index")

    def _index_target(self) -> S3Location | None:
        panel = self._state.active
        current = panel.current_entry
        if (
            current is not None
            and current.name != ".."
            and current.entry_type is EntryType.PREFIX
            and isinstance(current.location, S3Location)
        ):
            return current.location
        if isinstance(panel.location, S3Location):
            return panel.location
        return None

    def _apply_indexed_search(self, panel_id: PanelId, query: str) -> None:
        if not query:
            self._update(self._state.with_status("Search query must not be empty"))
            return
        panel = self._state.panel(panel_id)
        if not isinstance(panel.location, S3Location):
            self._update(self._state.with_status("Indexed search is only available for S3"))
            return
        try:
            result = asyncio.run(
                self._bucket_query.search(
                    panel.location,
                    criteria=parse_indexed_search_query(query),
                    sort_field=panel.sort_field,
                    sort_order=panel.sort_order,
                    limit=DEFAULT_QUERY_LIMIT,
                )
            )
        except (OSError, ValueError, TypeError) as error:
            self._update(self._state.with_status(f"Indexed search failed: {error}"))
            return
        entries = with_parent_entry(panel.location, result.entries)
        searched_panel = field_replace(
            panel.with_entries(entries),
            filter_text=query,
            cursor_index=0,
            status_message=f"{result.total_count} indexed result(s)",
        )
        page_note = "" if result.total_count <= result.limit else f", first {result.limit}"
        self._update(
            self._state.with_panel(panel_id, searched_panel).with_status(
                f"{panel.location.label}: {result.total_count} indexed result(s)"
                f"{page_note}; {result.coverage_message}"
            )
        )

    def _leave_search_mode(self, panel_id: PanelId) -> bool:
        panel = self._state.panel(panel_id)
        if not panel.filter_text:
            return False
        restored_panel = field_replace(
            PanelState(
                location=panel.location,
                sort_field=panel.sort_field,
                sort_order=panel.sort_order,
            ),
            status_message="Leaving search",
        )
        self._state = self._state.with_panel(panel_id, restored_panel).with_status(
            f"{panel.location.label}: leaving indexed search"
        )
        self._refresh_panel(panel_id)
        return True

    def _cycle_sort(self) -> None:
        panel_id = self._state.focused
        panel = self._state.active
        next_field, next_order = _next_sort(panel.sort_field, panel.sort_order)
        sorted_panel = field_replace(panel, sort_field=next_field, sort_order=next_order)
        self._state = self._state.with_panel(panel_id, sorted_panel)
        if isinstance(sorted_panel.location, S3Location) and sorted_panel.filter_text:
            self._apply_indexed_search(panel_id, sorted_panel.filter_text)
            return
        entries = _sort_entries(sorted_panel.entries, next_field, next_order)
        self._update(
            self._state.with_panel(
                panel_id,
                field_replace(sorted_panel.with_entries(entries), cursor_index=0),
            ).with_status(f"Sorted by {next_field.value} {next_order.value}")
        )

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
            jobs=self._durable_jobs(),
            on_help=self._show_help_dialog,
            on_command=self._handle_ui_command,
            on_jobs=self._show_jobs_dialog,
            on_location_picker=self._show_location_picker,
            on_bucket_menu=self._show_bucket_menu,
            on_settings=self._show_settings_dialog,
            on_panel_mouse=self._handle_panel_mouse,
            panel_focus_rows=self._panel_focus_rows,
        )
        widget = app
        if self._is_help_open:
            widget = render_help_overlay(app, on_close=self._close_help_dialog)
        elif self._view_dialog is not None:
            title, content = self._view_dialog
            widget = render_view_overlay(
                app,
                title=title,
                content=content,
                on_close=self._close_view_dialog,
            )
        elif self._create_prompt is not None:
            kind, name = self._create_prompt
            widget = render_name_prompt_overlay(
                app,
                title="New File" if kind == "file" else "New Folder",
                name=name,
                on_apply=self._apply_create_prompt,
                on_close=self._close_create_prompt,
            )
        elif self._is_jobs_open:
            widget = render_jobs_overlay(
                app,
                self._durable_jobs(),
                self._durable_job_items(),
                on_pause=self._pause_latest_job,
                on_resume=self._resume_latest_job,
                on_cancel=self._cancel_latest_job,
                on_retry=self._retry_latest_job,
                on_close=self._close_jobs_dialog,
            )
        elif self._is_settings_open:
            widget = render_settings_overlay(
                app,
                self._settings,
                on_close=self._close_settings_dialog,
            )
        elif self._pending_operation_plan is not None:
            widget = render_operation_plan_overlay(
                app,
                self._pending_operation_plan,
                on_confirm=self._confirm_operation_plan,
                on_cancel=self._cancel_operation_plan,
            )
        elif self._is_bucket_menu_open:
            widget = render_bucket_menu_overlay(
                app,
                on_command=self._handle_ui_command,
                on_close=self._close_bucket_menu,
            )
        elif self._search_panel is not None:
            widget = render_search_overlay(
                app,
                query=self._search_text,
                on_apply=self._apply_search_dialog,
                on_close=self._close_search_dialog,
            )
        elif self._location_picker_panel is not None:
            widget = render_location_picker_overlay(
                app,
                self._sources,
                self._location_picker_panel,
                on_select=self._select_location_source,
                on_close=self._close_location_picker,
            )
        return widget

    def _ensure_job_queue(self) -> DurableJobQueue:
        if self._job_queue is None:
            self._job_store = SQLiteJobStore(self._job_store_path)
            self._job_queue = DurableJobQueue(self._job_store)
        return self._job_queue

    def _ensure_job_store(self) -> SQLiteJobStore:
        self._ensure_job_queue()
        if self._job_store is None:
            msg = "Job store was not initialized"
            raise RuntimeError(msg)
        return self._job_store

    def _durable_jobs(self) -> tuple[JobRecord, ...]:
        try:
            return self._ensure_job_queue().list_jobs()
        except OSError:
            return ()

    def _durable_job_items(self) -> tuple[JobItem, ...]:
        job = self._latest_visible_job()
        if job is None:
            return ()
        try:
            return self._ensure_job_store().list_items(job.job_id)
        except OSError:
            return ()

    def _latest_visible_job(self) -> JobRecord | None:
        jobs = self._durable_jobs()
        for job in reversed(jobs):
            if not job.status.is_terminal:
                return job
        for job in reversed(jobs):
            if job.status is JobStatus.FAILED:
                return job
        return jobs[-1] if jobs else None

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


def _with_builtin_sources(sources: tuple[KnownSource, ...]) -> tuple[KnownSource, ...]:
    local_source = KnownSource(
        name="Local file system",
        location=parse_location(Path.home()),
        credential_source="local",
    )
    return (local_source, *sources)


def _s3_backend_config(settings: AppSettings) -> S3BackendConfig:
    profile = settings.profiles.default_s3
    return S3BackendConfig(
        profile_name=profile.profile_name,
        region_name=profile.region_name,
        endpoint_url=profile.endpoint_url,
    )


def _next_sort(current: SortField, order: SortOrder) -> tuple[SortField, SortOrder]:
    fields = (
        SortField.NAME,
        SortField.TYPE,
        SortField.SIZE,
        SortField.MODIFIED_AT,
    )
    if order is SortOrder.ASCENDING:
        return current, SortOrder.DESCENDING
    index = fields.index(current)
    return fields[(index + 1) % len(fields)], SortOrder.ASCENDING


def _sort_entries(
    entries: tuple[Entry, ...],
    sort_field: SortField,
    sort_order: SortOrder,
) -> tuple[Entry, ...]:
    parent = tuple(entry for entry in entries if entry.name == "..")
    sortable = tuple(entry for entry in entries if entry.name != "..")
    reverse = sort_order is SortOrder.DESCENDING
    return (
        *parent,
        *sorted(sortable, key=lambda entry: _sort_key(entry, sort_field), reverse=reverse),
    )


def _sort_key(entry: Entry, sort_field: SortField) -> tuple[object, ...]:
    if sort_field is SortField.TYPE:
        return (entry.entry_type.value, entry.name.casefold())
    if sort_field is SortField.SIZE:
        return (entry.size is None, entry.size or 0, entry.name.casefold())
    if sort_field is SortField.MODIFIED_AT:
        return (
            entry.modified_at is None,
            entry.modified_at or datetime.min,
            entry.name.casefold(),
        )
    return (entry.name.casefold(),)


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
