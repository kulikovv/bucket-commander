import asyncio
from collections.abc import Coroutine, Mapping
from pathlib import Path
from typing import Any, BinaryIO, TypeVar

import pytest

from bc.backends import (
    BackendError,
    BackendErrorKind,
    BackendRouter,
    LocalBackend,
    S3Backend,
    TransferBackend,
)
from bc.core import S3Location, parse_location

T = TypeVar("T")


class FakeTransferS3Client:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    async def list_buckets(self, **kwargs: object) -> Mapping[str, object]:
        _ = kwargs
        buckets = sorted({key.split("/", maxsplit=1)[0] for key in self.objects})
        return {"Buckets": [{"Name": name} for name in buckets]}

    async def list_objects_v2(self, **kwargs: object) -> Mapping[str, object]:
        bucket = str(kwargs["Bucket"])
        prefix = str(kwargs.get("Prefix", ""))
        return {
            "Contents": [
                {
                    "Key": key.removeprefix(f"{bucket}/"),
                    "Size": len(value),
                }
                for key, value in sorted(self.objects.items())
                if key.startswith(f"{bucket}/{prefix}")
            ],
            "IsTruncated": False,
        }

    async def head_object(self, **kwargs: object) -> Mapping[str, object]:
        key = f"{kwargs['Bucket']}/{kwargs['Key']}"
        return {"ContentLength": len(self.objects[key])}

    async def get_object(self, **kwargs: object) -> Mapping[str, object]:
        _ = kwargs
        return {"Body": FakeTransferBody(b"")}

    async def delete_object(self, **kwargs: object) -> Mapping[str, object]:
        self.objects.pop(f"{kwargs['Bucket']}/{kwargs['Key']}", None)
        return {}

    async def delete_objects(self, **kwargs: object) -> Mapping[str, object]:
        bucket = str(kwargs["Bucket"])
        delete = kwargs["Delete"]
        assert isinstance(delete, dict)
        for item in delete["Objects"]:
            assert isinstance(item, dict)
            self.objects.pop(f"{bucket}/{item['Key']}", None)
        return {}

    async def copy_object(self, **kwargs: object) -> Mapping[str, object]:
        source = kwargs["CopySource"]
        assert isinstance(source, dict)
        source_key = f"{source['Bucket']}/{source['Key']}"
        target_key = f"{kwargs['Bucket']}/{kwargs['Key']}"
        self.objects[target_key] = self.objects[source_key]
        return {}

    async def upload_fileobj(self, handle: BinaryIO, bucket: str, key: str) -> None:
        self.objects[f"{bucket}/{key}"] = handle.read()

    async def download_fileobj(self, bucket: str, key: str, handle: BinaryIO) -> None:
        handle.write(self.objects[f"{bucket}/{key}"])


class FakeTransferS3ClientContext:
    def __init__(self, client: FakeTransferS3Client) -> None:
        self._client = client

    async def __aenter__(self) -> FakeTransferS3Client:
        return self._client

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: object | None,
    ) -> bool | None:
        return None


class FakeTransferBody:
    def __init__(self, data: bytes) -> None:
        self._data = data

    async def read(self) -> bytes:
        return self._data


class FakeTransferS3ClientFactory:
    def __init__(self, client: FakeTransferS3Client) -> None:
        self._client = client

    def __call__(
        self,
        *,
        profile_name: str | None,
        region_name: str | None,
        endpoint_url: str | None,
    ) -> FakeTransferS3ClientContext:
        _ = profile_name, region_name, endpoint_url
        return FakeTransferS3ClientContext(self._client)


def test_router_copies_local_file_to_s3(tmp_path: Path) -> None:
    source = tmp_path / "hello.txt"
    source.write_text("hello", encoding="utf-8")
    client = FakeTransferS3Client()
    router = _router(client)

    result = run_async(
        router.copy(
            parse_location(source),
            S3Location(bucket="bucket-commander", prefix="uploads/"),
        )
    )

    assert client.objects["bucket-commander/uploads/hello.txt"] == b"hello"
    assert result.entries_affected == 1


def test_router_copies_local_file_to_s3_removes_parent_keep_marker(tmp_path: Path) -> None:
    source = tmp_path / "hello.txt"
    source.write_text("hello", encoding="utf-8")
    client = FakeTransferS3Client()
    client.objects["bucket-commander/uploads/.keep"] = b""
    router = _router(client)

    run_async(
        router.copy(
            parse_location(source),
            S3Location(bucket="bucket-commander", prefix="uploads/"),
        )
    )

    assert "bucket-commander/uploads/.keep" not in client.objects


