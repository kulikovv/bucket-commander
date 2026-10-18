import asyncio
import os
from collections.abc import Coroutine, Mapping
from pathlib import Path
from typing import Any, BinaryIO, TypeVar
from uuid import uuid4

import pytest

from bc.backends import (
    BackendError,
    BackendErrorKind,
    BackendRouter,
    LocalBackend,
    S3Backend,
    S3BackendConfig,
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


@pytest.mark.parametrize("operation", ["copy", "move", "delete"])
@pytest.mark.parametrize("route", ["local-local", "local-s3", "s3-local", "s3-s3"])
@pytest.mark.parametrize("storage", ["fake", pytest.param("minio", marks=pytest.mark.integration)])
def test_multiple_selected_trees_preserve_structure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str, route: str, storage: str
) -> None:
    """Exercise the same selected entries and destination used by the two panels."""
    s3, router = _operation_backends(storage, monkeypatch)
    namespace = f"operation-tests/{uuid4().hex}/"
    remote = S3Location(bucket="bucket-commander", prefix=namespace)
    source_dir = tmp_path / "source"
    destination_dir = tmp_path / "destination"
    source_dir.mkdir()
    destination_dir.mkdir()
    contents = {
        "first/shared.txt": b"first",
        "first/nested/shared.txt": b"nested first",
        "second/shared.txt": b"second",
        "second/nested/shared.txt": b"nested second",
        "loose.txt": b"loose",
        "firstish/untouched.txt": b"neighbor",
    }
    for relative, data in contents.items():
        path = source_dir / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    source_provider, destination_provider = route.split("-")

    async def exercise() -> None:
        try:
            if source_provider == "s3":
                for name in ("first", "second", "loose.txt", "firstish"):
                    await router.copy(parse_location(source_dir / name), remote)
            destination = (
                S3Location(bucket=remote.bucket, prefix=f"{namespace}target/")
                if destination_provider == "s3"
                else parse_location(destination_dir)
            )
            for name in ("first", "second", "loose.txt"):
                source = (
                    S3Location(
                        bucket=remote.bucket,
                        prefix=f"{namespace}{name}" + ("/" if name != "loose.txt" else ""),
                    )
                    if source_provider == "s3"
                    else parse_location(source_dir / name)
                )
                if operation == "delete":
                    await router.delete(source, recursive=name != "loose.txt")
                else:
                    result = await getattr(router, operation)(source, destination)
                    assert result.destination is not None
                    if name == "loose.txt":
                        assert not (await router.stat(result.destination)).is_container
            expected = {
                key: value for key, value in contents.items() if not key.startswith("firstish/")
            }
            if operation != "delete":
                if destination_provider == "s3":
                    await router.copy(destination, parse_location(destination_dir))
                    target = destination_dir / "target"
                else:
                    target = destination_dir
                assert {
                    path.relative_to(target).as_posix(): path.read_bytes()
                    for path in target.rglob("*")
                    if path.is_file()
                } == expected
            remaining = contents if operation == "copy" else {"firstish/untouched.txt": b"neighbor"}
            if source_provider == "s3":
                actual = await s3.acquire_client(remote)
                page = await actual.list_objects_v2(Bucket=remote.bucket, Prefix=namespace)
                objects = page.get("Contents", [])
                assert isinstance(objects, list)
                keys = {
                    str(item["Key"]).removeprefix(namespace)
                    for item in objects
                    if not str(item["Key"]).startswith(f"{namespace}target/")
                }
                assert keys == set(remaining)
            else:
                assert {
                    path.relative_to(source_dir).as_posix(): path.read_bytes()
                    for path in source_dir.rglob("*")
                    if path.is_file()
                } == remaining
        finally:
            await s3.delete(remote, recursive=True)
            await s3.aclose()

    run_async(exercise())


