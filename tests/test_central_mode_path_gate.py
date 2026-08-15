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
    GRANDFATHERED_REPO_ROOT_ANCHORS,
    GRANDFATHERED_REPO_ROOT_FINDERS,
    GRANDFATHERED_RESOLVER_ANCHORS,
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


def test_helper_return_of_file_flagged() -> None:
    # The helper-return false-negative: __file__ hidden behind a helper's return.
    src = (
        "from pathlib import Path\n"
        "def _project_root():\n"
        "    return Path(__file__).resolve().parents[3]\n"
        "def bad():\n"
        '    return _project_root() / ".vnx-data" / "state"\n'
    )
    assert len(check_source(src)) >= 1


def test_transitive_helper_anchor_flagged() -> None:
    # Anchor propagates through a chain of helpers to a fixpoint.
    src = (
        "from pathlib import Path\n"
        "def _root():\n"
        "    return Path(__file__).resolve()\n"
        "def _repo():\n"
        "    return _root().parent\n"
        "def bad():\n"
        '    return _repo() / ".vnx-data"\n'
    )
    assert len(check_source(src)) >= 1


def test_env_helper_return_not_flagged() -> None:
    # A helper returning an ENV-derived (not __file__) path is not anchored.
    src = (
        "import os\n"
        "from pathlib import Path\n"
        "def _root():\n"
        "    return Path(os.environ['X'])\n"
        "def ok():\n"
        '    return _root() / ".vnx-data"\n'
    )
    assert check_source(src) == []


def test_planted_helper_return_in_subdir_fails(tmp_path: Path) -> None:
    pkg = tmp_path / "scripts" / "lib" / "some_pkg"
    pkg.mkdir(parents=True)
    (pkg / "paths.py").write_text(
        "from pathlib import Path\n"
        "def _root():\n"
        "    return Path(__file__).resolve().parents[3]\n"
        "def d():\n"
        '    return _root() / ".vnx-data" / "state"\n',
        encoding="utf-8",
    )
    violations = scan_dir(tmp_path)
    assert any(rel.endswith("paths.py") for rel, _, _ in violations)


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
# Shape 2 (input side): canonical resolver fed a __file__ anchor
# ---------------------------------------------------------------------------


def test_resolver_fed_caller_file_anchor_flagged() -> None:
    # The exact plan_gate_panel.py:506 construct that blocked every plan-gate
    # fleet-wide (2026-07-31): the canonical resolver, fed a package anchor. The
    # old gate tested the mechanism (a __file__-derived .vnx-data literal), not
    # the input — this case is RED on the pre-fix tree.
    src = (
        "from project_root import resolve_data_dir\n"
        "def _resolve_data_dir(data_dir):\n"
        "    if data_dir:\n"
        "        return data_dir\n"
        "    return resolve_data_dir(caller_file=__file__)\n"
    )
    violations = check_source(src)
    assert len(violations) >= 1
    assert any("resolve_data_dir(caller_file=__file__)" in seg for _, seg in violations)


@pytest.mark.parametrize(
    "src",
    [
        # positional anchor
        "from project_root import resolve_state_dir\n"
        "p = resolve_state_dir(__file__)\n",
        # attribute form
        "import project_root\n"
        "p = project_root.resolve_data_dir(__file__)\n",
        # resolve_dispatch_dir with a keyword anchor
        "from project_root import resolve_dispatch_dir\n"
        "p = resolve_dispatch_dir(caller_file=__file__)\n",
        # anchor hidden behind a file-anchored local name
        "from project_root import resolve_data_dir\n"
        "HERE = __file__\n"
        "p = resolve_data_dir(caller_file=HERE)\n",
    ],
)
def test_resolver_fed_file_anchor_variants_flagged(src: str) -> None:
    assert len(check_source(src)) >= 1, f"expected a violation for:\n{src}"


