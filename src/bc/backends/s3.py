"""Async S3 backend for current-prefix browsing."""

from __future__ import annotations

import asyncio
import importlib
import inspect
import threading
import weakref
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from io import BytesIO
from typing import BinaryIO, Protocol, SupportsInt, cast

from bc.backends.base import Backend, BackendError, BackendErrorKind, PreviewResult
from bc.core import Entry, EntryType, Location, OperationResult, S3Location
from bc.core.task_manager import ProgressSink

KEEP_MARKER_NAME = ".keep"


class S3Client(Protocol):
    """Minimal async S3 client surface used by the browser backend."""

    async def list_buckets(self, **kwargs: object) -> Mapping[str, object]:
        """Return one `ListBuckets` response."""

    async def list_objects_v2(self, **kwargs: object) -> Mapping[str, object]:
        """Return one `ListObjectsV2` response page."""

    async def head_object(self, **kwargs: object) -> Mapping[str, object]:
        """Return one `HeadObject` response."""

    async def get_object(self, **kwargs: object) -> Mapping[str, object]:
        """Return one `GetObject` response."""

    async def delete_object(self, **kwargs: object) -> Mapping[str, object]:
        """Delete one object."""

    async def delete_objects(self, **kwargs: object) -> Mapping[str, object]:
        """Delete a batch of objects."""

    async def copy_object(self, **kwargs: object) -> Mapping[str, object]:
        """Copy one object on the provider side."""

    async def upload_fileobj(self, fileobj: BinaryIO, bucket: str, key: str) -> None:
        """Upload one file-like object."""

    async def download_fileobj(self, bucket: str, key: str, fileobj: BinaryIO) -> None:
        """Download one object into a file-like object."""


class S3ClientContext(Protocol):
    """Async context manager returned by an S3 client factory."""

    async def __aenter__(self) -> S3Client:
        """Open the client."""

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: object | None,
    ) -> bool | None:
        """Close the client."""


class S3ClientFactory(Protocol):
    """Factory for profile/region scoped async S3 clients."""

    def __call__(
        self,
        *,
        profile_name: str | None,
        region_name: str | None,
        endpoint_url: str | None,
    ) -> S3ClientContext:
        """Create a client context."""


@dataclass(frozen=True, slots=True)
class S3BackendConfig:
    """Default AWS resolution settings for S3 browsing."""

    profile_name: str | None = None
    region_name: str | None = None
    endpoint_url: str | None = None


_ClientKey = tuple[str | None, str | None, str | None]


class _LoopClientPool:
    """Open S3 clients cached for one event loop."""

    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.contexts: dict[_ClientKey, S3ClientContext] = {}
        self.clients: dict[_ClientKey, S3Client] = {}


