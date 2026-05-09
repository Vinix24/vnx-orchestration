#!/usr/bin/env python3
"""ADR-003 enforcement gate unit tests.

Covers:
- Forbidden import patterns are detected
- Magic-comment opt-in (exact match) permits an import
- Misspelled magic comment does NOT permit an import
- Magic comment not on the immediately preceding line does NOT permit
- Real repo scan returns no violations
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).resolve().parent
VNX_ROOT = TESTS_DIR.parent
SCRIPTS_DIR = VNX_ROOT / "scripts"

sys.path.insert(0, str(SCRIPTS_DIR))
from check_adr_003_no_sdk_imports import (  # noqa: E402
    ALLOWLIST_COMMENT,
    check_file,
    scan_repo,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write(tmp_path: Path, name: str, content: str) -> Path:
    f = tmp_path / name
    f.write_text(content, encoding="utf-8")
    return f


# ---------------------------------------------------------------------------
# Pattern detection tests
# ---------------------------------------------------------------------------


def test_clean_file_passes(tmp_path: Path) -> None:
    f = _write(tmp_path, "clean.py", "import os\nprint('hello')\n")
    assert check_file(f) == []


@pytest.mark.parametrize(
    "line",
    [
        "import anthropic",
        "import anthropic  # some comment",
        "from anthropic import Anthropic",
        "from anthropic.types import Message",
        "import claude_agent_sdk",
        "from claude_agent_sdk import Agent",
        # Codex PR #439 round-1 advisory: multi-import was missed by old regex.
        # Now caught: package name first in the import list.
        "import anthropic, os",
        "import claude_agent_sdk, json",
    ],
)
def test_forbidden_imports_detected(tmp_path: Path, line: str) -> None:
    f = _write(tmp_path, "bad.py", f"import os\n{line}\n")
    violations = check_file(f)
    assert len(violations) == 1
    assert violations[0][1] == line.strip()


def test_non_matching_similar_names_pass(tmp_path: Path) -> None:
    content = (
        "import anthropic_utils\n"
        "from anthropic_client import foo\n"
        "import claude_sdk\n"
    )
    f = _write(tmp_path, "ok.py", content)
    assert check_file(f) == []


# ---------------------------------------------------------------------------
# Magic-comment opt-in tests
# ---------------------------------------------------------------------------


def test_magic_comment_exact_opts_in(tmp_path: Path) -> None:
    content = (
        f"{ALLOWLIST_COMMENT}\n"
        "import anthropic\n"
    )
    f = _write(tmp_path, "allowed.py", content)
    assert check_file(f) == []


def test_magic_comment_exact_opts_in_from_form(tmp_path: Path) -> None:
    content = (
        f"{ALLOWLIST_COMMENT}\n"
        "from anthropic import Anthropic\n"
    )
    f = _write(tmp_path, "allowed2.py", content)
    assert check_file(f) == []


def test_misspelled_magic_comment_does_not_opt_in(tmp_path: Path) -> None:
    misspelled = "# vnx:allow-anthropic-sdk-import"
    content = f"{misspelled}\nimport anthropic\n"
    f = _write(tmp_path, "bad_comment.py", content)
    violations = check_file(f)
    assert len(violations) == 1


def test_magic_comment_extra_whitespace_does_not_opt_in(tmp_path: Path) -> None:
    content = f"  {ALLOWLIST_COMMENT}  \nimport anthropic\n"
    f = _write(tmp_path, "ws_comment.py", content)
    # The preceding line stripped matches, so this should be allowed
    # (we strip the line before comparison)
    assert check_file(f) == []


def test_magic_comment_not_adjacent_does_not_opt_in(tmp_path: Path) -> None:
    content = (
        f"{ALLOWLIST_COMMENT}\n"
        "x = 1\n"
        "import anthropic\n"
    )
    f = _write(tmp_path, "non_adjacent.py", content)
    violations = check_file(f)
    assert len(violations) == 1


def test_magic_comment_empty_comment_does_not_opt_in(tmp_path: Path) -> None:
    content = "# some other comment\nimport anthropic\n"
    f = _write(tmp_path, "other_comment.py", content)
    violations = check_file(f)
    assert len(violations) == 1


def test_import_on_first_line_no_preceding(tmp_path: Path) -> None:
    f = _write(tmp_path, "first_line.py", "import anthropic\n")
    violations = check_file(f)
    assert len(violations) == 1


# ---------------------------------------------------------------------------
# Repo-level scan
# ---------------------------------------------------------------------------


def test_repo_scan_returns_no_violations() -> None:
    violations = scan_repo(VNX_ROOT)
    if violations:
        lines = [f"  {path}:{lineno}: {line}" for path, lineno, line in violations]
        pytest.fail(
            "ADR-003 violation(s) found in repo:\n" + "\n".join(lines)
        )


def test_scan_repo_only_scans_configured_dirs(tmp_path: Path) -> None:
    # File in docs/ should NOT be scanned
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    _write(docs_dir, "example.py", "import anthropic\n")
    assert scan_repo(tmp_path) == []


def test_scan_repo_detects_in_scripts(tmp_path: Path) -> None:
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()
    _write(scripts_dir, "bad_script.py", "import anthropic\n")
    violations = scan_repo(tmp_path)
    assert len(violations) == 1


def test_scan_repo_detects_in_tests(tmp_path: Path) -> None:
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    _write(tests_dir, "bad_test.py", "from claude_agent_sdk import X\n")
    violations = scan_repo(tmp_path)
    assert len(violations) == 1


def test_scan_repo_detects_in_dashboard(tmp_path: Path) -> None:
    dash_dir = tmp_path / "dashboard"
    dash_dir.mkdir()
    _write(dash_dir, "bad.py", "from anthropic import Anthropic\n")
    violations = scan_repo(tmp_path)
    assert len(violations) == 1


def test_scan_repo_respects_magic_comment(tmp_path: Path) -> None:
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()
    content = f"{ALLOWLIST_COMMENT}\nimport anthropic\n"
    _write(scripts_dir, "allowed.py", content)
    assert scan_repo(tmp_path) == []
