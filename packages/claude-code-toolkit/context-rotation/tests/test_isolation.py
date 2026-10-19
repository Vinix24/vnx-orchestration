"""Standalone guarantees: stdlib-only imports and no leaked identity."""
from __future__ import annotations

import ast
import re
import sys

from conftest import PKG

TOOLKIT = PKG.parent

FALLBACK_STDLIB = {
    "__future__", "argparse", "ast", "collections", "contextlib", "dataclasses", "datetime",
    "errno", "fcntl", "fnmatch", "functools", "glob", "hashlib", "io", "itertools", "json", "os", "pathlib", "re",
    "shlex", "shutil", "signal", "stat", "subprocess", "sys", "tempfile", "textwrap", "time",
    "typing", "unicodedata", "uuid",
}
STDLIB = set(getattr(sys, "stdlib_module_names", ())) | FALLBACK_STDLIB


def package_modules():
    names = set()
    for sub in ("hooks", "lib", "scripts"):
        for py in (PKG / sub).glob("*.py"):
            names.add(py.stem)
    return names


def imported_roots(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    roots = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.extend(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, "relative import in %s" % path
            roots.append((node.module or "").split(".")[0])
    return roots


def test_runtime_imports_are_stdlib_or_own():
    own = package_modules()
    files = [p for sub in ("hooks", "lib", "scripts") for p in (PKG / sub).glob("**/*.py")]
    assert files
    for path in files:
        for root in imported_roots(path):
            assert root in STDLIB or root in own, "%s imports %s" % (path, root)


def test_test_imports_allow_pytest_and_own_modules():
    own = package_modules() | {"conftest"}
    for path in (PKG / "tests").glob("*.py"):
        for root in imported_roots(path):
            assert root in STDLIB or root in own or root == "pytest", "%s imports %s" % (path, root)


FORBIDDEN = {
    "product name": re.compile("v" + "nx", re.IGNORECASE),
    "runtime dir": re.compile(re.escape(".v" + "nx-data")),
    "home path": re.compile(re.escape("/Us" + "ers/")),
    "email": re.compile(r"[\w.+-]+" + "@" + r"[\w-]+\.[\w.-]+"),
    "uuid": re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"),
}
CREDITS_ALLOWED = {"product name"}


def toolkit_files():
    return [
        p for p in TOOLKIT.rglob("*")
        if p.is_file() and "__pycache__" not in p.parts and ".pytest_cache" not in p.parts
        and p.suffix != ".pyc"
    ]


def is_credits_line(path, line):
    return path.name == "README.md" and path.parent.name == "context-rotation" and line.startswith("Credits:")


def test_no_identity_leaks_anywhere_in_the_toolkit():
    files = toolkit_files()
    assert len(files) > 40
    hits = []
    for path in files:
        text = path.read_text(encoding="utf-8", errors="replace")
        for number, line in enumerate(text.splitlines(), 1):
            for label, pattern in FORBIDDEN.items():
                if pattern.search(line) and not (label in CREDITS_ALLOWED and is_credits_line(path, line)):
                    hits.append("%s:%d %s" % (path.relative_to(TOOLKIT), number, label))
    assert hits == []


def test_leak_patterns_do_detect_what_they_claim():
    assert FORBIDDEN["product name"].search("a V" + "nX line")
    assert FORBIDDEN["email"].search("someone" + "@" + "example.org")
    assert FORBIDDEN["uuid"].search("-".join(["123e4567", "e89b", "12d3", "a456", "426614174000"]))
    assert not FORBIDDEN["uuid"].search("<uuid>")
    assert is_credits_line(PKG / "README.md", "Credits: x")
    assert not is_credits_line(PKG / "README.md", " Credits: x")
