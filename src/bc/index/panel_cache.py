"""Cache-backed current-prefix listings for bucket panels."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TypeVar
from uuid import uuid4

from bc.core import Entry, S3Location
from bc.index.cache_paths import account_id, default_cache_root, index_dir
from bc.index.manifest import CoveredPrefix
from bc.index.metadata import metadata_rows
from bc.index.parquet_store import ParquetIndexStore

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
        objects, prefixes = metadata_rows(
            location,
            entries,
            source_listing_id=f"panel-{uuid4().hex}",
        )
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
            index_dir(self.cache_root, location),
            provider="s3",
            account_id=account_id(location),
            bucket=location.bucket,
            region=location.region,
            endpoint=location.endpoint_url,
        )


async def _to_thread(
    function: Callable[..., Result],
    *args: object,
    **kwargs: object,
) -> Result:
    return await asyncio.to_thread(function, *args, **kwargs)


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


def _normalize_prefix(prefix: str) -> str:
    normalized = prefix.strip("/")
    if not normalized:
        return ""
    return f"{normalized}/"
