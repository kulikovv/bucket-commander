"""Cross-provider transfer backend for local and S3 locations."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import SupportsInt

from bc.backends.base import Backend, BackendError, BackendErrorKind, PreviewResult
from bc.backends.local import LocalBackend
from bc.backends.s3 import S3Backend, S3Client, S3ClientFactory
from bc.core import Entry, LocalLocation, Location, OperationResult, S3Location
from bc.core.task_manager import ProgressSink


class TransferBackend(Backend):
    """Copy and move entries between the local filesystem and S3."""

    provider = "transfer"

    def __init__(
        self,
        *,
        s3_backend: S3Backend | None = None,
        s3_client_factory: S3ClientFactory | None = None,
    ) -> None:
        self._s3 = s3_backend or S3Backend(client_factory=s3_client_factory)

    def supports(self, location: Location) -> bool:
        return isinstance(location, LocalLocation | S3Location)

    async def list(self, location: Location) -> tuple[Entry, ...]:
        raise _unsupported("Transfer backend does not list locations", location)

    async def stat(self, location: Location) -> Entry:
        raise _unsupported("Transfer backend does not stat locations", location)

    async def preview(self, location: Location, *, max_bytes: int) -> PreviewResult:
        _ = max_bytes
        raise _unsupported("Transfer backend does not preview locations", location)

    async def mkdir(self, location: Location, *, parents: bool = True) -> OperationResult:
        _ = parents
        raise _unsupported("Transfer backend does not create locations", location)

    async def copy(
        self,
        source: Location,
        destination: Location,
        *,
        progress: ProgressSink | None = None,
    ) -> OperationResult:
        if isinstance(source, LocalLocation) and isinstance(destination, S3Location):
            return await self._copy_local_to_s3(source, destination, progress)
        if isinstance(source, S3Location) and isinstance(destination, LocalLocation):
            return await self._copy_s3_to_local(source, destination, progress)
        raise _unsupported("Transfer backend only supports local/S3 transfers", source, destination)

    async def move(self, source: Location, destination: Location) -> OperationResult:
        result = await self.copy(source, destination)
        if isinstance(source, LocalLocation):
            delete_result = await LocalBackend().delete(source, recursive=True)
        elif isinstance(source, S3Location):
            delete_result = await self._s3.delete(source, recursive=True)
        else:
            raise _unsupported(
                "Transfer backend only supports local/S3 transfers",
                source,
                destination,
            )
        return OperationResult.success(
            f"Moved {source.uri} to {destination.uri}",
            source=source,
            destination=result.destination,
            entries_affected=max(result.entries_affected, delete_result.entries_affected),
            bytes_affected=result.bytes_affected,
        )

    async def rename(self, source: Location, new_name: str) -> OperationResult:
        _ = new_name
        raise _unsupported("Transfer backend does not rename locations", source)

    async def delete(
        self,
        location: Location,
        *,
        recursive: bool = False,
        progress: ProgressSink | None = None,
    ) -> OperationResult:
        _ = recursive, progress
        raise _unsupported("Transfer backend does not delete locations", location)

    async def _copy_local_to_s3(
        self,
        source: LocalLocation,
        destination: S3Location,
        progress: ProgressSink | None,
    ) -> OperationResult:
        if not source.path.exists() and not source.path.is_symlink():
            msg = f"No such file: {source.path}"
            raise BackendError(BackendErrorKind.NOT_FOUND, msg, location=source)
        files = await asyncio.to_thread(_local_files, source.path)
        bytes_total = sum(size for _, _, size in files)
        _progress_update(
            progress,
            items_total=len(files),
            bytes_total=bytes_total,
            current_item=str(source.path),
            message=f"Uploading {source.path.name}",
        )
        async with self._s3._client(destination) as client:
            for path, relative_key, size in files:
                _progress_raise_if_cancelled(progress)
                key = _join_s3_key(destination.prefix, relative_key)
                _progress_update(progress, current_item=f"{path} -> s3://{destination.bucket}/{key}")
                await _upload_file(client, path, destination.bucket, key)
                _progress_advance(progress, items=1, bytes_count=size)
        return OperationResult.success(
            f"Copied {source.uri} to {destination.uri}",
            source=source,
            destination=destination,
            entries_affected=len(files),
            bytes_affected=bytes_total,
        )

    async def _copy_s3_to_local(
        self,
        source: S3Location,
        destination: LocalLocation,
        progress: ProgressSink | None,
    ) -> OperationResult:
        if not source.prefix:
            raise _unsupported(
                "S3 bucket root copy requires a selected prefix or object",
                source,
                destination,
            )
        objects = await self._s3_objects(source)
        if not objects:
            raise BackendError(
                BackendErrorKind.NOT_FOUND,
                f"No objects found at {source.uri}",
                location=source,
            )
        bytes_total = sum(size for _, size in objects)
        _progress_update(
            progress,
            items_total=len(objects),
            bytes_total=bytes_total,
            current_item=source.uri,
            message=f"Downloading {source.name}",
        )
        target_root = await asyncio.to_thread(_resolve_local_destination, source, destination.path)
        async with self._s3._client(source) as client:
            for key, size in objects:
                _progress_raise_if_cancelled(progress)
                target = target_root / _relative_s3_download_path(source, key)
                _progress_update(progress, current_item=f"s3://{source.bucket}/{key} -> {target}")
                await asyncio.to_thread(target.parent.mkdir, parents=True, exist_ok=True)
                await _download_file(client, source.bucket, key, target)
                _progress_advance(progress, items=1, bytes_count=size)
        return OperationResult.success(
            f"Copied {source.uri} to {destination.uri}",
            source=source,
            destination=LocalLocation(target_root.resolve()),
            entries_affected=len(objects),
            bytes_affected=bytes_total,
        )

    async def _s3_objects(self, source: S3Location) -> tuple[tuple[str, int], ...]:
        prefix = source.prefix
        objects: list[tuple[str, int]] = []
        async with self._s3._client(source) as client:
            objects.extend(await _list_s3_objects(client, source.bucket, prefix))
            if not objects and prefix.endswith("/"):
                objects.extend(await _list_s3_objects(client, source.bucket, prefix.rstrip("/")))
        return tuple(objects)


async def _upload_file(client: S3Client, path: Path, bucket: str, key: str) -> None:
    with path.open("rb") as handle:
        await client.upload_fileobj(handle, bucket, key)


async def _download_file(client: S3Client, bucket: str, key: str, target: Path) -> None:
    with target.open("wb") as handle:
        await client.download_fileobj(bucket, key, handle)


async def _list_s3_objects(
    client: S3Client,
    bucket: str,
    prefix: str,
) -> tuple[tuple[str, int], ...]:
    objects: list[tuple[str, int]] = []
    token: str | None = None
    while True:
        request: dict[str, object] = {"Bucket": bucket, "Prefix": prefix}
        if token is not None:
            request["ContinuationToken"] = token
        page = await client.list_objects_v2(**request)
        for item in _sequence_of_mappings(page.get("Contents")):
            key = _optional_str(item.get("Key"))
            if key is None or key.endswith("/"):
                continue
            objects.append((key, _optional_int(item.get("Size")) or 0))
        token = _optional_str(page.get("NextContinuationToken"))
        if not page.get("IsTruncated") or token is None:
            break
    return tuple(objects)


def _local_files(source: Path) -> tuple[tuple[Path, str, int], ...]:
    if source.is_dir() and not source.is_symlink():
        files: list[tuple[Path, str, int]] = []
        for path in sorted(source.rglob("*"), key=lambda item: item.as_posix().casefold()):
            if path.is_dir() and not path.is_symlink():
                continue
            relative = Path(source.name) / path.relative_to(source)
            files.append((path, relative.as_posix(), path.lstat().st_size))
        return tuple(files)
    return ((source, source.name, source.lstat().st_size),)


def _resolve_local_destination(source: S3Location, destination: Path) -> Path:
    if destination.exists() and destination.is_dir():
        return destination / source.name
    return destination


def _relative_s3_download_path(source: S3Location, key: str) -> Path:
    if key == source.prefix.rstrip("/"):
        return Path()
    if source.prefix.endswith("/"):
        relative = key.removeprefix(source.prefix).lstrip("/")
        return Path(relative) if relative else Path(key.rsplit("/", maxsplit=1)[-1])
    return Path(key.rsplit("/", maxsplit=1)[-1])


def _join_s3_key(prefix: str, relative_key: str) -> str:
    return f"{prefix.rstrip('/')}/{relative_key}".lstrip("/")


def _sequence_of_mappings(value: object) -> tuple[dict[str, object], ...]:
    if not isinstance(value, list):
        return ()
    return tuple(item for item in value if isinstance(item, dict))


def _optional_str(value: object) -> str | None:
    if value is None:
        return None
    return str(value)


def _optional_int(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, str | bytes | bytearray | SupportsInt):
        return int(value)
    msg = f"Expected integer-compatible value, got {type(value).__name__}"
    raise TypeError(msg)


def _progress_update(
    progress: ProgressSink | None,
    *,
    items_total: int | None = None,
    bytes_total: int | None = None,
    current_item: str | None = None,
    message: str | None = None,
) -> None:
    if progress is not None:
        progress.update(
            items_total=items_total,
            bytes_total=bytes_total,
            current_item=current_item,
            message=message,
        )


def _progress_advance(
    progress: ProgressSink | None,
    *,
    items: int = 0,
    bytes_count: int = 0,
) -> None:
    if progress is not None:
        progress.advance(items=items, bytes_count=bytes_count)


def _progress_raise_if_cancelled(progress: ProgressSink | None) -> None:
    if progress is not None:
        progress.raise_if_cancelled()


def _unsupported(
    message: str,
    location: Location,
    destination: Location | None = None,
) -> BackendError:
    return BackendError(
        BackendErrorKind.UNSUPPORTED,
        message,
        location=location,
        destination=destination,
    )
