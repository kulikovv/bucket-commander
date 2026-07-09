"""Application settings and environment overrides."""

from __future__ import annotations

import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Self

from bc.config.profiles import ProfilesConfigError, ProviderProfiles, S3ProviderProfile
from bc.config.sources import FORBIDDEN_KEY_PARTS
from bc.index.cache_paths import default_cache_root

DEFAULT_LISTING_CONCURRENCY = 4
DEFAULT_TRANSFER_CONCURRENCY = 4
DEFAULT_INDEX_TTL_SECONDS = 24 * 60 * 60
DEFAULT_MULTIPART_UPLOAD_THRESHOLD = 8 * 1024 * 1024
DEFAULT_MULTIPART_DOWNLOAD_THRESHOLD = 8 * 1024 * 1024


class SettingsConfigError(ValueError):
    """Raised when application settings are invalid."""


@dataclass(frozen=True, slots=True)
class OperationSettings:
    """Configurable operation behavior."""

    max_listing_concurrency: int = DEFAULT_LISTING_CONCURRENCY
    max_transfer_concurrency: int = DEFAULT_TRANSFER_CONCURRENCY
    multipart_upload_threshold: int = DEFAULT_MULTIPART_UPLOAD_THRESHOLD
    multipart_download_threshold: int = DEFAULT_MULTIPART_DOWNLOAD_THRESHOLD
    default_conflict_behavior: str = "backend default"
    confirm_destructive: bool = True

    def __post_init__(self) -> None:
        if self.max_listing_concurrency < 1:
            msg = "max_listing_concurrency must be at least 1"
            raise ValueError(msg)
        if self.max_transfer_concurrency < 1:
            msg = "max_transfer_concurrency must be at least 1"
            raise ValueError(msg)
        if self.multipart_upload_threshold < 1:
            msg = "multipart_upload_threshold must be at least 1"
            raise ValueError(msg)
        if self.multipart_download_threshold < 1:
            msg = "multipart_download_threshold must be at least 1"
            raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class ProviderSettings:
    """Configurable provider defaults."""

    default_provider: str = "local"


@dataclass(frozen=True, slots=True)
class IndexSettings:
    """Configurable metadata index behavior."""

    ttl_seconds: int = DEFAULT_INDEX_TTL_SECONDS

    def __post_init__(self) -> None:
        if self.ttl_seconds < 0:
            msg = "index ttl_seconds must not be negative"
            raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class UISettings:
    """Configurable UI behavior."""

    theme: str = "default"
    keymap: str = "default"
    show_hidden_files: bool = False
    show_advanced_metadata: bool = False


@dataclass(frozen=True, slots=True)
class AppSettings:
    """Top-level Bucket Commander settings."""

    cache_root: Path
    profiles: ProviderProfiles = field(default_factory=ProviderProfiles)
    provider: ProviderSettings = field(default_factory=ProviderSettings)
    index: IndexSettings = field(default_factory=IndexSettings)
    operations: OperationSettings = field(default_factory=OperationSettings)
    ui: UISettings = field(default_factory=UISettings)

    @classmethod
    def defaults(cls) -> Self:
        return cls(cache_root=default_cache_root())

    @classmethod
    def from_mapping(cls, data: Mapping[str, object]) -> Self:
        _reject_secret_fields(data)
        try:
            profiles = ProviderProfiles.from_mapping(data)
        except ProfilesConfigError as error:
            raise SettingsConfigError(str(error)) from error
        cache_root = _cache_root_from_mapping(_mapping(data.get("cache", {})))
        return cls(
            cache_root=cache_root,
            profiles=profiles,
            provider=_provider_settings(_mapping(data.get("provider", {}))),
            index=_index_settings(_mapping(data.get("index", {}))),
            operations=_operation_settings(_mapping(data.get("operations", {}))),
            ui=_ui_settings(_mapping(data.get("ui", {}))),
        )

    def with_environment(self, environ: Mapping[str, str] | None = None) -> AppSettings:
        """Return settings with documented environment overrides applied."""

        env = environ or os.environ
        cache_root = Path(env["BUCKET_COMMANDER_CACHE_DIR"]).expanduser() if env.get(
            "BUCKET_COMMANDER_CACHE_DIR"
        ) else self.cache_root
        s3 = self.profiles.default_s3
        default_s3 = S3ProviderProfile(
            name=s3.name,
            profile_name=(
                env.get("AWS_PROFILE")
                or env.get("BUCKET_COMMANDER_S3_PROFILE")
                or s3.profile_name
            ),
            region_name=env.get("AWS_REGION") or env.get("AWS_DEFAULT_REGION") or s3.region_name,
            endpoint_url=env.get("BUCKET_COMMANDER_S3_ENDPOINT_URL") or s3.endpoint_url,
        )
        return replace(
            self,
            cache_root=cache_root,
            profiles=replace(self.profiles, default_s3=default_s3),
        )


