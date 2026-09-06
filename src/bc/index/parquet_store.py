"""Append-only Parquet storage for bucket object and prefix metadata."""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import pyarrow as pa
import pyarrow.parquet as pq

from bc.core import Entry, EntryType, S3Location
from bc.index.manifest import IndexFile, IndexManifest, ManifestStore

logger = logging.getLogger(__name__)

OBJECT_SCHEMA = pa.schema(
    [
        pa.field("provider", pa.string()),
        pa.field("account_id", pa.string()),
        pa.field("bucket", pa.string()),
        pa.field("key", pa.string()),
        pa.field("parent_prefix", pa.string()),
        pa.field("name", pa.string()),
        pa.field("size", pa.int64()),
        pa.field("last_modified", pa.timestamp("us", tz="UTC")),
        pa.field("etag", pa.string()),
        pa.field("checksum", pa.string()),
        pa.field("storage_class", pa.string()),
        pa.field("content_type", pa.string()),
        pa.field("encryption", pa.string()),
        pa.field("version_id", pa.string()),
        pa.field("is_delete_marker", pa.bool_()),
        pa.field("discovered_at", pa.timestamp("us", tz="UTC")),
        pa.field("refreshed_at", pa.timestamp("us", tz="UTC")),
        pa.field("source_listing_id", pa.string()),
    ]
)

PREFIX_SCHEMA = pa.schema(
    [
        pa.field("provider", pa.string()),
        pa.field("account_id", pa.string()),
        pa.field("bucket", pa.string()),
        pa.field("prefix", pa.string()),
        pa.field("parent_prefix", pa.string()),
        pa.field("name", pa.string()),
        pa.field("object_count", pa.int64()),
        pa.field("recursive_object_count", pa.int64()),
        pa.field("total_size", pa.int64()),
        pa.field("recursive_total_size", pa.int64()),
        pa.field("fully_indexed", pa.bool_()),
        pa.field("listed_at", pa.timestamp("us", tz="UTC")),
        pa.field("recursive_indexed_at", pa.timestamp("us", tz="UTC")),
    ]
)


@dataclass(frozen=True, slots=True)
class ObjectMetadata:
    """Stable object metadata row stored in the object Parquet dataset."""

    provider: str
    account_id: str
    bucket: str
    key: str
    parent_prefix: str
    name: str
    size: int
    last_modified: datetime
    discovered_at: datetime
    refreshed_at: datetime
    source_listing_id: str
    etag: str | None = None
    checksum: str | None = None
    storage_class: str | None = None
    content_type: str | None = None
    encryption: str | None = None
    version_id: str | None = None
    is_delete_marker: bool = False

    def __post_init__(self) -> None:
        if self.size < 0:
            msg = "Object size must be non-negative"
            raise ValueError(msg)
        if not self.key:
            msg = "Object key must not be empty"
            raise ValueError(msg)
        if not self.name:
            msg = "Object name must not be empty"
            raise ValueError(msg)

    def to_entry(self) -> Entry:
        return Entry(
            location=S3Location(bucket=self.bucket, prefix=self.key),
            name=self.name,
            entry_type=EntryType.OBJECT,
            size=self.size,
            modified_at=self.last_modified,
            etag=self.etag,
            metadata={
                key: value
                for key, value in {
                    "provider": self.provider,
                    "account_id": self.account_id,
                    "storage_class": self.storage_class,
                    "content_type": self.content_type,
                    "encryption": self.encryption,
                    "version_id": self.version_id,
                    "source_listing_id": self.source_listing_id,
                }.items()
                if value is not None
            },
        )


