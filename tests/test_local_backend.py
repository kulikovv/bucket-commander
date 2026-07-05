import asyncio
from collections.abc import Coroutine
from pathlib import Path
from typing import Any, TypeVar

import pytest

from bc.backends import BackendError, BackendErrorKind, LocalBackend
from bc.core import EntryType, LocalLocation, parse_location

T = TypeVar("T")


def local(path: Path) -> LocalLocation:
    parsed = parse_location(path)
    assert isinstance(parsed, LocalLocation)
    return parsed


def run(coro: Coroutine[Any, Any, T]) -> T:
    return asyncio.run(coro)


def test_list_returns_entries_with_directories_first(tmp_path: Path) -> None:
    file_payload = "zeta"
    (tmp_path / "zeta.txt").write_text(file_payload, encoding="utf-8")
    (tmp_path / "alpha").mkdir()
    (tmp_path / "alpha" / "nested.txt").write_text("nested", encoding="utf-8")

    entries = run(LocalBackend().list(local(tmp_path)))

    assert [(entry.name, entry.entry_type) for entry in entries] == [
        ("alpha", EntryType.DIRECTORY),
        ("zeta.txt", EntryType.FILE),
    ]
    assert entries[1].size == len(file_payload)
    assert entries[1].modified_at is not None


def test_stat_returns_single_entry(tmp_path: Path) -> None:
    target = tmp_path / "document.txt"
    payload = "content"
    target.write_text(payload, encoding="utf-8")

    entry = run(LocalBackend().stat(local(target)))

    assert entry.name == "document.txt"
    assert entry.entry_type == EntryType.FILE
    assert entry.size == len(payload)


def test_preview_reads_file_with_truncation(tmp_path: Path) -> None:
    target = tmp_path / "document.txt"
    target.write_text("abcdef", encoding="utf-8")

    preview = run(LocalBackend().preview(local(target), max_bytes=3))

    assert preview.data == b"abc"
    assert preview.truncated


def test_mkdir_creates_directory(tmp_path: Path) -> None:
    target = local(tmp_path / "one" / "two")

    result = run(LocalBackend().mkdir(target))

    assert result.ok
    assert result.entries_affected == 1
    assert target.path.is_dir()


def test_copy_file_to_existing_directory(tmp_path: Path) -> None:
    source = tmp_path / "source.txt"
    destination = tmp_path / "destination"
    payload = "payload"
    source.write_text(payload, encoding="utf-8")
    destination.mkdir()

    result = run(LocalBackend().copy(local(source), local(destination)))

    assert result.ok
    assert result.entries_affected == 1
    assert result.bytes_affected == len(payload)
    assert (destination / "source.txt").read_text(encoding="utf-8") == payload


def test_copy_directory_merges_into_target(tmp_path: Path) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "copy"
    payload = "payload"
    source.mkdir()
    (source / "nested.txt").write_text(payload, encoding="utf-8")

    result = run(LocalBackend().copy(local(source), local(destination)))

    assert result.ok
    assert result.entries_affected == len((source, source / "nested.txt"))
    assert result.bytes_affected == len(payload)
    assert (destination / "nested.txt").read_text(encoding="utf-8") == payload


def test_move_file_to_exact_target(tmp_path: Path) -> None:
    source = tmp_path / "source.txt"
    destination = tmp_path / "renamed.txt"
    source.write_text("payload", encoding="utf-8")

    result = run(LocalBackend().move(local(source), local(destination)))

    assert result.ok
    assert not source.exists()
    assert destination.read_text(encoding="utf-8") == "payload"


def test_rename_keeps_parent_directory(tmp_path: Path) -> None:
    source = tmp_path / "source.txt"
    source.write_text("payload", encoding="utf-8")

    result = run(LocalBackend().rename(local(source), "renamed.txt"))

    assert result.ok
    assert not source.exists()
    assert (tmp_path / "renamed.txt").is_file()


def test_delete_file_and_directory(tmp_path: Path) -> None:
    file_path = tmp_path / "document.txt"
    directory_path = tmp_path / "directory"
    file_path.write_text("payload", encoding="utf-8")
    directory_path.mkdir()
    (directory_path / "nested.txt").write_text("nested", encoding="utf-8")

    file_result = run(LocalBackend().delete(local(file_path)))
    directory_result = run(LocalBackend().delete(local(directory_path), recursive=True))

    assert file_result.ok
    assert directory_result.ok
    assert not file_path.exists()
    assert not directory_path.exists()


def test_delete_directory_requires_recursive_flag(tmp_path: Path) -> None:
    directory_path = tmp_path / "directory"
    directory_path.mkdir()

    with pytest.raises(BackendError) as error_info:
        run(LocalBackend().delete(local(directory_path)))

    assert error_info.value.kind == BackendErrorKind.IS_A_DIRECTORY


def test_missing_path_raises_normalized_backend_error(tmp_path: Path) -> None:
    with pytest.raises(BackendError) as error_info:
        run(LocalBackend().stat(local(tmp_path / "missing.txt")))

    assert error_info.value.kind == BackendErrorKind.NOT_FOUND


def test_rejects_non_local_locations() -> None:
    with pytest.raises(BackendError) as error_info:
        run(LocalBackend().list(parse_location("s3://bucket/prefix/")))

    assert error_info.value.kind == BackendErrorKind.INVALID_LOCATION