class S3Backend(Backend):
    """S3-compatible backend with async direct-prefix listing."""

    provider = "s3"

    def __init__(
        self,
        config: S3BackendConfig | None = None,
        *,
        client_factory: S3ClientFactory | None = None,
    ) -> None:
        self._config = config or S3BackendConfig()
        self._client_factory = client_factory or _default_client_factory
        self._pools: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, _LoopClientPool] = (
            weakref.WeakKeyDictionary()
        )
        self._pools_guard = threading.Lock()

    def supports(self, location: Location) -> bool:
        return isinstance(location, S3Location)

    async def list_buckets(self) -> tuple[str, ...]:
        """Return bucket names visible to the configured default credentials."""

        try:
            async with self._client_factory(
                profile_name=self._config.profile_name,
                region_name=self._config.region_name,
                endpoint_url=self._config.endpoint_url,
            ) as client:
                response = await client.list_buckets()
        except BackendError:
            raise
        except Exception as error:
            raise _backend_error(error) from error
        names = [
            name
            for item in _sequence_of_mappings(response.get("Buckets"))
            if (name := _optional_str(item.get("Name"))) is not None
        ]
        return tuple(sorted(names))

    async def list(self, location: Location) -> tuple[Entry, ...]:
        s3_location = self._require_s3(location)
        try:
            client = await self.acquire_client(s3_location)
            entries: list[Entry] = []
            token: str | None = None
            while True:
                page = await self._list_page(client, s3_location, token)
                entries.extend(_prefix_entries(s3_location, page))
                entries.extend(_object_entries(s3_location, page))
                token = _optional_str(page.get("NextContinuationToken"))
                if not page.get("IsTruncated") or token is None:
                    break
        except BackendError:
            raise
        except Exception as error:
            raise _backend_error(error, location=s3_location) from error
        return tuple(
            sorted(entries, key=lambda entry: (not entry.is_container, entry.name.casefold()))
        )

    async def stat(self, location: Location) -> Entry:
        s3_location = self._require_s3(location)
        if not s3_location.prefix:
            return Entry(location=s3_location, name=s3_location.bucket, entry_type=EntryType.PREFIX)
        object_key = s3_location.prefix.rstrip("/")
        try:
            client = await self.acquire_client(s3_location)
            response = await client.head_object(Bucket=s3_location.bucket, Key=object_key)
        except BackendError:
            raise
        except Exception as error:
            raise _backend_error(error, location=s3_location) from error
        return _object_entry_from_head(s3_location, object_key, response)

    async def preview(self, location: Location, *, max_bytes: int) -> PreviewResult:
        s3_location = self._require_s3(location)
        if not s3_location.prefix:
            raise _unsupported("Cannot view an S3 bucket root", location)
        key = s3_location.prefix.rstrip("/")
        try:
            client = await self.acquire_client(s3_location)
            response = await client.get_object(
                Bucket=s3_location.bucket,
                Key=key,
                Range=f"bytes=0-{max_bytes}",
            )
            data = await _read_body(response.get("Body"))
        except BackendError:
            raise
        except Exception as error:
            raise _backend_error(error, location=s3_location) from error
        return PreviewResult(data=data[:max_bytes], truncated=len(data) > max_bytes)

    async def mkdir(self, location: Location, *, parents: bool = True) -> OperationResult:
        _ = parents
        s3_location = self._require_s3(location)
        if not s3_location.prefix:
            raise _unsupported("S3 bucket creation is not supported", location)
        key = _keep_marker_key(s3_location.prefix)
        try:
            client = await self.acquire_client(s3_location)
            await client.upload_fileobj(BytesIO(b""), s3_location.bucket, key)
        except BackendError:
            raise
        except Exception as error:
            raise _backend_error(error, location=s3_location) from error
        return OperationResult.success(
            f"Created {s3_location.uri}",
            destination=s3_location,
            entries_affected=1,
        )

    async def create_file(self, location: Location) -> OperationResult:
        s3_location = self._require_s3(location)
        if not s3_location.prefix:
            raise _unsupported("S3 file creation requires an object key", location)
        key = s3_location.prefix.rstrip("/")
        try:
            client = await self.acquire_client(s3_location)
            await client.upload_fileobj(BytesIO(b""), s3_location.bucket, key)
            await _delete_parent_keep_marker(client, s3_location.bucket, key)
        except BackendError:
            raise
        except Exception as error:
            raise _backend_error(error, location=s3_location) from error
        return OperationResult.success(
            f"Created {s3_location.uri}",
            destination=s3_location,
            entries_affected=1,
        )

    async def copy(
        self,
        source: Location,
        destination: Location,
        *,
        progress: ProgressSink | None = None,
    ) -> OperationResult:
        _ = progress
        raise _unsupported("S3 copy is not implemented yet", source, destination=destination)

    async def move(self, source: Location, destination: Location) -> OperationResult:
        raise _unsupported("S3 move is not implemented yet", source, destination=destination)

    async def rename(self, source: Location, new_name: str) -> OperationResult:
        _ = new_name
        raise _unsupported("S3 rename is not implemented yet", source)

    async def delete(
        self,
        location: Location,
        *,
        recursive: bool = False,
        progress: ProgressSink | None = None,
    ) -> OperationResult:
        s3_location = self._require_s3(location)
        if not s3_location.prefix:
            raise _unsupported("S3 bucket deletion is not supported", location)
        try:
            if recursive:
                return await self._delete_prefix(s3_location, progress)
            return await self._delete_object(s3_location, progress)
        except BackendError:
            raise
        except Exception as error:
            raise _backend_error(error, location=s3_location) from error

    async def acquire_client(self, location: S3Location) -> S3Client:
        """Return an open client for the location, reusing one per (profile, region, endpoint).

        Cached clients are bound to the running event loop; the same loop must
        later call `aclose` to release them.
        """

        key: _ClientKey = (
            location.profile or self._config.profile_name,
            location.region or self._config.region_name,
            location.endpoint_url or self._config.endpoint_url,
        )
        pool = self._loop_pool()
        client = pool.clients.get(key)
        if client is not None:
            return client
        async with pool.lock:
            client = pool.clients.get(key)
            if client is not None:
                return client
            context = self._client_factory(
                profile_name=key[0],
                region_name=key[1],
                endpoint_url=key[2],
            )
            client = await context.__aenter__()
            pool.contexts[key] = context
            pool.clients[key] = client
            return client

    async def aclose(self) -> None:
        """Close every cached client owned by the current event loop."""

        loop = asyncio.get_running_loop()
        with self._pools_guard:
            pool = self._pools.pop(loop, None)
        if pool is None:
            return
        async with pool.lock:
            contexts = tuple(pool.contexts.values())
            pool.contexts.clear()
            pool.clients.clear()
        for context in contexts:
            await context.__aexit__(None, None, None)

    def _loop_pool(self) -> _LoopClientPool:
        loop = asyncio.get_running_loop()
        with self._pools_guard:
            pool = self._pools.get(loop)
            if pool is None:
                pool = _LoopClientPool()
                self._pools[loop] = pool
            return pool

    async def _list_page(
        self,
        client: S3Client,
        location: S3Location,
        continuation_token: str | None,
    ) -> Mapping[str, object]:
        request: dict[str, object] = {
            "Bucket": location.bucket,
            "Prefix": location.prefix,
            "Delimiter": "/",
        }
        if continuation_token is not None:
            request["ContinuationToken"] = continuation_token
        return await client.list_objects_v2(**request)

    def _require_s3(self, location: Location) -> S3Location:
        if isinstance(location, S3Location):
            return location
        msg = f"S3 backend does not support {location.provider!r} locations"
        raise BackendError(BackendErrorKind.INVALID_LOCATION, msg, location=location)

    async def _delete_object(
        self,
        location: S3Location,
        progress: ProgressSink | None,
    ) -> OperationResult:
        key = location.prefix.rstrip("/")
        _progress_update(
            progress,
            items_total=1,
            current_item=location.uri,
            message=f"Deleting {location.name}",
        )
        client = await self.acquire_client(location)
        await client.delete_object(Bucket=location.bucket, Key=key)
        _progress_advance(progress, items=1)
        return OperationResult.success(
            f"Deleted {location.uri}",
            source=location,
            entries_affected=1,
        )

    async def _delete_prefix(
        self,
        location: S3Location,
        progress: ProgressSink | None,
    ) -> OperationResult:
        keys = await self._list_object_keys(location)
        if not keys:
            return await self._delete_object(location, progress)
        _progress_update(
            progress,
            items_total=len(keys),
            current_item=location.uri,
            message=f"Deleting {location.name}",
        )
        client = await self.acquire_client(location)
        for batch in _chunks(keys, 1000):
            _progress_raise_if_cancelled(progress)
            _progress_update(progress, current_item=f"{len(batch)} object batch")
            response = await client.delete_objects(
                Bucket=location.bucket,
                Delete={"Objects": [{"Key": key} for key in batch], "Quiet": True},
            )
            _raise_for_delete_errors(response, location)
            _progress_advance(progress, items=len(batch))
        return OperationResult.success(
            f"Deleted {location.uri}",
            source=location,
            entries_affected=len(keys),
        )

    async def _list_object_keys(self, location: S3Location) -> tuple[str, ...]:
        keys: list[str] = []
        client = await self.acquire_client(location)
        token: str | None = None
        while True:
            request: dict[str, object] = {
                "Bucket": location.bucket,
                "Prefix": location.prefix,
            }
            if token is not None:
                request["ContinuationToken"] = token
            page = await client.list_objects_v2(**request)
            for item in _sequence_of_mappings(page.get("Contents")):
                key = _optional_str(item.get("Key"))
                if key is not None:
                    keys.append(key)
            token = _optional_str(page.get("NextContinuationToken"))
            if not page.get("IsTruncated") or token is None:
                break
        return tuple(keys)


