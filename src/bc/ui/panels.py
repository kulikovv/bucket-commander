"""Urwid widgets for the two-panel file interface."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from enum import StrEnum
from typing import Protocol

import urwid

from bc.config import AppSettings, KnownSource
from bc.core import Entry, EntryType, PanelState
from bc.core.task_manager import TaskRecord, TaskState
from bc.jobs import JobItem, JobItemStatus, JobRecord, JobStatus, OperationPlan
from bc.ui.commands import PanelId, TwoPanelState

SIZE_STEP = 1024.0
TASK_DETAIL_MAX = 52
TASK_DETAIL_SUFFIX = 49
COMMAND_BUTTON_WIDTH = 14
PRIMARY_MOUSE_BUTTON = 1
SECONDARY_MOUSE_BUTTON = 3
COMMAND_BUTTONS = (
    ("Help", "help"),
    ("View", "view"),
    ("Sort", "sort"),
    ("Jobs", "jobs"),
    ("New File", "new_file"),
    ("Copy", "copy"),
    ("Move", "move"),
    ("New Folder", "new_folder"),
    ("Delete", "delete"),
    ("Quit", "quit"),
)
BUCKET_COMMANDS = (
    ("Search", "search"),
    ("Index", "index"),
    ("Cancel", "cancel"),
)
HELP_COMMANDS = (
    ("F1 or ?", "Show this help"),
    ("Tab", "Switch active panel"),
    ("Up/Down", "Move cursor"),
    ("Enter, Space, Right", "Open directory or prefix"),
    ("Backspace, Left", "Go to parent or leave search"),
    ("S", "Toggle selection"),
    ("R or Ctrl-R", "Refresh active panel"),
    ("F3", "View selected entry"),
    ("/", "Search indexed bucket metadata"),
    ("\\", "Leave indexed search"),
    ("O", "Cycle sort field"),
    ("G", "Show resolved settings"),
    ("F4", "Create a new file"),
    ("F5", "Copy selected entries"),
    ("F6", "Move selected entries"),
    ("F7", "Create a new folder"),
    ("F8 or Delete", "Delete selected entries"),
    ("I", "Index current bucket or prefix recursively"),
    ("C", "Cancel active task"),
    ("Q", "Quit Bucket Commander"),
)


class PanelMouseHandler(Protocol):
    def __call__(
        self,
        panel_id: PanelId,
        index: int | None,
        *,
        toggle_mark: bool = False,
        viewport_row: int | None = None,
    ) -> object: ...


class UiCommand(StrEnum):
    """Commands exposed by clickable footer buttons."""

    HELP = "help"
    VIEW = "view"
    SEARCH = "search"
    SORT = "sort"
    NEW_FILE = "new_file"
    JOBS = "jobs"
    SELECT = "select"
    REFRESH = "refresh"
    COPY = "copy"
    MOVE = "move"
    NEW_FOLDER = "new_folder"
    DELETE = "delete"
    INDEX = "index"
    CANCEL = "cancel"
    QUIT = "quit"


def render_app(
    state: TwoPanelState,
    *,
    sources: tuple[KnownSource, ...] = (),
    tasks: tuple[TaskRecord, ...] = (),
    jobs: tuple[JobRecord, ...] = (),
    on_help: Callable[[urwid.Button], object] | None = None,
    on_command: Callable[[UiCommand], object] | None = None,
    on_jobs: Callable[[urwid.Button], object] | None = None,
    on_location_picker: Callable[[PanelId], object] | None = None,
    on_bucket_menu: Callable[[urwid.Button], object] | None = None,
    on_settings: Callable[[urwid.Button], object] | None = None,
    on_panel_mouse: PanelMouseHandler | None = None,
    panel_focus_rows: dict[PanelId, int | None] | None = None,
) -> urwid.Widget:
    """Render the full two-panel application."""

    left = render_panel(
        state.left,
        title="Left",
        panel_id=PanelId.LEFT,
        is_focused=state.focused is PanelId.LEFT,
        on_mouse=on_panel_mouse,
        focus_row=panel_focus_rows.get(PanelId.LEFT) if panel_focus_rows else None,
    )
    right = render_panel(
        state.right,
        title="Right",
        panel_id=PanelId.RIGHT,
        is_focused=state.focused is PanelId.RIGHT,
        on_mouse=on_panel_mouse,
        focus_row=panel_focus_rows.get(PanelId.RIGHT) if panel_focus_rows else None,
    )
    body = urwid.Columns(
        [
            ("weight", 1, left),
            ("weight", 1, right),
        ],
        dividechars=1,
    )
    footer = urwid.Pile(
        [
            ("pack", _command_footer(on_help, on_command)),
            ("pack", _task_footer(tasks)),
            ("pack", _job_footer(jobs, on_jobs)),
            ("pack", urwid.AttrMap(urwid.Text(state.status_message, wrap="clip"), "footer")),
        ]
    )
    return urwid.Frame(
        body=body,
        header=_location_header(sources, on_location_picker, on_bucket_menu, on_settings),
        footer=footer,
    )


def render_help_dialog(
    *,
    on_close: Callable[[urwid.Button], object] | None = None,
) -> urwid.Widget:
    """Render the modal help dialog."""

    command_rows: list[urwid.Widget] = [
        urwid.Columns(
            [
                ("given", 20, urwid.Text(keys)),
                ("weight", 1, urwid.Text(description)),
            ],
            dividechars=2,
        )
        for keys, description in HELP_COMMANDS
    ]
    close_button = urwid.Button("Close", on_press=on_close)
    content = urwid.Pile(
        [
            *command_rows,
            ("pack", urwid.Divider()),
            ("pack", urwid.Padding(close_button, align="center", width=14)),
        ]
    )
    return urwid.AttrMap(urwid.LineBox(content, title=" Commands "), "dialog")


def render_help_overlay(
    base: urwid.Widget,
    *,
    on_close: Callable[[urwid.Button], object] | None = None,
) -> urwid.Widget:
    """Place the help dialog over the current application."""

    dialog = render_help_dialog(on_close=on_close)
    return urwid.Overlay(
        top_w=dialog,
        bottom_w=base,
        align="center",
        width=("relative", 58),
        valign="middle",
        height="pack",
    )


def render_view_overlay(
    base: urwid.Widget,
    *,
    title: str,
    content: str,
    on_close: Callable[[urwid.Button], object] | None = None,
) -> urwid.Widget:
    """Place a file/object preview dialog over the current application."""

    lines = content.splitlines() or [""]
    body = urwid.ListBox(urwid.SimpleFocusListWalker([urwid.Text(line) for line in lines]))
    close_button = urwid.Button("Close", on_press=on_close)
    content_widget = urwid.Pile(
        [
            ("weight", 1, body),
            ("pack", urwid.Divider()),
            ("pack", urwid.Padding(close_button, align="center", width=14)),
        ]
    )
    dialog = urwid.AttrMap(urwid.LineBox(content_widget, title=f" View: {title} "), "dialog")
    return urwid.Overlay(
        top_w=dialog,
        bottom_w=base,
        align="center",
        width=("relative", 78),
        valign="middle",
        height=("relative", 72),
    )


def render_message_overlay(
    base: urwid.Widget,
    *,
    title: str,
    content: str,
    on_close: Callable[[urwid.Button], object] | None = None,
) -> urwid.Widget:
    """Place a dismissible notification dialog over the current application."""

    lines = content.splitlines() or [""]
    body = urwid.Pile([("pack", urwid.Text(line)) for line in lines])
    close_button = urwid.Button("Close", on_press=on_close)
    content_widget = urwid.Pile(
        [
            ("pack", body),
            ("pack", urwid.Divider()),
            ("pack", urwid.Padding(close_button, align="center", width=14)),
        ]
    )
    dialog = urwid.AttrMap(urwid.LineBox(content_widget, title=f" {title} "), "dialog")
    return urwid.Overlay(
        top_w=dialog,
        bottom_w=base,
        align="center",
        width=("relative", 70),
        valign="middle",
        height="pack",
    )


def render_search_overlay(
    base: urwid.Widget,
    *,
    query: str,
    on_apply: Callable[[urwid.Button], object] | None = None,
    on_close: Callable[[urwid.Button], object] | None = None,
) -> urwid.Widget:
    """Place an indexed search prompt over the current application."""

    content = urwid.Pile(
        [
            ("pack", urwid.Text(f" Query: {query or '<type a search query>'}", wrap="clip")),
            ("pack", urwid.Divider()),
            (
                "pack",
                urwid.Columns(
                    [
                        ("given", 14, urwid.Button("Search", on_press=on_apply)),
                        ("given", 14, urwid.Button("Close", on_press=on_close)),
                    ],
                    dividechars=2,
                ),
            ),
        ]
    )
    dialog = urwid.AttrMap(urwid.LineBox(content, title=" Indexed Search "), "dialog")
    return urwid.Overlay(
        top_w=dialog,
        bottom_w=base,
        align="center",
        width=("relative", 64),
        valign="middle",
        height="pack",
    )


def render_name_prompt_overlay(
    base: urwid.Widget,
    *,
    title: str,
    name: str,
    on_apply: Callable[[urwid.Button], object] | None = None,
    on_close: Callable[[urwid.Button], object] | None = None,
) -> urwid.Widget:
    """Place a simple name prompt over the current application."""

    content = urwid.Pile(
        [
            ("pack", urwid.Text(f" Name: {name or '<type a name>'}", wrap="clip")),
            ("pack", urwid.Divider()),
            (
                "pack",
                urwid.Columns(
                    [
                        ("given", 14, urwid.Button("Create", on_press=on_apply)),
                        ("given", 14, urwid.Button("Close", on_press=on_close)),
                    ],
                    dividechars=2,
                ),
            ),
        ]
    )
    dialog = urwid.AttrMap(urwid.LineBox(content, title=f" {title} "), "dialog")
    return urwid.Overlay(
        top_w=dialog,
        bottom_w=base,
        align="center",
        width=("relative", 58),
        valign="middle",
        height="pack",
    )


def render_bucket_menu_overlay(
    base: urwid.Widget,
    *,
    on_command: Callable[[UiCommand], object] | None = None,
    on_close: Callable[[urwid.Button], object] | None = None,
) -> urwid.Widget:
    """Place bucket-related commands over the current application."""

    rows = [
        urwid.Button(label, on_press=_emit_bucket_menu_command, user_data=(command, on_command))
        for label, command in BUCKET_COMMANDS
    ]
    close_button = urwid.Button("Close", on_press=on_close)
    content = urwid.Pile(
        [
            *rows,
            ("pack", urwid.Divider()),
            ("pack", urwid.Padding(close_button, align="center", width=14)),
        ]
    )
    dialog = urwid.AttrMap(urwid.LineBox(content, title=" Bucket "), "dialog")
    return urwid.Overlay(
        top_w=dialog,
        bottom_w=base,
        align="center",
        width=("relative", 36),
        valign="top",
        top=2,
        height="pack",
    )


def render_settings_overlay(
    base: urwid.Widget,
    settings: AppSettings,
    *,
    on_close: Callable[[urwid.Button], object] | None = None,
) -> urwid.Widget:
    """Place resolved application settings over the current application."""

    rows = _settings_rows(settings)
    body = urwid.ListBox(
        urwid.SimpleFocusListWalker([urwid.Text(row, wrap="clip") for row in rows])
    )
    close_button = urwid.Button("Close", on_press=on_close)
    content = urwid.Pile(
        [
            ("weight", 1, body),
            ("pack", urwid.Divider()),
            ("pack", urwid.Padding(close_button, align="center", width=14)),
        ]
    )
    dialog = urwid.AttrMap(urwid.LineBox(content, title=" Settings "), "dialog")
    return urwid.Overlay(
        top_w=dialog,
        bottom_w=base,
        align="center",
        width=("relative", 74),
        valign="middle",
        height=("relative", 68),
    )


def render_operation_plan_overlay(
    base: urwid.Widget,
    plan: OperationPlan,
    *,
    on_confirm: Callable[[urwid.Button], object] | None = None,
    on_cancel: Callable[[urwid.Button], object] | None = None,
) -> urwid.Widget:
    """Place an operation planning confirmation over the current application."""

    destination = plan.destination.label if plan.destination is not None else "n/a"
    expanded = "unknown" if plan.expanded_count is None else str(plan.expanded_count)
    bytes_text = "unknown" if plan.estimated_bytes is None else _format_bytes(plan.estimated_bytes)
    entry_rows = [
        urwid.Text(f" - {entry.name} ({entry.entry_type.value})", wrap="clip")
        for entry in plan.entries[:8]
    ]
    remaining = plan.direct_count - len(entry_rows)
    if remaining > 0:
        entry_rows.append(urwid.Text(f" - ... {remaining} more", wrap="clip"))
    phase_rows = [urwid.Text(f" - {phase}", wrap="clip") for phase in plan.destructive_phases]
    content = urwid.Pile(
        [
            ("pack", urwid.Text(f" Operation: {plan.kind.value}", wrap="clip")),
            ("pack", urwid.Text(f" Source panel: {plan.source_panel}", wrap="clip")),
            ("pack", urwid.Text(f" Destination: {destination}", wrap="clip")),
            ("pack", urwid.Text(f" Direct entries: {plan.direct_count}", wrap="clip")),
            ("pack", urwid.Text(f" Expanded entries: {expanded}", wrap="clip")),
            ("pack", urwid.Text(f" Estimated bytes: {bytes_text}", wrap="clip")),
            ("pack", urwid.Text(f" Expansion: {plan.expansion_state.value}", wrap="clip")),
            ("pack", urwid.Text(f" Conflicts: {plan.conflict_policy.value}", wrap="clip")),
            ("pack", urwid.Text(f" Resumability: {plan.resumability}", wrap="clip")),
            ("pack", urwid.Divider()),
            ("pack", urwid.Text(" Selected entries:", wrap="clip")),
            *[("pack", row) for row in entry_rows],
            ("pack", urwid.Divider()),
            ("pack", urwid.Text(" Destructive phases:", wrap="clip")),
            *[("pack", row) for row in phase_rows],
            ("pack", urwid.Divider()),
            (
                "pack",
                urwid.Columns(
                    [
                        ("given", 14, urwid.Button("Confirm", on_press=on_confirm)),
                        ("given", 14, urwid.Button("Cancel", on_press=on_cancel)),
                    ],
                    dividechars=2,
                ),
            ),
        ]
    )
    title = f" Confirm {plan.kind.value.title()} "
    dialog = urwid.AttrMap(urwid.LineBox(content, title=title), "dialog")
    return urwid.Overlay(
        top_w=dialog,
        bottom_w=base,
        align="center",
        width=("relative", 72),
        valign="middle",
        height="pack",
    )


def render_jobs_overlay(
    base: urwid.Widget,
    jobs: tuple[JobRecord, ...],
    items: tuple[JobItem, ...] = (),
    *,
    on_pause: Callable[[urwid.Button], object] | None = None,
    on_resume: Callable[[urwid.Button], object] | None = None,
    on_cancel: Callable[[urwid.Button], object] | None = None,
    on_retry: Callable[[urwid.Button], object] | None = None,
    on_close: Callable[[urwid.Button], object] | None = None,
) -> urwid.Widget:
    """Place the durable job monitor over the current application."""

    if jobs:
        job_rows = [
            _job_detail_row(job, items if index == 0 else ()) for index, job in enumerate(jobs)
        ]
    else:
        job_rows = [urwid.Text(" No durable jobs.")]
    content = urwid.Pile(
        [
            *[("pack", row) for row in job_rows],
            ("pack", urwid.Divider()),
            (
                "pack",
                urwid.Columns(
                    [
                        ("given", 13, urwid.Button("Pause", on_press=on_pause)),
                        ("given", 13, urwid.Button("Resume", on_press=on_resume)),
                        ("given", 13, urwid.Button("Cancel", on_press=on_cancel)),
                        ("given", 13, urwid.Button("Retry", on_press=on_retry)),
                        ("given", 13, urwid.Button("Close", on_press=on_close)),
                    ],
                    dividechars=1,
                ),
            ),
        ]
    )
    dialog = urwid.AttrMap(urwid.LineBox(content, title=" Jobs "), "dialog")
    return urwid.Overlay(
        top_w=dialog,
        bottom_w=base,
        align="center",
        width=("relative", 86),
        valign="middle",
        height=("relative", 76),
    )


def render_location_picker_overlay(
    base: urwid.Widget,
    sources: tuple[KnownSource, ...],
    target_panel: PanelId,
    *,
    on_select: Callable[[KnownSource], object] | None = None,
    on_close: Callable[[urwid.Button], object] | None = None,
) -> urwid.Widget:
    """Place a known-location picker over the current application."""

    rows: list[urwid.Widget]
    if sources:
        rows = [
            urwid.Button(source.label, on_press=_emit_source, user_data=(source, on_select))
            for source in sources
        ]
    else:
        rows = [urwid.Text("No known locations configured.")]
    close_button = urwid.Button("Close", on_press=on_close)
    content = urwid.Pile(
        [
            *rows,
            ("pack", urwid.Divider()),
            ("pack", urwid.Padding(close_button, align="center", width=14)),
        ]
    )
    dialog = urwid.AttrMap(
        urwid.LineBox(content, title=f" {target_panel.value.title()} Locations "),
        "dialog",
    )
    return urwid.Overlay(
        top_w=dialog,
        bottom_w=base,
        align="center",
        width=("relative", 72),
        valign="middle",
        height="pack",
    )


def _location_header(
    sources: tuple[KnownSource, ...],
    on_location_picker: Callable[[PanelId], object] | None,
    on_bucket_menu: Callable[[urwid.Button], object] | None,
    on_settings: Callable[[urwid.Button], object] | None,
) -> urwid.Widget:
    left_button = urwid.Button(
        "Left",
        on_press=_emit_location_picker,
        user_data=(PanelId.LEFT, on_location_picker),
    )
    right_button = urwid.Button(
        "Right",
        on_press=_emit_location_picker,
        user_data=(PanelId.RIGHT, on_location_picker),
    )
    bucket_button = urwid.Button("Bucket", on_press=on_bucket_menu)
    settings_button = urwid.Button("Settings", on_press=on_settings)
    return urwid.AttrMap(
        urwid.Columns(
            [
                ("pack", urwid.Text(" Locations ")),
                ("given", 12, left_button),
                ("given", 12, right_button),
                ("given", 14, bucket_button),
                ("given", 16, settings_button),
                ("weight", 1, urwid.Text(f" {len(sources)} known location(s)", wrap="clip")),
            ],
            dividechars=1,
        ),
        "footer_commands",
    )


def _emit_location_picker(
    _button: urwid.Button,
    user_data: tuple[PanelId, Callable[[PanelId], object] | None],
) -> None:
    panel_id, on_location_picker = user_data
    if on_location_picker is not None:
        on_location_picker(panel_id)


def _emit_source(
    _button: urwid.Button,
    user_data: tuple[KnownSource, Callable[[KnownSource], object] | None],
) -> None:
    source, on_select = user_data
    if on_select is not None:
        on_select(source)


def _command_footer(
    on_help: Callable[[urwid.Button], object] | None,
    on_command: Callable[[UiCommand], object] | None,
) -> urwid.Widget:
    buttons = [
        _command_button(label, UiCommand(command), on_help=on_help, on_command=on_command)
        for label, command in COMMAND_BUTTONS
    ]
    return urwid.AttrMap(
        urwid.GridFlow(
            buttons,
            cell_width=COMMAND_BUTTON_WIDTH,
            h_sep=1,
            v_sep=0,
            align="left",
        ),
        "footer_commands",
    )


def _command_button(
    label: str,
    command: UiCommand,
    *,
    on_help: Callable[[urwid.Button], object] | None,
    on_command: Callable[[UiCommand], object] | None,
) -> urwid.Button:
    if command is UiCommand.HELP and on_command is None:
        return urwid.Button(label, on_press=on_help)
    return urwid.Button(label, on_press=_emit_command, user_data=(command, on_command))


def _emit_command(
    _button: urwid.Button,
    user_data: tuple[UiCommand, Callable[[UiCommand], object] | None],
) -> None:
    command, on_command = user_data
    if on_command is not None:
        on_command(command)


def _emit_bucket_menu_command(
    _button: urwid.Button,
    user_data: tuple[str, Callable[[UiCommand], object] | None],
) -> None:
    command, on_command = user_data
    if on_command is not None:
        on_command(UiCommand(command))


def _settings_rows(settings: AppSettings) -> tuple[str, ...]:
    default_s3 = settings.profiles.default_s3
    named_profiles = ", ".join(profile.name for profile in settings.profiles.s3_profiles) or "none"
    upload_threshold = _format_bytes(settings.operations.multipart_upload_threshold)
    download_threshold = _format_bytes(settings.operations.multipart_download_threshold)
    return (
        f" Cache root: {settings.cache_root}",
        f" Default provider: {settings.provider.default_provider}",
        f" Index TTL: {settings.index.ttl_seconds}s",
        "",
        " S3 default profile:",
        f"   profile: {default_s3.profile_name or 'provider default'}",
        f"   region: {default_s3.region_name or 'provider default'}",
        f"   endpoint: {default_s3.endpoint_url or 'provider default'}",
        f" Named S3 profiles: {named_profiles}",
        "",
        " Operations:",
        f"   max listing concurrency: {settings.operations.max_listing_concurrency}",
        f"   max transfer concurrency: {settings.operations.max_transfer_concurrency}",
        f"   multipart upload threshold: {upload_threshold}",
        f"   multipart download threshold: {download_threshold}",
        f"   conflict behavior: {settings.operations.default_conflict_behavior}",
        f"   confirm destructive: {_format_bool(settings.operations.confirm_destructive)}",
        "",
        " UI:",
        f"   theme: {settings.ui.theme}",
        f"   keymap: {settings.ui.keymap}",
        f"   show hidden files: {_format_bool(settings.ui.show_hidden_files)}",
        f"   advanced metadata: {_format_bool(settings.ui.show_advanced_metadata)}",
    )


def _task_footer(tasks: tuple[TaskRecord, ...]) -> urwid.Widget:
    task = _visible_task(tasks)
    if task is None:
        return urwid.AttrMap(urwid.Text(" No active tasks", wrap="clip"), "task_footer")
    progress = task.progress
    percent = _progress_percent(task)
    detail = progress.current_item
    if len(detail) > TASK_DETAIL_MAX:
        detail = f"...{detail[-TASK_DETAIL_SUFFIX:]}"
    parts = [
        f" {task.task_type.value.title()}",
        task.status.value,
        percent,
        f"{progress.items_done}/{progress.items_total} items",
        f"{_format_bytes(progress.bytes_done)}/{_format_bytes(progress.bytes_total)}",
    ]
    if progress.message:
        parts.append(progress.message)
    if task.latest_error:
        parts.append(task.latest_error)
    if detail:
        parts.append(detail)
    return urwid.AttrMap(urwid.Text(" | ".join(parts), wrap="clip"), _task_footer_attr(task))


def _job_footer(
    jobs: tuple[JobRecord, ...],
    on_jobs: Callable[[urwid.Button], object] | None,
) -> urwid.Widget:
    job = _visible_job(jobs)
    if job is None:
        return urwid.AttrMap(urwid.Text(" No durable jobs", wrap="clip"), "task_footer")
    failed = _failed_item_count(job)
    total = job.plan.direct_count
    detail = (
        f" Jobs: {job.kind.value} {job.status.value} | {job.phase.value} | "
        f"{total} item{'' if total == 1 else 's'}"
    )
    if failed:
        detail = f"{detail} | {failed} failed"
    button = urwid.Button(detail, on_press=on_jobs)
    return urwid.AttrMap(button, _job_footer_attr(job))


def _visible_job(jobs: tuple[JobRecord, ...]) -> JobRecord | None:
    for job in reversed(jobs):
        if not job.status.is_terminal:
            return job
    if jobs:
        return jobs[-1]
    return None


def _job_footer_attr(job: JobRecord) -> str:
    if job.status is JobStatus.FAILED:
        return "error"
    return "task_footer"


def _job_detail_row(job: JobRecord, items: tuple[JobItem, ...]) -> urwid.Widget:
    failed = sum(item.status is JobItemStatus.FAILED for item in items)
    retry_count = sum(item.attempts for item in items)
    source = job.plan.entries[0].location.label if job.plan.entries else "n/a"
    destination = job.plan.destination.label if job.plan.destination is not None else "n/a"
    created = _format_datetime(job.created_at)
    lines = [
        f" {job.job_id} | {job.kind.value} | {job.status.value} | {job.phase.value}",
        f"   source: {source}",
        f"   destination: {destination}",
        f"   items: {job.plan.direct_count} direct, failed: {failed}, retries: {retry_count}",
        f"   created: {created}",
    ]
    if job.latest_error:
        lines.append(f"   error: {job.latest_error}")
    for item in items[:5]:
        suffix = f" ({item.latest_error})" if item.latest_error else ""
        lines.append(f"   - {item.name}: {item.status.value}, attempts={item.attempts}{suffix}")
    return urwid.Pile([("pack", urwid.Text(line, wrap="clip")) for line in lines])


def _failed_item_count(job: JobRecord) -> int:
    if job.status is not JobStatus.FAILED:
        return 0
    return sum(1 for entry in job.plan.entries if entry.name)


def _visible_task(tasks: tuple[TaskRecord, ...]) -> TaskRecord | None:
    for task in reversed(tasks):
        if not task.status.is_terminal:
            return task
    if tasks:
        return tasks[-1]
    return None


def _task_footer_attr(task: TaskRecord) -> str:
    if task.status is TaskState.FAILED:
        return "error"
    return "task_footer"


def _progress_percent(task: TaskRecord) -> str:
    progress = task.progress
    if progress.bytes_total:
        value = progress.bytes_done / progress.bytes_total
    elif progress.items_total:
        value = progress.items_done / progress.items_total
    elif task.status is TaskState.COMPLETED:
        value = 1
    else:
        return "--%"
    return f"{min(100, int(value * 100)):>3}%"


def _format_bytes(value: int) -> str:
    units = ("B", "K", "M", "G", "T")
    size = float(value)
    unit = units[0]
    for unit in units:
        if size < SIZE_STEP or unit == units[-1]:
            break
        size /= SIZE_STEP
    if unit == units[0]:
        return f"{int(size)}{unit}"
    return f"{size:.1f}{unit}"


def _format_bool(value: bool) -> str:
    return "yes" if value else "no"


def render_panel(
    panel: PanelState,
    *,
    title: str,
    panel_id: PanelId,
    is_focused: bool,
    on_mouse: PanelMouseHandler | None = None,
    focus_row: int | None = None,
) -> urwid.Widget:
    """Render a single file panel."""

    status = " loading" if panel.is_loading else ""
    filter_status = f" search:{panel.filter_text}" if panel.filter_text else ""
    sort_status = f" sort:{panel.sort_field.value}/{panel.sort_order.value[:3]}"
    header = urwid.AttrMap(
        urwid.Text(
            (
                f" {title}: {panel.location.label}{status}{filter_status}{sort_status} "
                f"[{len(panel.selected_entries)} marked]"
            ),
            wrap="clip",
        ),
        "panel_header",
    )
    rows = [
        _entry_row(
            entry,
            is_current=index == panel.cursor_index,
            is_marked=entry.uri in panel.selected_uris,
        )
        for index, entry in enumerate(panel.entries)
    ]
    if not rows:
        rows = [urwid.Text("  <empty>")]
    walker = urwid.SimpleFocusListWalker(rows)
    if panel.entries:
        walker.set_focus(panel.cursor_index)
    list_box = PanelListBox(
        walker,
        panel_id=panel_id,
        row_count=len(panel.entries),
        on_mouse=on_mouse,
        focus_row=focus_row,
    )
    body = urwid.AttrMap(list_box, "panel_body")
    panel_frame: urwid.Widget = urwid.Frame(body=body, header=header)
    border_attr = "panel_border_focus" if is_focused else "panel_border"
    return _bordered(panel_frame, border_attr)


def _entry_row(entry: Entry, *, is_current: bool, is_marked: bool) -> urwid.Widget:
    current = ">" if is_current else " "
    selected = "*" if is_marked else " "
    marker = _entry_marker(entry.entry_type)
    size = _format_size(entry)
    modified = _format_datetime(entry.modified_at)
    row = urwid.Text(
        f"{current}{selected} {marker} {entry.name:<38.38} {size:>10} {modified}",
        wrap="clip",
    )
    if is_current:
        return urwid.AttrMap(row, "entry_current")
    return row


def _bordered(widget: urwid.Widget, attr: str) -> urwid.Widget:
    top = _border_line("┌", "┐", attr)
    bottom = _border_line("└", "┘", attr)
    left = urwid.AttrMap(urwid.SolidFill("│"), attr)
    right = urwid.AttrMap(urwid.SolidFill("│"), attr)
    middle = urwid.Columns(
        [
            ("given", 1, left),
            ("weight", 1, widget),
            ("given", 1, right),
        ]
    )
    return urwid.Pile(
        [
            ("pack", top),
            ("weight", 1, middle),
            ("pack", bottom),
        ]
    )


def _border_line(left: str, right: str, attr: str) -> urwid.Widget:
    return urwid.AttrMap(
        urwid.Columns(
            [
                ("given", 1, urwid.Text(left)),
                ("weight", 1, urwid.Divider("─")),
                ("given", 1, urwid.Text(right)),
            ]
        ),
        attr,
    )


class PanelListBox(urwid.ListBox):
    """ListBox that lets application state own panel cursor movement."""

    def __init__(
        self,
        body: urwid.SimpleFocusListWalker[urwid.Widget],
        *,
        panel_id: PanelId,
        row_count: int,
        on_mouse: PanelMouseHandler | None = None,
        focus_row: int | None = None,
    ) -> None:
        super().__init__(body)
        self._panel_id = panel_id
        self._row_count = row_count
        self._on_mouse = on_mouse
        self._focus_row = focus_row

    def keypress(self, size: tuple[()] | tuple[int] | tuple[int, int], key: str) -> str | None:
        if key in {"up", "down"}:
            return key
        match size:
            case (width, height):
                return super().keypress((width, height), key)
            case _:
                return key

    def render(
        self,
        size: tuple[()] | tuple[int] | tuple[int, int],
        focus: bool = False,
    ) -> urwid.CompositeCanvas | urwid.SolidCanvas:
        match size:
            case (width, height):
                pass
            case _:
                msg = "PanelListBox render requires a two-dimensional size"
                raise ValueError(msg)
        if self._focus_row is not None:
            max_row = max(1, height - 1)
            relative = max(0, min(100, round((self._focus_row / max_row) * 100)))
            _widget, position = self._body.get_focus()  # type: ignore[no-untyped-call]
            if position is not None:
                self.set_focus(position)
                self.set_focus_valign(("relative", relative))
        return super().render((width, height), focus)

    def mouse_event(
        self,
        size: tuple[()] | tuple[int] | tuple[int, int],
        event: str,
        button: int,
        col: int,
        row: int,
        focus: bool,
    ) -> bool | None:
        if event == "mouse press" and button in {PRIMARY_MOUSE_BUTTON, SECONDARY_MOUSE_BUTTON}:
            _ = col, focus
            index = self._position_at_row(size, row)
            if self._on_mouse is not None:
                if button == SECONDARY_MOUSE_BUTTON:
                    self._on_mouse(
                        self._panel_id,
                        index,
                        toggle_mark=True,
                        viewport_row=row,
                    )
                else:
                    self._on_mouse(self._panel_id, index, viewport_row=row)
            return True
        match size:
            case (width, height):
                return super().mouse_event((width, height), event, button, col, row, focus)
            case _:
                return False

    def _position_at_row(
        self,
        size: tuple[()] | tuple[int] | tuple[int, int],
        row: int,
    ) -> int | None:
        match size:
            case (width, height):
                pass
            case _:
                return None
        middle, top, bottom = self.calculate_visible((width, height), focus=True)
        if middle is None or top is None or bottom is None:
            return None
        _offset, focus_widget, focus_pos, focus_rows, _cursor = middle
        trim_top, fill_above = top
        _trim_bottom, fill_below = bottom

        visible_rows = [
            *reversed(fill_above),
            (focus_widget, focus_pos, focus_rows),
            *fill_below,
        ]
        widget_row = -trim_top
        for _widget, position, row_count in visible_rows:
            if widget_row + row_count > row:
                if isinstance(position, int) and 0 <= position < self._row_count:
                    return position
                return None
            widget_row += row_count
        return None


def _entry_marker(entry_type: EntryType) -> str:
    if entry_type is EntryType.DIRECTORY:
        return "/"
    if entry_type is EntryType.PREFIX:
        return ">"
    if entry_type is EntryType.SYMLINK:
        return "@"
    if entry_type is EntryType.SPECIAL:
        return "?"
    return " "


def _format_size(entry: Entry) -> str:
    if entry.size is None:
        return ""
    units = ("B", "K", "M", "G", "T")
    size = float(entry.size)
    unit = units[0]
    for unit in units:
        if size < SIZE_STEP or unit == units[-1]:
            break
        size /= SIZE_STEP
    if unit == units[0]:
        return f"{int(size)}{unit}"
    return f"{size:.1f}{unit}"


def _format_datetime(value: datetime | None) -> str:
    if value is None:
        return ""
    return value.strftime("%Y-%m-%d %H:%M")