@dataclass(frozen=True, slots=True)
class PrefixMetadata:
    """Stable prefix summary row stored in the prefix Parquet dataset."""

    provider: str
    account_id: str
    bucket: str
    prefix: str
    parent_prefix: str
    name: str
    object_count: int
    recursive_object_count: int
    total_size: int
    recursive_total_size: int
    fully_indexed: bool
    listed_at: datetime
    recursive_indexed_at: datetime | None = None

    def __post_init__(self) -> None:
        if not self.name:
            msg = "Prefix name must not be empty"
            raise ValueError(msg)
        for field_name in (
            "object_count",
            "recursive_object_count",
            "total_size",
            "recursive_total_size",
        ):
            if getattr(self, field_name) < 0:
                msg = f"{field_name} must be non-negative"
                raise ValueError(msg)

    def to_entry(self) -> Entry:
        return Entry(
            location=S3Location(bucket=self.bucket, prefix=self.prefix),
            name=self.name,
            entry_type=EntryType.PREFIX,
            size=self.total_size,
            modified_at=self.listed_at,
            metadata={
                "provider": self.provider,
                "account_id": self.account_id,
                "object_count": str(self.object_count),
                "recursive_object_count": str(self.recursive_object_count),
                "recursive_total_size": str(self.recursive_total_size),
                "fully_indexed": str(self.fully_indexed).lower(),
            },
        )


@dataclass(frozen=True, slots=True)
class CurrentPrefixListing:
    """Cached direct listing for one logical prefix."""

    prefix: str
    objects: tuple[ObjectMetadata, ...]
    prefixes: tuple[PrefixMetadata, ...]

    @property
    def entries(self) -> tuple[Entry, ...]:
        prefix_entries = tuple(prefix.to_entry() for prefix in self.prefixes)
        object_entries = tuple(object_metadata.to_entry() for object_metadata in self.objects)
        return (*prefix_entries, *object_entries)


@dataclass(frozen=True, slots=True)
class CompactionResult:
    """Summary of a metadata index compaction run."""

    object_rows_before: int
    object_rows_after: int
    prefix_rows_before: int
    prefix_rows_after: int
    object_files_before: int
    object_files_after: int
    prefix_files_before: int
    prefix_files_after: int


