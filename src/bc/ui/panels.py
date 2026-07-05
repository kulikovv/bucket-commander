"""Urwid widgets for the two-panel file interface."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from enum import StrEnum

import urwid

from bc.config import KnownSource
from bc.core import Entry, EntryType, PanelState
from bc.core.task_manager import TaskRecord, TaskState
from bc.ui.commands import PanelId, TwoPanelState

SIZE_STEP = 1024.0
TASK_DETAIL_MAX = 52
TASK_DETAIL_SUFFIX = 49
COMMAND_BUTTON_WIDTH = 14
COMMAND_BUTTONS = (
    ("Help", "help"),
    ("View", "view"),
    ("New File", "new_file"),
    ("Copy", "copy"),
    ("Move", "move"),
    ("New Folder", "new_folder"),
    ("Delete", "delete"),
    ("Cancel", "cancel"),
    ("Quit", "quit"),
)
HELP_COMMANDS = (
    ("F1 or ?", "Show this help"),
    ("Tab", "Switch active panel"),
    ("Up/Down", "Move cursor"),
    ("Enter, Space, Right", "Open directory or prefix"),
    ("Backspace, Left", "Go to parent"),
    ("S", "Toggle selection"),
    ("R or Ctrl-R", "Refresh active panel"),
    ("F3", "View selected entry"),
    ("F4", "Create a new file"),
    ("F5", "Copy selected entries"),
    ("F6", "Move selected entries"),
    ("F7", "Create a new folder"),
    ("F8 or Delete", "Delete selected entries"),
    ("C", "Cancel active task"),
    ("Q", "Quit Bucket Commander"),
)


class UiCommand(StrEnum):
    """Commands exposed by clickable footer buttons."""

    HELP = "help"
    VIEW = "view"
    NEW_FILE = "new_file"
    SELECT = "select"
    REFRESH = "refresh"
    COPY = "copy"
    MOVE = "move"
    NEW_FOLDER = "new_folder"
    DELETE = "delete"
    CANCEL = "cancel"
    QUIT = "quit"


def render_app(
    state: TwoPanelState,
    *,
    sources: tuple[KnownSource, ...] = (),
    tasks: tuple[TaskRecord, ...] = (),
    on_help: Callable[[urwid.Button], object] | None = None,
    on_command: Callable[[UiCommand], object] | None = None,
    on_location_picker: Callable[[PanelId], object] | None = None,
) -> urwid.Widget:
    """Render the full two-panel application."""

    left = render_panel(state.left, title="Left", is_focused=state.focused is PanelId.LEFT)
    right = render_panel(state.right, title="Right", is_focused=state.focused is PanelId.RIGHT)
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
            ("pack", urwid.AttrMap(urwid.Text(state.status_message, wrap="clip"), "footer")),
        ]
    )
    return urwid.Frame(
        body=body,
        header=_location_header(sources, on_location_picker),
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
    return urwid.AttrMap(
        urwid.Columns(
            [
                ("pack", urwid.Text(" Locations ")),
                ("given", 12, left_button),
                ("given", 12, right_button),
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


def render_panel(panel: PanelState, *, title: str, is_focused: bool) -> urwid.Widget:
    """Render a single file panel."""

    status = " loading" if panel.is_loading else ""
    header = urwid.AttrMap(
        urwid.Text(
            f" {title}: {panel.location.label}{status} [{len(panel.selected_entries)} marked]",
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
    list_box = PanelListBox(walker)
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

    def keypress(self, size: tuple[()] | tuple[int] | tuple[int, int], key: str) -> str | None:
        if key in {"up", "down"}:
            return key
        match size:
            case (width, height):
                return super().keypress((width, height), key)
            case _:
                return key


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
