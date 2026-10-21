"""vnx costs — the cost ledger on the pip CLI (ADR-043).

``vnx costs replay [--json]`` brings the cost ledger's spool back into the
ledger. The machinery lives in one script, ``scripts/cost_ledger_replay.py``,
which the fabric's ``bin/vnx costs replay`` runs too. Like ``providers.py``,
this entrance runs that script as a subprocess, so its exit codes (0 done,
1 an unreadable spool or a write failure, 3 a lock busy) reach the
shell unchanged.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from vnx_cli import _engine

# Mirror of cost_ledger_replay.EXIT_FAILED: the closest class when the engine lacks the script.
_EXIT_FAILED = 1
_EXIT_USAGE = 2


def register_costs_subparser(subparsers) -> None:
    """Register ``vnx costs`` and its verb ``replay``."""
    parser = subparsers.add_parser("costs", help="the cost ledger: bring its spool back (replay)")
    verbs = parser.add_subparsers(dest="costs_command", metavar="SUBCOMMAND")
    replay = verbs.add_parser("replay", help="bring the cost ledger's spool back into the ledger, bounded")
    replay.add_argument("--json", action="store_true", help="print the result as one JSON object")
    parser.set_defaults(func=vnx_costs)


def resolve_replay_script(root: Path | None = None) -> Path:
    """Return the packaged cost_ledger_replay.py path for the resolved engine."""
    root = root or _engine.engine_root()
    return root / "scripts" / "cost_ledger_replay.py"


def vnx_costs(args) -> int:
    """Run ``scripts/cost_ledger_replay.py`` for ``vnx costs replay``."""
    if getattr(args, "costs_command", None) != "replay":
        print("usage: vnx costs replay [--json]", file=sys.stderr)
        return _EXIT_USAGE
    script = resolve_replay_script()
    if not script.is_file():
        print(
            f"vnx costs: cost_ledger_replay.py not found at {script} — the installed engine is "
            "incomplete (reinstall: pip install --force-reinstall vnx-orchestration)",
            file=sys.stderr,
        )
        return _EXIT_FAILED
    argv = [sys.executable, str(script), *(["--json"] if getattr(args, "json", False) else [])]
    try:
        return subprocess.run(argv).returncode
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"vnx costs: could not start cost_ledger_replay.py: {exc}", file=sys.stderr)
        return _EXIT_FAILED