def test_resolver_without_file_anchor_not_flagged() -> None:
    # Legit uses: no args, an env-derived argument, a runtime Path parameter.
    src = (
        "import os\n"
        "from project_root import resolve_data_dir, resolve_state_dir\n"
        "a = resolve_data_dir()\n"
        "b = resolve_state_dir()\n"
        "def f(state_dir):\n"
        "    c = resolve_data_dir(caller_file=state_dir)\n"
        "    d = resolve_data_dir(caller_file=os.environ['X'])\n"
        "    return a, b, c, d\n"
    )
    assert check_source(src) == []


def test_resolver_anchor_only_flags_data_resolvers() -> None:
    # OI-1145 broadened the gate: resolve_project_root now IS flagged when fed a
    # __file__ anchor (shape 3a) — the same keystone bug one level up. The old
    # deliberate exclusion ("resolve_project_root resolves a REPO root, not a
    # .vnx-data dir") was reversed by the plan-gate opus-seat failure.
    src = (
        "from project_root import resolve_project_root\n"
        "p = resolve_project_root(__file__)\n"
    )
    violations = check_source(src)
    assert len(violations) >= 1
    assert any("resolve_project_root(__file__)" in seg for _, seg in violations)


def test_resolver_without_file_anchor_not_flagged_repo_root() -> None:
    # The fix pattern for the repo-root resolver: no __file__ anchor (no args,
    # or a runtime/env-derived argument) must not trip shape 3a.
    src = (
        "import os\n"
        "from project_root import resolve_project_root\n"
        "a = resolve_project_root()\n"
        "b = resolve_project_root(os.environ['X'])\n"
        "def f(repo_root):\n"
        "    return resolve_project_root(caller_file=repo_root)\n"
    )
    assert check_source(src) == []


def test_repo_root_finder_fed_file_anchor_flagged() -> None:
    # OI-1145 shape 3b: the exact plan_gate_panel construct that landed the seat
    # ledger in the keystone — a .git-marker finder fed a __file__-anchored start.
    src = (
        "from pathlib import Path\n"
        "def _find_repo_root(start):\n"
        "    return start\n"
        "def bad():\n"
        "    return _find_repo_root(Path(__file__).resolve().parent)\n"
    )
    violations = check_source(src)
    assert len(violations) >= 1
    assert any("_find_repo_root(Path(__file__).resolve().parent)" in seg for _, seg in violations)


def test_repo_root_finder_fed_cwd_not_flagged() -> None:
    # The OI-1145 fix pattern: the finder is fed CWD (not __file__), so it must
    # not trip shape 3b.
    src = (
        "from pathlib import Path\n"
        "def _find_repo_root(start):\n"
        "    return start\n"
        "def ok():\n"
        "    return _find_repo_root(Path.cwd())\n"
    )
    assert check_source(src) == []


def test_repo_root_resolver_attr_form_flagged() -> None:
    # Shape 3a attribute form (project_root.resolve_project_root) is caught too.
    src = (
        "import project_root\n"
        "p = project_root.resolve_project_root(__file__)\n"
    )
    violations = check_source(src)
    assert len(violations) >= 1
    assert any("project_root.resolve_project_root(__file__)" in seg for _, seg in violations)


def test_planted_resolver_anchor_in_lib_fails(tmp_path: Path) -> None:
    lib = tmp_path / "scripts" / "lib"
    lib.mkdir(parents=True)
    (lib / "planted.py").write_text(
        "from project_root import resolve_data_dir\n"
        "def bad():\n"
        "    return resolve_data_dir(caller_file=__file__)\n",
        encoding="utf-8",
    )
    violations = scan_dir(tmp_path)
    assert any(rel.endswith("planted.py") for rel, _, _ in violations)