@dataclass(frozen=True, slots=True)
class ParquetIndexStore:
    """Read and append bucket metadata under one index directory."""

    index_dir: Path

    @classmethod
    def open(
        cls,
        index_dir: Path,
        *,
        provider: str,
        account_id: str,
        bucket: str,
        region: str | None = None,
        endpoint: str | None = None,
    ) -> ParquetIndexStore:
        store = cls(index_dir)
        manifest_store = store.manifest_store
        manifest_store.create_if_missing(
            IndexManifest.create(
                provider=provider,
                account_id=account_id,
                bucket=bucket,
                region=region,
                endpoint=endpoint,
            )
        )
        return store

    @property
    def manifest_store(self) -> ManifestStore:
        return ManifestStore(self.index_dir)

    def load_manifest(self) -> IndexManifest:
        return self.manifest_store.load()

    def append_objects(
        self,
        rows: tuple[ObjectMetadata, ...],
        *,
        covered_prefix: str | None = None,
        indexing_mode: str | None = None,
    ) -> tuple[Path, ...]:
        if not rows:
            return ()

        written_files: list[IndexFile] = []
        for partition_hash, partition_rows in _group_objects_by_partition(rows).items():
            relative_path = (
                Path("objects") / f"partition_prefix_hash={partition_hash}" / _part_name()
            )
            path = self.index_dir / relative_path
            _write_table(path, _objects_to_table(partition_rows))
            written_files.append(
                IndexFile(
                    path=relative_path.as_posix(),
                    row_count=len(partition_rows),
                    created_at=datetime.now(UTC),
                )
            )

        self.manifest_store.update(
            lambda manifest: manifest.with_object_files(
                tuple(written_files),
                covered_prefix=covered_prefix,
                indexing_mode=indexing_mode,
            )
        )
        return tuple(self.index_dir / file.path for file in written_files)

    def mark_prefix_listed(
        self,
        prefix: str,
        *,
        indexing_mode: str | None = None,
    ) -> None:
        self.manifest_store.update(
            lambda manifest: manifest.with_object_files(
                (),
                covered_prefix=prefix,
                indexing_mode=indexing_mode,
            )
        )

    def mark_prefix_fully_indexed(
        self,
        prefix: str,
        *,
        indexing_mode: str | None = None,
    ) -> None:
        self.manifest_store.update(
            lambda manifest: manifest.with_covered_prefix(
                prefix,
                fully_indexed=True,
                indexing_mode=indexing_mode,
            )
        )

    def mark_checkpoint(self, checkpoint: str) -> None:
        self.manifest_store.update(lambda manifest: manifest.with_checkpoint(checkpoint))

    def append_prefixes(
        self,
        rows: tuple[PrefixMetadata, ...],
        *,
        indexing_mode: str | None = None,
    ) -> tuple[Path, ...]:
        if not rows:
            return ()

        relative_path = Path("prefixes") / _part_name()
        path = self.index_dir / relative_path
        _write_table(path, _prefixes_to_table(rows))
        written_file = IndexFile(
            path=relative_path.as_posix(),
            row_count=len(rows),
            created_at=datetime.now(UTC),
        )
        self.manifest_store.update(
            lambda manifest: manifest.with_prefix_files(
                (written_file,), indexing_mode=indexing_mode
            )
        )
        return (self.index_dir / written_file.path,)

    def read_current_prefix(self, prefix: str) -> CurrentPrefixListing:
        manifest = self.load_manifest()
        normalized_prefix = _normalize_prefix(prefix)
        objects = _latest_by_identity(
            _read_object_rows(self.index_dir, manifest.object_files, normalized_prefix),
            key_fields=("bucket", "key", "version_id"),
            timestamp_fields=("refreshed_at", "discovered_at"),
        )
        objects = tuple(row for row in objects if not bool(row.get("is_delete_marker")))
        prefixes = _latest_by_identity(
            _read_prefix_rows(self.index_dir, manifest.prefix_files, normalized_prefix),
            key_fields=("bucket", "prefix"),
            timestamp_fields=("recursive_indexed_at", "listed_at"),
        )
        return CurrentPrefixListing(
            prefix=normalized_prefix,
            objects=tuple(_object_from_row(row) for row in objects),
            prefixes=tuple(_prefix_from_row(row) for row in prefixes),
        )

    def compact(self, *, indexing_mode: str = "compaction") -> CompactionResult:
        """Rewrite active Parquet files to newest rows and tombstone-free objects."""

        with self.manifest_store.locked():
            manifest = self.load_manifest()
            object_rows = _read_all_object_rows(self.index_dir, manifest.object_files)
            prefix_rows = _read_all_prefix_rows(self.index_dir, manifest.prefix_files)
            latest_objects = _latest_by_identity(
                object_rows,
                key_fields=("bucket", "key", "version_id"),
                timestamp_fields=("refreshed_at", "discovered_at"),
            )
            compacted_objects = tuple(
                _object_from_row(row)
                for row in latest_objects
                if not bool(row.get("is_delete_marker"))
            )
            compacted_prefixes = tuple(
                _prefix_from_row(row)
                for row in _latest_by_identity(
                    prefix_rows,
                    key_fields=("bucket", "prefix"),
                    timestamp_fields=("recursive_indexed_at", "listed_at"),
                )
            )
            object_files = self._write_compacted_objects(compacted_objects)
            prefix_files = self._write_compacted_prefixes(compacted_prefixes)
            self.manifest_store.save_while_locked(
                manifest.with_active_files(
                    object_files=object_files,
                    prefix_files=prefix_files,
                    indexing_mode=indexing_mode,
                )
            )
        logger.info(
            "Compacted bucket index",
            extra={
                "bucket": manifest.bucket,
                "object_rows_before": len(object_rows),
                "object_rows_after": len(compacted_objects),
                "prefix_rows_before": len(prefix_rows),
                "prefix_rows_after": len(compacted_prefixes),
            },
        )
        return CompactionResult(
            object_rows_before=len(object_rows),
            object_rows_after=len(compacted_objects),
            prefix_rows_before=len(prefix_rows),
            prefix_rows_after=len(compacted_prefixes),
            object_files_before=len(manifest.object_files),
            object_files_after=len(object_files),
            prefix_files_before=len(manifest.prefix_files),
            prefix_files_after=len(prefix_files),
        )

    def _write_compacted_objects(self, rows: tuple[ObjectMetadata, ...]) -> tuple[IndexFile, ...]:
        written_files: list[IndexFile] = []
        for partition_hash, partition_rows in _group_objects_by_partition(rows).items():
            relative_path = (
                Path("objects")
                / f"partition_prefix_hash={partition_hash}"
                / f"compact-{uuid4().hex}.parquet"
            )
            _write_table(self.index_dir / relative_path, _objects_to_table(partition_rows))
            written_files.append(
                IndexFile(
                    path=relative_path.as_posix(),
                    row_count=len(partition_rows),
                    created_at=datetime.now(UTC),
                )
            )
        return tuple(written_files)

    def _write_compacted_prefixes(self, rows: tuple[PrefixMetadata, ...]) -> tuple[IndexFile, ...]:
        if not rows:
            return ()
        relative_path = Path("prefixes") / f"compact-{uuid4().hex}.parquet"
        _write_table(self.index_dir / relative_path, _prefixes_to_table(rows))
        return (
            IndexFile(
                path=relative_path.as_posix(),
                row_count=len(rows),
                created_at=datetime.now(UTC),
            ),
        )


