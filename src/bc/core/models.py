"""Shared domain models used by UI, backends, indexes, and jobs."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Self

from bc.core.locations import Location


class EntryType(StrEnum):
    """Visible entry types across local and bucket backends."""

    FILE = "file"
    DIRECTORY = "directory"
    OBJECT = "object"
    PREFIX = "prefix"
    SYMLINK = "symlink"
    SPECIAL = "special"

    @property
    def is_container(self) -> bool:
        return self in {EntryType.DIRECTORY, EntryType.PREFIX}


class SortField(StrEnum):
    """Fields that panels may sort by."""

    NAME = "name"
    TYPE = "type"
    SIZE = "size"
    MODIFIED_AT = "modified_at"


class SortOrder(StrEnum):
    """Panel sort direction."""

    ASCENDING = "ascending"
    DESCENDING = "descending"


@dataclass(frozen=True, slots=True)
class Entry:
    """A file, directory, object, prefix, or special row shown in a panel."""

    location: Location
    name: str
    entry_type: EntryType
    size: int | None = None
    modified_at: datetime | None = None
    etag: str | None = None
    metadata: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.name:
            msg = "Entry name must not be empty"
            raise ValueError(msg)
        if self.size is not None and self.size < 0:
            msg = f"Entry size must be non-negative: {self.size}"
            raise ValueError(msg)
        if not isinstance(self.entry_type, EntryType):
            object.__setattr__(self, "entry_type", EntryType(self.entry_type))
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))

    @property
    def uri(self) -> str:
        return self.location.uri

    @property
    def is_container(self) -> bool:
        return self.entry_type.is_container


@dataclass(frozen=True, slots=True)
class OperationResult:
    """Result returned by backend operations and later task/job layers."""

    ok: bool
    message: str = ""
    source: Location | None = None
    destination: Location | None = None
    entries_affected: int = 0
    bytes_affected: int = 0

    def __post_init__(self) -> None:
        if self.entries_affected < 0:
            msg = "entries_affected must be non-negative"
            raise ValueError(msg)
        if self.bytes_affected < 0:
            msg = "bytes_affected must be non-negative"
            raise ValueError(msg)

    @classmethod
    def success(
        cls,
        message: str = "",
        *,
        source: Location | None = None,
        destination: Location | None = None,
        entries_affected: int = 0,
        bytes_affected: int = 0,
    ) -> Self:
        return cls(
            ok=True,
            message=message,
            source=source,
            destination=destination,
            entries_affected=entries_affected,
            bytes_affected=bytes_affected,
        )

    @classmethod
    def failure(
        cls,
        message: str,
        *,
        source: Location | None = None,
        destination: Location | None = None,
        entries_affected: int = 0,
        bytes_affected: int = 0,
    ) -> Self:
        return cls(
            ok=False,
            message=message,
            source=source,
            destination=destination,
            entries_affected=entries_affected,
            bytes_affected=bytes_affected,
        )


@dataclass(frozen=True, slots=True)
class PanelState:
    """Pure state for one file-manager panel."""

    location: Location
    entries: tuple[Entry, ...] = ()
    cursor_index: int = 0
    selected_uris: frozenset[str] = frozenset()
    sort_field: SortField = SortField.NAME
    sort_order: SortOrder = SortOrder.ASCENDING
    filter_text: str = ""
    is_loading: bool = False
    status_message: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.sort_field, SortField):
            object.__setattr__(self, "sort_field", SortField(self.sort_field))
        if not isinstance(self.sort_order, SortOrder):
            object.__setattr__(self, "sort_order", SortOrder(self.sort_order))
        max_index = max(0, len(self.entries) - 1)
        clamped_index = min(max(self.cursor_index, 0), max_index)
        object.__setattr__(self, "cursor_index", clamped_index)
        selected = frozenset(uri for uri in self.selected_uris if uri in self.entry_uris)
        object.__setattr__(self, "selected_uris", selected)

    @property
    def entry_uris(self) -> frozenset[str]:
        return frozenset(entry.uri for entry in self.entries)

    @property
    def current_entry(self) -> Entry | None:
        if not self.entries:
            return None
        return self.entries[self.cursor_index]

    @property
    def selected_entries(self) -> tuple[Entry, ...]:
        return tuple(entry for entry in self.entries if entry.uri in self.selected_uris)

    def with_entries(self, entries: tuple[Entry, ...]) -> PanelState:
        return replace(self, entries=entries)

    def move_cursor(self, delta: int) -> PanelState:
        return replace(self, cursor_index=self.cursor_index + delta)

    def toggle_selection(self, entry: Entry | None = None) -> PanelState:
        target = entry or self.current_entry
        if target is None:
            return self
        selected = set(self.selected_uris)
        if target.uri in selected:
            selected.remove(target.uri)
        else:
            selected.add(target.uri)
        return replace(self, selected_uris=frozenset(selected))
