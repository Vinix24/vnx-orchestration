"""No read of VNX_T0_ROTATION or VNX_PROFILE_SELECTOR is left in the code (ADR-087).

Both switches are deleted with the Stop-hook branch they gated and with
``profile_gate_resolver.py``. A scan over the Python of ``scripts/``, ``vnx_cli/`` and
``dashboard/`` and over the bash of ``scripts/`` and ``.claude/settings.json`` finds no
read: ``os.environ.get/[...]/setdefault/pop``, ``os.getenv``, ``in os.environ``, the
``from os import environ/getenv`` forms, and a ``$VAR`` / ``${VAR...}`` expansion.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DELETED = ("VNX_T0_ROTATION", "VNX_PROFILE_SELECTOR")
PY_DIRS = ("scripts", "vnx_cli", "dashboard")
_SKIP_PARTS = {"__pycache__", "node_modules", ".venv"}


def _py_files():
    for d in PY_DIRS:
        for path in (ROOT / d).rglob("*.py"):
            if not _SKIP_PARTS.intersection(path.parts):
                yield path


def _shell_files():
    for path in (ROOT / "scripts").rglob("*.sh"):
        if not _SKIP_PARTS.intersection(path.parts):
            yield path
    yield ROOT / ".claude" / "settings.json"


def _is_environ(node: ast.AST, aliases: set) -> bool:
    if isinstance(node, ast.Attribute) and node.attr == "environ":
        return isinstance(node.value, ast.Name) and node.value.id == "os"
    return isinstance(node, ast.Name) and node.id in aliases


def _str(node: ast.AST):
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def python_reads(tree: ast.AST, name: str) -> int:
    environ_aliases, getenv_aliases = set(), set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "os":
            for alias in node.names:
                if alias.name == "environ":
                    environ_aliases.add(alias.asname or "environ")
                if alias.name == "getenv":
                    getenv_aliases.add(alias.asname or "getenv")
    count = 0
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and node.args and _str(node.args[0]) == name:
            f = node.func
            if isinstance(f, ast.Attribute) and f.attr in {"get", "setdefault", "pop"} and _is_environ(f.value, environ_aliases):
                count += 1
            elif isinstance(f, ast.Attribute) and f.attr == "getenv" and isinstance(f.value, ast.Name) and f.value.id == "os":
                count += 1
            elif isinstance(f, ast.Name) and f.id in getenv_aliases:
                count += 1
        elif isinstance(node, ast.Subscript) and _is_environ(node.value, environ_aliases) and _str(node.slice) == name:
            count += 1
        elif isinstance(node, ast.Compare) and _str(node.left) == name:
            if any(isinstance(op, (ast.In, ast.NotIn)) for op in node.ops) and any(
                _is_environ(c, environ_aliases) for c in node.comparators
            ):
                count += 1
    return count


def shell_reads(text: str, name: str) -> int:
    return len(re.findall(r"\$\{?" + re.escape(name) + r"(?![A-Za-z0-9_])", text))


@pytest.mark.parametrize("name", DELETED)
def test_no_python_read_of_a_deleted_variable(name):
    hits = []
    for path in _py_files():
        n = python_reads(ast.parse(path.read_text(encoding="utf-8"), filename=str(path)), name)
        if n:
            hits.append(f"{path.relative_to(ROOT)}: {n}")
    assert not hits, f"{name} is read in: {hits}"


@pytest.mark.parametrize("name", DELETED)
def test_no_shell_read_of_a_deleted_variable(name):
    hits = []
    for path in _shell_files():
        n = shell_reads(path.read_text(encoding="utf-8"), name)
        if n:
            hits.append(f"{path.relative_to(ROOT)}: {n}")
    assert not hits, f"{name} is expanded in: {hits}"


@pytest.mark.parametrize("source,expected", [
    ('import os\nos.environ.get("VNX_X")', 1),
    ('import os\nos.environ["VNX_X"]', 1),
    ('import os\nos.getenv("VNX_X", "0")', 1),
    ('import os\nos.environ.setdefault("VNX_X", "1")', 1),
    ('import os\nos.environ.pop("VNX_X", None)', 1),
    ('import os\n"VNX_X" in os.environ', 1),
    ('from os import environ\nenviron.get("VNX_X")', 1),
    ('from os import getenv as g\ng("VNX_X")', 1),
    ('import os\nos.environ.get("VNX_Y")', 0),
])
def test_the_scanner_sees_every_read_form(source, expected):
    assert python_reads(ast.parse(source), "VNX_X") == expected


@pytest.mark.parametrize("text,expected", [
    ('if [ "$VNX_X" = 1 ]', 1), ("echo ${VNX_X:-}", 1), ("echo ${VNX_X}", 1),
    ("echo $VNX_XY", 0), ("echo VNX_X", 0),
])
def test_the_shell_scanner_sees_every_expansion(text, expected):
    assert shell_reads(text, "VNX_X") == expected
