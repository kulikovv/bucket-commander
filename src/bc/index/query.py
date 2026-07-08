"""Read-only indexed search and sort over bucket Parquet metadata."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal, TypeVar, cast

import duckdb

from bc.core import Entry, EntryType, S3Location, SortField, SortOrder
from bc.index.cache_paths import default_cache_root, index_dir
from bc.index.manifest import CoveredPrefix, IndexFile, IndexManifest, ManifestStore

DEFAULT_QUERY_LIMIT = 500
DEFAULT_INDEX_STALE_TTL = timedelta(hours=24)
Result = TypeVar("Result")
EntryKind = Literal["prefix", "object"]


@dataclass(frozen=True, slots=True)
class IndexedSearchCriteria:
    """Search constraints applied to indexed object metadata."""

    text: str = ""
    prefix: str | None = None
    min_size: int | None = None
    max_size: int | None = None
    modified_after: datetime | None = None
    modified_before: datetime | None = None


@dataclass(frozen=True, slots=True)
class IndexedSearchResult:
    """A bounded page of indexed entries plus cache coverage context."""

    entries: tuple[Entry, ...]
    total_count: int
    offset: int
    limit: int
    is_partial: bool
    is_stale: bool
    coverage_message: str


@dataclass(frozen=True, slots=True)
class IndexedBucketQuery:
    """Query bucket metadata stored in append-only Parquet index files."""

    cache_root: Path
    stale_ttl: timedelta = DEFAULT_INDEX_STALE_TTL

    @classmethod
    def default(cls) -> IndexedBucketQuery:
        return cls(default_cache_root())

    async def search(
        self,
        location: S3Location,
        *,
        criteria: IndexedSearchCriteria | None = None,
        sort_field: SortField = SortField.NAME,
        sort_order: SortOrder = SortOrder.ASCENDING,
        limit: int = DEFAULT_QUERY_LIMIT,
        offset: int = 0,
    ) -> IndexedSearchResult:
        return await _to_thread(
            self.search_sync,
            location,
            criteria=criteria,
            sort_field=sort_field,
            sort_order=sort_order,
            limit=limit,
            offset=offset,
        )

    def search_sync(
        self,
        location: S3Location,
        *,
        criteria: IndexedSearchCriteria | None = None,
        sort_field: SortField = SortField.NAME,
        sort_order: SortOrder = SortOrder.ASCENDING,
        limit: int = DEFAULT_QUERY_LIMIT,
        offset: int = 0,
    ) -> IndexedSearchResult:
        criteria = criteria or IndexedSearchCriteria()
        manifest_store = ManifestStore(index_dir(self.cache_root, location))
        if not manifest_store.exists():
            return IndexedSearchResult(
                entries=(),
                total_count=0,
                offset=max(0, offset),
                limit=_valid_limit(limit),
                is_partial=True,
                is_stale=False,
                coverage_message="index empty",
            )
        manifest = manifest_store.load()
        query_prefix = _criteria_prefix(location, criteria)
        files = (*manifest.object_files, *manifest.prefix_files)
        if not files:
            return IndexedSearchResult(
                entries=(),
                total_count=0,
                offset=max(0, offset),
                limit=_valid_limit(limit),
                is_partial=True,
                is_stale=False,
                coverage_message=_coverage_message(
                    manifest,
                    query_prefix,
                    stale_ttl=self._stale_ttl(manifest),
                ),
            )
        return _execute_query(
            location,
            manifest,
            criteria=criteria,
            query_prefix=query_prefix,
            sort_field=sort_field,
            sort_order=sort_order,
            limit=_valid_limit(limit),
            offset=max(0, offset),
            index_dir=manifest_store.index_dir,
            stale_ttl=self._stale_ttl(manifest),
        )

    def _stale_ttl(self, manifest: IndexManifest) -> timedelta:
        if manifest.stale_ttl_seconds is None:
            return self.stale_ttl
        return timedelta(seconds=manifest.stale_ttl_seconds)


def parse_indexed_search_query(text: str) -> IndexedSearchCriteria:
    """Parse a compact bucket search query string."""

    terms: list[str] = []
    prefix: str | None = None
    min_size: int | None = None
    max_size: int | None = None
    modified_after: datetime | None = None
    modified_before: datetime | None = None
    for token in text.split():
        if token.startswith(("prefix:", "key:")):
            prefix = _normalize_prefix(token.split(":", maxsplit=1)[1])
        elif token.startswith(("name:", "text:")):
            terms.append(token.split(":", maxsplit=1)[1])
        elif match := re.fullmatch(r"size(<=|>=|<|>)(\d+)", token):
            operator, value_text = match.groups()
            size_value = int(value_text)
            if operator in {">", ">="}:
                min_size = size_value + (1 if operator == ">" else 0)
            else:
                max_size = size_value - (1 if operator == "<" else 0)
        elif match := re.fullmatch(r"modified(<=|>=|<|>)(\d{4}-\d{2}-\d{2})", token):
            operator, value_text = match.groups()
            modified_value = datetime.fromisoformat(value_text).replace(tzinfo=UTC)
            if operator in {">", ">="}:
                modified_after = modified_value
            else:
                modified_before = modified_value
        else:
            terms.append(token)
    return IndexedSearchCriteria(
        text=" ".join(terms),
        prefix=prefix,
        min_size=min_size,
        max_size=max_size,
        modified_after=modified_after,
        modified_before=modified_before,
    )


async def _to_thread(
    function: Callable[..., Result],
    *args: object,
    **kwargs: object,
) -> Result:
    return await asyncio.to_thread(function, *args, **kwargs)


def _execute_query(
    location: S3Location,
    manifest: IndexManifest,
    *,
    criteria: IndexedSearchCriteria,
    query_prefix: str,
    sort_field: SortField,
    sort_order: SortOrder,
    limit: int,
    offset: int,
    index_dir: Path,
    stale_ttl: timedelta,
) -> IndexedSearchResult:
    sql, params = _search_sql(
        manifest,
        criteria=criteria,
        query_prefix=query_prefix,
        sort_field=sort_field,
        sort_order=sort_order,
        limit=limit,
        offset=offset,
        index_dir=index_dir,
    )
    with duckdb.connect(database=":memory:") as connection:
        rows = connection.execute(sql, params).fetchall()
    total_count = int(rows[0][7]) if rows else 0
    entries = tuple(_entry_from_row(location, row) for row in rows)
    return IndexedSearchResult(
        entries=entries,
        total_count=total_count,
        offset=offset,
        limit=limit,
        is_partial=_is_partial(manifest.covered_prefixes, query_prefix),
        is_stale=_is_stale(manifest.covered_prefixes, query_prefix, stale_ttl),
        coverage_message=_coverage_message(manifest, query_prefix, stale_ttl=stale_ttl),
    )


def _search_sql(
    manifest: IndexManifest,
    *,
    criteria: IndexedSearchCriteria,
    query_prefix: str,
    sort_field: SortField,
    sort_order: SortOrder,
    limit: int,
    offset: int,
    index_dir: Path,
) -> tuple[str, list[object]]:
    params: list[object] = []
    filters = ["key LIKE ?"]
    params.append(f"{query_prefix}%")
    if criteria.text:
        filters.append("(lower(name) LIKE ? OR lower(key) LIKE ?)")
        text = f"%{criteria.text.casefold()}%"
        params.extend((text, text))
    if criteria.min_size is not None:
        filters.append("(size IS NOT NULL AND size >= ?)")
        params.append(criteria.min_size)
    if criteria.max_size is not None:
        filters.append("(size IS NOT NULL AND size <= ?)")
        params.append(criteria.max_size)
    if criteria.modified_after is not None:
        filters.append("(modified_at IS NOT NULL AND modified_at >= ?)")
        params.append(_normalize_datetime(criteria.modified_after))
    if criteria.modified_before is not None:
        filters.append("(modified_at IS NOT NULL AND modified_at <= ?)")
        params.append(_normalize_datetime(criteria.modified_before))
    order_sql = _order_sql(sort_field, sort_order)
    params.extend((limit, offset))
    sql = f"""
        WITH entries AS (
            {_objects_sql(manifest.object_files, index_dir)}
            UNION ALL
            {_prefixes_sql(manifest.prefix_files, index_dir)}
        ),
        latest AS (
            SELECT *
            FROM entries
            QUALIFY row_number() OVER (
                PARTITION BY entry_kind, key
                ORDER BY updated_at DESC NULLS LAST
            ) = 1
        ),
        filtered AS (
            SELECT *, count(*) OVER () AS total_count
            FROM latest
            WHERE {' AND '.join(filters)}
        )
        SELECT entry_kind, key, name, size, modified_at, etag, fully_indexed, total_count
        FROM filtered
        {order_sql}
        LIMIT ? OFFSET ?
    """
    return sql, params


def _objects_sql(files: tuple[IndexFile, ...], index_dir: Path) -> str:
    if not files:
        return """
            SELECT
                'object' AS entry_kind,
                '' AS key,
                '' AS name,
                CAST(NULL AS BIGINT) AS size,
                CAST(NULL AS TIMESTAMPTZ) AS modified_at,
                CAST(NULL AS VARCHAR) AS etag,
                false AS fully_indexed,
                CAST(NULL AS TIMESTAMPTZ) AS updated_at
            WHERE false
        """
    return f"""
        SELECT
            'object' AS entry_kind,
            key,
            name,
            size,
            last_modified AS modified_at,
            etag,
            false AS fully_indexed,
            refreshed_at AS updated_at
        FROM read_parquet({_duckdb_file_list(files, index_dir)})
        WHERE NOT is_delete_marker
    """


def _prefixes_sql(files: tuple[IndexFile, ...], index_dir: Path) -> str:
    if not files:
        return """
            SELECT
                'prefix' AS entry_kind,
                '' AS key,
                '' AS name,
                CAST(NULL AS BIGINT) AS size,
                CAST(NULL AS TIMESTAMPTZ) AS modified_at,
                CAST(NULL AS VARCHAR) AS etag,
                false AS fully_indexed,
                CAST(NULL AS TIMESTAMPTZ) AS updated_at
            WHERE false
        """
    return f"""
        SELECT
            'prefix' AS entry_kind,
            prefix AS key,
            name,
            total_size AS size,
            listed_at AS modified_at,
            CAST(NULL AS VARCHAR) AS etag,
            fully_indexed,
            coalesce(recursive_indexed_at, listed_at) AS updated_at
        FROM read_parquet({_duckdb_file_list(files, index_dir)})
    """


def _duckdb_file_list(files: tuple[IndexFile, ...], root: Path) -> str:
    paths = [_sql_quote((root / file.path).as_posix()) for file in files]
    return f"[{', '.join(paths)}]"


def _sql_quote(value: str) -> str:
    escaped = value.replace("'", "''")
    return f"'{escaped}'"


def _order_sql(sort_field: SortField, sort_order: SortOrder) -> str:
    direction = "DESC" if sort_order is SortOrder.DESCENDING else "ASC"
    if sort_field is SortField.TYPE:
        expression = "entry_kind"
    elif sort_field is SortField.SIZE:
        expression = "size"
    elif sort_field is SortField.MODIFIED_AT:
        expression = "modified_at"
    else:
        expression = "lower(name)"
    return (
        "ORDER BY "
        f"{expression} {direction} NULLS LAST, "
        "CASE entry_kind WHEN 'prefix' THEN 0 ELSE 1 END ASC, lower(name) ASC"
    )


def _entry_from_row(location: S3Location, row: tuple[object, ...]) -> Entry:
    entry_kind = cast("EntryKind", row[0])
    key = str(row[1])
    name = str(row[2])
    size = cast("int | None", row[3])
    modified_at = cast("datetime | None", row[4])
    etag = cast("str | None", row[5])
    fully_indexed = bool(row[6])
    return Entry(
        location=S3Location(
            bucket=location.bucket,
            prefix=key,
            profile=location.profile,
            region=location.region,
            endpoint_url=location.endpoint_url,
        ),
        name=name,
        entry_type=EntryType.PREFIX if entry_kind == "prefix" else EntryType.OBJECT,
        size=size,
        modified_at=_normalize_optional_datetime(modified_at),
        etag=etag,
        metadata={
            "cache_state": "indexed",
            "fully_indexed": str(fully_indexed).lower(),
        },
    )


def _criteria_prefix(location: S3Location, criteria: IndexedSearchCriteria) -> str:
    if criteria.prefix is None:
        return _normalize_prefix(location.prefix)
    return _normalize_prefix(criteria.prefix)


def _is_partial(prefixes: tuple[CoveredPrefix, ...], query_prefix: str) -> bool:
    covering = _covering_prefix(prefixes, query_prefix)
    return covering is None or not covering.fully_indexed


def _is_stale(
    prefixes: tuple[CoveredPrefix, ...],
    query_prefix: str,
    stale_ttl: timedelta,
) -> bool:
    covering = _covering_prefix(prefixes, query_prefix)
    if covering is None:
        return False
    return datetime.now(UTC) - covering.listed_at > stale_ttl


def _coverage_message(
    manifest: IndexManifest,
    query_prefix: str,
    *,
    stale_ttl: timedelta,
) -> str:
    covering = _covering_prefix(manifest.covered_prefixes, query_prefix)
    if covering is None:
        return "index coverage unknown"
    state = "fully indexed" if covering.fully_indexed else "partial index"
    freshness = "stale" if datetime.now(UTC) - covering.listed_at > stale_ttl else "fresh"
    return f"{state}, {freshness}"


def _covering_prefix(
    prefixes: tuple[CoveredPrefix, ...],
    query_prefix: str,
) -> CoveredPrefix | None:
    normalized = _normalize_prefix(query_prefix)
    matches = [
        covered
        for covered in prefixes
        if normalized.startswith(covered.prefix) or covered.prefix.startswith(normalized)
    ]
    if not matches:
        return None
    return max(matches, key=lambda covered: len(covered.prefix))


def _valid_limit(limit: int) -> int:
    return max(1, limit)


def _normalize_prefix(prefix: str) -> str:
    normalized = prefix.strip("/")
    if not normalized:
        return ""
    return f"{normalized}/"


def _normalize_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _normalize_optional_datetime(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return _normalize_datetime(value)
