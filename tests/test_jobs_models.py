from datetime import UTC, datetime
from pathlib import Path

import pytest

from bc.core import Entry, EntryType, parse_location
from bc.jobs import (
    JobItem,
    JobItemStatus,
    JobPhase,
    JobRecord,
    JobStatus,
    plan_copy,
    transition_item_status,
    transition_job_status,
)


def test_job_status_transitions_are_validated(tmp_path: Path) -> None:
    entry = Entry(
        location=parse_location(tmp_path / "document.txt"),
        name="document.txt",
        entry_type=EntryType.FILE,
    )
    plan = plan_copy(
        (entry,),
        source_panel="left",
        destination=parse_location(tmp_path / "target"),
    )
    record = JobRecord(
        job_id="job-test",
        plan=plan,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        updated_at=datetime(2026, 1, 1, tzinfo=UTC),
    )

    running = transition_job_status(record, JobStatus.RUNNING, phase=JobPhase.COPYING)

    assert running.status is JobStatus.RUNNING
    assert running.phase is JobPhase.COPYING
    assert running.started_at is not None

    with pytest.raises(ValueError, match="Invalid job transition"):
        transition_job_status(running, JobStatus.QUEUED)


def test_job_item_status_transitions_are_validated(tmp_path: Path) -> None:
    item = JobItem(
        item_id="item-test",
        job_id="job-test",
        item_index=0,
        name="document.txt",
        location=parse_location(tmp_path / "document.txt"),
        entry_type=EntryType.FILE,
    )

    running = transition_item_status(item, JobItemStatus.RUNNING)
    completed = transition_item_status(running, JobItemStatus.COMPLETED)

    assert running.attempts == 1
    assert completed.status is JobItemStatus.COMPLETED

    with pytest.raises(ValueError, match="Invalid job item transition"):
        transition_item_status(completed, JobItemStatus.RUNNING)
