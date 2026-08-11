"""vnx gate-check — deterministic pre-merge gate (GO/HOLD) on the pip CLI.

The fabric repo's ``bin/vnx gate-check`` is a one-line delegation to
``scripts/pre_merge_gate.py`` (bin/vnx: ``"$VNX_PYTHON"
"$VNX_HOME/scripts/pre_merge_gate.py" "$@"``). Consumer repos have no
``bin/`` — they hold the pip-installed ``vnx``, which until OI-1135 lacked
the command entirely while the docs prescribed it (the pip-CLI-vs-scripts
surface split). The gate script itself ships in the wheel — pyproject's
``[tool.setuptools.package-data]`` carries ``scripts/**/*`` under the
``vnx_orchestration`` namespace package — so the pip CLI can run the exact
same machinery: resolve the engine root and execute the packaged
``scripts/pre_merge_gate.py`` as a subprocess, mirroring the repo form.

Subprocess, not import: ``pre_merge_gate.main()`` parses ``sys.argv``
directly and the script self-bootstraps ``scripts/lib`` from its own
location (``SCRIPT_DIR / "lib"``), so a child process keeps a single launch
path for both entrances and inherits the script's own exit-code contract
(0 GO, 1 HOLD, 10 bad args, 20 I/O error, 40 internal error).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import List

from vnx_cli import _engine

# Mirror of pre_merge_gate.py's I/O-error exit code — used when the engine
# install is missing the gate script (the closest failure class).
_EXIT_IO_ERROR = 20


def build_gate_check_argv(args) -> List[str]:
    """Translate the parsed vnx_cli namespace into pre_merge_gate.py argv.

    Only non-default flags are forwarded, so the subprocess sees the same
    argv a direct ``bin/vnx gate-check`` invocation would have carried.
    """
    argv = ["--pr", str(args.pr)]
    if getattr(args, "project_root", None):
        argv += ["--project-root", str(args.project_root)]
    if getattr(args, "json", False):
        argv.append("--json")
    if getattr(args, "output_file", None):
        argv += ["--output-file", str(args.output_file)]
    if getattr(args, "skip_pytest", False):
        argv.append("--skip-pytest")
    if not getattr(args, "store", True):
        argv.append("--no-store")
    return argv


def resolve_gate_script(root: Path | None = None) -> Path:
    """Return the packaged pre_merge_gate.py path for the resolved engine."""
    root = root or _engine.engine_root()
    return root / "scripts" / "pre_merge_gate.py"


def vnx_gate_check(args) -> int:
    """Run the pre-merge gate for a PR via the packaged engine script."""
    script = resolve_gate_script()
    if not script.is_file():
        print(
            f"vnx gate-check: pre_merge_gate.py not found at {script} — "
            "the installed engine is incomplete "
            "(reinstall: pip install --force-reinstall vnx-orchestration)",
            file=sys.stderr,
        )
        return _EXIT_IO_ERROR
    cmd = [sys.executable, str(script), *build_gate_check_argv(args)]
    return subprocess.run(cmd).returncode
