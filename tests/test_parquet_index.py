import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pyarrow.parquet as pq

from bc.core import Entry, EntryType, S3Location
from bc.index import (
    OBJECT_SCHEMA,
    CacheBackedBucketPanel,
    IndexedBucketQuery,
    ObjectMetadata,
    ParquetIndexStore,
    PrefixMetadata,
)
from bc.index.cache_paths import index_dir

EXPECTED_APPENDED_OBJECT_FILES = 2
COMPACTION_OBJECT_ROWS_BEFORE = 4
COMPACTION_PREFIX_ROWS_BEFORE = 2
LARGE_INDEX_ROWS = 1500


def object_row(
    key: str,
    *,
    parent_prefix: str = "logs/",
    name: str | None = None,
    size: int = 10,
    refreshed_at: datetime | None = None,
    source_listing_id: str = "listing-1",
    is_delete_marker: bool = False,
) -> ObjectMetadata:
    timestamp = datetime(2026, 1, 1, tzinfo=UTC)
    return ObjectMetadata(
        provider="s3",
        account_id="dev",
        bucket="example-bucket",
        key=key,
        parent_prefix=parent_prefix,
        name=name or key.rstrip("/").rsplit("/", maxsplit=1)[-1],
        size=size,
        last_modified=timestamp,
        etag="etag",
        discovered_at=timestamp,
        refreshed_at=refreshed_at or timestamp,
        source_listing_id=source_listing_id,
        is_delete_marker=is_delete_marker,
    )


def prefix_row(
    prefix: str,
    *,
    parent_prefix: str = "logs/",
    name: str | None = None,
    listed_at: datetime | None = None,
) -> PrefixMetadata:
    timestamp = datetime(2026, 1, 1, tzinfo=UTC)
    return PrefixMetadata(
        provider="s3",
        account_id="dev",
        bucket="example-bucket",
        prefix=prefix,
        parent_prefix=parent_prefix,
        name=name or prefix.rstrip("/").rsplit("/", maxsplit=1)[-1],
        object_count=1,
        recursive_object_count=2,
        total_size=10,
        recursive_total_size=20,
        fully_indexed=False,
        listed_at=listed_at or timestamp,
    )


def open_store(tmp_path: Path) -> ParquetIndexStore:
    return ParquetIndexStore.open(
        tmp_path / "indexes" / "s3" / "dev" / "example-bucket",
        provider="s3",
        account_id="dev",
        bucket="example-bucket",
        region="us-east-1",
    )


def test_append_objects_writes_parquet_and_manifest_tracks_active_files(tmp_path: Path) -> None:
    store = open_store(tmp_path)
    rows = (
        object_row("logs/a.txt", size=11),
        object_row("logs/b.txt", size=12),
    )

    files = store.append_objects(rows, covered_prefix="logs/", indexing_mode="on-demand")
    manifest = store.load_manifest()

    assert len(files) == 1
    assert files[0].exists()
    assert manifest.provider == "s3"
    assert manifest.region == "us-east-1"
    assert [file.row_count for file in manifest.object_files] == [2]
    assert manifest.covered_prefixes[0].prefix == "logs/"
    assert manifest.indexing_history == ("on-demand",)

    table = pq.read_table(files[0])  # type: ignore[no-untyped-call]
    stored_column_names = [name for name in table.column_names if name != "partition_prefix_hash"]

    assert stored_column_names == OBJECT_SCHEMA.names
    assert stored_column_names[:6] == [
        "provider",
        "account_id",
        "bucket",
        "key",
        "parent_prefix",
        "name",
    ]


def test_append_prefixes_and_read_current_prefix_as_entries(tmp_path: Path) -> None:
    store = open_store(tmp_path)

    store.append_prefixes((prefix_row("logs/archive/"),))
    store.append_objects((object_row("logs/a.txt"),))

    listing = store.read_current_prefix("logs")

    assert [entry.name for entry in listing.entries] == ["archive", "a.txt"]
    assert [entry.entry_type for entry in listing.entries] == [EntryType.PREFIX, EntryType.OBJECT]
    assert listing.entries[1].location.uri == "s3://example-bucket/logs/a.txt/"


def test_current_prefix_query_returns_newest_rows_without_mutating_old_files(
    tmp_path: Path,
) -> None:
    store = open_store(tmp_path)
    older = datetime(2026, 1, 1, tzinfo=UTC)
    newer = older + timedelta(hours=1)

    first_files = store.append_objects(
        (object_row("logs/a.txt", size=1, refreshed_at=older, source_listing_id="first"),)
    )
    second_files = store.append_objects(
        (object_row("logs/a.txt", size=99, refreshed_at=newer, source_listing_id="second"),)
    )

    manifest = store.load_manifest()
    listing = store.read_current_prefix("logs/")

    assert len(manifest.object_files) == EXPECTED_APPENDED_OBJECT_FILES
    assert first_files[0] != second_files[0]
    assert first_files[0].exists()
    assert second_files[0].exists()
    assert [(row.key, row.size, row.source_listing_id) for row in listing.objects] == [
        ("logs/a.txt", 99, "second")
    ]


def test_current_prefix_query_hides_newest_delete_marker(tmp_path: Path) -> None:
    store = open_store(tmp_path)
    older = datetime(2026, 1, 1, tzinfo=UTC)
    newer = older + timedelta(hours=1)

    store.append_objects(
        (object_row("logs/a.txt", size=99, refreshed_at=older, source_listing_id="live"),)
    )
    store.append_objects(
        (
            object_row(
                "logs/a.txt",
                size=0,
                refreshed_at=newer,
                source_listing_id="delete",
                is_delete_marker=True,
            ),
        )
    )

    listing = store.read_current_prefix("logs/")

    assert listing.objects == ()


