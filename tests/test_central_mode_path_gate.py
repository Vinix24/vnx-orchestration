#!/usr/bin/env python3
"""Unit tests for the central-mode path-correctness gate.

Covers:
- The scanner flags a planted ``__file__``-derived ``.vnx-data`` literal.
- The current repo tree passes (all remaining sites grandfathered/exempt).
- A ``state_dir.parent.parent`` derived from a runtime Path param is NOT flagged.
- Comments and docstrings mentioning ``.vnx-data`` never trip the AST scanner.
- The canonical resolvers (vnx_paths.py / project_root.py) are exempt.
- A call routed through the resolver is not flagged.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).resolve().parent
VNX_ROOT = TESTS_DIR.parent
SCRIPTS_DIR = VNX_ROOT / "scripts"

sys.path.insert(0, str(SCRIPTS_DIR))
from check_no_file_derived_data_paths import (  # noqa: E402
    GRANDFATHERED,
    check_source,
    scan_dir,
)


# ---------------------------------------------------------------------------
# check_source unit tests (AST detection)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "src",
    [
        'p = Path(__file__).resolve().parent.parent / ".vnx-data" / "state"\n',
        'p = Path(__file__).parent.parent.parent / ".vnx-data"\n',
        'ROOT = Path(__file__).resolve().parents[2]\np = ROOT / ".vnx-data" / "events"\n',
        'HERE = Path(__file__).resolve()\np = HERE.parent.parent / "ROADMAP.yaml"\n',
        'sd = Path(__file__).parent\np = sd.parent.parent / ".vnx-data"\n',
    ],
)
def test_file_derived_data_paths_flagged(src: str) -> None:
    violations = check_source(src)
    assert len(violations) >= 1, f"expected a violation for:\n{src}"


def test_state_dir_param_not_flagged() -> None:
    # state_dir is a resolved runtime Path parameter, NOT __file__-anchored.
    src = (
        "def f(state_dir):\n"
        '    return state_dir.parent.parent / "ROADMAP.yaml"\n'
    )
    assert check_source(src) == []


def test_data_dir_env_param_not_flagged() -> None:
    src = (
        "def f(vnx_data_dir):\n"
        '    return Path(vnx_data_dir) / ".vnx-data" / "state"\n'
    )
    assert check_source(src) == []


def test_canonical_resolver_call_not_flagged() -> None:
    # The fix pattern: route through the resolver — no __file__ anchor.
    src = (
        "from vnx_paths import resolve_paths\n"
        'p = Path(resolve_paths()["VNX_DATA_DIR"]) / "events"\n'
    )
    assert check_source(src) == []


def test_comment_mentioning_data_path_not_flagged() -> None:
    src = (
        "def f():\n"
        "    # A Path(__file__).parent.parent / '.vnx-data' walk would hit the keystone\n"
        "    from vnx_paths import resolve_state_dir\n"
        "    return resolve_state_dir()\n"
    )
    assert check_source(src) == []


def test_docstring_mentioning_data_path_not_flagged() -> None:
    src = (
        "def f():\n"
        '    """Resolve ~/.vnx-data/<project> — never Path(__file__)/.vnx-data."""\n'
        "    from vnx_paths import resolve_state_dir\n"
        "    return resolve_state_dir()\n"
    )
    assert check_source(src) == []


# ---------------------------------------------------------------------------
# scan_dir integration tests
# ---------------------------------------------------------------------------


def test_current_tree_passes() -> None:
    """The live tree must be clean: every remaining site is grandfathered/exempt."""
    violations = scan_dir(VNX_ROOT)
    if violations:
        lines = [f"  {rel}:{ln}: {seg}" for rel, ln, seg in violations]
        pytest.fail(
            "central-mode path gate found un-grandfathered violation(s):\n"
            + "\n".join(lines)
        )


def test_planted_literal_in_lib_fails(tmp_path: Path) -> None:
    lib = tmp_path / "scripts" / "lib"
    lib.mkdir(parents=True)
    (lib / "planted.py").write_text(
        "from pathlib import Path\n"
        "def bad():\n"
        '    return Path(__file__).resolve().parent.parent / ".vnx-data" / "planted"\n',
        encoding="utf-8",
    )
    violations = scan_dir(tmp_path)
    assert any(rel.endswith("planted.py") for rel, _, _ in violations)


def test_exempt_resolvers_skipped(tmp_path: Path) -> None:
    lib = tmp_path / "scripts" / "lib"
    lib.mkdir(parents=True)
    body = (
        "from pathlib import Path\n"
        "def r():\n"
        '    return Path(__file__).resolve().parents[2] / ".vnx-data" / "state"\n'
    )
    (lib / "vnx_paths.py").write_text(body, encoding="utf-8")
    (lib / "project_root.py").write_text(body, encoding="utf-8")
    assert scan_dir(tmp_path) == []


def test_grandfathered_segment_allows_current_but_new_line_fails(tmp_path: Path) -> None:
    # A grandfathered segment passes; a DIFFERENT planted segment in the same
    # file still fails (the gate blocks new occurrences).
    lib = tmp_path / "scripts" / "lib"
    lib.mkdir(parents=True)
    (lib / "gate_register_emit.py").write_text(
        "from pathlib import Path\n"
        "_REPO_ROOT = Path(__file__).resolve().parents[2]\n"
        "def a():\n"
        '    return _REPO_ROOT / ".vnx-data" / "state" / "dispatch_register.ndjson"\n'
        "def b():\n"
        '    return _REPO_ROOT / ".vnx-data" / "planted-new"\n',
        encoding="utf-8",
    )
    violations = scan_dir(tmp_path)
    segs = {seg for _, _, seg in violations}
    assert '_REPO_ROOT / ".vnx-data" / "planted-new"' in segs
    assert (
        '_REPO_ROOT / ".vnx-data" / "state" / "dispatch_register.ndjson"'
        not in segs
    )


def test_grandfather_keys_reference_real_files() -> None:
    # Guard against stale allow-list entries drifting from the tree.
    for rel in GRANDFATHERED:
        assert (VNX_ROOT / rel).is_file(), f"grandfathered path missing: {rel}"
