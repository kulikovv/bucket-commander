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
    def __init__(
        self,
        pages: tuple[Mapping[str, object], ...],
        buckets: tuple[str, ...] = (),
    ) -> None:
        self._pages = pages
        self._buckets = buckets
        self.list_requests: list[dict[str, object]] = []
        self.delete_requests: list[dict[str, object]] = []
        self.upload_requests: list[tuple[str, str, bytes]] = []

    async def list_buckets(self, **kwargs: object) -> Mapping[str, object]:
        _ = kwargs
        return {"Buckets": [{"Name": name} for name in self._buckets]}

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
        self.upload_requests.append((bucket, key, fileobj.read()))

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
        self.exited = False

    async def __aenter__(self) -> FakeS3Client:
        return self._client

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: object | None,
    ) -> bool | None:
        self.exited = True
        return None


class RecordingClientFactory:
    def __init__(self, client: FakeS3Client) -> None:
        self._client = client
        self.calls: list[dict[str, str | None]] = []
        self.contexts: list[FakeS3ClientContext] = []

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
        context = FakeS3ClientContext(self._client)
        self.contexts.append(context)
        return context


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


def test_client_is_reused_across_calls_and_closed_by_aclose() -> None:
    empty_page: dict[str, object] = {"Contents": [], "IsTruncated": False}
    client = FakeS3Client((empty_page, empty_page))
    factory = RecordingClientFactory(client)
    backend = S3Backend(client_factory=factory)
    location = S3Location(bucket="example-bucket", prefix="logs/")

    async def scenario() -> None:
        await backend.list(location)
        await backend.list(location)
        await backend.aclose()

    run_async(scenario())

    assert len(factory.calls) == 1
    assert [context.exited for context in factory.contexts] == [True]


def test_distinct_connection_settings_use_distinct_clients() -> None:
    empty_page: dict[str, object] = {"Contents": [], "IsTruncated": False}
    client = FakeS3Client((empty_page, empty_page))
    factory = RecordingClientFactory(client)
    backend = S3Backend(client_factory=factory)

    async def scenario() -> None:
        await backend.list(S3Location(bucket="example-bucket", prefix="logs/", profile="alpha"))
        await backend.list(S3Location(bucket="example-bucket", prefix="logs/", profile="beta"))
        await backend.aclose()

    run_async(scenario())

    assert [call["profile_name"] for call in factory.calls] == ["alpha", "beta"]
    assert [context.exited for context in factory.contexts] == [True, True]


def test_list_buckets_returns_sorted_names_using_backend_defaults() -> None:
    client = FakeS3Client((), buckets=("beta", "alpha"))
    factory = RecordingClientFactory(client)
    backend = S3Backend(
        S3BackendConfig(
            profile_name="default-profile",
            region_name="us-east-1",
            endpoint_url="http://localhost:9000",
        ),
        client_factory=factory,
    )

    names = run_async(backend.list_buckets())

    assert names == ("alpha", "beta")
    assert factory.calls == [
        {
            "profile_name": "default-profile",
            "region_name": "us-east-1",
            "endpoint_url": "http://localhost:9000",
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
        entry.location.endpoint_url for entry in entries if isinstance(entry.location, S3Location)
    } == {"http://127.0.0.1:9000"}


def test_list_hides_keep_marker_objects() -> None:
    client = FakeS3Client(
        (
            {
                "Contents": [
                    {"Key": "logs/.keep", "Size": 0},
                    {"Key": "logs/a.txt", "Size": FIRST_OBJECT_SIZE},
                ],
                "IsTruncated": False,
            },
        )
    )
    backend = S3Backend(client_factory=RecordingClientFactory(client))

    entries = run_async(backend.list(parse_location("s3://example-bucket/logs/")))

    assert [entry.name for entry in entries] == ["a.txt"]


def test_mkdir_creates_keep_marker_object() -> None:
    client = FakeS3Client(())
    backend = S3Backend(client_factory=RecordingClientFactory(client))

    result = run_async(backend.mkdir(parse_location("s3://example-bucket/logs/new/")))

    assert result.ok
    assert client.upload_requests == [("example-bucket", "logs/new/.keep", b"")]


def test_create_file_uploads_empty_object_and_deletes_parent_keep_marker() -> None:
    client = FakeS3Client(())
    backend = S3Backend(client_factory=RecordingClientFactory(client))

    result = run_async(backend.create_file(parse_location("s3://example-bucket/logs/a.txt")))

    assert result.ok
    assert client.upload_requests == [("example-bucket", "logs/a.txt", b"")]
    assert client.delete_requests == [{"Bucket": "example-bucket", "Key": "logs/.keep"}]


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
            "Prefix": "logs/a.txt",
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


def test_delete_prefix_raises_when_s3_reports_partial_failure() -> None:
    class PartiallyFailingS3Client(FakeS3Client):
        async def delete_objects(self, **kwargs: object) -> Mapping[str, object]:
            self.delete_requests.append(dict(kwargs))
            return {
                "Errors": [
                    {
                        "Key": "logs/a.txt",
                        "Code": "AccessDenied",
                        "Message": "Access denied",
                    }
                ]
            }

    client = PartiallyFailingS3Client(
        ({"Contents": [{"Key": "logs/a.txt"}], "IsTruncated": False},)
    )
    backend = S3Backend(client_factory=RecordingClientFactory(client))

    with pytest.raises(BackendError) as error_info:
        run_async(
            backend.delete(S3Location(bucket="example-bucket", prefix="logs/"), recursive=True)
        )

    assert error_info.value.kind is BackendErrorKind.PERMISSION_DENIED


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
