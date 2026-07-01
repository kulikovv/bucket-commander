from pathlib import Path

import pytest

from bc.core.locations import LocalLocation, LocationError, S3Location, parse_location


def test_absolute_local_path_round_trips(tmp_path: Path) -> None:
    location = parse_location(tmp_path)

    assert isinstance(location, LocalLocation)
    assert location.path == tmp_path
    assert parse_location(location.uri) == location


def test_relative_local_path_is_normalized(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)

    location = parse_location("data")

    assert isinstance(location, LocalLocation)
    assert location.path == tmp_path / "data"
    assert location.uri == (tmp_path / "data").as_uri()


def test_file_uri_with_encoded_characters_round_trips(tmp_path: Path) -> None:
    path = tmp_path / "hello world"
    location = parse_location(path.as_uri())

    assert isinstance(location, LocalLocation)
    assert location.path == path
    assert parse_location(location.uri) == location


def test_s3_bucket_root_round_trips() -> None:
    location = parse_location("s3://example-bucket")

    assert location == S3Location(bucket="example-bucket")
    assert location.uri == "s3://example-bucket/"
    assert parse_location(location.uri) == location


def test_s3_prefix_is_normalized_and_round_trips() -> None:
    location = parse_location("s3://example-bucket/path/to/prefix")

    assert location == S3Location(bucket="example-bucket", prefix="path/to/prefix/")
    assert location.uri == "s3://example-bucket/path/to/prefix/"
    assert parse_location(location.uri) == location


def test_s3_prefix_parent_and_child() -> None:
    location = S3Location(bucket="example-bucket", prefix="a/b/")

    assert location.parent() == S3Location(bucket="example-bucket", prefix="a/")
    assert location.child("c") == S3Location(bucket="example-bucket", prefix="a/b/c/")


@pytest.mark.parametrize(
    "value",
    [
        "",
        "https://example.com",
        "file://remote-host/tmp/data",
        "s3://",
    ],
)
def test_invalid_locations_have_clear_domain_error(value: str) -> None:
    with pytest.raises(LocationError):
        parse_location(value)
