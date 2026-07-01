"""Urwid widgets for the two-panel file interface."""

from __future__ import annotations

from datetime import datetime

import urwid

from bc.core import Entry, EntryType, PanelState
from bc.ui.commands import PanelId, TwoPanelState

SIZE_STEP = 1024.0


def render_app(state: TwoPanelState) -> urwid.Widget:
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
    footer = urwid.AttrMap(
        urwid.Text(state.status_message, wrap="clip"),
        "footer",
    )
    return urwid.Frame(body=body, footer=footer)


def render_panel(panel: PanelState, *, title: str, is_focused: bool) -> urwid.Widget:
    """Render a single file panel."""

    header_attr = "panel_header_focus" if is_focused else "panel_header"
    status = " loading" if panel.is_loading else ""
    header = urwid.AttrMap(
        urwid.Text(f" {title}: {panel.location.label}{status}", wrap="clip"),
        header_attr,
    )
    rows = [_entry_row(entry) for entry in panel.entries]
    if not rows:
        rows = [urwid.Text("  <empty>")]
    walker = urwid.SimpleFocusListWalker(rows)
    if panel.entries:
        walker.set_focus(panel.cursor_index)
    list_box = urwid.ListBox(walker)
    body = urwid.AttrMap(list_box, "panel_body", "entry_focus")
    return urwid.Frame(body=body, header=header)


def _entry_row(entry: Entry) -> urwid.Widget:
    marker = _entry_marker(entry.entry_type)
    size = _format_size(entry)
    modified = _format_datetime(entry.modified_at)
    return urwid.Text(f" {marker} {entry.name:<40.40} {size:>10} {modified}", wrap="clip")


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
