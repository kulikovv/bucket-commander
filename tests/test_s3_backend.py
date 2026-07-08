import asyncio
from collections.abc import Coroutine, Mapping
from datetime import UTC, datetime
from typing import Any, BinaryIO, TypeVar

import pytest

from bc.backends import BackendError, BackendErrorKind, S3Backend, S3BackendConfig
from bc.core import EntryType, S3Location, parse_location

T = TypeVar("T")
FIRST_OBJECT_SIZE = 11
HEAD_OBJECT_SIZE = 42
PREFIX_DELETE_COUNT = 2


class FakeS3Client:
    def __init__(self, pages: tuple[Mapping[str, object], ...]) -> None:
        self._pages = pages
        self.list_requests: list[dict[str, object]] = []
        self.delete_requests: list[dict[str, object]] = []

    async def list_objects_v2(self, **kwargs: object) -> Mapping[str, object]:
        self.list_requests.append(dict(kwargs))
        index = len(self.list_requests) - 1
        return self._pages[index]

    async def head_object(self, **kwargs: object) -> Mapping[str, object]:
        _ = kwargs
        return {
            "ContentLength": HEAD_OBJECT_SIZE,
            "LastModified": datetime(2026, 1, 1, tzinfo=UTC),
            "ETag": "etag",
            "ContentType": "text/plain",
        }

    async def get_object(self, **kwargs: object) -> Mapping[str, object]:
        _ = kwargs
        return {"Body": FakeBody(b"hello from s3")}

    async def delete_object(self, **kwargs: object) -> Mapping[str, object]:
        self.delete_requests.append(dict(kwargs))
        return {}

    async def delete_objects(self, **kwargs: object) -> Mapping[str, object]:
        self.delete_requests.append(dict(kwargs))
        return {}

    async def copy_object(self, **kwargs: object) -> Mapping[str, object]:
        _ = kwargs
        return {}

    async def upload_fileobj(self, fileobj: BinaryIO, bucket: str, key: str) -> None:
        _ = fileobj, bucket, key

    async def download_fileobj(self, bucket: str, key: str, fileobj: BinaryIO) -> None:
        _ = bucket, key, fileobj


class FakeBody:
    def __init__(self, data: bytes) -> None:
        self._data = data

    async def read(self) -> bytes:
        return self._data


class FakeS3ClientContext:
    def __init__(self, client: FakeS3Client) -> None:
        self._client = client

    async def __aenter__(self) -> FakeS3Client:
        return self._client

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: object | None,
    ) -> bool | None:
        return None


class RecordingClientFactory:
    def __init__(self, client: FakeS3Client) -> None:
        self._client = client
        self.calls: list[dict[str, str | None]] = []

    def __call__(
        self,
        *,
        profile_name: str | None,
        region_name: str | None,
        endpoint_url: str | None,
    ) -> FakeS3ClientContext:
        self.calls.append(
            {
                "profile_name": profile_name,
                "region_name": region_name,
                "endpoint_url": endpoint_url,
            }
        )
        return FakeS3ClientContext(self._client)


def test_list_returns_common_prefixes_and_objects_from_all_pages() -> None:
    client = FakeS3Client(
        (
            {
                "CommonPrefixes": [{"Prefix": "logs/archive/"}],
                "Contents": [
                    {
                        "Key": "logs/",
                        "Size": 0,
                    },
                    {
                        "Key": "logs/a.txt",
                        "Size": FIRST_OBJECT_SIZE,
                        "LastModified": datetime(2026, 1, 1, tzinfo=UTC),
                        "ETag": "etag-a",
                    },
                    {
                        "Key": "logs/archive/deep.txt",
                        "Size": 99,
                    },
                ],
                "IsTruncated": True,
                "NextContinuationToken": "next-page",
            },
            {
                "Contents": [
                    {
                        "Key": "logs/b.txt",
                        "Size": 12,
                        "StorageClass": "STANDARD",
                    }
                ],
                "IsTruncated": False,
            },
        )
    )
    backend = S3Backend(client_factory=RecordingClientFactory(client))

    entries = run_async(backend.list(parse_location("s3://example-bucket/logs/")))

    assert [(entry.name, entry.entry_type) for entry in entries] == [
        ("archive", EntryType.PREFIX),
        ("a.txt", EntryType.OBJECT),
        ("b.txt", EntryType.OBJECT),
    ]
    assert entries[1].size == FIRST_OBJECT_SIZE
    assert entries[1].modified_at == datetime(2026, 1, 1, tzinfo=UTC)
    assert entries[1].etag == "etag-a"
    assert client.list_requests == [
        {
            "Bucket": "example-bucket",
            "Prefix": "logs/",
            "Delimiter": "/",
        },
        {
            "Bucket": "example-bucket",
            "Prefix": "logs/",
            "Delimiter": "/",
            "ContinuationToken": "next-page",
        },
    ]


