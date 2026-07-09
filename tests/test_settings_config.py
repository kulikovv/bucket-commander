from pathlib import Path
from typing import Any

import pytest

from bc.config import SettingsConfigError, load_app_settings

DEFAULT_CONCURRENCY = 4
LISTING_CONCURRENCY = 2
TRANSFER_CONCURRENCY = 3
ENV_TRANSFER_CONCURRENCY = 7
INDEX_TTL_SECONDS = 600
MULTIPART_UPLOAD_THRESHOLD = 16 * 1024 * 1024
MULTIPART_DOWNLOAD_THRESHOLD = 32 * 1024 * 1024


def test_load_app_settings_missing_file_uses_defaults(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.delenv("BUCKET_COMMANDER_CACHE_DIR", raising=False)
    monkeypatch.delenv("BUCKET_COMMANDER_S3_ENDPOINT_URL", raising=False)

    settings = load_app_settings(tmp_path / "missing.toml")

    assert settings.cache_root.name == "bucket-commander"
    assert settings.provider.default_provider == "local"
    assert settings.index.ttl_seconds == 24 * 60 * 60
    assert settings.operations.max_listing_concurrency == DEFAULT_CONCURRENCY
    assert settings.operations.max_transfer_concurrency == DEFAULT_CONCURRENCY
    assert settings.operations.multipart_upload_threshold == 8 * 1024 * 1024
    assert settings.operations.multipart_download_threshold == 8 * 1024 * 1024
    assert settings.operations.confirm_destructive
    assert settings.ui.theme == "default"


def test_load_app_settings_reads_cache_s3_operations_and_ui(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    cache_root = tmp_path / "cache"
    config_path.write_text(
        f"""
[cache]
root = "{cache_root}"

[s3]
profile = "dev"
region = "us-east-1"
endpoint_url = "http://127.0.0.1:9000/"

[provider]
default_provider = "s3"

[index]
ttl_seconds = {INDEX_TTL_SECONDS}

[operations]
max_listing_concurrency = {LISTING_CONCURRENCY}
max_transfer_concurrency = {TRANSFER_CONCURRENCY}
multipart_upload_threshold = {MULTIPART_UPLOAD_THRESHOLD}
multipart_download_threshold = {MULTIPART_DOWNLOAD_THRESHOLD}
default_conflict_behavior = "fail if exists"
confirm_destructive = false

[ui]
theme = "high-contrast"
keymap = "commander"
show_hidden_files = true
show_advanced_metadata = true

[[profiles.s3]]
name = "archive"
profile = "archive-profile"
region = "eu-west-1"
""",
        encoding="utf-8",
    )

    settings = load_app_settings(config_path)

    assert settings.cache_root == cache_root
    assert settings.profiles.default_s3.profile_name == "dev"
    assert settings.profiles.default_s3.region_name == "us-east-1"
    assert settings.profiles.default_s3.endpoint_url == "http://127.0.0.1:9000/"
    assert settings.profiles.s3_profiles[0].name == "archive"
    assert settings.provider.default_provider == "s3"
    assert settings.index.ttl_seconds == INDEX_TTL_SECONDS
    assert settings.operations.max_listing_concurrency == LISTING_CONCURRENCY
    assert settings.operations.max_transfer_concurrency == TRANSFER_CONCURRENCY
    assert settings.operations.multipart_upload_threshold == MULTIPART_UPLOAD_THRESHOLD
    assert settings.operations.multipart_download_threshold == MULTIPART_DOWNLOAD_THRESHOLD
    assert settings.operations.default_conflict_behavior == "fail if exists"
    assert not settings.operations.confirm_destructive
    assert settings.ui.theme == "high-contrast"
    assert settings.ui.keymap == "commander"
    assert settings.ui.show_hidden_files
    assert settings.ui.show_advanced_metadata


def test_load_app_settings_environment_overrides_file(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
[cache]
root = "/from-file"

[s3]
profile = "file-profile"
region = "file-region"
endpoint_url = "http://file"
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("BUCKET_COMMANDER_CACHE_DIR", str(tmp_path / "env-cache"))
    monkeypatch.setenv("AWS_PROFILE", "env-profile")
    monkeypatch.setenv("AWS_REGION", "env-region")
    monkeypatch.setenv("BUCKET_COMMANDER_S3_ENDPOINT_URL", "http://env")

    settings = load_app_settings(config_path)

    assert settings.cache_root == tmp_path / "env-cache"
    assert settings.profiles.default_s3.profile_name == "env-profile"
    assert settings.profiles.default_s3.region_name == "env-region"
    assert settings.profiles.default_s3.endpoint_url == "http://env"


def test_load_app_settings_rejects_credentials(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
[s3]
secret_access_key = "do-not-store"
""",
        encoding="utf-8",
    )

    with pytest.raises(SettingsConfigError, match="credential field"):
        load_app_settings(config_path)


def test_load_app_settings_rejects_invalid_numeric_values(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
[index]
ttl_seconds = -1
""",
        encoding="utf-8",
    )

    with pytest.raises(SettingsConfigError, match="ttl_seconds"):
        load_app_settings(config_path)


def test_load_app_settings_uses_bucket_commander_config_env(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        f"""
[operations]
max_transfer_concurrency = {ENV_TRANSFER_CONCURRENCY}
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("BUCKET_COMMANDER_CONFIG", str(config_path))

    settings = load_app_settings()

    assert settings.operations.max_transfer_concurrency == ENV_TRANSFER_CONCURRENCY
