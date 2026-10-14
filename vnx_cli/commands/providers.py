"""vnx providers — the operator's provider switch, on the pip CLI.

The operator's shell ``vnx`` is this pip CLI, so the command must exist here or the operator
cannot type it. The machinery lives in one module, ``scripts/providers_cli.py``, which the
fabric's ``bin/vnx providers`` also runs. Like ``gate_check.py``, this entrance runs that
script as a subprocess: one launch path for both entrances, and the script's own exit-code
contract (0 ok, 1 write failure, 2 usage, 3 unreadable file, 4 refused by ``check``,
5 write refused) reaches the shell unchanged.

Every argument after ``providers`` is passed through verbatim, so the verbs and flags are
defined once, in the script.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from vnx_cli import _engine

# Mirror of providers_cli.EXIT_WRITE_FAILED: the closest class when the engine lacks the script.
_EXIT_WRITE_FAILED = 1


def register_providers_subparser(subparsers) -> None:
    """Register ``vnx providers``. All arguments pass through to ``scripts/providers_cli.py``."""
    # No argument starts with "\x00", so argparse sees no option here and REMAINDER takes all; add_help=False keeps
    # -h for the script. Without both, a leading dash (`--json`, `-h`, any flag) dies as "unrecognized arguments".
    parser = subparsers.add_parser(
        "providers",
        help="turn model providers on and off (list/off/on/reset/repair/check)",
        add_help=False,
        prefix_chars="\x00",
    )
    parser.add_argument("providers_args", nargs=argparse.REMAINDER)
    parser.set_defaults(func=vnx_providers)


def resolve_providers_script(root: Path | None = None) -> Path:
    """Return the packaged providers_cli.py path for the resolved engine."""
    root = root or _engine.engine_root()
    return root / "scripts" / "providers_cli.py"


def vnx_providers(args) -> int:
    """Run ``scripts/providers_cli.py`` with the passed-through arguments."""
    script = resolve_providers_script()
    if not script.is_file():
        print(
            f"vnx providers: providers_cli.py not found at {script} — the installed engine is "
            "incomplete (reinstall: pip install --force-reinstall vnx-orchestration)",
            file=sys.stderr,
        )
        return _EXIT_WRITE_FAILED
    try:
        return subprocess.run([sys.executable, str(script), *getattr(args, "providers_args", [])]).returncode
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"vnx providers: could not start providers_cli.py: {exc}", file=sys.stderr)
        return _EXIT_WRITE_FAILED
