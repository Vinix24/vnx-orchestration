"""Discovered command modules for the daily-metrics CLI.

The CLI imports every ``*.py`` in this directory except this ``__init__`` and
registers the subcommands exposed by modules that declare
``register(subparsers)`` and ``run(args)`` (decision 4). Later command rows
(PR-11 to PR-14) drop a module here; the CLI discovers it at start-up. This
file stays a docstring-only anchor.
"""