def test_grandfathered_resolver_anchor_allows_current_but_new_line_fails(tmp_path: Path) -> None:
    lib = tmp_path / "scripts" / "lib"
    lib.mkdir(parents=True)
    (lib / "staging_validator.py").write_text(
        "from project_root import resolve_data_dir\n"
        "def a():\n"
        "    return resolve_data_dir(caller_file=__file__)\n"
        "def b():\n"
        "    return resolve_data_dir(caller_file=__file__)  # different line, same segment is fine\n",
        encoding="utf-8",
    )
    # Grandfathered segment passes.
    assert scan_dir(tmp_path) == []
    # A NEW resolver-anchor segment in the same file still trips the gate.
    (lib / "staging_validator.py").write_text(
        "from project_root import resolve_state_dir\n"
        "def a():\n"
        "    return resolve_state_dir(caller_file=__file__)\n",
        encoding="utf-8",
    )
    violations = scan_dir(tmp_path)
    segs = {seg for _, _, seg in violations}
    assert "resolve_state_dir(caller_file=__file__)" in segs


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
    for rel in GRANDFATHERED_RESOLVER_ANCHORS:
        assert (VNX_ROOT / rel).is_file(), f"grandfathered resolver-anchor path missing: {rel}"
    for rel in GRANDFATHERED_REPO_ROOT_ANCHORS:
        assert (VNX_ROOT / rel).is_file(), f"grandfathered repo-root anchor path missing: {rel}"
    for rel in GRANDFATHERED_REPO_ROOT_FINDERS:
        assert (VNX_ROOT / rel).is_file(), f"grandfathered repo-root finder path missing: {rel}"


def test_grandfathered_resolver_anchors_still_present_in_tree() -> None:
    # Each grandfathered resolver-anchor segment must still exist verbatim in its
    # file — a migrated site must DROP its entry (forcing the list to shrink),
    # not silently rot. Reuses the gate's own scanner: strip the allow-list and
    # confirm every entry comes back as a violation.
    import check_no_file_derived_data_paths as gate

    found = set()
    for py in (VNX_ROOT / "scripts" / "lib").rglob("*.py"):
        rel = py.relative_to(VNX_ROOT).as_posix()
        if rel not in GRANDFATHERED_RESOLVER_ANCHORS:
            continue
        for _, seg in gate._dedup_violations(py.read_text(encoding="utf-8")):
            if seg in GRANDFATHERED_RESOLVER_ANCHORS[rel]:
                found.add((rel, seg))
    expected = {
        (rel, seg) for rel, segs in GRANDFATHERED_RESOLVER_ANCHORS.items() for seg in segs
    }
    assert found == expected, (
        "stale or missing resolver-anchor grandfather entries:\n"
        + "\n".join(sorted(f"  {rel}: {seg}" for rel, seg in expected - found))
    )


def test_grandfathered_repo_root_anchors_still_present_in_tree() -> None:
    # OI-1145: the repo-root resolver + finder grandfather entries must still
    # exist verbatim — a migrated site must drop its entry, not rot silently.
    import check_no_file_derived_data_paths as gate

    found = set()
    for py in (VNX_ROOT / "scripts" / "lib").rglob("*.py"):
        rel = py.relative_to(VNX_ROOT).as_posix()
        allowed = (
            GRANDFATHERED_REPO_ROOT_ANCHORS.get(rel, set())
            | GRANDFATHERED_REPO_ROOT_FINDERS.get(rel, set())
        )
        if not allowed:
            continue
        for _, seg in gate._dedup_violations(py.read_text(encoding="utf-8")):
            if seg in allowed:
                found.add((rel, seg))
    expected = {
        (rel, seg)
        for rel, segs in (
            {**GRANDFATHERED_REPO_ROOT_ANCHORS, **GRANDFATHERED_REPO_ROOT_FINDERS}
        ).items()
        for seg in segs
    }
    assert found == expected, (
        "stale or missing repo-root grandfather entries:\n"
        + "\n".join(sorted(f"  {rel}: {seg}" for rel, seg in expected - found))
    )
