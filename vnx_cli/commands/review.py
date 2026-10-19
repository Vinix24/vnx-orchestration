"""vnx review — the round ledger of the PR review (R1), on the pip CLI.

Mirrors ``vnx_cli/commands/pr_ready.py``: the fabric repo's ``bin/vnx review`` is
a one-line delegation to ``scripts/review_rounds_cli.py``, and consumer repos
have no ``bin/``; they hold the pip-installed ``vnx``. The script ships in the
wheel, so the pip CLI runs the same machinery. Subprocess, not import: the
script bootstraps ``scripts/lib`` from its own location and keeps its own exit
codes (0 opened, 1 opened without its receipt, 2 bad input or an unreadable ledger).
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from vnx_cli import _engine

_EXIT_ERROR = 2


def register_review_subparser(subparsers) -> None:
    """Register ``vnx review`` (``rounds open --pr N --reason "..."``), handed to the script verbatim."""
    parser = subparsers.add_parser(
        "review", help='the round ledger of the PR review: vnx review rounds open --pr N --reason "..."',
    )
    parser.add_argument("review_args", nargs=argparse.REMAINDER, help='rounds open --pr N --reason "..." [--json]')
    parser.set_defaults(func=vnx_review)


def resolve_review_script(root: Path | None = None) -> Path:
    """The packaged review_rounds_cli.py of the resolved engine."""
    root = root or _engine.engine_root()
    return root / "scripts" / "review_rounds_cli.py"


def vnx_review(args) -> int:
    script = resolve_review_script()
    if not script.is_file():
        print(
            f"vnx review: review_rounds_cli.py not found at {script} — the installed engine is "
            "incomplete (reinstall: pip install --force-reinstall vnx-orchestration)",
            file=sys.stderr,
        )
        return _EXIT_ERROR
    try:
        return subprocess.run([sys.executable, str(script), *args.review_args]).returncode
    except OSError as exc:
        print(f"vnx review: could not start {script}: {exc}", file=sys.stderr)
        return _EXIT_ERROR