def _default_client_factory(
    *,
    profile_name: str | None,
    region_name: str | None,
    endpoint_url: str | None,
) -> S3ClientContext:
    try:
        aioboto3 = importlib.import_module("aioboto3")
    except ModuleNotFoundError as error:
        msg = "S3 browsing requires the optional aioboto3 package"
        raise BackendError(BackendErrorKind.UNSUPPORTED, msg) from error
    session = aioboto3.Session(profile_name=profile_name)
    return cast(
        "S3ClientContext",
        session.client("s3", region_name=region_name, endpoint_url=endpoint_url),
    )


def _prefix_entries(location: S3Location, page: Mapping[str, object]) -> tuple[Entry, ...]:
    common_prefixes = _sequence_of_mappings(page.get("CommonPrefixes"))
    entries: list[Entry] = []
    for item in common_prefixes:
        prefix = _optional_str(item.get("Prefix"))
        if prefix is None:
            continue
        entries.append(
            Entry(
                location=S3Location(
                    bucket=location.bucket,
                    prefix=prefix,
                    profile=location.profile,
                    region=location.region,
                    endpoint_url=location.endpoint_url,
                ),
                name=_prefix_name(prefix, location.prefix),
                entry_type=EntryType.PREFIX,
                metadata={"provider": "s3"},
            )
        )
    return tuple(entries)


