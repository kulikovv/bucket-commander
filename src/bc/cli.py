"""Command-line entry point for Bucket Commander."""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

from bc import __version__
from bc.app import AppConfig, run_app
from bc.backends import BackendError, S3Backend, S3BackendConfig
from bc.config import (
    AppSettings,
    SettingsConfigError,
    SourcesConfig,
    SourcesConfigError,
    discovered_bucket_sources,
    load_app_settings,
    load_sources_config,
    resolve_sources_config_path,
    write_sources_config,
)
from bc.core import parse_location

BUCKET_DISCOVERY_TIMEOUT_SECONDS = 10.0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bc",
        description="Two-panel terminal file manager for local files and object-storage buckets.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    parser.add_argument(
        "--left",
        default=str(Path.cwd()),
        help="Initial location for the left panel, such as a path or s3://bucket/prefix/.",
    )
    parser.add_argument(
        "--right",
        default=str(Path.cwd()),
        help="Initial location for the right panel, such as a path or s3://bucket/prefix/.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help=(
            "Application settings TOML file. Defaults to "
            "~/.config/bucket-commander/config.toml or BUCKET_COMMANDER_CONFIG."
        ),
    )
    parser.add_argument(
        "--sources-config",
        type=Path,
        default=None,
        help=(
            "Known location sources TOML file. Defaults to config/sources.toml or "
            "~/.config/bucket-commander/sources.toml."
        ),
    )
    parser.add_argument(
        "--discover-buckets",
        action="store_true",
        help=(
            "Discover S3 buckets visible to the default credentials and add new ones "
            "to the sources config, even when the config already exists."
        ),
    )
    parser.set_defaults(command="run")
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    app_runner: Callable[[AppConfig], int] = run_app,
) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    startup_notice: str | None = None
    try:
        settings = load_app_settings(args.config)
        sources_path = resolve_sources_config_path(args.sources_config)
        sources = load_sources_config(sources_path)
        if args.discover_buckets or not sources_path.exists():
            sources, startup_notice = _discover_sources(
                sources_path,
                settings,
                existing=sources,
                forced=args.discover_buckets,
            )
    except (SettingsConfigError, SourcesConfigError) as error:
        parser.error(str(error))
    return app_runner(
        AppConfig(
            left=parse_location(args.left),
            right=parse_location(args.right),
            sources=sources,
            settings=settings,
            startup_notice=startup_notice,
        )
    )


def _discover_sources(
    path: Path,
    settings: AppSettings,
    *,
    existing: SourcesConfig,
    forced: bool,
) -> tuple[SourcesConfig, str | None]:
    """Discover account buckets and persist newly found ones as known sources.

    Returns the resulting config and an optional warning for the UI to show
    as a startup dialog; stderr notes alone would be hidden by the TUI.
    """

    try:
        bucket_names = _discover_buckets(settings)
    except (BackendError, TimeoutError) as error:
        message = f"Bucket discovery failed: {error}"
        _notify(message)
        return existing, message
    profile = settings.profiles.default_s3
    discovered = discovered_bucket_sources(
        bucket_names,
        profile=profile.profile_name,
        region=profile.region_name,
        endpoint_url=profile.endpoint_url,
    )
    known_uris = {source.location.uri for source in existing.sources}
    added = tuple(source for source in discovered if source.location.uri not in known_uris)
    if not added:
        if forced:
            _notify("no new buckets discovered")
        return existing, None
    config = SourcesConfig(sources=(*existing.sources, *added))
    try:
        write_sources_config(path, config)
    except OSError as error:
        message = f"Could not save discovered sources to {path}: {error}"
        _notify(message)
        return config, message
    _notify(f"discovered {len(added)} new bucket(s); saved to {path}")
    return config, None


def _notify(message: str) -> None:
    sys.stderr.write(f"bc: {message}\n")


def _discover_buckets(settings: AppSettings) -> tuple[str, ...]:
    profile = settings.profiles.default_s3
    backend = S3Backend(
        S3BackendConfig(
            profile_name=profile.profile_name,
            region_name=profile.region_name,
            endpoint_url=profile.endpoint_url,
        )
    )
    return asyncio.run(
        asyncio.wait_for(backend.list_buckets(), timeout=BUCKET_DISCOVERY_TIMEOUT_SECONDS)
    )
