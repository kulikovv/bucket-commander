import asyncio
from collections.abc import Mapping
from pathlib import Path

from bc.core import Entry, EntryType, S3Location
from bc.index import (
    CheckpointStore,
    ParquetIndexStore,
    RecursiveBucketIndexer,
    RecursiveIndexCheckpoint,
)
from bc.index.cache_paths import account_id, index_dir

INDEXED_OBJECTS = 2
INDEXED_BYTES = 30
OBJECT_FILE_COUNT = 2


class FakeRecursiveBackend:
    def __init__(self, listings: Mapping[str, tuple[Entry, ...]]) -> None:
        self._listings = listings
        self.requests: list[str] = []

    async def list(self, location: S3Location) -> tuple[Entry, ...]:
        self.requests.append(location.prefix)
        return self._listings.get(location.prefix, ())


def test_recursive_indexer_writes_objects_prefixes_and_checkpoint(tmp_path: Path) -> None:
    location = S3Location(bucket="example-bucket", prefix="logs/")
    backend = FakeRecursiveBackend(
        {
            "logs/": (
                prefix_entry("logs/archive/"),
                object_entry("logs/a.txt", size=10),
            ),
            "logs/archive/": (object_entry("logs/archive/deep.txt", size=20),),
        }
    )
    indexer = RecursiveBucketIndexer(
        backend=backend,
        cache_root=tmp_path / "cache",
        max_concurrency=2,
        batch_size=1,
    )

    result = asyncio.run(indexer.index(location))

    store = open_store(tmp_path, location)
    manifest = store.load_manifest()
    root_coverage = {prefix.prefix: prefix for prefix in manifest.covered_prefixes}["logs/"]
    checkpoint = CheckpointStore(store.index_dir).load()
    archive_listing = store.read_current_prefix("logs/")

    assert result.objects_indexed == INDEXED_OBJECTS
    assert result.bytes_indexed == INDEXED_BYTES
    assert sorted(backend.requests) == ["logs/", "logs/archive/"]
    assert root_coverage.fully_indexed
    assert checkpoint.pending_prefixes == ()
    assert checkpoint.completed_prefixes == ("logs/", "logs/archive/")
    assert len(manifest.object_files) == OBJECT_FILE_COUNT
    assert [entry.name for entry in archive_listing.entries] == ["archive", "a.txt"]
    assert archive_listing.entries[0].metadata["fully_indexed"] == "true"
    assert archive_listing.entries[0].metadata["recursive_object_count"] == "1"


def test_recursive_indexer_resumes_from_checkpoint(tmp_path: Path) -> None:
    location = S3Location(bucket="example-bucket", prefix="logs/")
    store = open_store(tmp_path, location)
    checkpoint_store = CheckpointStore(store.index_dir)
    checkpoint_store.save(
        RecursiveIndexCheckpoint.start(location).with_pending(
            {"logs/archive/"},
            {"logs/"},
            objects_indexed=1,
            bytes_indexed=10,
        )
    )
    backend = FakeRecursiveBackend(
        {"logs/archive/": (object_entry("logs/archive/deep.txt", size=20),)}
    )
    indexer = RecursiveBucketIndexer(
        backend=backend,
        cache_root=tmp_path / "cache",
        max_concurrency=1,
    )

    result = asyncio.run(indexer.index(location, resume=True))
    checkpoint = checkpoint_store.load()

    assert backend.requests == ["logs/archive/"]
    assert result.objects_indexed == INDEXED_OBJECTS
    assert result.bytes_indexed == INDEXED_BYTES
    assert checkpoint.pending_prefixes == ()
    assert checkpoint.completed_prefixes == ("logs/", "logs/archive/")


def open_store(tmp_path: Path, location: S3Location) -> ParquetIndexStore:
    return ParquetIndexStore.open(
        index_dir(tmp_path / "cache", location),
        provider="s3",
        account_id=account_id(location),
        bucket=location.bucket,
    )


def object_entry(key: str, *, size: int) -> Entry:
    return Entry(
        location=S3Location(bucket="example-bucket", prefix=key),
        name=key.rsplit("/", maxsplit=1)[-1],
        entry_type=EntryType.OBJECT,
        size=size,
    )


def prefix_entry(prefix: str) -> Entry:
    return Entry(
        location=S3Location(bucket="example-bucket", prefix=prefix),
        name=prefix.strip("/").rsplit("/", maxsplit=1)[-1],
        entry_type=EntryType.PREFIX,
    )