def _object_entries(location: S3Location, page: Mapping[str, object]) -> tuple[Entry, ...]:
    contents = _sequence_of_mappings(page.get("Contents"))
    entries: list[Entry] = []
    for item in contents:
        key = _optional_str(item.get("Key"))
        if key is None or key == location.prefix or _is_keep_marker_key(key):
            continue
        name = _object_name(key, location.prefix)
        if "/" in name.rstrip("/"):
            continue
        entries.append(
            Entry(
                location=S3Location(
                    bucket=location.bucket,
                    prefix=key,
                    profile=location.profile,
                    region=location.region,
                    endpoint_url=location.endpoint_url,
                ),
                name=name,
                entry_type=EntryType.OBJECT,
                size=_optional_int(item.get("Size")),
                modified_at=_optional_datetime(item.get("LastModified")),
                etag=_optional_str(item.get("ETag")),
                metadata={
                    key: value
                    for key, value in {
                        "provider": "s3",
                        "storage_class": _optional_str(item.get("StorageClass")),
                    }.items()
                    if value is not None
                },
            )
        )
    return tuple(entries)


def _object_entry_from_head(
    location: S3Location,
    object_key: str,
    response: Mapping[str, object],
) -> Entry:
    return Entry(
        location=S3Location(
            bucket=location.bucket,
            prefix=object_key,
            profile=location.profile,
            region=location.region,
            endpoint_url=location.endpoint_url,
        ),
        name=object_key.rsplit("/", maxsplit=1)[-1],
        entry_type=EntryType.OBJECT,
        size=_optional_int(response.get("ContentLength")),
        modified_at=_optional_datetime(response.get("LastModified")),
        etag=_optional_str(response.get("ETag")),
        metadata={
            key: value
            for key, value in {
                "provider": "s3",
                "content_type": _optional_str(response.get("ContentType")),
                "storage_class": _optional_str(response.get("StorageClass")),
                "version_id": _optional_str(response.get("VersionId")),
            }.items()
            if value is not None
        },
    )