def _operation_backends(
    storage: str, monkeypatch: pytest.MonkeyPatch
) -> tuple[S3Backend, BackendRouter]:
    endpoint = os.environ.get("BC_MINIO_ENDPOINT")
    if storage == "minio":
        if endpoint is None:
            pytest.skip("Set BC_MINIO_ENDPOINT to run MinIO operation tests")
        monkeypatch.setenv("AWS_ACCESS_KEY_ID", "bucketcommander")
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "bucketcommander123")
        monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    s3 = (
        S3Backend(S3BackendConfig(endpoint_url=endpoint, region_name="us-east-1"))
        if storage == "minio"
        else S3Backend(client_factory=FakeTransferS3ClientFactory(FakeTransferS3Client()))
    )
    return s3, BackendRouter((LocalBackend(), s3, TransferBackend(s3_backend=s3)))


@pytest.mark.parametrize("storage", ["local", pytest.param("minio", marks=pytest.mark.integration)])
def test_create_list_preview_rename_and_delete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, storage: str
) -> None:
    s3, router = _operation_backends(storage, monkeypatch)
    namespace = f"operation-tests/{uuid4().hex}/"
    remote = S3Location(bucket="bucket-commander", prefix=namespace)
    folder = (
        S3Location(bucket=remote.bucket, prefix=f"{namespace}folder/")
        if storage == "minio"
        else parse_location(tmp_path / "folder")
    )
    file = (
        S3Location(bucket=remote.bucket, prefix=f"{namespace}folder/empty.txt")
        if storage == "minio"
        else parse_location(tmp_path / "folder" / "empty.txt")
    )

    async def exercise() -> None:
        try:
            await router.mkdir(folder)
            await router.create_file(file)
            assert [entry.name for entry in await router.list(folder)] == ["empty.txt"]
            assert (await router.stat(file)).size == 0
            assert (await router.preview(file, max_bytes=10)).data == b""
            if storage == "minio":
                with pytest.raises(BackendError) as error:
                    await router.rename(file, "renamed.txt")
                assert error.value.kind is BackendErrorKind.UNSUPPORTED
                assert (await router.stat(file)).size == 0
                await router.delete(file)
            else:
                renamed = await router.rename(file, "renamed.txt")
                assert renamed.destination is not None
                assert [entry.name for entry in await router.list(folder)] == ["renamed.txt"]
                await router.delete(renamed.destination)
            assert await router.list(folder) == ()
            await router.delete(folder, recursive=True)
        finally:
            if storage == "minio":
                await s3.delete(remote, recursive=True)
            await s3.aclose()

    run_async(exercise())


@pytest.mark.parametrize("failure", [OSError, asyncio.CancelledError])
@pytest.mark.parametrize("route", ["local-s3", "s3-local", "s3-s3"])
def test_failed_or_cancelled_move_retains_source(
    tmp_path: Path, failure: type[BaseException], route: str
) -> None:
    class FailingClient(FakeTransferS3Client):
        async def upload_fileobj(self, handle: BinaryIO, bucket: str, key: str) -> None:
            _ = handle, bucket, key
            raise failure("interrupted upload")

        async def download_fileobj(self, bucket: str, key: str, handle: BinaryIO) -> None:
            _ = bucket, key, handle
            raise failure("interrupted download")

        async def copy_object(self, **kwargs: object) -> Mapping[str, object]:
            _ = kwargs
            raise failure("interrupted copy")

    client = FailingClient()
    client.objects["bucket-commander/source/nested/file.txt"] = b"original"
    path = tmp_path / "source" / "nested" / "file.txt"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"original")
    destination = tmp_path / "destination"
    destination.mkdir()
    source = (
        parse_location(tmp_path / "source")
        if route.startswith("local-")
        else S3Location(bucket="bucket-commander", prefix="source/")
    )
    target = (
        parse_location(destination)
        if route.endswith("-local")
        else S3Location(bucket="bucket-commander", prefix="target/")
    )
    with pytest.raises(failure):
        run_async(_router(client).move(source, target))
    assert path.read_bytes() == b"original"
    assert client.objects == {"bucket-commander/source/nested/file.txt": b"original"}
