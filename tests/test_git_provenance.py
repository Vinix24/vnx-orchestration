#!/usr/bin/env python3
"""Tests for scripts/lib/append_receipt_internals/git_provenance.py::_build_git_provenance
(ADR-035 §3.1.1/§3.2, r2 HIGH-4) — the changed-file `paths` addition to
`diff_summary`, needed by the doc-only invariant.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).resolve().parent
VNX_ROOT = TESTS_DIR.parent
sys.path.insert(0, str(VNX_ROOT / "scripts" / "lib"))

from append_receipt_internals.git_provenance import _build_git_provenance  # noqa: E402


def _run(cmd, cwd):
    subprocess.run(cmd, cwd=str(cwd), check=True, capture_output=True, text=True)


@pytest.fixture()
def git_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _run(["git", "init", "-q"], repo)
    _run(["git", "config", "user.email", "test@example.com"], repo)
    _run(["git", "config", "user.name", "Test"], repo)
    (repo / "docs").mkdir()
    (repo / "README.md").write_text("hello\n", encoding="utf-8")
    (repo / "docs" / "ADR.md").write_text("adr\n", encoding="utf-8")
    (repo / "scripts.py").write_text("print(1)\n", encoding="utf-8")
    _run(["git", "add", "."], repo)
    _run(["git", "commit", "-q", "-m", "init"], repo)
    return repo


def test_paths_absent_when_clean(git_repo):
    provenance = _build_git_provenance(git_repo)
    assert provenance["is_dirty"] is False
    assert provenance["diff_summary"] is None


def test_paths_lists_changed_files_when_dirty(git_repo):
    (git_repo / "docs" / "ADR.md").write_text("adr changed\n", encoding="utf-8")
    (git_repo / "scripts.py").write_text("print(2)\n", encoding="utf-8")

    provenance = _build_git_provenance(git_repo)
    assert provenance["is_dirty"] is True
    assert provenance["diff_summary"] is not None
    paths = provenance["diff_summary"]["paths"]
    assert set(paths) == {"docs/ADR.md", "scripts.py"}


def test_paths_all_docs_when_only_docs_changed(git_repo):
    (git_repo / "docs" / "ADR.md").write_text("adr changed again\n", encoding="utf-8")

    provenance = _build_git_provenance(git_repo)
    paths = provenance["diff_summary"]["paths"]
    assert paths == ["docs/ADR.md"]
    assert all(p.startswith("docs/") and p.endswith(".md") for p in paths)


def test_paths_uses_same_is_dirty_gate_as_shortstat(git_repo):
    """paths is populated inside the same `if is_dirty` branch as shortstat —
    both are present together or both absent, never one without the other."""
    provenance = _build_git_provenance(git_repo)
    assert ("paths" in (provenance["diff_summary"] or {})) == (provenance["diff_summary"] is not None)

    (git_repo / "README.md").write_text("hello changed\n", encoding="utf-8")
    provenance = _build_git_provenance(git_repo)
    assert "paths" in provenance["diff_summary"]
    assert "files_changed" in provenance["diff_summary"]


# ---------------------------------------------------------------------------
# ADR-035 §9 PR-5 paths-fix: `git diff --name-only` alone only sees unstaged
# tracked changes. A dispatch that STAGES its code change and leaves docs
# unstaged would previously show only the docs path -- and the doc-only
# invariant (§3.1.1) would wrongly accept a change that includes staged code.
# ---------------------------------------------------------------------------


def test_paths_includes_staged_changes(git_repo):
    """A fully staged (git add, not committed) change must appear in paths —
    `git diff --name-only` alone would miss it entirely."""
    (git_repo / "scripts.py").write_text("print(3)\n", encoding="utf-8")
    _run(["git", "add", "scripts.py"], git_repo)

    provenance = _build_git_provenance(git_repo)
    assert provenance["is_dirty"] is True
    paths = provenance["diff_summary"]["paths"]
    assert "scripts.py" in paths


def test_paths_includes_untracked_files(git_repo):
    """A brand-new, never-added file must appear in paths — `git diff
    --name-only` (staged or unstaged) never sees untracked files at all."""
    (git_repo / "new_untracked.py").write_text("print('new')\n", encoding="utf-8")

    provenance = _build_git_provenance(git_repo)
    assert provenance["is_dirty"] is True
    paths = provenance["diff_summary"]["paths"]
    assert "new_untracked.py" in paths


def test_paths_includes_staged_code_plus_unstaged_docs(git_repo):
    """The dispatch's exact failure scenario: staged code-change + unstaged
    docs change under method='n/a' must NOT read as doc-only — paths must
    carry the staged code path alongside the unstaged docs path."""
    (git_repo / "scripts.py").write_text("print('staged change')\n", encoding="utf-8")
    _run(["git", "add", "scripts.py"], git_repo)
    (git_repo / "docs" / "ADR.md").write_text("unstaged docs change\n", encoding="utf-8")

    provenance = _build_git_provenance(git_repo)
    paths = provenance["diff_summary"]["paths"]
    assert set(paths) == {"scripts.py", "docs/ADR.md"}
    # Not every path is docs/**/*.md -- the doc-only invariant must reject this.
    assert not all(p.startswith("docs/") and p.endswith(".md") for p in paths)