def test_compaction_keeps_newest_rows_and_removes_tombstoned_objects(tmp_path: Path) -> None:
    store = open_store(tmp_path)
    older = datetime(2026, 1, 1, tzinfo=UTC)
    newer = older + timedelta(hours=1)
    newest = newer + timedelta(hours=1)

    store.append_prefixes((prefix_row("logs/archive/", listed_at=older),))
    store.append_prefixes((prefix_row("logs/archive/", listed_at=newer),))
    store.append_objects(
        (
            object_row("logs/a.txt", size=1, refreshed_at=older, source_listing_id="old"),
            object_row("logs/b.txt", size=2, refreshed_at=older, source_listing_id="old"),
        )
    )
    store.append_objects(
        (
            object_row("logs/a.txt", size=10, refreshed_at=newer, source_listing_id="new"),
            object_row(
                "logs/b.txt",
                size=0,
                refreshed_at=newest,
                source_listing_id="delete",
                is_delete_marker=True,
            ),
        )
    )

    result = store.compact()
    manifest = store.load_manifest()
    listing = store.read_current_prefix("logs/")

    assert result.object_rows_before == COMPACTION_OBJECT_ROWS_BEFORE
    assert result.object_rows_after == 1
    assert result.prefix_rows_before == COMPACTION_PREFIX_ROWS_BEFORE
    assert result.prefix_rows_after == 1
    assert [file.row_count for file in manifest.object_files] == [1]
    assert [file.row_count for file in manifest.prefix_files] == [1]
    assert manifest.indexing_history[-1] == "compaction"
    assert [(row.key, row.size, row.source_listing_id) for row in listing.objects] == [
        ("logs/a.txt", 10, "new")
    ]
    assert [row.prefix for row in listing.prefixes] == ["logs/archive/"]


def test_compaction_handles_large_index(tmp_path: Path) -> None:
    store = open_store(tmp_path)
    base = datetime(2026, 1, 1, tzinfo=UTC)
    rows = tuple(
        object_row(
            f"logs/item-{index:04d}.txt",
            size=index,
            refreshed_at=base + timedelta(seconds=index),
        )
        for index in range(LARGE_INDEX_ROWS)
    )

    store.append_objects(rows[:750])
    store.append_objects(rows[750:])

    result = store.compact()
    listing = store.read_current_prefix("logs/")

    assert result.object_rows_before == LARGE_INDEX_ROWS
    assert result.object_rows_after == LARGE_INDEX_ROWS
    assert len(store.load_manifest().object_files) == 1
    assert len(listing.objects) == LARGE_INDEX_ROWS


def test_query_is_limited_to_direct_parent_prefix(tmp_path: Path) -> None:
    store = open_store(tmp_path)
    store.append_objects(
        (
            object_row("logs/a.txt", parent_prefix="logs/"),
            object_row("logs/archive/a.txt", parent_prefix="logs/archive/"),
        )
    )

    listing = store.read_current_prefix("logs/")

    assert [row.key for row in listing.objects] == ["logs/a.txt"]


def test_cache_backed_panel_persists_live_listing_and_loads_cached_entries(
    tmp_path: Path,
) -> None:
    cache = CacheBackedBucketPanel(tmp_path / "cache")
    location = S3Location(
        bucket="example-bucket",
        prefix="logs/",
        profile="dev-profile",
        region="us-east-1",
        endpoint_url="http://localhost:9000",
    )
    live_entries = (
        Entry(
            location=S3Location(
                bucket="example-bucket",
                prefix="logs/archive/",
                profile="dev-profile",
                region="us-east-1",
                endpoint_url="http://localhost:9000",
            ),
            name="archive",
            entry_type=EntryType.PREFIX,
        ),
        Entry(
            location=S3Location(
                bucket="example-bucket",
                prefix="logs/a.txt",
                profile="dev-profile",
                region="us-east-1",
                endpoint_url="http://localhost:9000",
            ),
            name="a.txt",
            entry_type=EntryType.OBJECT,
            size=15,
            modified_at=datetime(2026, 1, 1, tzinfo=UTC),
            etag="etag-a",
        ),
    )

    asyncio.run(cache.store_live_listing(location, live_entries))
    cached = asyncio.run(cache.load_cached(location))

    assert cached.has_cache
    assert cached.is_partial
    assert not cached.is_stale
    assert "2 cached entries" in cached.status
    assert [(entry.name, entry.entry_type) for entry in cached.entries] == [
        ("archive", EntryType.PREFIX),
        ("a.txt", EntryType.OBJECT),
    ]
    assert {
        entry.location.endpoint_url
        for entry in cached.entries
        if isinstance(entry.location, S3Location)
    } == {"http://localhost:9000"}


def test_cache_backed_panel_marks_empty_live_listing_as_cached(tmp_path: Path) -> None:
    cache = CacheBackedBucketPanel(tmp_path / "cache")
    location = S3Location(bucket="example-bucket", prefix="empty/")

    asyncio.run(cache.store_live_listing(location, ()))
    cached = asyncio.run(cache.load_cached(location))

    assert cached.has_cache
    assert cached.entries == ()
    assert "0 cached entries" in cached.status


def test_indexed_search_reports_damaged_manifest_without_raising(tmp_path: Path) -> None:
    cache_root = tmp_path / "cache"
    location = S3Location(bucket="example-bucket", prefix="logs/")
    store = ParquetIndexStore.open(
        index_dir(cache_root, location),
        provider="s3",
        account_id="default",
        bucket="example-bucket",
    )
    store.manifest_store.path.write_text("{not-json", encoding="utf-8")

    result = IndexedBucketQuery(cache_root).search_sync(location)

    assert result.entries == ()
    assert result.is_partial
    assert result.is_stale
    assert "index damaged" in result.coverage_message
