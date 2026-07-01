"""Command-line entry point for Bucket Commander."""

from __future__ import annotations

import argparse
from collections.abc import Sequence

from bc import __version__


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
    parser.set_defaults(command="run")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    parser.parse_args(argv)
    parser.print_help()
    return 0

