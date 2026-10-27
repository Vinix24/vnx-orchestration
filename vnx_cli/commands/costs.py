"""vnx costs — the cost ledger on the pip CLI (ADR-043).

``vnx costs report [--by ...] [--since ...] [--json] [--ledger PATH]`` reports
the cost ledger grouped by model, role, day or provider; ``vnx costs replay
[--json]`` brings its spool back into the ledger; ``vnx costs observe [--json]
[--ledger PATH]`` measures each provider's balance into an ``account_observation``
(K5b: a calibration is never a spend). The machinery lives in three scripts under
``scripts/`` — ``cost_ledger_{report,replay,observe}.py`` — which the fabric's
``bin/vnx costs`` runs too. Like ``providers.py``, this entrance runs them as a
subprocess, so their exit codes (report: 0 a report, 1 an unreadable ledger,
2 usage; replay: 0 done, 1 unreadable spool, write failure or read-back mismatch,
3 lock busy; observe: 0 written, 1 a write failure) reach the shell unchanged.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from vnx_cli import _engine

# Mirror of cost_ledger_replay.EXIT_FAILED: the closest class when the engine lacks the script.
_EXIT_FAILED = 1
_EXIT_USAGE = 2
# Mirror of cost_ledger.GROUP_KEYS: the pip CLI knows the keys without the engine's lib on its path.
_GROUP_KEYS = ("model", "role", "day", "provider")


def register_costs_subparser(subparsers) -> None:
    """Register ``vnx costs`` and its verbs ``report`` and ``replay``."""
    parser = subparsers.add_parser("costs", help="the cost ledger: report it, bring its spool back (replay)")
    verbs = parser.add_subparsers(dest="costs_command", metavar="SUBCOMMAND")
    report = verbs.add_parser("report", help="report the cost ledger grouped by model, role, day or provider")
    report.add_argument("--by", choices=_GROUP_KEYS, default="provider", help="the group key (default provider)")
    report.add_argument("--since", metavar="YYYY-MM-DD",
                        help="only runs whose effective occurred_from (UTC) is on or after this date")
    report.add_argument("--json", action="store_true", help="print the report as one JSON object")
    report.add_argument("--ledger", metavar="PATH", help="the cost ledger to read (default: this project's store)")
    replay = verbs.add_parser("replay", help="bring the cost ledger's spool back into the ledger, bounded")
    replay.add_argument("--json", action="store_true", help="print the result as one JSON object")
    observe = verbs.add_parser("observe", help="measure each provider's balance into an account_observation")
    observe.add_argument("--json", action="store_true", help="print the records as one JSON list")
    observe.add_argument("--coverage", action="store_true", help="also report the OpenRouter missed-catch coverage")
    observe.add_argument("--ledger", metavar="PATH", help="the cost ledger whose store receives the records")
    parser.set_defaults(func=vnx_costs)


def resolve_replay_script(root: Path | None = None) -> Path:
    """Return the packaged cost_ledger_replay.py path for the resolved engine."""
    root = root or _engine.engine_root()
    return root / "scripts" / "cost_ledger_replay.py"


def resolve_report_script(root: Path | None = None) -> Path:
    """Return the packaged cost_ledger_report.py path for the resolved engine."""
    root = root or _engine.engine_root()
    return root / "scripts" / "cost_ledger_report.py"


def resolve_observe_script(root: Path | None = None) -> Path:
    """Return the packaged cost_ledger_observe.py path for the resolved engine."""
    root = root or _engine.engine_root()
    return root / "scripts" / "cost_ledger_observe.py"


def _report_argv(script: Path, args) -> list[str]:
    """``scripts/cost_ledger_report.py``'s argv for this ``vnx costs report``."""
    argv = [sys.executable, str(script), "--by", getattr(args, "by", "provider")]
    for flag, name in (("--since", "since"), ("--ledger", "ledger")):
        value = getattr(args, name, None)
        if value:
            argv.extend([flag, value])
    return argv + (["--json"] if getattr(args, "json", False) else [])


def _replay_argv(script: Path, args) -> list[str]:
    """``scripts/cost_ledger_replay.py``'s argv for this ``vnx costs replay``."""
    return [sys.executable, str(script)] + (["--json"] if getattr(args, "json", False) else [])


def _observe_argv(script: Path, args) -> list[str]:
    """``scripts/cost_ledger_observe.py``'s argv for this ``vnx costs observe``."""
    argv = [sys.executable, str(script)] + (["--ledger", args.ledger] if getattr(args, "ledger", None) else [])
    return argv + [flag for flag in ("--json", "--coverage") if getattr(args, flag.lstrip("-"), False)]


def vnx_costs(args) -> int:
    """Run the report, replay or observe script for ``vnx costs <verb>``."""
    verb = getattr(args, "costs_command", None)
    usage = "usage: vnx costs report [--by model|role|day|provider] [--since YYYY-MM-DD] [--json] " \
            "[--ledger PATH] | vnx costs replay [--json] | vnx costs observe [--json] [--coverage] [--ledger PATH]"
    if verb == "report":
        script = resolve_report_script()
        argv = _report_argv(script, args)
    elif verb == "replay":
        script = resolve_replay_script()
        argv = _replay_argv(script, args)
    elif verb == "observe":
        script = resolve_observe_script()
        argv = _observe_argv(script, args)
    else:
        print(usage, file=sys.stderr)
        return _EXIT_USAGE
    if not script.is_file():
        print(
            f"vnx costs: {script.name} not found at {script} — the installed engine is "
            "incomplete (reinstall: pip install --force-reinstall vnx-orchestration)",
            file=sys.stderr,
        )
        return _EXIT_FAILED
    try:
        return subprocess.run(argv).returncode
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"vnx costs: could not start {script.name}: {exc}", file=sys.stderr)
        return _EXIT_FAILED
