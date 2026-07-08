from pathlib import Path

from bc.core import Entry, EntryType, S3Location, parse_location
from bc.jobs import (
    DurableJobQueue,
    ExpansionState,
    IndexedPlanEstimate,
    JobStatus,
    OperationKind,
    SQLiteJobStore,
    plan_copy,
    plan_delete,
    plan_move,
)

INDEXED_COUNT = 3
INDEXED_BYTES = 300


def test_sqlite_job_store_persists_copy_move_delete_plans(tmp_path: Path) -> None:
    store = SQLiteJobStore(tmp_path / "jobs.sqlite3")
    source = Entry(
        location=parse_location(tmp_path / "source.txt"),
        name="source.txt",
        entry_type=EntryType.FILE,
        size=12,
    )
    destination = S3Location(
        bucket="bucket-commander",
        prefix="uploads/",
        profile="dev",
        region="us-east-1",
        endpoint_url="http://127.0.0.1:9000/",
    )

    copy_record = store.add_plan(
        plan_copy((source,), source_panel="left", destination=destination),
        job_id="job-copy",
    )
    move_record = store.add_plan(
        plan_move((source,), source_panel="left", destination=parse_location(tmp_path / "dest")),
        job_id="job-move",
    )
    delete_record = store.add_plan(
        plan_delete((source,), source_panel="left"),
        job_id="job-delete",
    )

    reopened = SQLiteJobStore(tmp_path / "jobs.sqlite3")
    records = reopened.list_jobs()

    assert [record.job_id for record in records] == [
        copy_record.job_id,
        move_record.job_id,
        delete_record.job_id,
    ]
    assert [record.kind for record in records] == [
        OperationKind.COPY,
        OperationKind.MOVE,
        OperationKind.DELETE,
    ]
    assert records[0].plan.destination == destination
    assert records[0].plan.entries[0].location == source.location
    assert reopened.list_items(copy_record.job_id)[0].name == "source.txt"


def test_durable_queue_updates_status_in_store(tmp_path: Path) -> None:
    store = SQLiteJobStore(tmp_path / "jobs.sqlite3")
    queue = DurableJobQueue(store)
    entry = Entry(
        location=parse_location(tmp_path / "source.txt"),
        name="source.txt",
        entry_type=EntryType.FILE,
    )
    record = queue.enqueue(
        plan_delete((entry,), source_panel="right"),
    )

    paused = queue.pause(record.job_id)
    resumed = queue.resume(record.job_id)
    cancelled = queue.request_cancel(record.job_id)

    assert paused.status is JobStatus.PAUSED
    assert resumed.status is JobStatus.QUEUED
    assert cancelled.status is JobStatus.CANCELLED
    stored = store.get_job(record.job_id)
    assert stored is not None
    assert stored.status is JobStatus.CANCELLED


def test_planner_can_apply_indexed_estimates_to_bucket_prefix(tmp_path: Path) -> None:
    prefix = Entry(
        location=S3Location(bucket="bucket-commander", prefix="logs/"),
        name="logs",
        entry_type=EntryType.PREFIX,
    )
    destination = parse_location(tmp_path)

    plan = plan_copy(
        (prefix,),
        source_panel="left",
        destination=destination,
        indexed_estimates={
            prefix.uri: IndexedPlanEstimate(
                expanded_count=INDEXED_COUNT,
                estimated_bytes=INDEXED_BYTES,
                expansion_state=ExpansionState.PARTIAL,
            )
        },
    )

    assert plan.expanded_count == INDEXED_COUNT
    assert plan.estimated_bytes == INDEXED_BYTES
    assert plan.expansion_state is ExpansionState.PARTIAL
