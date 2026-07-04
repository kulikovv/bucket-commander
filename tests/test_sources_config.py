from pathlib import Path

import pytest

from bc.config import SourcesConfigError, load_sources_config
from bc.core import S3Location


def test_load_sources_config_reads_non_secret_s3_metadata(tmp_path: Path) -> None:
    config_path = tmp_path / "sources.toml"
    config_path.write_text(
        """
[[sources]]
name = "Local MinIO"
uri = "s3://bucket-commander/logs/"
region = "us-east-1"
endpoint_url = "http://127.0.0.1:9000"
credential_source = "environment"
""",
        encoding="utf-8",
    )

    config = load_sources_config(config_path)

    assert len(config.sources) == 1
    source = config.sources[0]
    assert isinstance(source.location, S3Location)
    assert source.name == "Local MinIO"
    assert source.location.bucket == "bucket-commander"
    assert source.location.prefix == "logs/"
    assert source.location.region == "us-east-1"
    assert source.location.endpoint_url == "http://127.0.0.1:9000"
    assert source.credential_source == "environment"


def test_load_sources_config_accepts_local_locations(tmp_path: Path) -> None:
    config_path = tmp_path / "sources.toml"
    local_path = tmp_path / "workspace"
    config_path.write_text(
        f"""
[[sources]]
name = "Workspace"
uri = "{local_path}"
""",
        encoding="utf-8",
    )

    config = load_sources_config(config_path)

    assert config.sources[0].name == "Workspace"
    assert config.sources[0].location.label == str(local_path)


def test_load_sources_config_missing_file_is_empty(tmp_path: Path) -> None:
    assert load_sources_config(tmp_path / "missing.toml").sources == ()


def test_load_sources_config_rejects_explicit_credentials(tmp_path: Path) -> None:
    config_path = tmp_path / "sources.toml"
    config_path.write_text(
        """
[[sources]]
name = "Unsafe"
uri = "s3://unsafe/"
aws_secret_access_key = "do-not-store-me"
""",
        encoding="utf-8",
    )

    with pytest.raises(SourcesConfigError, match="credential field"):
        load_sources_config(config_path)
