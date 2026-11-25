"""Terminal start configuration (config-een-oppervlak PR-3a): the deleted names have zero reads.

The terminal start names are deleted and keep the path the code followed when the variable was
not set (fixed constants). ``VNX_GEMINI_MODEL`` is checked only in the start files (PR-3b owns the
gemini adapters). Four registry keys read through ``config_runtime`` and, in bash,
``vnx config get --raw``: see ``test_terminal_start_config.py``.
"""
from __future__ import annotations

import ast
import re
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

DELETED = (
    "VNX_T0_FLAGS", "VNX_T0_MODEL", "VNX_T1_MODEL", "VNX_T2_MODEL", "VNX_T3_MODEL",
)
# Read elsewhere until PR-3b; here only the terminal start files must be clean of it.
START_ONLY = "VNX_GEMINI_MODEL"
_SCANNED = DELETED + (START_ONLY,)
START_FILES = (
    "scripts/lib/vnx_start_runtime.py", "scripts/commands/start.sh",
    "scripts/commands/resume.sh", "scripts/vnx_supervisor_simple.sh",
)
ROOTS = ("scripts", "vnx_cli", "dashboard")
BASH_FILES = (
    "bin/vnx", "scripts/commands/start.sh", "scripts/commands/resume.sh",
    "scripts/queue_auto_accept.sh", "scripts/vnx_supervisor_simple.sh",
)


def _py_reads(path: Path) -> list[tuple[str, int]]:
    """Every exact string constant equal to a deleted name (an env read, ``in os.environ`` or a
    dict key); prose in docstrings and comments never equals a bare name."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return [(n.value, n.lineno) for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str) and n.value in _SCANNED]


def _bash_reads(path: Path) -> list[tuple[str, int]]:
    hits = []
    for no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if line.lstrip().startswith("#"):
            continue
        hits += [(m, no) for m in re.findall(r"\b(" + "|".join(_SCANNED) + r")\b", line)]
    return hits


def _all_reads() -> list[str]:
    found = []
    for root in ROOTS:
        for p in sorted((REPO / root).rglob("*")):
            rel = p.relative_to(REPO).as_posix()
            if "__pycache__" in rel or False:
                continue
            if p.suffix == ".py":
                found += [f"{rel}:{ln} {name}" for name, ln in _py_reads(p) if name in DELETED]
            elif p.suffix == ".sh":
                found += [f"{rel}:{ln} {name}" for name, ln in _bash_reads(p) if name in DELETED]
    for rel in START_FILES:
        p = REPO / rel
        reads = _py_reads(p) if p.suffix == ".py" else _bash_reads(p)
        found += [f"{rel}:{ln} {name}" for name, ln in reads if name == START_ONLY]
    for rel in BASH_FILES:
        if not rel.startswith("scripts/"):
            found += [f"{rel}:{ln} {name}" for name, ln in _bash_reads(REPO / rel) if name in DELETED]
    return found


def test_deleted_names_have_zero_reads():
    assert _all_reads() == []


def test_start_runtime_literal_env_read_pin():
    tree = ast.parse((REPO / START_FILES[0]).read_text(encoding="utf-8"))
    keys = [n.args[0] for n in ast.walk(tree) if isinstance(n, ast.Call)
            and ast.unparse(n.func) in {"os.environ.get", "os.getenv"}]
    assert all(isinstance(key, ast.Constant) for key in keys)
    assert Counter(key.value for key in keys) == {
        "PROJECT_ROOT": 2, "VNX_HOME": 2, "VNX_DATA_DIR": 2, "VNX_SKILLS_DIR": 2,
        "VNX_CODEX_MODEL": 1,
    }


def test_scan_sees_a_read(tmp_path):
    """The scanner is not blind: a planted read in each language is found."""
    py = tmp_path / "x.py"
    py.write_text('import os\nos.environ.get("VNX_T1_MODEL", "kimi-k3")\n')
    sh = tmp_path / "x.sh"
    sh.write_text('# VNX_T0_FLAGS in a comment\nx="${VNX_T0_FLAGS:-}"\n')
    assert _py_reads(py) == [("VNX_T1_MODEL", 2)]
    assert _bash_reads(sh) == [("VNX_T0_FLAGS", 2)]
