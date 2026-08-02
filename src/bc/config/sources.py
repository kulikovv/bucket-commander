"""Known local and S3 source configuration without stored credentials."""

from __future__ import annotations

import os
import tomllib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Self

from bc.core import Location, LocationError, S3Location, parse_location

FORBIDDEN_KEY_PARTS = ("access_key", "secret", "token", "password")


class SourcesConfigError(ValueError):
    """Raised when a known-source configuration file is invalid."""


@dataclass(frozen=True, slots=True)
class KnownSource:
    """A selectable local or S3 location."""

    name: str
    location: Location
    credential_source: str = "default-chain"

    @property
    def label(self) -> str:
        return f"{self.name} ({self.location.uri})"

    @classmethod
    def from_mapping(cls, data: Mapping[str, object], *, index: int) -> Self:
        _reject_secret_fields(data)
        name = _required_str(data, "name", index=index)
        uri = _required_str(data, "uri", index=index)
        credential_source = _optional_str(data, "credential_source") or "default-chain"
        try:
            parsed = parse_location(uri)
        except LocationError as error:
            msg = f"sources[{index}] has invalid uri: {error}"
            raise SourcesConfigError(msg) from error
        location: Location = parsed
        if isinstance(parsed, S3Location):
            location = S3Location(
                bucket=parsed.bucket,
                prefix=parsed.prefix,
                profile=_optional_str(data, "profile"),
                region=_optional_str(data, "region"),
                endpoint_url=_optional_str(data, "endpoint_url"),
            )
        return cls(
            name=name,
            location=location,
            credential_source=credential_source,
        )


@dataclass(frozen=True, slots=True)
class SourcesConfig:
    """Known source entries loaded from disk."""

    sources: tuple[KnownSource, ...] = ()

    @classmethod
    def from_mapping(cls, data: Mapping[str, object]) -> Self:
        _reject_secret_fields(data)
        raw_sources = data.get("sources", ())
        if not isinstance(raw_sources, list):
            msg = "sources must be a TOML array of tables"
            raise SourcesConfigError(msg)
        return cls(
            sources=tuple(
                KnownSource.from_mapping(_mapping(item, index), index=index)
                for index, item in enumerate(raw_sources)
            )
        )


KnownS3Source = KnownSource


def default_sources_config_path() -> Path:
    """Return the user-level known-source config path."""

    config_home = os.environ.get("XDG_CONFIG_HOME")
    base = Path(config_home).expanduser() if config_home else Path.home() / ".config"
    return base / "bucket-commander" / "sources.toml"


def project_sources_config_path() -> Path:
    """Return the repo-local known-source config path for development runs."""

    return Path.cwd() / "config" / "sources.toml"


def resolve_sources_config_path(path: Path | None = None) -> Path:
    """Return the known-source config path an explicit or discovered load would use."""

    return path or _discover_sources_config_path()


def discovered_bucket_sources(
    bucket_names: Iterable[str],
    *,
    profile: str | None = None,
    region: str | None = None,
    endpoint_url: str | None = None,
) -> tuple[KnownSource, ...]:
    """Build known-source entries for buckets discovered in a provider account."""

    return tuple(
        KnownSource(
            name=name,
            location=S3Location(
                bucket=name,
                profile=profile,
                region=region,
                endpoint_url=endpoint_url,
            ),
        )
        for name in sorted(set(bucket_names))
    )


def write_sources_config(path: Path, config: SourcesConfig) -> None:
    """Write known sources as TOML routing metadata; entries never carry credentials."""

    lines = ["# Known sources for Bucket Commander. Do not store credentials here.", ""]
    for source in config.sources:
        lines.append("[[sources]]")
        lines.append(f"name = {_toml_string(source.name)}")
        lines.append(f"uri = {_toml_string(source.location.uri)}")
        if isinstance(source.location, S3Location):
            for key, value in (
                ("profile", source.location.profile),
                ("region", source.location.region),
                ("endpoint_url", source.location.endpoint_url),
            ):
                if value is not None:
                    lines.append(f"{key} = {_toml_string(value)}")
        if source.credential_source != "default-chain":
            lines.append(f"credential_source = {_toml_string(source.credential_source)}")
        lines.append("")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


def _toml_string(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def load_sources_config(path: Path | None = None) -> SourcesConfig:
    """Load known location sources, returning an empty config when the file is absent."""

    config_path = path or _discover_sources_config_path()
    if not config_path.exists():
        return SourcesConfig()
    try:
        with config_path.open("rb") as handle:
            data = tomllib.load(handle)
    except tomllib.TOMLDecodeError as error:
        msg = f"Invalid sources config {config_path}: {error}"
        raise SourcesConfigError(msg) from error
    try:
        return SourcesConfig.from_mapping(data)
    except SourcesConfigError as error:
        msg = f"Invalid sources config {config_path}: {error}"
        raise SourcesConfigError(msg) from error


def _discover_sources_config_path() -> Path:
    project_path = project_sources_config_path()
    if project_path.exists():
        return project_path
    return default_sources_config_path()


def _mapping(value: object, index: int) -> Mapping[str, object]:
    if isinstance(value, Mapping):
        return value
    msg = f"sources[{index}] must be a table"
    raise SourcesConfigError(msg)


def _required_str(data: Mapping[str, object], key: str, *, index: int) -> str:
    value = data.get(key)
    if isinstance(value, str) and value.strip():
        return value.strip()
    msg = f"sources[{index}] {key!r} must be a non-empty string"
    raise SourcesConfigError(msg)


def _optional_str(data: Mapping[str, object], key: str) -> str | None:
    value = data.get(key)
    if value is None:
        return None
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    msg = f"{key!r} must be a string"
    raise SourcesConfigError(msg)


def _reject_secret_fields(data: Mapping[str, object]) -> None:
    for key, value in data.items():
        normalized = key.lower().replace("-", "_")
        if any(part in normalized for part in FORBIDDEN_KEY_PARTS):
            msg = f"Refusing to load credential field {key!r}; use an AWS profile or environment"
            raise SourcesConfigError(msg)
        if isinstance(value, Mapping):
            _reject_secret_fields(value)
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, Mapping):
                    _reject_secret_fields(item)
