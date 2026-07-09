"""Provider profile settings without embedded credentials."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Self

FORBIDDEN_PROFILE_KEY_PARTS = ("access_key", "secret", "token", "password")


class ProfilesConfigError(ValueError):
    """Raised when provider profile configuration is invalid."""


@dataclass(frozen=True, slots=True)
class S3ProviderProfile:
    """Non-secret S3-compatible provider routing metadata."""

    name: str = "default"
    profile_name: str | None = None
    region_name: str | None = None
    endpoint_url: str | None = None

    @classmethod
    def from_mapping(cls, data: Mapping[str, object], *, default_name: str = "default") -> Self:
        _reject_secret_fields(data)
        return cls(
            name=_optional_str(data, "name") or default_name,
            profile_name=_optional_str(data, "profile_name") or _optional_str(data, "profile"),
            region_name=_optional_str(data, "region_name") or _optional_str(data, "region"),
            endpoint_url=_optional_str(data, "endpoint_url"),
        )


@dataclass(frozen=True, slots=True)
class ProviderProfiles:
    """Configured provider profiles."""

    default_s3: S3ProviderProfile = S3ProviderProfile()
    s3_profiles: tuple[S3ProviderProfile, ...] = ()

    @classmethod
    def from_mapping(cls, data: Mapping[str, object]) -> Self:
        _reject_secret_fields(data)
        default_s3 = S3ProviderProfile.from_mapping(_mapping(data.get("s3", {})))
        named = data.get("profiles", {})
        s3_profiles = _profile_s3_entries(named)
        return cls(default_s3=default_s3, s3_profiles=s3_profiles)


def _profile_s3_entries(value: object) -> tuple[S3ProviderProfile, ...]:
    if value is None:
        return ()
    profiles = _mapping(value)
    raw_s3 = profiles.get("s3")
    if raw_s3 is None:
        return ()
    if not isinstance(raw_s3, list):
        msg = "profiles.s3 must be a TOML array of tables"
        raise ProfilesConfigError(msg)
    return tuple(
        S3ProviderProfile.from_mapping(_mapping(item), default_name=f"s3-{index}")
        for index, item in enumerate(raw_s3)
    )


def _mapping(value: object) -> Mapping[str, object]:
    if isinstance(value, Mapping):
        return value
    msg = "Expected a TOML table"
    raise ProfilesConfigError(msg)


def _optional_str(data: Mapping[str, object], key: str) -> str | None:
    value = data.get(key)
    if value is None:
        return None
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    msg = f"{key!r} must be a string"
    raise ProfilesConfigError(msg)


def _reject_secret_fields(data: Mapping[str, object]) -> None:
    for key, value in data.items():
        normalized = key.lower().replace("-", "_")
        if any(part in normalized for part in FORBIDDEN_PROFILE_KEY_PARTS):
            msg = f"Refusing to load credential field {key!r}; use profiles or environment"
            raise ProfilesConfigError(msg)
        if isinstance(value, Mapping):
            _reject_secret_fields(value)
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, Mapping):
                    _reject_secret_fields(item)
