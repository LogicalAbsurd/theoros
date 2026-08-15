"""Theoros CLI entrypoint.

Run as a module:

    python -m theoros.cli <command> [args...]

Subcommands are registered from theoros.cli.commands.*.
"""

from __future__ import annotations

import argparse
import sys

from .commands import audit_search, health, purge


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="theoros",
        description="Theoros local admin CLI.",
    )
    subparsers = parser.add_subparsers(dest="command")

    # Register each command module.  Every command module exposes
    # register(subparsers) and run(args).
    purge.register(subparsers)
    audit_search.register(subparsers)
    health.register(subparsers)

    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        sys.exit(1)

    # Dispatch to the command's run function, stored by register().
    args.func(args)


if __name__ == "__main__":
    main()
