"""Async local filesystem backend."""

from __future__ import annotations

import asyncio
import errno
import os
import shutil
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import TypeVar

from bc.backends.base import Backend, BackendError, BackendErrorKind
from bc.core import Entry, EntryType, LocalLocation, Location, OperationResult

T = TypeVar("T")


class LocalBackend(Backend):
    """Local filesystem backend exposed through non-blocking async methods."""

    provider = "file"

    def supports(self, location: Location) -> bool:
        return isinstance(location, LocalLocation)

    async def list(self, location: Location) -> tuple[Entry, ...]:
        local = self._require_local(location)
        return await self._run(lambda: self._list_sync(local))

    async def stat(self, location: Location) -> Entry:
        local = self._require_local(location)
        return await self._run(lambda: self._entry_for_path(local.path))

    async def mkdir(self, location: Location, *, parents: bool = True) -> OperationResult:
        local = self._require_local(location)
        return await self._run(lambda: self._mkdir_sync(local, parents=parents))

    async def copy(self, source: Location, destination: Location) -> OperationResult:
        source_local = self._require_local(source)
        destination_local = self._require_local(destination)
        return await self._run(lambda: self._copy_sync(source_local, destination_local))

    async def move(self, source: Location, destination: Location) -> OperationResult:
        source_local = self._require_local(source)
        destination_local = self._require_local(destination)
        return await self._run(lambda: self._move_sync(source_local, destination_local))

    async def rename(self, source: Location, new_name: str) -> OperationResult:
        source_local = self._require_local(source)
        if not new_name or "/" in new_name or os.sep in new_name:
            msg = f"Invalid local entry name: {new_name!r}"
            raise BackendError(BackendErrorKind.INVALID_LOCATION, msg, location=source_local)
        destination = LocalLocation(source_local.path.with_name(new_name))
        return await self.move(source_local, destination)

    async def delete(self, location: Location, *, recursive: bool = False) -> OperationResult:
        local = self._require_local(location)
        return await self._run(lambda: self._delete_sync(local, recursive=recursive))

    def _require_local(self, location: Location) -> LocalLocation:
        if isinstance(location, LocalLocation):
            return location
        msg = f"Local backend does not support {location.provider!r} locations"
        raise BackendError(BackendErrorKind.INVALID_LOCATION, msg, location=location)

    async def _run(self, func: Callable[[], T]) -> T:
        try:
            return await asyncio.to_thread(func)
        except BackendError:
            raise
        except OSError as error:
            raise _backend_error(error) from error

    def _list_sync(self, location: LocalLocation) -> tuple[Entry, ...]:
        if not location.path.exists():
            raise _backend_error(FileNotFoundError(errno.ENOENT, "No such file", location.path))
        if not location.path.is_dir():
            raise _backend_error(
                NotADirectoryError(errno.ENOTDIR, "Not a directory", location.path)
            )
        entries = [self._entry_for_path(path) for path in location.path.iterdir()]
        return tuple(
            sorted(entries, key=lambda entry: (not entry.is_container, entry.name.casefold()))
        )

    def _entry_for_path(self, path: Path) -> Entry:
        stat_result = path.lstat()
        entry_type = _entry_type(path)
        size = stat_result.st_size if entry_type in {EntryType.FILE, EntryType.SYMLINK} else None
        return Entry(
            location=LocalLocation(path.resolve()),
            name=path.name or path.anchor,
            entry_type=entry_type,
            size=size,
            modified_at=datetime.fromtimestamp(stat_result.st_mtime, tz=UTC),
        )

    def _mkdir_sync(self, location: LocalLocation, *, parents: bool) -> OperationResult:
        location.path.mkdir(parents=parents, exist_ok=False)
        return OperationResult.success(
            f"Created {location.path}",
            destination=location,
            entries_affected=1,
        )

    def _copy_sync(self, source: LocalLocation, destination: LocalLocation) -> OperationResult:
        if not source.path.exists():
            raise _backend_error(FileNotFoundError(errno.ENOENT, "No such file", source.path))
        target = _resolve_target(source.path, destination.path)
        entries, bytes_total = _tree_totals(source.path)
        if source.path.is_dir() and not source.path.is_symlink():
            shutil.copytree(source.path, target, dirs_exist_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source.path, target)
        return OperationResult.success(
            f"Copied {source.path} to {target}",
            source=source,
            destination=LocalLocation(target.resolve()),
            entries_affected=entries,
            bytes_affected=bytes_total,
        )

    def _move_sync(self, source: LocalLocation, destination: LocalLocation) -> OperationResult:
        if not source.path.exists():
            raise _backend_error(FileNotFoundError(errno.ENOENT, "No such file", source.path))
        target = _resolve_target(source.path, destination.path)
        entries, bytes_total = _tree_totals(source.path)
        target.parent.mkdir(parents=True, exist_ok=True)
        moved_path = Path(shutil.move(str(source.path), str(target)))
        return OperationResult.success(
            f"Moved {source.path} to {moved_path}",
            source=source,
            destination=LocalLocation(moved_path.resolve()),
            entries_affected=entries,
            bytes_affected=bytes_total,
        )

    def _delete_sync(self, location: LocalLocation, *, recursive: bool) -> OperationResult:
        if not location.path.exists() and not location.path.is_symlink():
            raise _backend_error(FileNotFoundError(errno.ENOENT, "No such file", location.path))
        entries, bytes_total = _tree_totals(location.path)
        if location.path.is_dir() and not location.path.is_symlink():
            if not recursive:
                raise BackendError(
                    BackendErrorKind.IS_A_DIRECTORY,
                    f"Directory delete requires recursive=True: {location.path}",
                    location=location,
                )
            shutil.rmtree(location.path)
        else:
            location.path.unlink()
        return OperationResult.success(
            f"Deleted {location.path}",
            source=location,
            entries_affected=entries,
            bytes_affected=bytes_total,
        )


def _entry_type(path: Path) -> EntryType:
    if path.is_symlink():
        return EntryType.SYMLINK
    if path.is_dir():
        return EntryType.DIRECTORY
    if path.is_file():
        return EntryType.FILE
    return EntryType.SPECIAL


def _tree_totals(path: Path) -> tuple[int, int]:
    if path.is_dir() and not path.is_symlink():
        entries = 1
        bytes_total = 0
        for child in path.rglob("*"):
            entries += 1
            if child.is_file() and not child.is_symlink():
                bytes_total += child.stat().st_size
        return entries, bytes_total
    size = path.lstat().st_size if path.exists() or path.is_symlink() else 0
    return 1, size


def _resolve_target(source: Path, destination: Path) -> Path:
    if destination.exists() and destination.is_dir():
        return destination / source.name
    return destination


def _backend_error(error: OSError) -> BackendError:
    kind = _error_kind(error)
    filename = error.filename or ""
    message = error.strerror or str(error)
    if filename:
        message = f"{message}: {filename}"
    return BackendError(kind, message)


def _error_kind(error: OSError) -> BackendErrorKind:
    if isinstance(error, FileNotFoundError):
        return BackendErrorKind.NOT_FOUND
    if isinstance(error, PermissionError):
        return BackendErrorKind.PERMISSION_DENIED
    if isinstance(error, FileExistsError):
        return BackendErrorKind.ALREADY_EXISTS
    if isinstance(error, NotADirectoryError):
        return BackendErrorKind.NOT_A_DIRECTORY
    if isinstance(error, IsADirectoryError):
        return BackendErrorKind.IS_A_DIRECTORY
    return BackendErrorKind.IO_ERROR
