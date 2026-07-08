from pathlib import Path

from bc.core import Entry, EntryType, S3Location, parse_location
from bc.jobs import ExpansionState, OperationKind, plan_delete, plan_move

DIRECT_ENTRY_COUNT = 2
TOTAL_BYTES = 30


def test_delete_plan_summarizes_direct_files(tmp_path: Path) -> None:
    first = Entry(
        location=parse_location(tmp_path / "first.txt"),
        name="first.txt",
        entry_type=EntryType.FILE,
        size=10,
    )
    second = Entry(
        location=S3Location(bucket="bucket-commander", prefix="logs/second.txt"),
        name="second.txt",
        entry_type=EntryType.OBJECT,
        size=20,
    )

    plan = plan_delete((first, second), source_panel="left")

    assert plan.kind is OperationKind.DELETE
    assert plan.direct_count == DIRECT_ENTRY_COUNT
    assert plan.expanded_count == DIRECT_ENTRY_COUNT
    assert plan.estimated_bytes == TOTAL_BYTES
    assert plan.expansion_state is ExpansionState.COMPLETE
    assert plan.requires_confirmation
    assert plan.destructive_phases == ("delete selected entries",)


def test_move_plan_marks_container_expansion_unknown(tmp_path: Path) -> None:
    directory = Entry(
        location=parse_location(tmp_path / "folder"),
        name="folder",
        entry_type=EntryType.DIRECTORY,
    )
    destination = parse_location(tmp_path / "target")

    plan = plan_move((directory,), source_panel="right", destination=destination)

    assert plan.kind is OperationKind.MOVE
    assert plan.destination == destination
    assert plan.expanded_count is None
    assert plan.estimated_bytes is None
    assert plan.expansion_state is ExpansionState.UNKNOWN
    assert plan.destructive_phases == (
        "copy to destination",
        "verify copy",
        "delete source after verify",
    )
