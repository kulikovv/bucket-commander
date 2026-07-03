"""Async S3 backend for current-prefix browsing."""

from __future__ import annotations

import importlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol, SupportsInt, cast

from bc.backends.base import Backend, BackendError, BackendErrorKind
from bc.core import Entry, EntryType, Location, OperationResult, S3Location
from bc.core.task_manager import ProgressSink


class S3Client(Protocol):
    """Minimal async S3 client surface used by the browser backend."""

    async def list_objects_v2(self, **kwargs: object) -> Mapping[str, object]:
        """Return one `ListObjectsV2` response page."""

    async def head_object(self, **kwargs: object) -> Mapping[str, object]:
        """Return one `HeadObject` response."""


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

    def supports(self, location: Location) -> bool:
        return isinstance(location, S3Location)

    async def list(self, location: Location) -> tuple[Entry, ...]:
        s3_location = self._require_s3(location)
        try:
            async with self._client(s3_location) as client:
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
            async with self._client(s3_location) as client:
                response = await client.head_object(Bucket=s3_location.bucket, Key=object_key)
        except BackendError:
            raise
        except Exception as error:
            raise _backend_error(error, location=s3_location) from error
        return _object_entry_from_head(s3_location, object_key, response)

    async def mkdir(self, location: Location, *, parents: bool = True) -> OperationResult:
        _ = parents
        raise _unsupported("S3 prefix creation is not implemented yet", location)

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
        _ = recursive, progress
        raise _unsupported("S3 delete is not implemented yet", location)

    def _client(self, location: S3Location) -> S3ClientContext:
        return self._client_factory(
            profile_name=location.profile or self._config.profile_name,
            region_name=location.region or self._config.region_name,
            endpoint_url=self._config.endpoint_url,
        )

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
        if key is None or key == location.prefix:
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


def _prefix_name(prefix: str, parent_prefix: str) -> str:
    relative = prefix.removeprefix(parent_prefix).strip("/")
    return relative.rsplit("/", maxsplit=1)[-1] if relative else prefix.strip("/")


def _object_name(key: str, parent_prefix: str) -> str:
    return key.removeprefix(parent_prefix)


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


def _backend_error(error: Exception, *, location: S3Location) -> BackendError:
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
