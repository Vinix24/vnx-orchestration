"""The daily-metrics commands directory: discovered, never registered.

Every other ``*.py`` file here that exposes a callable ``register(subparsers)``
and a callable ``run(args)`` is a CLI subcommand, and ``daily`` runs each such
module that declares an integer ``DAILY_STEP`` (decision 4 and 14). This module
is not a command and is never imported as one: it holds only this docstring, and
no row after PR-3 edits it.
"""
