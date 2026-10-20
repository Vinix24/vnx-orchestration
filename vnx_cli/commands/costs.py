"""vnx costs — the cost ledger per run (ADR-043), on the pip CLI.

Mirrors ``vnx_cli/commands/review.py``: the fabric repo's ``bin/vnx costs`` is a
one-line delegation to ``scripts/costs_cli.py``, and consumer repos have no
``bin/``; they hold the pip-installed ``vnx``. The script ships in the wheel, so
the pip CLI runs the same machinery. Subprocess, not import: the script
bootstraps ``scripts/lib`` from its own location and keeps its own exit codes
(0 spool empty, 1 spool files remain, 2 the replay failed).
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from vnx_cli import _engine

_EXIT_ERROR = 2


def register_costs_subparser(subparsers) -> None:
    """Register ``vnx costs`` (``replay [--json]``), handed to the script verbatim."""
    parser = subparsers.add_parser(
        "costs", help="the cost ledger per run: vnx costs replay moves spooled records into the ledger",
    )
    parser.add_argument("costs_args", nargs=argparse.REMAINDER, help="replay [--json]")
    parser.set_defaults(func=vnx_costs)


def resolve_costs_script(root: Path | None = None) -> Path:
    """The packaged costs_cli.py of the resolved engine."""
    root = root or _engine.engine_root()
    return root / "scripts" / "costs_cli.py"


def vnx_costs(args) -> int:
    script = resolve_costs_script()
    if not script.is_file():
        print(
            f"vnx costs: costs_cli.py not found at {script} — the installed engine is "
            "incomplete (reinstall: pip install --force-reinstall vnx-orchestration)",
            file=sys.stderr,
        )
        return _EXIT_ERROR
    try:
        return subprocess.run([sys.executable, str(script), *args.costs_args]).returncode
    except OSError as exc:
        print(f"vnx costs: could not start {script}: {exc}", file=sys.stderr)
        return _EXIT_ERROR
