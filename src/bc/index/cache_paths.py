"""Cache path and source identity helpers for bucket indexes."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

from bc.core import S3Location


def default_cache_root() -> Path:
    configured = os.environ.get("BUCKET_COMMANDER_CACHE_ROOT")
    if configured:
        return Path(configured).expanduser()
    xdg_cache_home = os.environ.get("XDG_CACHE_HOME")
    if xdg_cache_home:
        return Path(xdg_cache_home).expanduser() / "bucket-commander"
    return Path.home() / ".cache" / "bucket-commander"


def index_dir(cache_root: Path, location: S3Location) -> Path:
    return (
        cache_root
        / "indexes"
        / "s3"
        / safe_segment(account_id(location))
        / safe_segment(scope_id(location))
        / safe_segment(location.bucket)
    )


def account_id(location: S3Location) -> str:
    if location.profile:
        return f"profile-{location.profile}"
    if location.endpoint_url:
        return f"endpoint-{short_hash(location.endpoint_url)}"
    return "default"


def scope_id(location: S3Location) -> str:
    parts = [location.region or "default-region"]
    if location.endpoint_url:
        parts.append(short_hash(location.endpoint_url))
    return "-".join(parts)


def short_hash(value: str) -> str:
    return hashlib.blake2b(value.encode("utf-8"), digest_size=6).hexdigest()


def safe_segment(value: str) -> str:
    allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-")
    return "".join(character if character in allowed else "_" for character in value)
