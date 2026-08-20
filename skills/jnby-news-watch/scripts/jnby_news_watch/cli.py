from __future__ import annotations

import argparse
from collections.abc import Sequence


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="jnby-news-watch",
        description="Verified JNBY news and Customer Voice intelligence.",
    )
    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser("digest", help="Generate a ranked intelligence digest")
    subparsers.add_parser("focus", help="Propose or manage dynamic work focus")
    subparsers.add_parser("deepen", help="Expand one stored item")
    subparsers.add_parser("health", help="Show redacted integration health")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
    return 0
