import asyncio
import os
from collections.abc import Coroutine
from typing import Any, TypeVar

import pytest

from bc.backends import S3Backend, S3BackendConfig
from bc.core import EntryType, parse_location

T = TypeVar("T")

MINIO_BUCKET = "bucket-commander"


@pytest.mark.integration
def test_s3_backend_lists_seeded_minio_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    endpoint_url = os.environ.get("BC_MINIO_ENDPOINT")
    if endpoint_url is None:
        pytest.skip("Set BC_MINIO_ENDPOINT to run the MinIO integration test")

    monkeypatch.setenv("AWS_ACCESS_KEY_ID", os.environ.get("AWS_ACCESS_KEY_ID", "bucketcommander"))
    monkeypatch.setenv(
        "AWS_SECRET_ACCESS_KEY",
        os.environ.get("AWS_SECRET_ACCESS_KEY", "bucketcommander123"),
    )
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")

    backend = S3Backend(
        S3BackendConfig(
            region_name="us-east-1",
            endpoint_url=endpoint_url,
        )
    )

    entries = run_async(backend.list(parse_location(f"s3://{MINIO_BUCKET}/logs/")))
    stat_entry = run_async(backend.stat(parse_location(f"s3://{MINIO_BUCKET}/logs/a.txt")))

    assert [(entry.name, entry.entry_type) for entry in entries] == [
        ("archive", EntryType.PREFIX),
        ("a.txt", EntryType.OBJECT),
        ("b.txt", EntryType.OBJECT),
    ]
    assert stat_entry.name == "a.txt"
    assert stat_entry.entry_type is EntryType.OBJECT
    assert stat_entry.size == len("alpha\n")


def run_async(coro: Coroutine[Any, Any, T]) -> T:
    return asyncio.run(coro)
