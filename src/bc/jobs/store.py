"""SQLite persistence for durable batch jobs."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import SupportsInt, TypeAlias
from uuid import uuid4

from bc.core import EntryType, LocalLocation, Location, S3Location
from bc.jobs.models import JobItem, JobItemStatus, JobPhase, JobRecord, JobStatus
from bc.jobs.planner import (
    ConflictPolicy,
    ExpansionState,
    OperationKind,
    OperationPlan,
    OperationPlanEntry,
)

JsonValue: TypeAlias = str | int | bool | None | dict[str, "JsonValue"] | list["JsonValue"]

SCHEMA_VERSION = 1


class SQLiteJobStore:
    """Durable SQLite store for job records and planned items."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def add_plan(
        self,
        plan: OperationPlan,
        *,
        job_id: str | None = None,
        now: datetime | None = None,
    ) -> JobRecord:
        """Persist a planned job and return the durable record."""

        timestamp = _normalize_datetime(now or datetime.now(UTC))
        record = JobRecord(
            job_id=job_id or f"job-{uuid4().hex}",
            plan=plan,
            created_at=timestamp,
            updated_at=timestamp,
        )
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO jobs (
                    job_id, kind, status, phase, source_panel, destination_json,
                    expanded_count, estimated_bytes, expansion_state, conflict_policy,
                    destructive_phases_json, resumability, latest_error,
                    created_at, updated_at, started_at, ended_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                _record_row(record),
            )
            connection.executemany(
                """
                INSERT INTO job_items (
                    item_id, job_id, item_index, status, name, location_json,
                    entry_type, size, attempts, latest_error
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                _item_rows(record.job_id, plan.entries),
            )
        return record

    def get_job(self, job_id: str) -> JobRecord | None:
        """Load one job record by id."""

        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM jobs WHERE job_id = ?",
                (job_id,),
            ).fetchone()
        if row is None:
            return None
        return _record_from_row(row, self.list_items(job_id))

    def list_jobs(self) -> tuple[JobRecord, ...]:
        """Load all durable jobs ordered by creation time."""

        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM jobs ORDER BY created_at, job_id").fetchall()
        return tuple(_record_from_row(row, self.list_items(str(row["job_id"]))) for row in rows)

    def next_queued_job(self) -> JobRecord | None:
        """Load the oldest queued job, if any."""

        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM jobs WHERE status = ? ORDER BY created_at, job_id LIMIT 1",
                (JobStatus.QUEUED.value,),
            ).fetchone()
        if row is None:
            return None
        return _record_from_row(row, self.list_items(str(row["job_id"])))

    def list_items(self, job_id: str) -> tuple[JobItem, ...]:
        """Load planned items for a job."""

        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM job_items WHERE job_id = ? ORDER BY item_index",
                (job_id,),
            ).fetchall()
        return tuple(_item_from_row(row) for row in rows)

    def update_job_status(
        self,
        record: JobRecord,
        *,
        now: datetime | None = None,
    ) -> None:
        """Persist the current status fields from a job record."""

        timestamp = _normalize_datetime(now or datetime.now(UTC))
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE jobs
                SET status = ?, phase = ?, latest_error = ?, updated_at = ?,
                    started_at = ?, ended_at = ?
                WHERE job_id = ?
                """,
                (
                    record.status.value,
                    record.phase.value,
                    record.latest_error,
                    _format_datetime(timestamp),
                    _format_optional_datetime(record.started_at),
                    _format_optional_datetime(record.ended_at),
                    record.job_id,
                ),
            )

    def update_item(self, item: JobItem) -> None:
        """Persist the current status fields from a job item."""

        with self._connect() as connection:
            connection.execute(
                """
                UPDATE job_items
                SET status = ?, attempts = ?, latest_error = ?
                WHERE item_id = ?
                """,
                (item.status.value, item.attempts, item.latest_error, item.item_id),
            )

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA user_version = 1")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    job_id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    status TEXT NOT NULL,
                    phase TEXT NOT NULL,
                    source_panel TEXT NOT NULL,
                    destination_json TEXT,
                    expanded_count INTEGER,
                    estimated_bytes INTEGER,
                    expansion_state TEXT NOT NULL,
                    conflict_policy TEXT NOT NULL,
                    destructive_phases_json TEXT NOT NULL,
                    resumability TEXT NOT NULL,
                    latest_error TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    started_at TEXT,
                    ended_at TEXT
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS job_items (
                    item_id TEXT PRIMARY KEY,
                    job_id TEXT NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
                    item_index INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    name TEXT NOT NULL,
                    location_json TEXT NOT NULL,
                    entry_type TEXT NOT NULL,
                    size INTEGER,
                    attempts INTEGER NOT NULL,
                    latest_error TEXT NOT NULL,
                    UNIQUE(job_id, item_index)
                )
                """
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection


def _record_row(record: JobRecord) -> tuple[object, ...]:
    plan = record.plan
    return (
        record.job_id,
        plan.kind.value,
        record.status.value,
        record.phase.value,
        plan.source_panel,
        _location_json(plan.destination) if plan.destination is not None else None,
        plan.expanded_count,
        plan.estimated_bytes,
        plan.expansion_state.value,
        plan.conflict_policy.value,
        json.dumps(list(plan.destructive_phases)),
        plan.resumability,
        record.latest_error,
        _format_required_datetime(record.created_at),
        _format_required_datetime(record.updated_at),
        _format_optional_datetime(record.started_at),
        _format_optional_datetime(record.ended_at),
    )


def _item_rows(
    job_id: str,
    entries: tuple[OperationPlanEntry, ...],
) -> Iterable[tuple[object, ...]]:
    for index, entry in enumerate(entries):
        yield (
            f"item-{uuid4().hex}",
            job_id,
            index,
            JobItemStatus.PENDING.value,
            entry.name,
            _location_json(entry.location),
            entry.entry_type.value,
            entry.size,
            0,
            "",
        )


def _record_from_row(row: sqlite3.Row, items: tuple[JobItem, ...]) -> JobRecord:
    plan = OperationPlan(
        kind=OperationKind(str(row["kind"])),
        entries=tuple(
            OperationPlanEntry(
                name=item.name,
                location=item.location,
                entry_type=item.entry_type,
                size=item.size,
            )
            for item in items
        ),
        source_panel=str(row["source_panel"]),
        destination=_optional_location_from_json(row["destination_json"]),
        expanded_count=_optional_int(row["expanded_count"]),
        estimated_bytes=_optional_int(row["estimated_bytes"]),
        expansion_state=ExpansionState(str(row["expansion_state"])),
        conflict_policy=ConflictPolicy(str(row["conflict_policy"])),
        destructive_phases=tuple(
            str(value) for value in json.loads(str(row["destructive_phases_json"]))
        ),
        resumability=str(row["resumability"]),
    )
    return JobRecord(
        job_id=str(row["job_id"]),
        plan=plan,
        status=JobStatus(str(row["status"])),
        phase=JobPhase(str(row["phase"])),
        created_at=_parse_datetime(str(row["created_at"])),
        updated_at=_parse_datetime(str(row["updated_at"])),
        started_at=_parse_optional_datetime(row["started_at"]),
        ended_at=_parse_optional_datetime(row["ended_at"]),
        latest_error=str(row["latest_error"]),
    )


def _item_from_row(row: sqlite3.Row) -> JobItem:
    return JobItem(
        item_id=str(row["item_id"]),
        job_id=str(row["job_id"]),
        item_index=int(row["item_index"]),
        name=str(row["name"]),
        location=_location_from_json(str(row["location_json"])),
        entry_type=EntryType(str(row["entry_type"])),
        size=_optional_int(row["size"]),
        status=JobItemStatus(str(row["status"])),
        attempts=int(row["attempts"]),
        latest_error=str(row["latest_error"]),
    )


def _location_json(location: Location) -> str:
    return json.dumps(_location_to_json(location), sort_keys=True)


def _location_to_json(location: Location) -> dict[str, JsonValue]:
    if isinstance(location, LocalLocation):
        return {"provider": "file", "path": str(location.path)}
    return {
        "provider": "s3",
        "bucket": location.bucket,
        "prefix": location.prefix,
        "profile": location.profile,
        "region": location.region,
        "endpoint_url": location.endpoint_url,
    }


def _optional_location_from_json(value: object) -> Location | None:
    if value is None:
        return None
    return _location_from_json(str(value))


def _location_from_json(value: str) -> Location:
    data = json.loads(value)
    if not isinstance(data, dict):
        msg = "Location JSON must be an object"
        raise ValueError(msg)
    provider = data.get("provider")
    if provider == "file":
        path = data.get("path")
        if not isinstance(path, str):
            msg = "Local location JSON requires a path"
            raise ValueError(msg)
        return LocalLocation(Path(path))
    if provider == "s3":
        bucket = data.get("bucket")
        prefix = data.get("prefix")
        if not isinstance(bucket, str) or not isinstance(prefix, str):
            msg = "S3 location JSON requires bucket and prefix"
            raise ValueError(msg)
        return S3Location(
            bucket=bucket,
            prefix=prefix,
            profile=_optional_str(data.get("profile")),
            region=_optional_str(data.get("region")),
            endpoint_url=_optional_str(data.get("endpoint_url")),
        )
    msg = f"Unsupported location provider in job store: {provider!r}"
    raise ValueError(msg)


def _optional_str(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    msg = f"Expected string or null, got {type(value).__name__}"
    raise ValueError(msg)


def _optional_int(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, str | bytes | bytearray | SupportsInt):
        return int(value)
    msg = f"Expected integer-compatible value, got {type(value).__name__}"
    raise ValueError(msg)


def _format_required_datetime(value: datetime | None) -> str:
    if value is None:
        msg = "Required datetime is missing"
        raise ValueError(msg)
    return _format_datetime(value)


def _format_optional_datetime(value: datetime | None) -> str | None:
    if value is None:
        return None
    return _format_datetime(value)


def _format_datetime(value: datetime) -> str:
    return _normalize_datetime(value).isoformat()


def _parse_optional_datetime(value: object) -> datetime | None:
    if value is None:
        return None
    return _parse_datetime(str(value))


def _parse_datetime(value: str) -> datetime:
    return _normalize_datetime(datetime.fromisoformat(value))


def _normalize_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