def test_location_profile_and_region_override_backend_defaults() -> None:
    client = FakeS3Client(({"Contents": [], "IsTruncated": False},))
    factory = RecordingClientFactory(client)
    backend = S3Backend(
        S3BackendConfig(
            profile_name="default-profile",
            region_name="us-east-1",
            endpoint_url="http://localhost:9000",
        ),
        client_factory=factory,
    )

    run_async(
        backend.list(
            S3Location(
                bucket="example-bucket",
                prefix="logs/",
                profile="panel-profile",
                region="eu-west-1",
                endpoint_url="http://127.0.0.1:9000",
            )
        )
    )

    assert factory.calls == [
        {
            "profile_name": "panel-profile",
            "region_name": "eu-west-1",
            "endpoint_url": "http://127.0.0.1:9000",
        }
    ]


def test_s3_entries_preserve_location_connection_metadata() -> None:
    client = FakeS3Client(
        (
            {
                "CommonPrefixes": [{"Prefix": "logs/archive/"}],
                "Contents": [{"Key": "logs/a.txt", "Size": 5}],
                "IsTruncated": False,
            },
        )
    )
    location = S3Location(
        bucket="example-bucket",
        prefix="logs/",
        profile="panel-profile",
        region="eu-west-1",
        endpoint_url="http://127.0.0.1:9000",
    )
    backend = S3Backend(client_factory=RecordingClientFactory(client))

    entries = run_async(backend.list(location))

    assert {
        entry.location.endpoint_url
        for entry in entries
        if isinstance(entry.location, S3Location)
    } == {"http://127.0.0.1:9000"}


def test_stat_reads_object_head_metadata() -> None:
    backend = S3Backend(client_factory=RecordingClientFactory(FakeS3Client(())))

    entry = run_async(backend.stat(parse_location("s3://example-bucket/logs/a.txt")))

    assert entry.name == "a.txt"
    assert entry.entry_type is EntryType.OBJECT
    assert entry.size == HEAD_OBJECT_SIZE
    assert entry.metadata["content_type"] == "text/plain"


def test_preview_reads_object_bytes() -> None:
    client = FakeS3Client(())
    backend = S3Backend(client_factory=RecordingClientFactory(client))

    preview = run_async(
        backend.preview(parse_location("s3://example-bucket/logs/a.txt"), max_bytes=5)
    )

    assert preview.data == b"hello"
    assert preview.truncated


def test_delete_object_calls_s3_delete_object() -> None:
    client = FakeS3Client(())
    backend = S3Backend(client_factory=RecordingClientFactory(client))

    result = run_async(backend.delete(parse_location("s3://example-bucket/logs/a.txt")))

    assert result.ok
    assert client.delete_requests == [{"Bucket": "example-bucket", "Key": "logs/a.txt"}]


def test_recursive_delete_object_falls_back_to_exact_key() -> None:
    client = FakeS3Client(({"Contents": [], "IsTruncated": False},))
    backend = S3Backend(client_factory=RecordingClientFactory(client))

    result = run_async(
        backend.delete(parse_location("s3://example-bucket/logs/a.txt"), recursive=True)
    )

    assert result.ok
    assert client.list_requests == [
        {
            "Bucket": "example-bucket",
            "Prefix": "logs/a.txt/",
        }
    ]
    assert client.delete_requests == [{"Bucket": "example-bucket", "Key": "logs/a.txt"}]


def test_delete_prefix_batches_s3_objects() -> None:
    client = FakeS3Client(
        (
            {
                "Contents": [
                    {"Key": "logs/a.txt"},
                    {"Key": "logs/b.txt"},
                ],
                "IsTruncated": False,
            },
        )
    )
    backend = S3Backend(client_factory=RecordingClientFactory(client))

    result = run_async(backend.delete(parse_location("s3://example-bucket/logs/"), recursive=True))

    assert result.entries_affected == PREFIX_DELETE_COUNT
    assert client.delete_requests == [
        {
            "Bucket": "example-bucket",
            "Delete": {
                "Objects": [{"Key": "logs/a.txt"}, {"Key": "logs/b.txt"}],
                "Quiet": True,
            },
        }
    ]


def test_s3_copy_operations_are_not_implemented_yet() -> None:
    backend = S3Backend(client_factory=RecordingClientFactory(FakeS3Client(())))

    with pytest.raises(BackendError) as error_info:
        run_async(
            backend.copy(
                parse_location("s3://example-bucket/logs/a.txt"),
                parse_location("s3://example-bucket/copy/"),
            )
        )

    assert error_info.value.kind is BackendErrorKind.UNSUPPORTED


def run_async(coro: Coroutine[Any, Any, T]) -> T:
    return asyncio.run(coro)
