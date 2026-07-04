"""Command-line entry point for Bucket Commander."""

from __future__ import annotations

import argparse
from collections.abc import Callable, Sequence
from pathlib import Path

from bc import __version__
from bc.app import AppConfig, run_app
from bc.config import SourcesConfigError, load_sources_config
from bc.core import parse_location


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bucket-commander",
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
        "--sources-config",
        type=Path,
        default=None,
        help=(
            "Known location sources TOML file. Defaults to config/sources.toml or "
            "~/.config/bucket-commander/sources.toml."
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
    try:
        sources = load_sources_config(args.sources_config)
    except SourcesConfigError as error:
        parser.error(str(error))
    return app_runner(
        AppConfig(
            left=parse_location(args.left),
            right=parse_location(args.right),
            sources=sources,
        )
    )