def default_settings_config_path() -> Path:
    """Return the user-level application settings path."""

    configured = os.environ.get("BUCKET_COMMANDER_CONFIG")
    if configured:
        return Path(configured).expanduser()
    config_home = os.environ.get("XDG_CONFIG_HOME")
    base = Path(config_home).expanduser() if config_home else Path.home() / ".config"
    return base / "bucket-commander" / "config.toml"


def load_app_settings(path: Path | None = None) -> AppSettings:
    """Load app settings, returning defaults when the file is absent."""

    config_path = path or default_settings_config_path()
    if not config_path.exists():
        return AppSettings.defaults().with_environment()
    try:
        with config_path.open("rb") as handle:
            data = tomllib.load(handle)
    except tomllib.TOMLDecodeError as error:
        msg = f"Invalid settings config {config_path}: {error}"
        raise SettingsConfigError(msg) from error
    try:
        return AppSettings.from_mapping(data).with_environment()
    except (SettingsConfigError, ValueError) as error:
        msg = f"Invalid settings config {config_path}: {error}"
        raise SettingsConfigError(msg) from error


def _cache_root_from_mapping(data: Mapping[str, object]) -> Path:
    value = data.get("root") or data.get("cache_root")
    if value is None:
        return default_cache_root()
    if isinstance(value, str) and value.strip():
        return Path(value).expanduser()
    msg = "cache.root must be a non-empty string"
    raise SettingsConfigError(msg)


def _operation_settings(data: Mapping[str, object]) -> OperationSettings:
    return OperationSettings(
        max_listing_concurrency=_optional_int(
            data,
            "max_listing_concurrency",
            DEFAULT_LISTING_CONCURRENCY,
        ),
        max_transfer_concurrency=_optional_int(
            data,
            "max_transfer_concurrency",
            DEFAULT_TRANSFER_CONCURRENCY,
        ),
        multipart_upload_threshold=_optional_int(
            data,
            "multipart_upload_threshold",
            DEFAULT_MULTIPART_UPLOAD_THRESHOLD,
        ),
        multipart_download_threshold=_optional_int(
            data,
            "multipart_download_threshold",
            DEFAULT_MULTIPART_DOWNLOAD_THRESHOLD,
        ),
        default_conflict_behavior=_optional_str(
            data,
            "default_conflict_behavior",
        )
        or "backend default",
        confirm_destructive=_optional_bool(data, "confirm_destructive", True),
    )


def _provider_settings(data: Mapping[str, object]) -> ProviderSettings:
    return ProviderSettings(
        default_provider=_optional_str(data, "default_provider") or "local",
    )


def _index_settings(data: Mapping[str, object]) -> IndexSettings:
    return IndexSettings(
        ttl_seconds=_optional_int(data, "ttl_seconds", DEFAULT_INDEX_TTL_SECONDS),
    )


def _ui_settings(data: Mapping[str, object]) -> UISettings:
    return UISettings(
        theme=_optional_str(data, "theme") or "default",
        keymap=_optional_str(data, "keymap") or "default",
        show_hidden_files=_optional_bool(data, "show_hidden_files", False),
        show_advanced_metadata=_optional_bool(data, "show_advanced_metadata", False),
    )


def _mapping(value: object) -> Mapping[str, object]:
    if isinstance(value, Mapping):
        return value
    msg = "Expected a TOML table"
    raise SettingsConfigError(msg)


def _optional_str(data: Mapping[str, object], key: str) -> str | None:
    value = data.get(key)
    if value is None:
        return None
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    msg = f"{key!r} must be a string"
    raise SettingsConfigError(msg)


def _optional_int(data: Mapping[str, object], key: str, default: int) -> int:
    value = data.get(key)
    if value is None:
        return default
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    msg = f"{key!r} must be an integer"
    raise SettingsConfigError(msg)


def _optional_bool(data: Mapping[str, object], key: str, default: bool) -> bool:
    value = data.get(key)
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    msg = f"{key!r} must be a boolean"
    raise SettingsConfigError(msg)


def _reject_secret_fields(data: Mapping[str, object]) -> None:
    for key, value in data.items():
        normalized = key.lower().replace("-", "_")
        if any(part in normalized for part in FORBIDDEN_KEY_PARTS):
            msg = f"Refusing to load credential field {key!r}; use profiles or environment"
            raise SettingsConfigError(msg)
        if isinstance(value, Mapping):
            _reject_secret_fields(value)
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, Mapping):
                    _reject_secret_fields(item)
