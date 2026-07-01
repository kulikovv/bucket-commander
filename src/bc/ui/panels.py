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
