"""Command-line entry point for Bucket Commander."""

from __future__ import annotations

import argparse
from collections.abc import Callable, Sequence
from pathlib import Path

from bc import __version__
from bc.app import AppConfig, run_app


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
        type=Path,
        default=Path.cwd(),
        help="Initial path for the left panel.",
    )
    parser.add_argument(
        "--right",
        type=Path,
        default=Path.cwd(),
        help="Initial path for the right panel.",
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
    return app_runner(AppConfig(left=args.left, right=args.right))
