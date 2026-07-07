"""Manifest persistence for append-only bucket index datasets."""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Self

INDEX_SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True)
class IndexFile:
    """A Parquet data file tracked by the manifest."""

    path: str
    row_count: int
    created_at: datetime

    def __post_init__(self) -> None:
        if not self.path:
            msg = "Index file path must not be empty"
            raise ValueError(msg)
        if self.row_count < 0:
            msg = "Index file row count must be non-negative"
            raise ValueError(msg)

    def to_json(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "row_count": self.row_count,
            "created_at": _format_datetime(self.created_at),
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Self:
        return cls(
            path=str(data["path"]),
            row_count=int(data["row_count"]),
            created_at=_parse_datetime(str(data["created_at"])),
        )


@dataclass(frozen=True, slots=True)
class CoveredPrefix:
    """Freshness and completeness metadata for one logical prefix."""

    prefix: str
    listed_at: datetime
    fully_indexed: bool = False
    recursive_indexed_at: datetime | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "prefix": self.prefix,
            "listed_at": _format_datetime(self.listed_at),
            "fully_indexed": self.fully_indexed,
            "recursive_indexed_at": _format_optional_datetime(self.recursive_indexed_at),
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Self:
        recursive_indexed_at = data.get("recursive_indexed_at")
        return cls(
            prefix=str(data["prefix"]),
            listed_at=_parse_datetime(str(data["listed_at"])),
            fully_indexed=bool(data.get("fully_indexed", False)),
            recursive_indexed_at=(
                _parse_datetime(str(recursive_indexed_at)) if recursive_indexed_at else None
            ),
        )


@dataclass(frozen=True, slots=True)
class IndexManifest:
    """Durable manifest for active Parquet object and prefix files."""

    provider: str
    account_id: str
    bucket: str
    created_at: datetime
    updated_at: datetime
    schema_version: int = INDEX_SCHEMA_VERSION
    region: str | None = None
    endpoint: str | None = None
    stale_ttl_seconds: int | None = None
    indexing_history: tuple[str, ...] = ()
    covered_prefixes: tuple[CoveredPrefix, ...] = ()
    object_files: tuple[IndexFile, ...] = ()
    prefix_files: tuple[IndexFile, ...] = ()
    last_successful_checkpoint: str | None = None

    def __post_init__(self) -> None:
        if self.schema_version != INDEX_SCHEMA_VERSION:
            msg = f"Unsupported index schema version: {self.schema_version}"
            raise ValueError(msg)
        if not self.provider:
            msg = "Manifest provider must not be empty"
            raise ValueError(msg)
        if not self.account_id:
            msg = "Manifest account_id must not be empty"
            raise ValueError(msg)
        if not self.bucket:
            msg = "Manifest bucket must not be empty"
            raise ValueError(msg)
        if self.stale_ttl_seconds is not None and self.stale_ttl_seconds < 0:
            msg = "stale_ttl_seconds must be non-negative"
            raise ValueError(msg)

    @classmethod
    def create(
        cls,
        *,
        provider: str,
        account_id: str,
        bucket: str,
        region: str | None = None,
        endpoint: str | None = None,
        stale_ttl: timedelta | None = None,
        now: datetime | None = None,
    ) -> Self:
        timestamp = _normalize_datetime(now or datetime.now(UTC))
        return cls(
            provider=provider,
            account_id=account_id,
            bucket=bucket,
            region=region,
            endpoint=endpoint,
            stale_ttl_seconds=int(stale_ttl.total_seconds()) if stale_ttl else None,
            created_at=timestamp,
            updated_at=timestamp,
        )

    def with_object_files(
        self,
        files: tuple[IndexFile, ...],
        *,
        covered_prefix: str | None = None,
        now: datetime | None = None,
        indexing_mode: str | None = None,
    ) -> IndexManifest:
        updated = _normalize_datetime(now or datetime.now(UTC))
        return replace(
            self,
            updated_at=updated,
            indexing_history=_append_optional(self.indexing_history, indexing_mode),
            covered_prefixes=_upsert_covered_prefix(
                self.covered_prefixes,
                covered_prefix,
                updated,
                fully_indexed=False,
            ),
            object_files=(*self.object_files, *files),
        )

    def with_prefix_files(
        self,
        files: tuple[IndexFile, ...],
        *,
        now: datetime | None = None,
        indexing_mode: str | None = None,
    ) -> IndexManifest:
        updated = _normalize_datetime(now or datetime.now(UTC))
        return replace(
            self,
            updated_at=updated,
            indexing_history=_append_optional(self.indexing_history, indexing_mode),
            prefix_files=(*self.prefix_files, *files),
        )

    def with_covered_prefix(
        self,
        prefix: str,
        *,
        fully_indexed: bool,
        now: datetime | None = None,
        indexing_mode: str | None = None,
    ) -> IndexManifest:
        updated = _normalize_datetime(now or datetime.now(UTC))
        return replace(
            self,
            updated_at=updated,
            indexing_history=_append_optional(self.indexing_history, indexing_mode),
            covered_prefixes=_upsert_covered_prefix(
                self.covered_prefixes,
                prefix,
                updated,
                fully_indexed=fully_indexed,
            ),
        )

    def with_checkpoint(
        self,
        checkpoint: str,
        *,
        now: datetime | None = None,
    ) -> IndexManifest:
        updated = _normalize_datetime(now or datetime.now(UTC))
        return replace(
            self,
            updated_at=updated,
            last_successful_checkpoint=checkpoint,
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "provider": self.provider,
            "account_id": self.account_id,
            "bucket": self.bucket,
            "region": self.region,
            "endpoint": self.endpoint,
            "created_at": _format_datetime(self.created_at),
            "updated_at": _format_datetime(self.updated_at),
            "indexing_history": list(self.indexing_history),
            "covered_prefixes": [prefix.to_json() for prefix in self.covered_prefixes],
            "stale_ttl_seconds": self.stale_ttl_seconds,
            "object_files": [file.to_json() for file in self.object_files],
            "prefix_files": [file.to_json() for file in self.prefix_files],
            "last_successful_checkpoint": self.last_successful_checkpoint,
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Self:
        return cls(
            schema_version=int(data["schema_version"]),
            provider=str(data["provider"]),
            account_id=str(data["account_id"]),
            bucket=str(data["bucket"]),
            region=data.get("region"),
            endpoint=data.get("endpoint"),
            created_at=_parse_datetime(str(data["created_at"])),
            updated_at=_parse_datetime(str(data["updated_at"])),
            indexing_history=tuple(str(item) for item in data.get("indexing_history", ())),
            covered_prefixes=tuple(
                CoveredPrefix.from_json(item) for item in data.get("covered_prefixes", ())
            ),
            stale_ttl_seconds=data.get("stale_ttl_seconds"),
            object_files=tuple(IndexFile.from_json(item) for item in data.get("object_files", ())),
            prefix_files=tuple(IndexFile.from_json(item) for item in data.get("prefix_files", ())),
            last_successful_checkpoint=data.get("last_successful_checkpoint"),
        )


@dataclass(frozen=True, slots=True)
class ManifestStore:
    """Read and replace `manifest.json` under an index directory."""

    index_dir: Path

    @property
    def path(self) -> Path:
        return self.index_dir / "manifest.json"

    def exists(self) -> bool:
        return self.path.exists()

    def load(self) -> IndexManifest:
        with self.path.open("r", encoding="utf-8") as manifest_file:
            data = json.load(manifest_file)
        if not isinstance(data, dict):
            msg = f"Manifest must contain a JSON object: {self.path}"
            raise ValueError(msg)
        return IndexManifest.from_json(data)

    def save(self, manifest: IndexManifest) -> None:
        self.index_dir.mkdir(parents=True, exist_ok=True)
        temporary_path = self.path.with_suffix(".json.tmp")
        with temporary_path.open("w", encoding="utf-8") as manifest_file:
            json.dump(manifest.to_json(), manifest_file, indent=2, sort_keys=True)
            manifest_file.write("\n")
        temporary_path.replace(self.path)


def _append_optional(values: tuple[str, ...], value: str | None) -> tuple[str, ...]:
    if value is None:
        return values
    return (*values, value)


def _upsert_covered_prefix(
    values: tuple[CoveredPrefix, ...],
    prefix: str | None,
    listed_at: datetime,
    *,
    fully_indexed: bool,
) -> tuple[CoveredPrefix, ...]:
    if prefix is None:
        return values
    normalized = prefix.strip("/")
    if normalized:
        normalized = f"{normalized}/"
    replacement = CoveredPrefix(
        prefix=normalized,
        listed_at=listed_at,
        fully_indexed=fully_indexed,
        recursive_indexed_at=listed_at if fully_indexed else None,
    )
    return (*tuple(value for value in values if value.prefix != normalized), replacement)


def _normalize_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _format_datetime(value: datetime) -> str:
    return _normalize_datetime(value).isoformat().replace("+00:00", "Z")


def _format_optional_datetime(value: datetime | None) -> str | None:
    if value is None:
        return None
    return _format_datetime(value)


def _parse_datetime(value: str) -> datetime:
    normalized = value.replace("Z", "+00:00")
    return _normalize_datetime(datetime.fromisoformat(normalized))
