"""config-een-oppervlak PR-5: none of the 18 deleted VNX_* switches is read in scripts/, vnx_cli/, dashboard/."""
from __future__ import annotations

import ast
import re
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]

DELETED = (
    "VNX_DEEPSEEK_CLASSIFIER_MODEL", "VNX_DEEPSEEK_CLASSIFIER_TIMEOUT",
    "VNX_HAIKU_MODEL", "VNX_HAIKU_TIMEOUT",
    "VNX_GLM_HARNESS_MODEL", "VNX_GLM_PROXY_URL",
    "VNX_KIMI_STALL_THRESHOLD", "VNX_KIMI_TIMEOUT",
    "VNX_LITELLM_STALL_THRESHOLD", "VNX_LITELLM_TIMEOUT",
    "VNX_DEEPSEEK_HARNESS_MODEL", "VNX_HEADLESS_TIMEOUT", "VNX_DISPATCH_MODEL",
    "VNX_GOVERNANCE_MODE", "VNX_BENCH_MODELS_FILE", "VNX_BENCH_TASKS_DIR",
    "VNX_BENCH_EXTERNAL_SEED", "VNX_BENCH_SCORE_DELIVERABLE_ON_FAILURE",
)
_READ_METHODS = {"get", "setdefault", "pop"}


def _tracked(*suffixes: str) -> list[Path]:
    out = subprocess.run(["git", "ls-files", "scripts", "vnx_cli", "dashboard", "bin"],
                         cwd=REPO, check=True, capture_output=True, text=True).stdout.splitlines()
    return [REPO / p for p in out if p.endswith(suffixes) and (REPO / p).is_file()]


def _is_environ(node: ast.AST, environ_names: set[str]) -> bool:
    if isinstance(node, ast.Attribute) and node.attr == "environ":
        return True
    return isinstance(node, ast.Name) and node.id in environ_names


def _python_reads(path: Path) -> list[tuple[str, int]]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    environ_names: set[str] = set()
    getenv_names: set[str] = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.ImportFrom) and n.module == "os":
            for a in n.names:
                if a.name == "environ":
                    environ_names.add(a.asname or a.name)
                elif a.name == "getenv":
                    getenv_names.add(a.asname or a.name)

    def _const(node: ast.AST | None) -> ast.Constant | None:
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value in DELETED:
            return node
        return None

    found: dict[tuple[int, int], str] = {}
    for n in ast.walk(tree):
        name: ast.Constant | None = None
        if isinstance(n, ast.Call) and n.args:
            f = n.func
            is_getenv = (isinstance(f, ast.Attribute) and f.attr == "getenv") or (
                isinstance(f, ast.Name) and f.id in getenv_names)
            is_method = (isinstance(f, ast.Attribute) and f.attr in _READ_METHODS
                         and _is_environ(f.value, environ_names))
            if is_getenv or is_method:
                name = _const(n.args[0])
        elif isinstance(n, ast.Subscript) and _is_environ(n.value, environ_names):
            name = _const(n.slice)
        elif isinstance(n, ast.Compare):
            if any(isinstance(op, (ast.In, ast.NotIn)) for op in n.ops) and any(
                    _is_environ(c, environ_names) for c in n.comparators):
                name = _const(n.left)
        elif isinstance(n, ast.Constant):
            # A bare name in a table (e.g. a gate -> env-var map) is a read by indirection.
            name = _const(n)
        if name is not None:
            found[(name.lineno, name.col_offset)] = name.value
    return sorted((v, line) for (line, _col), v in found.items())


def _shell_reads(path: Path) -> list[tuple[str, int]]:
    pattern = re.compile(r"\$\{?(" + "|".join(DELETED) + r")\b")
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    return [(m.group(1), i) for i, line in enumerate(lines, 1) for m in pattern.finditer(line)]


@pytest.mark.parametrize("suffix,scan", [(".py", _python_reads), (".sh", _shell_reads)])
def test_no_file_reads_a_deleted_variable(suffix, scan):
    found = {str(p.relative_to(REPO)): h for p in _tracked(suffix) if (h := scan(p))}
    assert not found, f"deleted VNX_* variables still read: {found}"


@pytest.mark.parametrize("source,expected", [
    ('import os\nos.environ.get("VNX_KIMI_TIMEOUT", 1)', 1),
    ('import os\nos.environ["VNX_KIMI_TIMEOUT"]', 1),
    ('import os\nos.getenv("VNX_KIMI_TIMEOUT")', 1),
    ('import os\nos.environ.setdefault("VNX_KIMI_TIMEOUT", "1")', 1),
    ('import os\nos.environ.pop("VNX_KIMI_TIMEOUT", None)', 1),
    ('import os\nx = "VNX_KIMI_TIMEOUT" in os.environ', 1),
    ('from os import environ\nenviron.get("VNX_KIMI_TIMEOUT")', 1),
    ('from os import getenv as g\ng("VNX_KIMI_TIMEOUT")', 1),
    ('import os\nos.environ.get("VNX_KIMI_MODEL")', 0),
])
def test_the_scan_sees_every_read_form(tmp_path, source, expected):
    f = tmp_path / "m.py"
    f.write_text(source)
    assert len(_python_reads(f)) == expected


def test_the_shell_scan_sees_both_expansion_forms(tmp_path):
    f = tmp_path / "x.sh"
    f.write_text('a=$VNX_HEADLESS_TIMEOUT\nb=${VNX_DISPATCH_MODEL:-x}\nc=${VNX_OTHER}\n')
    assert [n for n, _ in _shell_reads(f)] == ["VNX_HEADLESS_TIMEOUT", "VNX_DISPATCH_MODEL"]
