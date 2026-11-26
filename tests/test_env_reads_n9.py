"""N9 reader boundary: the five names may only reach the registry in scripts/lib."""
import ast
from fnmatch import fnmatchcase
from pathlib import Path

import pytest

KEYS = ("VNX_T0_PROVIDER", *(f"VNX_T{i}_SKIP_PERMISSIONS" for i in range(4)))
LIB = Path(__file__).resolve().parent.parent / "scripts" / "lib"


def env_reads(source):
    reads = []
    for node in ast.walk(ast.parse(source)):
        keys = []
        if isinstance(node, ast.Call) and ast.unparse(node.func) in {
            "os.getenv", "os.environ.get", "os.environ.setdefault", "os.environ.pop",
        }:
            keys = node.args[:1]
        elif (isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Load)
              and ast.unparse(node.value) == "os.environ"):
            keys = [node.slice]
        elif isinstance(node, ast.Compare) and any(isinstance(op, (ast.In, ast.NotIn)) for op in node.ops):
            if any(ast.unparse(value) == "os.environ" for value in node.comparators):
                keys = [node.left]
        for key in keys:
            if not isinstance(key, (ast.Constant, ast.JoinedStr)):
                continue
            parts = key.values if isinstance(key, ast.JoinedStr) else [key]
            pattern = "".join(part.value if isinstance(part, ast.Constant) and isinstance(part.value, str)
                              else "*" for part in parts)
            reads.extend((name, node.lineno) for name in KEYS if fnmatchcase(name, pattern))
    return reads


def test_n9_names_have_no_direct_env_reads():
    hits = [f"{path.relative_to(LIB)}:{line} {key}" for path in sorted(LIB.rglob("*.py"))
            for key, line in env_reads(path.read_text(encoding="utf-8"))]
    assert hits == []


@pytest.mark.parametrize("key", KEYS)
@pytest.mark.parametrize("expression", [
    'os.environ.get("{key}")', 'os.getenv("{key}")', 'os.environ["{key}"]',
    'os.environ.setdefault("{key}", "0")', 'os.environ.pop("{key}")', '"{key}" in os.environ',
])
def test_guard_sees_literal_reads(key, expression):
    assert env_reads(expression.format(key=key)) == [(key, 1)]


def test_guard_sees_the_old_dynamic_read_and_allows_config_reads():
    assert env_reads('os.environ.get(f"VNX_{tid}_SKIP_PERMISSIONS", "0")') == [
        (key, 1) for key in KEYS[1:]]
    assert env_reads('config_runtime.get_bool(f"VNX_{tid}_SKIP_PERMISSIONS")') == []
    assert env_reads('config_runtime.get("VNX_T0_PROVIDER")') == []


def test_guard_refuses_unparseable_source():
    with pytest.raises(SyntaxError):
        env_reads("os.environ.get(")
