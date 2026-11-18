"""No read of VNX_PROFILE_SELECTOR is left in scripts/, vnx_cli/ or dashboard/ (ADR-087)."""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
NAME = "VNX_PROFILE_SELECTOR"


def _files(pattern):
    return [p for d in ("scripts", "vnx_cli", "dashboard") for p in (ROOT / d).rglob(pattern) if "__pycache__" not in p.parts]


def _str(node):
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def python_reads(tree, name):
    env, getenv = set(), set()
    for n in ast.walk(tree):
        if isinstance(n, ast.ImportFrom) and n.module == "os":
            env |= {a.asname or a.name for a in n.names if a.name == "environ"}
            getenv |= {a.asname or a.name for a in n.names if a.name == "getenv"}

    def is_env(n):
        return (isinstance(n, ast.Attribute) and n.attr == "environ") or (isinstance(n, ast.Name) and n.id in env)

    count = 0
    for n in ast.walk(tree):
        if isinstance(n, ast.Call) and n.args and _str(n.args[0]) == name:
            f = n.func
            if isinstance(f, ast.Attribute) and (f.attr == "getenv" or (f.attr in {"get", "setdefault", "pop"} and is_env(f.value))):
                count += 1
            elif isinstance(f, ast.Name) and f.id in getenv:
                count += 1
        elif isinstance(n, ast.Subscript) and is_env(n.value) and _str(n.slice) == name:
            count += 1
        elif isinstance(n, ast.Compare) and _str(n.left) == name and any(is_env(c) for c in n.comparators):
            count += 1
    return count


def shell_reads(text, name):
    return len(re.findall(r"\$\{?" + re.escape(name) + r"(?![A-Za-z0-9_])", text))


def test_no_python_read():
    hits = [str(p.relative_to(ROOT)) for p in _files("*.py") if python_reads(ast.parse(p.read_text(encoding="utf-8")), NAME)]
    assert not hits, hits


def test_no_shell_expansion():
    hits = [str(p.relative_to(ROOT)) for p in _files("*.sh") if shell_reads(p.read_text(encoding="utf-8"), NAME)]
    assert not hits, hits


@pytest.mark.parametrize("src,n", [
    ('import os\nos.environ.get("X")', 1), ('import os\nos.environ["X"]', 1), ('import os\nos.getenv("X")', 1),
    ('import os\nos.environ.setdefault("X", "1")', 1), ('import os\nos.environ.pop("X")', 1),
    ('import os\n"X" in os.environ', 1), ('from os import environ\nenviron.get("X")', 1),
    ('from os import getenv as g\ng("X")', 1), ('import os\nos.environ.get("Y")', 0),
])
def test_scanner_sees_every_python_read_form(src, n):
    assert python_reads(ast.parse(src), "X") == n


@pytest.mark.parametrize("text,n", [("$X", 1), ("${X:-}", 1), ("$XY", 0)])
def test_scanner_sees_shell_expansions(text, n):
    assert shell_reads(text, "X") == n
