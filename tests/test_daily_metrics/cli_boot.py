"""Run ``scripts/daily_metrics.py`` in a subprocess with its directories pinned.

The sources and commands directories are module constants, not environment
knobs, so a subprocess test sets them on the loaded modules before ``main``.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
CLI = REPO_ROOT / "scripts" / "daily_metrics.py"

_BOOT = """
import importlib.util, sys
from pathlib import Path

cli, sources, commands = sys.argv[1:4]
sys.argv = [cli, *sys.argv[4:]]
spec = importlib.util.spec_from_file_location("daily_metrics_cli", cli)
module = importlib.util.module_from_spec(spec)
sys.modules["daily_metrics_cli"] = module
spec.loader.exec_module(module)
from daily_metrics import collect

collect.SOURCES_DIR = Path(sources)
if commands:
    module.COMMANDS_DIR = Path(commands)
raise SystemExit(module.main(sys.argv[1:]))
"""


def cli_command(sources_dir: Path, args: list[str], commands_dir: Path | None = None) -> list[str]:
    """The argv that runs the CLI with ``sources_dir`` and ``commands_dir`` pinned."""
    return [sys.executable, "-c", _BOOT, str(CLI), str(sources_dir),
            str(commands_dir) if commands_dir else "", *args]
