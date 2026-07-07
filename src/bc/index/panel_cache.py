"""Cache-backed current-prefix listings for bucket panels."""

from __future__ import annotations

import asyncio
import hashlib
import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TypeVar
from uuid import uuid4

from bc.core import Entry, EntryType, S3Location
from bc.index.manifest import CoveredPrefix
from bc.index.parquet_store import ObjectMetadata, ParquetIndexStore, PrefixMetadata

DEFAULT_CACHE_TTL = timedelta(hours=24)
Result = TypeVar("Result")


@dataclass(frozen=True, slots=True)
class CachedPanelListing:
    """Entries and status text loaded from the bucket metadata cache."""

    entries: tuple[Entry, ...]
    status: str
    has_cache: bool
    is_stale: bool
    is_partial: bool


@dataclass(frozen=True, slots=True)
class CacheBackedBucketPanel:
    """Coordinates direct-prefix S3 listing cache reads and writes."""

    cache_root: Path
    stale_ttl: timedelta = DEFAULT_CACHE_TTL

    @classmethod
    def default(cls) -> CacheBackedBucketPanel:
        return cls(default_cache_root())

    async def load_cached(self, location: S3Location) -> CachedPanelListing:
        store = self._open_store(location)
        listing = await _to_thread(store.read_current_prefix, location.prefix)
        manifest = await _to_thread(store.load_manifest)
        covered = _covered_prefix(manifest.covered_prefixes, location.prefix)
        entries = tuple(_with_connection(entry, location) for entry in listing.entries)
        has_cache = bool(entries) or covered is not None
        is_stale = _is_stale(covered, self.stale_ttl)
        is_partial = covered is not None and not covered.fully_indexed
        return CachedPanelListing(
            entries=entries,
            status=_cache_status(entries, has_cache, is_stale, is_partial),
            has_cache=has_cache,
            is_stale=is_stale,
            is_partial=is_partial,
        )

    async def store_live_listing(
        self,
        location: S3Location,
        entries: tuple[Entry, ...],
    ) -> None:
        objects, prefixes = _metadata_rows(location, entries)
        store = self._open_store(location)
        if prefixes:
            await _to_thread(store.append_prefixes, prefixes, indexing_mode="on-demand")
        if objects:
            await _to_thread(
                store.append_objects,
                objects,
                covered_prefix=location.prefix,
                indexing_mode="on-demand",
            )
        else:
            await _to_thread(
                store.mark_prefix_listed,
                location.prefix,
                indexing_mode="on-demand",
            )

    def _open_store(self, location: S3Location) -> ParquetIndexStore:
        return ParquetIndexStore.open(
            _index_dir(self.cache_root, location),
            provider="s3",
            account_id=_account_id(location),
            bucket=location.bucket,
            region=location.region,
            endpoint=location.endpoint_url,
        )


def default_cache_root() -> Path:
    configured = os.environ.get("BUCKET_COMMANDER_CACHE_ROOT")
    if configured:
        return Path(configured).expanduser()
    xdg_cache_home = os.environ.get("XDG_CACHE_HOME")
    if xdg_cache_home:
        return Path(xdg_cache_home).expanduser() / "bucket-commander"
    return Path.home() / ".cache" / "bucket-commander"


async def _to_thread(
    function: Callable[..., Result],
    *args: object,
    **kwargs: object,
) -> Result:
    return await asyncio.to_thread(function, *args, **kwargs)


def _metadata_rows(
    location: S3Location,
    entries: tuple[Entry, ...],
) -> tuple[tuple[ObjectMetadata, ...], tuple[PrefixMetadata, ...]]:
    timestamp = datetime.now(UTC)
    source_listing_id = f"panel-{uuid4().hex}"
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
                    account_id=_account_id(location),
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
                    account_id=_account_id(location),
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


def _with_connection(entry: Entry, panel_location: S3Location) -> Entry:
    if not isinstance(entry.location, S3Location):
        return entry
    cached_location = entry.location
    return Entry(
        location=S3Location(
            bucket=cached_location.bucket,
            prefix=cached_location.prefix,
            profile=panel_location.profile,
            region=panel_location.region,
            endpoint_url=panel_location.endpoint_url,
        ),
        name=entry.name,
        entry_type=entry.entry_type,
        size=entry.size,
        modified_at=entry.modified_at,
        etag=entry.etag,
        metadata={**entry.metadata, "cache_state": "cached"},
    )


def _covered_prefix(
    prefixes: tuple[CoveredPrefix, ...],
    prefix: str,
) -> CoveredPrefix | None:
    normalized = _normalize_prefix(prefix)
    for covered in prefixes:
        if covered.prefix == normalized:
            return covered
    return None


def _is_stale(covered: CoveredPrefix | None, ttl: timedelta) -> bool:
    if covered is None:
        return False
    return datetime.now(UTC) - covered.listed_at > ttl


def _cache_status(
    entries: tuple[Entry, ...],
    has_cache: bool,
    is_stale: bool,
    is_partial: bool,
) -> str:
    if not has_cache:
        return "cache empty, loading live"
    state = "stale" if is_stale else "fresh"
    coverage = "partial" if is_partial else "cached"
    return f"{len(entries)} cached entries ({coverage}, {state}); refreshing live"


def _index_dir(cache_root: Path, location: S3Location) -> Path:
    return (
        cache_root
        / "indexes"
        / "s3"
        / _safe_segment(_account_id(location))
        / _safe_segment(_scope_id(location))
        / _safe_segment(location.bucket)
    )


def _account_id(location: S3Location) -> str:
    if location.profile:
        return f"profile-{location.profile}"
    if location.endpoint_url:
        return f"endpoint-{_short_hash(location.endpoint_url)}"
    return "default"


def _scope_id(location: S3Location) -> str:
    parts = [location.region or "default-region"]
    if location.endpoint_url:
        parts.append(_short_hash(location.endpoint_url))
    return "-".join(parts)


def _short_hash(value: str) -> str:
    return hashlib.blake2b(value.encode("utf-8"), digest_size=6).hexdigest()


def _safe_segment(value: str) -> str:
    allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-")
    return "".join(character if character in allowed else "_" for character in value)


def _normalize_prefix(prefix: str) -> str:
    normalized = prefix.strip("/")
    if not normalized:
        return ""
    return f"{normalized}/"
