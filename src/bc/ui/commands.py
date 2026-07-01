"""Pure-ish UI commands for panel navigation and refresh."""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum

from bc.backends import Backend
from bc.core import EntryType, LocalLocation, PanelState


class PanelId(StrEnum):
    """Identifiers for the left and right panels."""

    LEFT = "left"
    RIGHT = "right"

    @property
    def other(self) -> PanelId:
        return PanelId.RIGHT if self is PanelId.LEFT else PanelId.LEFT


@dataclass(frozen=True, slots=True)
class TwoPanelState:
    """State for the two-panel application shell."""

    left: PanelState
    right: PanelState
    focused: PanelId = PanelId.LEFT
    status_message: str = "Ready"

    def panel(self, panel_id: PanelId) -> PanelState:
        return self.left if panel_id is PanelId.LEFT else self.right

    @property
    def active(self) -> PanelState:
        return self.panel(self.focused)

    def with_panel(self, panel_id: PanelId, panel: PanelState) -> TwoPanelState:
        if panel_id is PanelId.LEFT:
            return replace(self, left=panel)
        return replace(self, right=panel)

    def with_status(self, message: str) -> TwoPanelState:
        return replace(self, status_message=message)


def switch_focus(state: TwoPanelState) -> TwoPanelState:
    return replace(state, focused=state.focused.other)


def move_cursor(state: TwoPanelState, delta: int) -> TwoPanelState:
    panel = state.active.move_cursor(delta)
    return state.with_panel(state.focused, panel)


async def refresh(
    state: TwoPanelState,
    panel_id: PanelId,
    backend: Backend,
) -> TwoPanelState:
    panel = state.panel(panel_id)
    loading_panel = replace(panel, is_loading=True, status_message="Loading")
    loading_state = state.with_panel(panel_id, loading_panel)
    entries = await backend.list(panel.location)
    refreshed_panel = replace(
        loading_panel,
        entries=entries,
        cursor_index=min(loading_panel.cursor_index, max(0, len(entries) - 1)),
        is_loading=False,
        status_message=f"{len(entries)} entries",
    )
    return loading_state.with_panel(panel_id, refreshed_panel).with_status(
        f"{panel.location.label}: {len(entries)} entries"
    )


async def enter(state: TwoPanelState, backend: Backend) -> TwoPanelState:
    panel = state.active
    entry = panel.current_entry
    if entry is None:
        return state.with_status("No entry selected")
    if entry.entry_type not in {EntryType.DIRECTORY, EntryType.PREFIX}:
        return state.with_status(f"{entry.name} is not a directory")
    next_panel = PanelState(location=entry.location)
    next_state = state.with_panel(state.focused, next_panel)
    return await refresh(next_state, state.focused, backend)


async def go_parent(state: TwoPanelState, backend: Backend) -> TwoPanelState:
    panel = state.active
    location = panel.location
    if not isinstance(location, LocalLocation):
        return state.with_status("Parent navigation is not available for this location")
    parent = location.parent()
    if parent is None:
        return state.with_status("Already at filesystem root")
    next_state = state.with_panel(state.focused, PanelState(location=parent))
    return await refresh(next_state, state.focused, backend)
