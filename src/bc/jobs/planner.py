"""Operation planning values used before potentially destructive work starts."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

from bc.core import Entry, EntryType, Location


class OperationKind(StrEnum):
    """Operations that require an explicit safety plan."""

    DELETE = "delete"
    MOVE = "move"


class ExpansionState(StrEnum):
    """How complete a plan's item and byte expansion is."""

    COMPLETE = "complete"
    UNKNOWN = "unknown"


class ConflictPolicy(StrEnum):
    """Destination conflict behavior advertised by the plan."""

    BACKEND_DEFAULT = "backend default"


@dataclass(frozen=True, slots=True)
class OperationPlanEntry:
    """One direct user-selected entry in an operation plan."""

    name: str
    location: Location
    entry_type: EntryType
    size: int | None = None

    @property
    def is_container(self) -> bool:
        return self.entry_type.is_container


@dataclass(frozen=True, slots=True)
class OperationPlan:
    """Human-confirmable scope for a pending operation."""

    kind: OperationKind
    entries: tuple[OperationPlanEntry, ...]
    source_panel: str
    destination: Location | None = None
    expanded_count: int | None = None
    estimated_bytes: int | None = None
    expansion_state: ExpansionState = ExpansionState.COMPLETE
    conflict_policy: ConflictPolicy = ConflictPolicy.BACKEND_DEFAULT
    destructive_phases: tuple[str, ...] = ()
    resumability: str = "Short-lived task; retry by running the operation again."

    @property
    def direct_count(self) -> int:
        return len(self.entries)

    @property
    def requires_confirmation(self) -> bool:
        return self.kind in {OperationKind.DELETE, OperationKind.MOVE}


def plan_delete(entries: Sequence[Entry], *, source_panel: str) -> OperationPlan:
    """Create a confirmation plan for deleting selected entries."""

    planned_entries = _plan_entries(entries)
    expansion_state = _expansion_state(planned_entries)
    return OperationPlan(
        kind=OperationKind.DELETE,
        entries=planned_entries,
        source_panel=source_panel,
        expanded_count=_expanded_count(planned_entries, expansion_state),
        estimated_bytes=_estimated_bytes(planned_entries, expansion_state),
        expansion_state=expansion_state,
        destructive_phases=("delete selected entries",),
    )


def plan_move(
    entries: Sequence[Entry],
    *,
    source_panel: str,
    destination: Location,
) -> OperationPlan:
    """Create a confirmation plan for moving selected entries."""

    planned_entries = _plan_entries(entries)
    expansion_state = _expansion_state(planned_entries)
    return OperationPlan(
        kind=OperationKind.MOVE,
        entries=planned_entries,
        source_panel=source_panel,
        destination=destination,
        expanded_count=_expanded_count(planned_entries, expansion_state),
        estimated_bytes=_estimated_bytes(planned_entries, expansion_state),
        expansion_state=expansion_state,
        destructive_phases=("copy to destination", "verify copy", "delete source after verify"),
    )


def _plan_entries(entries: Sequence[Entry]) -> tuple[OperationPlanEntry, ...]:
    return tuple(
        OperationPlanEntry(
            name=entry.name,
            location=entry.location,
            entry_type=entry.entry_type,
            size=entry.size,
        )
        for entry in entries
        if entry.name != ".."
    )


def _expansion_state(entries: tuple[OperationPlanEntry, ...]) -> ExpansionState:
    if any(entry.is_container for entry in entries):
        return ExpansionState.UNKNOWN
    return ExpansionState.COMPLETE


def _expanded_count(
    entries: tuple[OperationPlanEntry, ...],
    expansion_state: ExpansionState,
) -> int | None:
    if expansion_state is ExpansionState.UNKNOWN:
        return None
    return len(entries)


def _estimated_bytes(
    entries: tuple[OperationPlanEntry, ...],
    expansion_state: ExpansionState,
) -> int | None:
    if expansion_state is ExpansionState.UNKNOWN or any(entry.size is None for entry in entries):
        return None
    return sum(entry.size or 0 for entry in entries)
