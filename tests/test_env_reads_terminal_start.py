"""Terminal start configuration (config-een-oppervlak PR-3): the deleted names have zero reads.

13 names are deleted and keep the path the code followed when the variable was not set (fixed
constants, ``args.session_id`` only, no signal dir). Four registry keys read through
``config_runtime`` and, in bash, ``vnx config get --raw``: see ``test_terminal_start_config.py``.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

DELETED = (
    "VNX_T0_FLAGS", "VNX_T0_MODEL", "VNX_T1_MODEL", "VNX_T2_MODEL", "VNX_T3_MODEL",
    "VNX_GEMINI_MODEL", "VNX_TMUX_SIGNAL_DIR", "VNX_GEMINI_FLAT_COST_USD",
    "VNX_GEMINI_STALL_THRESHOLD", "VNX_GEMINI_STREAM", "VNX_GEMINI_TIMEOUT",
    "VNX_POOL_ID", "VNX_SESSION_ID",
)
ROOTS = ("scripts", "vnx_cli", "dashboard")
BASH_FILES = (
    "bin/vnx", "scripts/commands/start.sh", "scripts/commands/resume.sh",
    "scripts/queue_auto_accept.sh", "scripts/vnx_supervisor_simple.sh",
)
# Deleted by the open PR "delete VNX_T0_ROTATION with the no-op Stop hook"; until it merges its
# one VNX_TMUX_SIGNAL_DIR test belongs to that PR.
PENDING_DELETION = {"scripts/hooks/session_stop_rotation.py"}


def _py_reads(path: Path) -> list[tuple[str, int]]:
    """Every exact string constant equal to a deleted name (an env read, ``in os.environ`` or a
    dict key); prose in docstrings and comments never equals a bare name."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return [(n.value, n.lineno) for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str) and n.value in DELETED]


def _bash_reads(path: Path) -> list[tuple[str, int]]:
    hits = []
    for no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if line.lstrip().startswith("#"):
            continue
        hits += [(m, no) for m in re.findall(r"\b(" + "|".join(DELETED) + r")\b", line)]
    return hits


def _all_reads() -> list[str]:
    found = []
    for root in ROOTS:
        for p in sorted((REPO / root).rglob("*")):
            rel = p.relative_to(REPO).as_posix()
            if "__pycache__" in rel or rel in PENDING_DELETION:
                continue
            if p.suffix == ".py":
                found += [f"{rel}:{ln} {name}" for name, ln in _py_reads(p)]
            elif p.suffix == ".sh":
                found += [f"{rel}:{ln} {name}" for name, ln in _bash_reads(p)]
    for rel in BASH_FILES:
        if not rel.startswith("scripts/"):
            found += [f"{rel}:{ln} {name}" for name, ln in _bash_reads(REPO / rel)]
    return found


def test_deleted_names_have_zero_reads():
    assert _all_reads() == []


def test_scan_sees_a_read(tmp_path):
    """The scanner is not blind: a planted read in each language is found."""
    py = tmp_path / "x.py"
    py.write_text('import os\nos.environ.get("VNX_POOL_ID", "headless")\n')
    sh = tmp_path / "x.sh"
    sh.write_text('# VNX_T0_FLAGS in a comment\nx="${VNX_T0_FLAGS:-}"\n')
    assert _py_reads(py) == [("VNX_POOL_ID", 2)]
    assert _bash_reads(sh) == [("VNX_T0_FLAGS", 2)]