def _write_table(path: Path, table: pa.Table) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, path)  # type: ignore[no-untyped-call]


def _part_name() -> str:
    return f"part-{uuid4().hex}.parquet"


def _group_objects_by_partition(
    rows: tuple[ObjectMetadata, ...],
) -> dict[str, tuple[ObjectMetadata, ...]]:
    grouped: dict[str, list[ObjectMetadata]] = {}
    for row in rows:
        grouped.setdefault(_partition_hash(row.parent_prefix), []).append(row)
    return {key: tuple(value) for key, value in grouped.items()}


def _partition_hash(parent_prefix: str) -> str:
    return hashlib.blake2b(parent_prefix.encode("utf-8"), digest_size=1).hexdigest()


def _objects_to_table(rows: tuple[ObjectMetadata, ...]) -> pa.Table:
    return pa.Table.from_pylist([_object_to_row(row) for row in rows], schema=OBJECT_SCHEMA)


def _prefixes_to_table(rows: tuple[PrefixMetadata, ...]) -> pa.Table:
    return pa.Table.from_pylist([_prefix_to_row(row) for row in rows], schema=PREFIX_SCHEMA)


def _object_to_row(row: ObjectMetadata) -> dict[str, Any]:
    return {
        "provider": row.provider,
        "account_id": row.account_id,
        "bucket": row.bucket,
        "key": row.key,
        "parent_prefix": _normalize_prefix(row.parent_prefix),
        "name": row.name,
        "size": row.size,
        "last_modified": _normalize_datetime(row.last_modified),
        "etag": row.etag,
        "checksum": row.checksum,
        "storage_class": row.storage_class,
        "content_type": row.content_type,
        "encryption": row.encryption,
        "version_id": row.version_id,
        "is_delete_marker": row.is_delete_marker,
        "discovered_at": _normalize_datetime(row.discovered_at),
        "refreshed_at": _normalize_datetime(row.refreshed_at),
        "source_listing_id": row.source_listing_id,
    }


def _prefix_to_row(row: PrefixMetadata) -> dict[str, Any]:
    return {
        "provider": row.provider,
        "account_id": row.account_id,
        "bucket": row.bucket,
        "prefix": _normalize_prefix(row.prefix),
        "parent_prefix": _normalize_prefix(row.parent_prefix),
        "name": row.name,
        "object_count": row.object_count,
        "recursive_object_count": row.recursive_object_count,
        "total_size": row.total_size,
        "recursive_total_size": row.recursive_total_size,
        "fully_indexed": row.fully_indexed,
        "listed_at": _normalize_datetime(row.listed_at),
        "recursive_indexed_at": _normalize_optional_datetime(row.recursive_indexed_at),
    }


