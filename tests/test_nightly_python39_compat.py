"""Python 3.9 compatibility regression tests for the nightly pipeline.

Verifies that quality_db_init.py (and any other nightly pipeline script that
carries X | Y union annotations) has a ``from __future__ import annotations``
guard, so the module loads without TypeError on Python 3.9.

These tests use ast + compile so they run on any Python version that can
import the test itself — no subprocess needed.
"""

from __future__ import annotations

import ast
import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"

# Scripts in the nightly pipeline that must carry the future-import when they
# contain X | Y union annotations.
NIGHTLY_PIPELINE_SCRIPTS: list[Path] = [
    SCRIPTS_DIR / "quality_db_init.py",
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _has_future_annotations(source: str) -> bool:
    """Return True if the source contains ``from __future__ import annotations``."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return False
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.module == "__future__" and any(
                alias.name == "annotations" for alias in node.names
            ):
                return True
    return False


def _has_union_annotations(source: str) -> bool:
    """Return True if the source contains X | Y in annotation positions.

    Walks the AST and looks for BinOp nodes with BitOr operator that appear
    inside annotation contexts (function arguments, return annotations, or
    AnnAssign nodes).  This is more accurate than regex — it ignores ``|``
    inside expressions, f-strings, and comments.
    """
    try:
        # ast.parse succeeds on 3.9 only if the syntax is valid 3.9 syntax.
        # Union annotations (X | Y) are *not* valid syntax in 3.9 at runtime,
        # but they are parseable by Python 3.10+ ast.  When running under 3.9
        # we use a text-based heuristic as a fallback.
        tree = ast.parse(source)
    except SyntaxError:
        # Fallback: regex-based check for annotation-like patterns.
        import re
        return bool(re.search(r"(?:->|:\s*\w).*\|\s*\w", source))

    for node in ast.walk(tree):
        # Return annotation: def f() -> A | B
        if isinstance(node, ast.FunctionDef) and node.returns is not None:
            if isinstance(node.returns, ast.BinOp) and isinstance(node.returns.op, ast.BitOr):
                return True
        # Argument annotation: def f(x: A | B)
        if isinstance(node, ast.arg) and node.annotation is not None:
            if isinstance(node.annotation, ast.BinOp) and isinstance(node.annotation.op, ast.BitOr):
                return True
        # Variable annotation: x: A | B = ...
        if isinstance(node, ast.AnnAssign) and isinstance(node.annotation, ast.BinOp):
            if isinstance(node.annotation.op, ast.BitOr):
                return True
    return False


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("script_path", NIGHTLY_PIPELINE_SCRIPTS, ids=lambda p: p.name)
def test_future_annotations_present_when_union_annotations_exist(script_path: Path) -> None:
    """Scripts with X | Y annotations must carry from __future__ import annotations."""
    assert script_path.exists(), f"Script not found: {script_path}"
    source = script_path.read_text()
    if _has_union_annotations(source):
        assert _has_future_annotations(source), (
            f"{script_path.name} contains X | Y union annotations but is missing "
            "'from __future__ import annotations'. This causes a TypeError on Python 3.9 "
            "when the module is imported (crashes nightly pipeline phase 0-schema-init)."
        )


@pytest.mark.parametrize("script_path", NIGHTLY_PIPELINE_SCRIPTS, ids=lambda p: p.name)
def test_script_compiles_cleanly(script_path: Path) -> None:
    """Script must compile without SyntaxError using the current Python interpreter."""
    assert script_path.exists(), f"Script not found: {script_path}"
    source = script_path.read_text()
    try:
        compile(source, str(script_path), "exec")
    except SyntaxError as exc:
        pytest.fail(
            f"{script_path.name} failed to compile: {exc}\n"
            "Check for Python 3.10+ syntax that requires 'from __future__ import annotations'."
        )


def test_quality_db_init_importable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """quality_db_init must be importable without crashing at module level.

    Uses a fresh subprocess so the import happens in isolation and any
    module-level crash (the original bug: TypeError on X | Y annotation
    evaluation before main() runs) surfaces as a non-zero exit code.
    """
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; "
                f"sys.path.insert(0, '{SCRIPTS_DIR!s}'); "
                f"sys.path.insert(0, '{SCRIPTS_DIR / 'lib'!s}'); "
                "import quality_db_init"
            ),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, (
        f"quality_db_init failed to import (exit={result.returncode}).\n"
        f"stderr: {result.stderr.strip()}\n"
        "This is the Python 3.9 union-annotation crash that was the root cause "
        "of 9 consecutive nightly pipeline failures (Jun 1-9)."
    )
