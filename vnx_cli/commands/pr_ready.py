"""vnx pr-ready — merge readiness for a PR, on the pip CLI.

Mirrors ``vnx_cli/commands/gate_check.py`` exactly: the fabric repo's
``bin/vnx pr-ready`` is a one-line delegation to ``scripts/pr_ready.py``, and
consumer repos have no ``bin/`` — they hold the pip-installed ``vnx``. The
script ships in the wheel (pyproject's ``[tool.setuptools.package-data]``
carries ``scripts/**/*``), so the pip CLI runs the exact same machinery.

Subprocess, not import: ``pr_ready.main()`` self-bootstraps ``scripts/lib``
from its own location, so a child process keeps one launch path for both
entrances and inherits the script's exit-code contract (0 ready, 1 not ready,
2 unmeasurable, 10 bad input).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import List

from vnx_cli import _engine

# Mirror of pr_ready.py's bad-input exit code — the closest failure class when
# the engine install is missing the script.
_EXIT_BAD_INPUT = 10


def register_pr_ready_subparser(subparsers) -> None:
    """Register the `vnx pr-ready` subparser (flag surface mirrors pr_ready.py)."""
    parser = subparsers.add_parser(
        "pr-ready",
        help="merge readiness for a PR: required contexts + gate evidence on the current head",
    )
    parser.add_argument("pr", nargs="+", metavar="PR", help="PR number(s)")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("--verbose", action="store_true", help="one line per context and gate")
    parser.add_argument(
        "--protected-branch", dest="protected_branch", default="main", metavar="BRANCH",
        help="branch whose protection defines the required contexts (default: main)",
    )
    parser.add_argument(
        "--project-root", dest="project_root", default=None, metavar="DIR",
        help="project root (default: cwd)",
    )
    parser.add_argument("--timeout", type=int, default=20, metavar="SECONDS")


def build_pr_ready_argv(args) -> List[str]:
    """Translate the parsed namespace into pr_ready.py argv (non-defaults only)."""
    argv = [str(p) for p in args.pr]
    if getattr(args, "json", False):
        argv.append("--json")
    if getattr(args, "verbose", False):
        argv.append("--verbose")
    if getattr(args, "protected_branch", "main") != "main":
        argv += ["--protected-branch", str(args.protected_branch)]
    if getattr(args, "project_root", None):
        argv += ["--project-root", str(args.project_root)]
    if getattr(args, "timeout", 20) != 20:
        argv += ["--timeout", str(args.timeout)]
    return argv


def resolve_pr_ready_script(root: Path | None = None) -> Path:
    """Return the packaged pr_ready.py path for the resolved engine."""
    root = root or _engine.engine_root()
    return root / "scripts" / "pr_ready.py"


def vnx_pr_ready(args) -> int:
    """Run the readiness report via the packaged engine script."""
    script = resolve_pr_ready_script()
    if not script.is_file():
        print(
            f"vnx pr-ready: pr_ready.py not found at {script} — the installed engine is "
            "incomplete (reinstall: pip install --force-reinstall vnx-orchestration)",
            file=sys.stderr,
        )
        return _EXIT_BAD_INPUT
    return subprocess.run([sys.executable, str(script), *build_pr_ready_argv(args)]).returncode