def _read_object_rows(
    index_dir: Path,
    files: tuple[IndexFile, ...],
    parent_prefix: str,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for file in files:
        table = pq.read_table(  # type: ignore[no-untyped-call]
            index_dir / file.path,
            filters=[("parent_prefix", "=", parent_prefix)],
        )
        rows.extend(_table_to_rows(table))
    return rows


def _read_prefix_rows(
    index_dir: Path,
    files: tuple[IndexFile, ...],
    parent_prefix: str,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for file in files:
        table = pq.read_table(  # type: ignore[no-untyped-call]
            index_dir / file.path,
            filters=[("parent_prefix", "=", parent_prefix)],
        )
        rows.extend(_table_to_rows(table))
    return rows


def _read_all_object_rows(
    index_dir: Path,
    files: tuple[IndexFile, ...],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for file in files:
        rows.extend(_table_to_rows(pq.read_table(index_dir / file.path)))  # type: ignore[no-untyped-call]
    return rows


def _read_all_prefix_rows(
    index_dir: Path,
    files: tuple[IndexFile, ...],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for file in files:
        rows.extend(_table_to_rows(pq.read_table(index_dir / file.path)))  # type: ignore[no-untyped-call]
    return rows


def _table_to_rows(table: pa.Table) -> list[dict[str, Any]]:
    return cast("list[dict[str, Any]]", table.to_pylist())


def _latest_by_identity(
    rows: list[dict[str, Any]],
    *,
    key_fields: tuple[str, ...],
    timestamp_fields: tuple[str, ...],
) -> tuple[dict[str, Any], ...]:
    latest: dict[tuple[Any, ...], dict[str, Any]] = {}
    for row in rows:
        key = tuple(row[field] for field in key_fields)
        existing = latest.get(key)
        if existing is None or _row_timestamp(row, timestamp_fields) >= _row_timestamp(
            existing, timestamp_fields
        ):
            latest[key] = row
    return tuple(sorted(latest.values(), key=lambda row: str(row.get("name", ""))))


def _row_timestamp(row: dict[str, Any], fields: tuple[str, ...]) -> datetime:
    for field in fields:
        value = row.get(field)
        if isinstance(value, datetime):
            return _normalize_datetime(value)
    return datetime.min.replace(tzinfo=UTC)


def _object_from_row(row: dict[str, Any]) -> ObjectMetadata:
    return ObjectMetadata(
        provider=str(row["provider"]),
        account_id=str(row["account_id"]),
        bucket=str(row["bucket"]),
        key=str(row["key"]),
        parent_prefix=str(row["parent_prefix"]),
        name=str(row["name"]),
        size=int(row["size"]),
        last_modified=_coerce_datetime(row["last_modified"]),
        etag=_optional_str(row["etag"]),
        checksum=_optional_str(row["checksum"]),
        storage_class=_optional_str(row["storage_class"]),
        content_type=_optional_str(row["content_type"]),
        encryption=_optional_str(row["encryption"]),
        version_id=_optional_str(row["version_id"]),
        is_delete_marker=bool(row["is_delete_marker"]),
        discovered_at=_coerce_datetime(row["discovered_at"]),
        refreshed_at=_coerce_datetime(row["refreshed_at"]),
        source_listing_id=str(row["source_listing_id"]),
    )


def _prefix_from_row(row: dict[str, Any]) -> PrefixMetadata:
    recursive_indexed_at = row["recursive_indexed_at"]
    return PrefixMetadata(
        provider=str(row["provider"]),
        account_id=str(row["account_id"]),
        bucket=str(row["bucket"]),
        prefix=str(row["prefix"]),
        parent_prefix=str(row["parent_prefix"]),
        name=str(row["name"]),
        object_count=int(row["object_count"]),
        recursive_object_count=int(row["recursive_object_count"]),
        total_size=int(row["total_size"]),
        recursive_total_size=int(row["recursive_total_size"]),
        fully_indexed=bool(row["fully_indexed"]),
        listed_at=_coerce_datetime(row["listed_at"]),
        recursive_indexed_at=(
            _coerce_datetime(recursive_indexed_at) if recursive_indexed_at is not None else None
        ),
    )


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


def _coerce_datetime(value: object) -> datetime:
    if isinstance(value, datetime):
        return _normalize_datetime(value)
    msg = f"Expected datetime value, got {type(value).__name__}"
    raise TypeError(msg)


def _optional_str(value: object) -> str | None:
    if value is None:
        return None
    return str(value)
