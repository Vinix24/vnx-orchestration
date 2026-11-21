"""The codex-lane env switches are gone (config-een-oppervlak PR-2s, ADR-087).

``VNX_CODEX_SANDBOX``, ``VNX_CODEX_STALL_THRESHOLD`` and ``VNX_CODEX_TIMEOUT``
are read nowhere: not directly (``os.environ.get``, ``os.environ[...]``,
``os.getenv``, ``setdefault``, ``pop``, ``in os.environ``, the ``from os import``
forms), not through a name-to-variable table (a bare string literal equal to
the name, as ``headless_adapter.GATE_STALL_ENV`` had), and not in bash.

The codex argv is pinned to what ``origin/main`` (ff0bed9fe) built with
``VNX_CODEX_SANDBOX`` unset: the bypass route.
"""

from __future__ import annotations

import ast
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts" / "lib"))

DELETED = ("VNX_CODEX_SANDBOX", "VNX_CODEX_STALL_THRESHOLD", "VNX_CODEX_TIMEOUT")
PY_ROOTS = ("scripts", "vnx_cli", "dashboard")
_ENV_METHODS = {"get", "setdefault", "pop"}


def _tracked(pattern: str) -> list:
    out = subprocess.run(
        ["git", "ls-files", "-z", "--", pattern], cwd=REPO,
        capture_output=True, text=True, check=True,
    ).stdout
    return [REPO / p for p in out.split("\0") if p]


def _python_files() -> list:
    return [p for root in PY_ROOTS for p in _tracked(f"{root}/*.py")]


def _bash_files() -> list:
    return _tracked("*.sh") + [p for p in _tracked("bin/vnx") if p.is_file()]


def _is_environ(node: ast.AST) -> bool:
    if isinstance(node, ast.Attribute):
        return node.attr == "environ"
    return isinstance(node, ast.Name) and node.id == "environ"


def _is_getenv(node: ast.AST) -> bool:
    if isinstance(node, ast.Attribute):
        return node.attr == "getenv"
    return isinstance(node, ast.Name) and node.id == "getenv"


def _literal(node: ast.AST):
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _direct_reads(tree: ast.AST) -> list:
    """(name, line) for every env read of a literal name, in all the read forms."""
    found = []
    for node in ast.walk(tree):
        name = None
        if isinstance(node, ast.Call) and node.args:
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr in _ENV_METHODS and _is_environ(func.value):
                name = _literal(node.args[0])
            elif _is_getenv(func):
                name = _literal(node.args[0])
        elif isinstance(node, ast.Subscript) and _is_environ(node.value):
            name = _literal(node.slice)
        elif isinstance(node, ast.Compare) and any(_is_environ(c) for c in node.comparators):
            if any(isinstance(op, (ast.In, ast.NotIn)) for op in node.ops):
                name = _literal(node.left)
        if name in DELETED:
            found.append((name, node.lineno))
    return found


def _literal_mentions(tree: ast.AST) -> list:
    """(name, line) for every string constant that IS a deleted name: an indirect read via a table."""
    return [(n.value, n.lineno) for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and n.value in DELETED]


def _scan(collect) -> dict:
    hits = {name: [] for name in DELETED}
    for path in _python_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for name, line in collect(tree):
            hits[name].append(f"{path.relative_to(REPO)}:{line}")
    return hits


def test_the_scan_sees_python_and_bash_files():
    assert any(p.name == "codex_spawn.py" for p in _python_files())
    assert _bash_files()


@pytest.mark.parametrize("name", DELETED)
def test_no_python_source_reads_the_variable(name):
    assert _scan(_direct_reads)[name] == []


@pytest.mark.parametrize("name", DELETED)
def test_no_python_source_names_the_variable_as_a_literal(name):
    assert _scan(_literal_mentions)[name] == []


@pytest.mark.parametrize("name", DELETED)
def test_no_bash_file_expands_the_variable(name):
    pattern = re.compile(r"\$\{?" + name + r"\b")
    hits = [f"{p.relative_to(REPO)}:{i}"
            for p in _bash_files()
            for i, line in enumerate(p.read_text(encoding="utf-8", errors="replace").splitlines(), 1)
            if pattern.search(line)]
    assert hits == []


def test_scan_detects_every_read_form():
    """The scanner itself: each read form is seen, so a green scan is not a blind one."""
    src = "\n".join([
        "import os",
        "from os import environ, getenv",
        "os.environ.get('VNX_CODEX_SANDBOX')",
        "os.environ['VNX_CODEX_SANDBOX']",
        "os.getenv('VNX_CODEX_SANDBOX')",
        "os.environ.setdefault('VNX_CODEX_SANDBOX', '')",
        "os.environ.pop('VNX_CODEX_SANDBOX', None)",
        "'VNX_CODEX_SANDBOX' in os.environ",
        "'VNX_CODEX_SANDBOX' not in os.environ",
        "environ.get('VNX_CODEX_SANDBOX')",
        "getenv('VNX_CODEX_SANDBOX')",
    ])
    assert len(_direct_reads(ast.parse(src))) == 9


# The argv origin/main (ff0bed9fe) built with VNX_CODEX_SANDBOX unset, measured
# there with the model pinned to "gpt-test".
UNSET_ARGV_ON_MAIN = [
    "codex", "exec", "--json", "--dangerously-bypass-approvals-and-sandbox", "--model", "gpt-test",
]


def test_codex_argv_is_the_unset_variable_argv(monkeypatch):
    """Every caller of ``_build_cmd`` (``spawn_codex`` and its policy record) gets the bypass route."""
    from provider_spawns import codex_spawn

    monkeypatch.setattr(codex_spawn, "_resolve_codex_model", lambda m: m or "gpt-test")
    assert codex_spawn._build_cmd("gpt-test") == UNSET_ARGV_ON_MAIN