def test_router_copies_s3_object_to_local_directory(tmp_path: Path) -> None:
    destination = tmp_path / "downloads"
    destination.mkdir()
    client = FakeTransferS3Client()
    client.objects["bucket-commander/logs/a.txt"] = b"alpha"
    router = _router(client)

    result = run_async(
        router.copy(
            S3Location(bucket="bucket-commander", prefix="logs/a.txt"),
            parse_location(destination),
        )
    )

    assert (destination / "a.txt").read_bytes() == b"alpha"
    assert result.entries_affected == 1


def test_router_moves_local_file_to_s3(tmp_path: Path) -> None:
    source = tmp_path / "move-me.txt"
    source.write_text("move", encoding="utf-8")
    client = FakeTransferS3Client()
    router = _router(client)

    run_async(
        router.move(
            parse_location(source),
            S3Location(bucket="bucket-commander", prefix="uploads/"),
        )
    )

    assert not source.exists()
    assert client.objects["bucket-commander/uploads/move-me.txt"] == b"move"


def test_router_copies_s3_object_to_s3_prefix() -> None:
    client = FakeTransferS3Client()
    client.objects["bucket-commander/logs/a.txt"] = b"alpha"
    router = _router(client)

    result = run_async(
        router.copy(
            S3Location(bucket="bucket-commander", prefix="logs/a.txt"),
            S3Location(bucket="archive-bucket", prefix="backup/"),
        )
    )

    assert client.objects["archive-bucket/backup/a.txt"] == b"alpha"
    assert result.entries_affected == 1


def test_router_copies_s3_object_to_s3_removes_parent_keep_marker() -> None:
    client = FakeTransferS3Client()
    client.objects["bucket-commander/logs/a.txt"] = b"alpha"
    client.objects["archive-bucket/backup/.keep"] = b""
    router = _router(client)

    run_async(
        router.copy(
            S3Location(bucket="bucket-commander", prefix="logs/a.txt"),
            S3Location(bucket="archive-bucket", prefix="backup/"),
        )
    )

    assert "archive-bucket/backup/.keep" not in client.objects


def test_router_moves_s3_object_to_s3_prefix() -> None:
    client = FakeTransferS3Client()
    client.objects["bucket-commander/logs/a.txt"] = b"alpha"
    router = _router(client)

    run_async(
        router.move(
            S3Location(bucket="bucket-commander", prefix="logs/a.txt"),
            S3Location(bucket="archive-bucket", prefix="backup/"),
        )
    )

    assert "bucket-commander/logs/a.txt" not in client.objects
    assert client.objects["archive-bucket/backup/a.txt"] == b"alpha"


def test_router_move_s3_object_does_not_delete_matching_prefix_contents() -> None:
    client = FakeTransferS3Client()
    client.objects["bucket-commander/report"] = b"report"
    client.objects["bucket-commander/report/child.txt"] = b"child"
    router = _router(client)

    run_async(
        router.move(
            S3Location(bucket="bucket-commander", prefix="report"),
            S3Location(bucket="archive-bucket", prefix="backup/"),
        )
    )

    assert client.objects["archive-bucket/backup/report"] == b"report"
    assert client.objects["bucket-commander/report/child.txt"] == b"child"


def test_router_rejects_s3_keys_that_escape_download_directory(tmp_path: Path) -> None:
    client = FakeTransferS3Client()
    client.objects["bucket-commander/logs/../../outside.txt"] = b"outside"
    destination = tmp_path / "downloads"
    destination.mkdir()

    with pytest.raises(BackendError) as error_info:
        run_async(
            _router(client).copy(
                S3Location(bucket="bucket-commander", prefix="logs/"),
                parse_location(destination),
            )
        )

    assert error_info.value.kind is BackendErrorKind.INVALID_LOCATION
    assert not (tmp_path / "outside.txt").exists()


def _router(client: FakeTransferS3Client) -> BackendRouter:
    factory = FakeTransferS3ClientFactory(client)
    s3_backend = S3Backend(client_factory=factory)
    return BackendRouter((LocalBackend(), s3_backend, TransferBackend(s3_backend=s3_backend)))


def run_async(coro: Coroutine[Any, Any, T]) -> T:
    return asyncio.run(coro)
