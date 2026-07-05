"""Backend contracts for local and bucket storage providers."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import StrEnum

from bc.core import Entry, Location, OperationResult
from bc.core.task_manager import ProgressSink


class BackendErrorKind(StrEnum):
    """Stable categories for backend failures shown to users or jobs."""

    INVALID_LOCATION = "invalid_location"
    NOT_FOUND = "not_found"
    ALREADY_EXISTS = "already_exists"
    PERMISSION_DENIED = "permission_denied"
    NOT_A_DIRECTORY = "not_a_directory"
    IS_A_DIRECTORY = "is_a_directory"
    UNSUPPORTED = "unsupported"
    IO_ERROR = "io_error"


class BackendError(Exception):
    """A backend failure with a normalized kind and optional source locations."""

    def __init__(
        self,
        kind: BackendErrorKind,
        message: str,
        *,
        location: Location | None = None,
        destination: Location | None = None,
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.location = location
        self.destination = destination


@dataclass(frozen=True, slots=True)
class PreviewResult:
    """Small content preview returned by storage backends."""

    data: bytes
    truncated: bool = False


class Backend(ABC):
    """Async storage backend interface consumed by UI, task, and job layers."""

    provider: str

    @abstractmethod
    def supports(self, location: Location) -> bool:
        """Return whether this backend can handle the location."""

    @abstractmethod
    async def list(self, location: Location) -> tuple[Entry, ...]:
        """List immediate children for a container location."""

    @abstractmethod
    async def stat(self, location: Location) -> Entry:
        """Return metadata for a single location."""

    @abstractmethod
    async def preview(self, location: Location, *, max_bytes: int) -> PreviewResult:
        """Return up to `max_bytes` for displaying one file or object."""

    @abstractmethod
    async def mkdir(self, location: Location, *, parents: bool = True) -> OperationResult:
        """Create a directory or prefix."""

    @abstractmethod
    async def copy(
        self,
        source: Location,
        destination: Location,
        *,
        progress: ProgressSink | None = None,
    ) -> OperationResult:
        """Copy source to destination."""

    @abstractmethod
    async def move(self, source: Location, destination: Location) -> OperationResult:
        """Move source to destination."""

    @abstractmethod
    async def rename(self, source: Location, new_name: str) -> OperationResult:
        """Rename source inside its current parent."""

    @abstractmethod
    async def delete(
        self,
        location: Location,
        *,
        recursive: bool = False,
        progress: ProgressSink | None = None,
    ) -> OperationResult:
        """Delete a file, object, directory, or prefix."""
