"""PR-3b (config-een-oppervlak): the deleted gemini, pool, session and signal-dir names have zero reads."""
from __future__ import annotations

import ast
import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DELETED = (
    "VNX_GEMINI_MODEL", "VNX_GEMINI_TIMEOUT", "VNX_GEMINI_STALL_THRESHOLD",
    "VNX_GEMINI_FLAT_COST_USD", "VNX_GEMINI_STREAM", "VNX_TMUX_SIGNAL_DIR",
    "VNX_POOL_ID", "VNX_SESSION_ID",
)
# Owned by other open PRs: PR-3a (start path reads VNX_GEMINI_MODEL) and the VNX_T0_ROTATION PR.
PENDING = {
    ("scripts/lib/vnx_start_runtime.py", "VNX_GEMINI_MODEL"),
    ("scripts/commands/start.sh", "VNX_GEMINI_MODEL"),
    ("scripts/hooks/session_stop_rotation.py", "VNX_TMUX_SIGNAL_DIR"),
}


def _py_reads(path: Path) -> list[tuple[str, int]]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return [(n.value, n.lineno) for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str) and n.value in DELETED]


def _bash_reads(path: Path) -> list[tuple[str, int]]:
    pat = re.compile(r"\b(" + "|".join(DELETED) + r")\b")
    return [(m, no) for no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
            if not line.lstrip().startswith("#") for m in pat.findall(line)]


def test_deleted_names_have_zero_reads():
    files = [p for root in ("scripts", "vnx_cli", "dashboard") for p in sorted((REPO / root).rglob("*"))]
    files += [REPO / "bin/vnx"]
    found = []
    for p in files:
        rel = p.relative_to(REPO).as_posix()
        reads = _py_reads(p) if p.suffix == ".py" else _bash_reads(p) if p.suffix == ".sh" or rel == "bin/vnx" else []
        found += [f"{rel}:{ln} {n}" for n, ln in reads if (rel, n) not in PENDING]
    assert found == []


def test_scan_sees_a_read(tmp_path):
    py, sh = tmp_path / "x.py", tmp_path / "x.sh"
    py.write_text('import os\nos.environ.get("VNX_POOL_ID", "headless")\n')
    sh.write_text('# VNX_GEMINI_STREAM in a comment\nx="${VNX_TMUX_SIGNAL_DIR:-}"\n')
    assert _py_reads(py) == [("VNX_POOL_ID", 2)]
    assert _bash_reads(sh) == [("VNX_TMUX_SIGNAL_DIR", 2)]