def _sequence_of_mappings(value: object) -> tuple[Mapping[str, object], ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return ()
    return tuple(item for item in value if isinstance(item, Mapping))


def _chunks(values: Sequence[str], size: int) -> tuple[tuple[str, ...], ...]:
    return tuple(tuple(values[index : index + size]) for index in range(0, len(values), size))


async def _read_body(body: object) -> bytes:
    read = getattr(body, "read", None)
    if read is None:
        msg = "S3 get_object response did not include a readable body"
        raise TypeError(msg)
    data = read()
    if inspect.isawaitable(data):
        data = await data
    if isinstance(data, bytes):
        return data
    msg = f"Expected S3 body bytes, got {type(data).__name__}"
    raise TypeError(msg)


def _prefix_name(prefix: str, parent_prefix: str) -> str:
    relative = prefix.removeprefix(parent_prefix).strip("/")
    return relative.rsplit("/", maxsplit=1)[-1] if relative else prefix.strip("/")


def _object_name(key: str, parent_prefix: str) -> str:
    return key.removeprefix(parent_prefix)


def _keep_marker_key(prefix: str) -> str:
    return f"{prefix.rstrip('/')}/{KEEP_MARKER_NAME}".lstrip("/")


def _is_keep_marker_key(key: str) -> bool:
    return key.rsplit("/", maxsplit=1)[-1] == KEEP_MARKER_NAME


def _raise_for_delete_errors(response: Mapping[str, object], location: S3Location) -> None:
    errors = _sequence_of_mappings(response.get("Errors"))
    if not errors:
        return
    first_error = errors[0]
    code = _optional_str(first_error.get("Code"))
    key = _optional_str(first_error.get("Key"))
    detail = _optional_str(first_error.get("Message")) or "S3 refused to delete an object"
    suffix = f" for {key}" if key else ""
    raise BackendError(_error_kind(code), f"{detail}{suffix}", location=location)


async def _delete_parent_keep_marker(client: S3Client, bucket: str, key: str) -> None:
    parent = key.rsplit("/", maxsplit=1)[0] if "/" in key else ""
    marker_key = _keep_marker_key(f"{parent}/" if parent else "")
    if marker_key != key:
        await client.delete_object(Bucket=bucket, Key=marker_key)


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


def _optional_datetime(value: object) -> datetime | None:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)
    return None


def _backend_error(error: Exception, *, location: S3Location | None = None) -> BackendError:
    response = getattr(error, "response", None)
    if isinstance(response, Mapping):
        error_data = response.get("Error")
        if isinstance(error_data, Mapping):
            code = _optional_str(error_data.get("Code"))
            message = _optional_str(error_data.get("Message")) or str(error)
            return BackendError(_error_kind(code), message, location=location)
    return BackendError(BackendErrorKind.IO_ERROR, str(error), location=location)


def _error_kind(code: str | None) -> BackendErrorKind:
    if code in {"NoSuchBucket", "NoSuchKey", "404", "NotFound"}:
        return BackendErrorKind.NOT_FOUND
    if code in {"AccessDenied", "403"}:
        return BackendErrorKind.PERMISSION_DENIED
    return BackendErrorKind.IO_ERROR


def _unsupported(
    message: str,
    location: Location,
    *,
    destination: Location | None = None,
) -> BackendError:
    return BackendError(
        BackendErrorKind.UNSUPPORTED,
        message,
        location=location,
        destination=destination,
    )


def _progress_update(
    progress: ProgressSink | None,
    *,
    items_total: int | None = None,
    current_item: str | None = None,
    message: str | None = None,
) -> None:
    if progress is not None:
        progress.update(items_total=items_total, current_item=current_item, message=message)


def _progress_advance(progress: ProgressSink | None, *, items: int = 0) -> None:
    if progress is not None:
        progress.advance(items=items)


def _progress_raise_if_cancelled(progress: ProgressSink | None) -> None:
    if progress is not None:
        progress.raise_if_cancelled()
