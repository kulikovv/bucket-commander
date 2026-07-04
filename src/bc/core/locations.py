"""Normalized location values for local files and object-storage buckets."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar, TypeAlias
from urllib.parse import quote, unquote, urlsplit


class LocationError(ValueError):
    """Raised when a user-provided location cannot be normalized."""


@dataclass(frozen=True, slots=True)
class LocalLocation:
    """A normalized local filesystem location."""

    path: Path

    provider: ClassVar[str] = "file"

    def __post_init__(self) -> None:
        if not self.path.is_absolute():
            msg = f"Local location must be absolute: {self.path}"
            raise LocationError(msg)

    @property
    def uri(self) -> str:
        return self.path.as_uri()

    @property
    def label(self) -> str:
        return str(self.path)

    @property
    def name(self) -> str:
        return self.path.name or self.path.anchor

    def parent(self) -> LocalLocation | None:
        parent = self.path.parent
        if parent == self.path:
            return None
        return LocalLocation(parent)

    def child(self, name: str) -> LocalLocation:
        if not name or "/" in name:
            msg = f"Invalid local child name: {name!r}"
            raise LocationError(msg)
        return LocalLocation(self.path / name)

    def __str__(self) -> str:
        return self.uri


@dataclass(frozen=True, slots=True)
class S3Location:
    """A normalized S3 bucket or prefix location."""

    bucket: str
    prefix: str = ""
    profile: str | None = None
    region: str | None = None
    endpoint_url: str | None = None

    provider: ClassVar[str] = "s3"

    def __post_init__(self) -> None:
        if not self.bucket:
            msg = "S3 location requires a bucket name"
            raise LocationError(msg)
        if "/" in self.bucket:
            msg = f"S3 bucket name must not contain '/': {self.bucket!r}"
            raise LocationError(msg)
        if self.prefix.startswith("/"):
            msg = f"S3 prefix must not start with '/': {self.prefix!r}"
            raise LocationError(msg)
        normalized = _normalize_s3_prefix(self.prefix)
        object.__setattr__(self, "prefix", normalized)

    @property
    def uri(self) -> str:
        if not self.prefix:
            return f"s3://{self.bucket}/"
        encoded_prefix = quote(self.prefix, safe="/")
        return f"s3://{self.bucket}/{encoded_prefix}"

    @property
    def label(self) -> str:
        return self.uri

    @property
    def name(self) -> str:
        if not self.prefix:
            return self.bucket
        return self.prefix.rstrip("/").rsplit("/", maxsplit=1)[-1]

    def parent(self) -> S3Location | None:
        if not self.prefix:
            return None
        parts = self.prefix.rstrip("/").rsplit("/", maxsplit=1)
        parent_prefix = parts[0] if len(parts) > 1 else ""
        if parent_prefix:
            parent_prefix = f"{parent_prefix}/"
        return S3Location(
            bucket=self.bucket,
            prefix=parent_prefix,
            profile=self.profile,
            region=self.region,
            endpoint_url=self.endpoint_url,
        )

    def child(self, name: str) -> S3Location:
        if not name or "/" in name:
            msg = f"Invalid S3 prefix child name: {name!r}"
            raise LocationError(msg)
        return S3Location(
            bucket=self.bucket,
            prefix=f"{self.prefix}{name}/",
            profile=self.profile,
            region=self.region,
            endpoint_url=self.endpoint_url,
        )

    def __str__(self) -> str:
        return self.uri


Location: TypeAlias = LocalLocation | S3Location


def parse_location(value: str | os.PathLike[str]) -> Location:
    """Parse a user location into a normalized value object."""

    text = os.fspath(value).strip()
    if not text:
        msg = "Location must not be empty"
        raise LocationError(msg)

    parsed = urlsplit(text)
    if parsed.scheme == "file":
        return _parse_file_uri(parsed.netloc, parsed.path, text)
    if parsed.scheme == "s3":
        return _parse_s3_uri(parsed.netloc, parsed.path)
    if parsed.scheme:
        msg = f"Unsupported location scheme: {parsed.scheme!r}"
        raise LocationError(msg)
    return _parse_local_path(text)


def _parse_local_path(value: str) -> LocalLocation:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    return LocalLocation(path.resolve())


def _parse_file_uri(netloc: str, path: str, original: str) -> LocalLocation:
    if netloc and netloc != "localhost":
        msg = f"Only local file URIs are supported: {original!r}"
        raise LocationError(msg)
    if not path:
        msg = f"File URI requires a path: {original!r}"
        raise LocationError(msg)
    return LocalLocation(Path(unquote(path)).resolve())


def _parse_s3_uri(bucket: str, path: str) -> S3Location:
    if not bucket:
        msg = "S3 URI requires a bucket name"
        raise LocationError(msg)
    prefix = unquote(path.lstrip("/"))
    return S3Location(bucket=bucket, prefix=prefix)


def _normalize_s3_prefix(prefix: str) -> str:
    normalized = prefix.strip("/")
    if not normalized:
        return ""
    return f"{normalized}/"
