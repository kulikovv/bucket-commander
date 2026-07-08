from datetime import UTC, datetime, timedelta
from pathlib import Path

from bc.core import S3Location, SortField, SortOrder
from bc.index import (
    IndexedBucketQuery,
    IndexedSearchCriteria,
    ObjectMetadata,
    ParquetIndexStore,
    PrefixMetadata,
    parse_indexed_search_query,
)
from bc.index.cache_paths import account_id, index_dir

MIN_SIZE_FILTER = 10


def object_row(
    key: str,
    *,
    parent_prefix: str,
    name: str | None = None,
    size: int,
    refreshed_at: datetime | None = None,
) -> ObjectMetadata:
    timestamp = datetime(2026, 1, 1, tzinfo=UTC)
    return ObjectMetadata(
        provider="s3",
        account_id="default",
        bucket="example-bucket",
        key=key,
        parent_prefix=parent_prefix,
        name=name or key.rstrip("/").rsplit("/", maxsplit=1)[-1],
        size=size,
        last_modified=timestamp,
        discovered_at=timestamp,
        refreshed_at=refreshed_at or timestamp,
        source_listing_id="listing-1",
    )


def prefix_row(prefix: str, *, parent_prefix: str) -> PrefixMetadata:
    timestamp = datetime(2026, 1, 1, tzinfo=UTC)
    return PrefixMetadata(
        provider="s3",
        account_id="default",
        bucket="example-bucket",
        prefix=prefix,
        parent_prefix=parent_prefix,
        name=prefix.rstrip("/").rsplit("/", maxsplit=1)[-1],
        object_count=1,
        recursive_object_count=1,
        total_size=10,
        recursive_total_size=10,
        fully_indexed=False,
        listed_at=timestamp,
    )


def open_store(tmp_path: Path) -> ParquetIndexStore:
    root = location()
    return ParquetIndexStore.open(
        index_dir(tmp_path / "cache", root),
        provider="s3",
        account_id=account_id(root),
        bucket=root.bucket,
        region=root.region,
    )


def location() -> S3Location:
    return S3Location(bucket="example-bucket", prefix="logs/", region="us-east-1")


def test_indexed_search_filters_by_text_prefix_size_and_modified_time(tmp_path: Path) -> None:
    store = open_store(tmp_path)
    older = datetime(2026, 1, 1, tzinfo=UTC)
    newer = datetime(2026, 1, 2, tzinfo=UTC)
    store.append_objects(
        (
            object_row("logs/app/a.txt", parent_prefix="logs/app/", size=4, refreshed_at=older),
            object_row("logs/app/big.log", parent_prefix="logs/app/", size=30, refreshed_at=newer),
            object_row("logs/other/big.log", parent_prefix="logs/other/", size=40),
        ),
        covered_prefix="logs/",
    )

    result = IndexedBucketQuery(tmp_path / "cache").search_sync(
        location(),
        criteria=IndexedSearchCriteria(
            text="big",
            prefix="logs/app/",
            min_size=10,
            modified_after=datetime(2025, 12, 31, tzinfo=UTC),
        ),
    )

    assert result.total_count == 1
    assert [entry.name for entry in result.entries] == ["big.log"]
    assert result.entries[0].metadata["cache_state"] == "indexed"
    assert result.is_partial


def test_indexed_search_sorts_and_reports_full_stale_coverage(tmp_path: Path) -> None:
    store = open_store(tmp_path)
    old_time = datetime.now(UTC) - timedelta(days=3)
    store.append_objects(
        (
            object_row("logs/small.txt", parent_prefix="logs/", size=1),
            object_row("logs/large.txt", parent_prefix="logs/", size=50),
        ),
        covered_prefix="logs/",
    )
    store.append_prefixes((prefix_row("logs/archive/", parent_prefix="logs/"),))
    manifest = store.load_manifest()
    store.manifest_store.save(
        manifest.with_covered_prefix("logs/", fully_indexed=True, now=old_time)
    )

    result = IndexedBucketQuery(tmp_path / "cache", stale_ttl=timedelta(hours=1)).search_sync(
        location(),
        sort_field=SortField.SIZE,
        sort_order=SortOrder.DESCENDING,
    )

    assert [entry.name for entry in result.entries] == ["large.txt", "archive", "small.txt"]
    assert not result.is_partial
    assert result.is_stale
    assert result.coverage_message == "fully indexed, stale"


def test_parse_indexed_search_query_supports_field_tokens() -> None:
    criteria = parse_indexed_search_query(
        f"error prefix:logs/app size>={MIN_SIZE_FILTER} modified<2026-01-03"
    )

    assert criteria.text == "error"
    assert criteria.prefix == "logs/app/"
    assert criteria.min_size == MIN_SIZE_FILTER
    assert criteria.modified_before == datetime(2026, 1, 3, tzinfo=UTC)
