"""Convert backend entries into durable index metadata rows."""

from __future__ import annotations

from datetime import UTC, datetime

from bc.core import Entry, EntryType, S3Location
from bc.index.cache_paths import account_id
from bc.index.parquet_store import ObjectMetadata, PrefixMetadata


def metadata_rows(
    location: S3Location,
    entries: tuple[Entry, ...],
    *,
    source_listing_id: str,
    discovered_at: datetime | None = None,
) -> tuple[tuple[ObjectMetadata, ...], tuple[PrefixMetadata, ...]]:
    timestamp = _normalize_datetime(discovered_at or datetime.now(UTC))
    objects: list[ObjectMetadata] = []
    prefixes: list[PrefixMetadata] = []
    for entry in entries:
        if not isinstance(entry.location, S3Location):
            continue
        if entry.name == "..":
            continue
        if entry.entry_type is EntryType.OBJECT:
            objects.append(
                ObjectMetadata(
                    provider="s3",
                    account_id=account_id(location),
                    bucket=location.bucket,
                    key=entry.location.prefix.rstrip("/"),
                    parent_prefix=location.prefix,
                    name=entry.name,
                    size=entry.size or 0,
                    last_modified=entry.modified_at or timestamp,
                    etag=entry.etag,
                    storage_class=entry.metadata.get("storage_class"),
                    content_type=entry.metadata.get("content_type"),
                    encryption=entry.metadata.get("encryption"),
                    version_id=entry.metadata.get("version_id"),
                    discovered_at=timestamp,
                    refreshed_at=timestamp,
                    source_listing_id=source_listing_id,
                )
            )
        elif entry.entry_type is EntryType.PREFIX:
            prefixes.append(
                PrefixMetadata(
                    provider="s3",
                    account_id=account_id(location),
                    bucket=location.bucket,
                    prefix=entry.location.prefix,
                    parent_prefix=location.prefix,
                    name=entry.name,
                    object_count=0,
                    recursive_object_count=0,
                    total_size=entry.size or 0,
                    recursive_total_size=0,
                    fully_indexed=False,
                    listed_at=timestamp,
                )
            )
    return tuple(objects), tuple(prefixes)


def _normalize_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
