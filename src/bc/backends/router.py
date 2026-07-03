"""Provider router for dispatching operations to concrete backends."""

from __future__ import annotations

from bc.backends.base import Backend, BackendError, BackendErrorKind
from bc.core import Entry, Location, OperationResult
from bc.core.task_manager import ProgressSink


class BackendRouter(Backend):
    """Dispatch backend operations based on the location provider."""

    provider = "router"

    def __init__(self, backends: tuple[Backend, ...]) -> None:
        if not backends:
            msg = "BackendRouter requires at least one backend"
            raise ValueError(msg)
        self._backends = backends

    def supports(self, location: Location) -> bool:
        return any(backend.supports(location) for backend in self._backends)

    async def list(self, location: Location) -> tuple[Entry, ...]:
        return await self._backend_for(location).list(location)

    async def stat(self, location: Location) -> Entry:
        return await self._backend_for(location).stat(location)

    async def mkdir(self, location: Location, *, parents: bool = True) -> OperationResult:
        return await self._backend_for(location).mkdir(location, parents=parents)

    async def copy(
        self,
        source: Location,
        destination: Location,
        *,
        progress: ProgressSink | None = None,
    ) -> OperationResult:
        return await self._backend_for(source).copy(source, destination, progress=progress)

    async def move(self, source: Location, destination: Location) -> OperationResult:
        return await self._backend_for(source).move(source, destination)

    async def rename(self, source: Location, new_name: str) -> OperationResult:
        return await self._backend_for(source).rename(source, new_name)

    async def delete(
        self,
        location: Location,
        *,
        recursive: bool = False,
        progress: ProgressSink | None = None,
    ) -> OperationResult:
        return await self._backend_for(location).delete(
            location,
            recursive=recursive,
            progress=progress,
        )

    def _backend_for(self, location: Location) -> Backend:
        for backend in self._backends:
            if backend.supports(location):
                return backend
        msg = f"No backend supports {location.provider!r} locations"
        raise BackendError(BackendErrorKind.INVALID_LOCATION, msg, location=location)
