"""Recursive bucket indexer with durable checkpoints."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol, TypeVar
from uuid import uuid4

from bc.core import Entry, EntryType, S3Location
from bc.core.task_manager import ProgressSink
from bc.index.cache_paths import account_id, default_cache_root, index_dir
from bc.index.metadata import metadata_rows
from bc.index.parquet_store import ObjectMetadata, ParquetIndexStore, PrefixMetadata

Result = TypeVar("Result")
CHECKPOINT_PATH = Path("checkpoints") / "recursive-index.json"
DEFAULT_INDEX_BATCH_SIZE = 1000
DEFAULT_INDEX_CONCURRENCY = 4


class PrefixLister(Protocol):
    async def list(self, location: S3Location) -> tuple[Entry, ...]:
        """List direct entries under one bucket prefix."""


@dataclass(frozen=True, slots=True)
class RecursiveIndexResult:
    """Summary returned after a recursive indexing run."""

    root: S3Location
    prefixes_indexed: int
    objects_indexed: int
    bytes_indexed: int
    checkpoint_path: Path


@dataclass(frozen=True, slots=True)
class RecursiveIndexCheckpoint:
    """Durable resume state for recursive bucket indexing."""

    bucket: str
    root_prefix: str
    source_listing_id: str
    pending_prefixes: tuple[str, ...]
    completed_prefixes: tuple[str, ...]
    objects_indexed: int = 0
    bytes_indexed: int = 0
    updated_at: datetime = field(default_factory=lambda: datetime.min.replace(tzinfo=UTC))

    @classmethod
    def start(cls, location: S3Location) -> RecursiveIndexCheckpoint:
        return cls(
            bucket=location.bucket,
            root_prefix=location.prefix,
            source_listing_id=f"recursive-{uuid4().hex}",
            pending_prefixes=(location.prefix,),
            completed_prefixes=(),
            updated_at=datetime.now(UTC),
        )

    def matches(self, location: S3Location) -> bool:
        return self.bucket == location.bucket and self.root_prefix == location.prefix

    def with_pending(
        self,
        pending_prefixes: set[str],
        completed_prefixes: set[str],
        *,
        objects_indexed: int,
        bytes_indexed: int,
    ) -> RecursiveIndexCheckpoint:
        return replace(
            self,
            pending_prefixes=tuple(sorted(pending_prefixes)),
            completed_prefixes=tuple(sorted(completed_prefixes)),
            objects_indexed=objects_indexed,
            bytes_indexed=bytes_indexed,
            updated_at=datetime.now(UTC),
        )

    def to_json(self) -> dict[str, object]:
        return {
            "bucket": self.bucket,
            "root_prefix": self.root_prefix,
            "source_listing_id": self.source_listing_id,
            "pending_prefixes": list(self.pending_prefixes),
            "completed_prefixes": list(self.completed_prefixes),
            "objects_indexed": self.objects_indexed,
            "bytes_indexed": self.bytes_indexed,
            "updated_at": _format_datetime(self.updated_at),
        }

    @classmethod
    def from_json(cls, data: dict[str, object]) -> RecursiveIndexCheckpoint:
        return cls(
            bucket=str(data["bucket"]),
            root_prefix=str(data["root_prefix"]),
            source_listing_id=str(data["source_listing_id"]),
            pending_prefixes=_strings(data.get("pending_prefixes")),
            completed_prefixes=_strings(data.get("completed_prefixes")),
            objects_indexed=_int_value(data.get("objects_indexed")),
            bytes_indexed=_int_value(data.get("bytes_indexed")),
            updated_at=_parse_datetime(str(data["updated_at"])),
        )


@dataclass(frozen=True, slots=True)
class CheckpointStore:
    """Read and replace recursive indexer checkpoint state."""

    index_dir: Path

    @property
    def path(self) -> Path:
        return self.index_dir / CHECKPOINT_PATH

    def exists(self) -> bool:
        return self.path.exists()

    def load(self) -> RecursiveIndexCheckpoint:
        with self.path.open("r", encoding="utf-8") as checkpoint_file:
            data = json.load(checkpoint_file)
        if not isinstance(data, dict):
            msg = f"Checkpoint must contain a JSON object: {self.path}"
            raise ValueError(msg)
        return RecursiveIndexCheckpoint.from_json(data)

    def save(self, checkpoint: RecursiveIndexCheckpoint) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = self.path.with_suffix(".json.tmp")
        with temporary_path.open("w", encoding="utf-8") as checkpoint_file:
            json.dump(checkpoint.to_json(), checkpoint_file, indent=2, sort_keys=True)
            checkpoint_file.write("\n")
        temporary_path.replace(self.path)


@dataclass(slots=True)
class _PrefixStats:
    direct_object_count: int = 0
    direct_total_size: int = 0
    recursive_object_count: int = 0
    recursive_total_size: int = 0
    child_prefixes: set[str] | None = None

    @property
    def children(self) -> set[str]:
        if self.child_prefixes is None:
            self.child_prefixes = set()
        return self.child_prefixes


@dataclass(frozen=True, slots=True)
class RecursiveBucketIndexer:
    """Index all objects under one S3 bucket or prefix."""

    backend: PrefixLister
    cache_root: Path
    batch_size: int = DEFAULT_INDEX_BATCH_SIZE
    max_concurrency: int = DEFAULT_INDEX_CONCURRENCY

    @classmethod
    def default(cls, backend: PrefixLister) -> RecursiveBucketIndexer:
        return cls(backend=backend, cache_root=default_cache_root())

    async def index(
        self,
        location: S3Location,
        *,
        progress: ProgressSink | None = None,
        resume: bool = True,
    ) -> RecursiveIndexResult:
        store = self._open_store(location)
        checkpoint_store = CheckpointStore(store.index_dir)
        checkpoint = self._initial_checkpoint(checkpoint_store, location, resume=resume)
        session = _IndexSession(
            backend=self.backend,
            location=location,
            store=store,
            checkpoint_store=checkpoint_store,
            checkpoint=checkpoint,
            progress=progress,
            batch_size=max(1, self.batch_size),
            max_concurrency=max(1, self.max_concurrency),
        )
        await session.run()
        await _append_fully_indexed_summaries(store, location, session.stats, session.completed)
        for prefix in sorted(session.completed):
            await _to_thread(store.mark_prefix_fully_indexed, prefix, indexing_mode="recursive")
        await session.save_checkpoint()
        return RecursiveIndexResult(
            root=location,
            prefixes_indexed=len(session.completed),
            objects_indexed=session.objects_indexed,
            bytes_indexed=session.bytes_indexed,
            checkpoint_path=checkpoint_store.path,
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

    def _initial_checkpoint(
        self,
        store: CheckpointStore,
        location: S3Location,
        *,
        resume: bool,
    ) -> RecursiveIndexCheckpoint:
        if resume and store.exists():
            checkpoint = store.load()
            if checkpoint.matches(location):
                return checkpoint
        return RecursiveIndexCheckpoint.start(location)


@dataclass(slots=True)
class _IndexSession:
    backend: PrefixLister
    location: S3Location
    store: ParquetIndexStore
    checkpoint_store: CheckpointStore
    checkpoint: RecursiveIndexCheckpoint
    progress: ProgressSink | None
    batch_size: int
    max_concurrency: int
    pending: set[str] = field(init=False)
    completed: set[str] = field(init=False)
    queued: asyncio.Queue[str] = field(init=False)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    stats: dict[str, _PrefixStats] = field(default_factory=dict)
    objects_indexed: int = field(init=False)
    bytes_indexed: int = field(init=False)

    def __post_init__(self) -> None:
        self.pending = set(self.checkpoint.pending_prefixes or (self.location.prefix,))
        self.completed = set(self.checkpoint.completed_prefixes)
        self.queued = asyncio.Queue()
        for prefix in sorted(self.pending):
            self.queued.put_nowait(prefix)
        self.objects_indexed = self.checkpoint.objects_indexed
        self.bytes_indexed = self.checkpoint.bytes_indexed

    async def run(self) -> None:
        await asyncio.gather(*(self.worker() for _ in range(self.max_concurrency)))

    async def worker(self) -> None:
        while True:
            try:
                prefix = self.queued.get_nowait()
            except asyncio.QueueEmpty:
                return
            try:
                await self.process_prefix(prefix)
            finally:
                self.queued.task_done()

    async def process_prefix(self, prefix: str) -> None:
        async with self.lock:
            if prefix in self.completed:
                self.pending.discard(prefix)
                return
            self.pending.add(prefix)
            self.update_progress(prefix)
            await self.save_checkpoint()

        prefix_location = _location_for_prefix(self.location, prefix)
        entries = await self.backend.list(prefix_location)
        objects, prefixes = metadata_rows(
            prefix_location,
            entries,
            source_listing_id=self.checkpoint.source_listing_id,
        )
        await _write_listing(self.store, prefix_location, objects, prefixes, self.batch_size)
        await self.record_completed_prefix(
            prefix, entries, len(objects), sum(row.size for row in objects)
        )

    async def record_completed_prefix(
        self,
        prefix: str,
        entries: tuple[Entry, ...],
        object_count: int,
        byte_count: int,
    ) -> None:
        child_prefixes = _child_prefixes(entries)
        async with self.lock:
            prefix_stats = self.stats.setdefault(prefix, _PrefixStats())
            prefix_stats.direct_object_count = object_count
            prefix_stats.direct_total_size = byte_count
            prefix_stats.children.update(child_prefixes)
            for child_prefix in child_prefixes:
                if child_prefix not in self.completed and child_prefix not in self.pending:
                    self.pending.add(child_prefix)
                    self.queued.put_nowait(child_prefix)
            self.pending.discard(prefix)
            self.completed.add(prefix)
            self.objects_indexed += object_count
            self.bytes_indexed += byte_count
            self.update_progress(prefix)
            await self.save_checkpoint()

    async def save_checkpoint(self) -> None:
        current = self.checkpoint.with_pending(
            self.pending,
            self.completed,
            objects_indexed=self.objects_indexed,
            bytes_indexed=self.bytes_indexed,
        )
        await _to_thread(self.checkpoint_store.save, current)
        await _to_thread(self.store.mark_checkpoint, CHECKPOINT_PATH.as_posix())

    def update_progress(self, current_prefix: str) -> None:
        _progress(
            self.progress,
            self.pending,
            self.completed,
            current_prefix,
            self.objects_indexed,
            self.bytes_indexed,
        )


async def _write_listing(
    store: ParquetIndexStore,
    location: S3Location,
    objects: tuple[ObjectMetadata, ...],
    prefixes: tuple[PrefixMetadata, ...],
    batch_size: int,
) -> None:
    if prefixes:
        await _to_thread(store.append_prefixes, prefixes, indexing_mode="recursive")
    if objects:
        for batch in _chunks(objects, batch_size):
            await _to_thread(
                store.append_objects,
                batch,
                covered_prefix=location.prefix,
                indexing_mode="recursive",
            )
    else:
        await _to_thread(store.mark_prefix_listed, location.prefix, indexing_mode="recursive")


async def _append_fully_indexed_summaries(
    store: ParquetIndexStore,
    root: S3Location,
    stats: dict[str, _PrefixStats],
    completed: set[str],
) -> None:
    _compute_recursive_stats(root.prefix, stats)
    rows: list[PrefixMetadata] = []
    now = datetime.now(UTC)
    for prefix in sorted(completed):
        if prefix == root.prefix:
            continue
        prefix_stats = stats.get(prefix, _PrefixStats())
        rows.append(
            PrefixMetadata(
                provider="s3",
                account_id=account_id(root),
                bucket=root.bucket,
                prefix=prefix,
                parent_prefix=_parent_prefix(prefix),
                name=_prefix_name(prefix),
                object_count=prefix_stats.direct_object_count,
                recursive_object_count=prefix_stats.recursive_object_count,
                total_size=prefix_stats.direct_total_size,
                recursive_total_size=prefix_stats.recursive_total_size,
                fully_indexed=True,
                listed_at=now,
                recursive_indexed_at=now,
            )
        )
    if rows:
        await _to_thread(store.append_prefixes, tuple(rows), indexing_mode="recursive")


def _compute_recursive_stats(prefix: str, stats: dict[str, _PrefixStats]) -> _PrefixStats:
    prefix_stats = stats.setdefault(prefix, _PrefixStats())
    recursive_count = prefix_stats.direct_object_count
    recursive_size = prefix_stats.direct_total_size
    for child_prefix in sorted(prefix_stats.children):
        child_stats = _compute_recursive_stats(child_prefix, stats)
        recursive_count += child_stats.recursive_object_count
        recursive_size += child_stats.recursive_total_size
    prefix_stats.recursive_object_count = recursive_count
    prefix_stats.recursive_total_size = recursive_size
    return prefix_stats


def _child_prefixes(entries: tuple[Entry, ...]) -> set[str]:
    return {
        entry.location.prefix
        for entry in entries
        if entry.entry_type is EntryType.PREFIX and isinstance(entry.location, S3Location)
    }


def _progress(
    progress: ProgressSink | None,
    pending: set[str],
    completed: set[str],
    current_prefix: str,
    objects_indexed: int,
    bytes_indexed: int,
) -> None:
    if progress is None:
        return
    progress.update(
        items_total=len(pending) + len(completed),
        items_done=len(completed),
        bytes_done=bytes_indexed,
        current_item=current_prefix or "/",
        message=f"Indexed {objects_indexed} objects",
    )


def _location_for_prefix(location: S3Location, prefix: str) -> S3Location:
    return S3Location(
        bucket=location.bucket,
        prefix=prefix,
        profile=location.profile,
        region=location.region,
        endpoint_url=location.endpoint_url,
    )


def _parent_prefix(prefix: str) -> str:
    normalized = prefix.strip("/")
    if not normalized:
        return ""
    parts = normalized.rsplit("/", maxsplit=1)
    if len(parts) == 1:
        return ""
    return f"{parts[0]}/"


def _prefix_name(prefix: str) -> str:
    normalized = prefix.strip("/")
    return normalized.rsplit("/", maxsplit=1)[-1] if normalized else "/"


Item = TypeVar("Item")


def _chunks(values: tuple[Item, ...], size: int) -> tuple[tuple[Item, ...], ...]:
    return tuple(tuple(values[index : index + size]) for index in range(0, len(values), size))


def _strings(value: object) -> tuple[str, ...]:
    if isinstance(value, list | tuple):
        return tuple(str(item) for item in value)
    return ()


def _int_value(value: object) -> int:
    if value is None:
        return 0
    if isinstance(value, int | str | bytes | bytearray):
        return int(value)
    msg = f"Expected integer-compatible checkpoint value, got {type(value).__name__}"
    raise TypeError(msg)


async def _to_thread(
    function: Callable[..., Result],
    *args: object,
    **kwargs: object,
) -> Result:
    return await asyncio.to_thread(function, *args, **kwargs)


def _format_datetime(value: datetime) -> str:
    normalized = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    return normalized.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
