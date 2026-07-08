"""Operation planning values used before potentially destructive work starts."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum

from bc.core import Entry, EntryType, Location


class OperationKind(StrEnum):
    """Operations that require an explicit safety plan."""

    COPY = "copy"
    DELETE = "delete"
    MOVE = "move"


class ExpansionState(StrEnum):
    """How complete a plan's item and byte expansion is."""

    COMPLETE = "complete"
    PARTIAL = "partial"
    STALE = "stale"
    UNKNOWN = "unknown"


class ConflictPolicy(StrEnum):
    """Destination conflict behavior advertised by the plan."""

    BACKEND_DEFAULT = "backend default"
    FAIL_IF_EXISTS = "fail if exists"
    OVERWRITE = "overwrite"


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
class IndexedPlanEstimate:
    """Item and byte estimate derived from persisted bucket metadata."""

    expanded_count: int
    estimated_bytes: int
    expansion_state: ExpansionState = ExpansionState.COMPLETE

    def __post_init__(self) -> None:
        if self.expanded_count < 0:
            msg = "expanded_count must be non-negative"
            raise ValueError(msg)
        if self.estimated_bytes < 0:
            msg = "estimated_bytes must be non-negative"
            raise ValueError(msg)
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


def plan_copy(
    entries: Sequence[Entry],
    *,
    source_panel: str,
    destination: Location,
    indexed_estimates: Mapping[str, IndexedPlanEstimate] | None = None,
) -> OperationPlan:
    """Create a durable plan for copying selected entries."""

    planned_entries = _plan_entries(entries)
    expansion_state = _expansion_state(planned_entries)
    plan = OperationPlan(
        kind=OperationKind.COPY,
        entries=planned_entries,
        source_panel=source_panel,
        destination=destination,
        expanded_count=_expanded_count(planned_entries, expansion_state),
        estimated_bytes=_estimated_bytes(planned_entries, expansion_state),
        expansion_state=expansion_state,
        destructive_phases=(),
    )
    return apply_indexed_estimates(plan, indexed_estimates or {})


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
    indexed_estimates: Mapping[str, IndexedPlanEstimate] | None = None,
) -> OperationPlan:
    """Create a confirmation plan for moving selected entries."""

    planned_entries = _plan_entries(entries)
    expansion_state = _expansion_state(planned_entries)
    plan = OperationPlan(
        kind=OperationKind.MOVE,
        entries=planned_entries,
        source_panel=source_panel,
        destination=destination,
        expanded_count=_expanded_count(planned_entries, expansion_state),
        estimated_bytes=_estimated_bytes(planned_entries, expansion_state),
        expansion_state=expansion_state,
        destructive_phases=("copy to destination", "verify copy", "delete source after verify"),
    )
    return apply_indexed_estimates(plan, indexed_estimates or {})


def apply_indexed_estimates(
    plan: OperationPlan,
    estimates: Mapping[str, IndexedPlanEstimate],
) -> OperationPlan:
    """Return `plan` with indexed count and byte estimates folded in when present."""

    if not estimates:
        return plan
    matched = tuple(
        estimates[entry.location.uri]
        for entry in plan.entries
        if entry.location.uri in estimates
    )
    if not matched:
        return plan
    known_count = sum(estimate.expanded_count for estimate in matched)
    known_bytes = sum(estimate.estimated_bytes for estimate in matched)
    unestimated_entries = tuple(
        entry for entry in plan.entries if entry.location.uri not in estimates
    )
    direct_count = sum(1 for entry in unestimated_entries if not entry.is_container)
    direct_bytes = tuple(entry.size for entry in unestimated_entries if not entry.is_container)
    has_unknown_entry = any(entry.is_container for entry in unestimated_entries) or any(
        size is None for size in direct_bytes
    )
    if has_unknown_entry:
        expansion_state = _least_complete_state(
            (plan.expansion_state, *(estimate.expansion_state for estimate in matched))
        )
        return _replace_plan_estimate(
            plan,
            expanded_count=None,
            estimated_bytes=None,
            expansion_state=expansion_state,
        )
    expansion_state = _least_complete_state(estimate.expansion_state for estimate in matched)
    return _replace_plan_estimate(
        plan,
        expanded_count=known_count + direct_count,
        estimated_bytes=known_bytes + sum(size or 0 for size in direct_bytes),
        expansion_state=expansion_state,
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


def _replace_plan_estimate(
    plan: OperationPlan,
    *,
    expanded_count: int | None,
    estimated_bytes: int | None,
    expansion_state: ExpansionState,
) -> OperationPlan:
    return OperationPlan(
        kind=plan.kind,
        entries=plan.entries,
        source_panel=plan.source_panel,
        destination=plan.destination,
        expanded_count=expanded_count,
        estimated_bytes=estimated_bytes,
        expansion_state=expansion_state,
        conflict_policy=plan.conflict_policy,
        destructive_phases=plan.destructive_phases,
        resumability=plan.resumability,
    )


def _least_complete_state(states: Iterable[ExpansionState]) -> ExpansionState:
    order = {
        ExpansionState.COMPLETE: 0,
        ExpansionState.STALE: 1,
        ExpansionState.PARTIAL: 2,
        ExpansionState.UNKNOWN: 3,
    }
    return max(states, key=lambda state: order[state])
