from datetime import UTC, datetime, timedelta
from pathlib import Path

import pyarrow.parquet as pq

from bc.core import EntryType
from bc.index import OBJECT_SCHEMA, ObjectMetadata, ParquetIndexStore, PrefixMetadata

EXPECTED_APPENDED_OBJECT_FILES = 2


def object_row(
    key: str,
    *,
    parent_prefix: str = "logs/",
    name: str | None = None,
    size: int = 10,
    refreshed_at: datetime | None = None,
    source_listing_id: str = "listing-1",
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
    stored_column_names = [
        name for name in table.column_names if name != "partition_prefix_hash"
    ]

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
