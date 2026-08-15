"""Tests for scripts/ci/classify_ci_profile.py.

The classification is an allowlist: only docs-only paths may be "light", and
everything else — code, unclassifiable paths, empty input, malformed paths —
must be "heavy" (fail closed).  These tests pin that contract, including the
most important one: an unknown path never produces a lighter run.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

_CI_DIR = Path(__file__).resolve().parent.parent / "scripts" / "ci"
if str(_CI_DIR) not in sys.path:
    sys.path.insert(0, str(_CI_DIR))

import classify_ci_profile as classifier  # noqa: E402

LIGHT = classifier.PROFILE_LIGHT
HEAVY = classifier.PROFILE_HEAVY


# ---------------------------------------------------------------------------
# The light allowlist
# ---------------------------------------------------------------------------


def test_docs_only_paths_are_light():
    assert classifier.classify_paths(["docs/core/DISPATCH_RULES.md"]) == LIGHT
    assert classifier.classify_paths(["docs/governance/decisions/ADR-036.md"]) == LIGHT
    assert classifier.classify_paths(["docs/a.md", "docs/b/c.md"]) == LIGHT


def test_root_markdown_is_light():
    assert classifier.classify_paths(["README.md"]) == LIGHT
    assert classifier.classify_paths(["CHANGELOG.md", "SECURITY.md"]) == LIGHT


def test_claudedocs_is_light():
    assert classifier.classify_paths(["claudedocs/plan-t0-daemon-driven-lifecycle.md"]) == LIGHT


def test_mixed_docs_only_paths_are_light():
    # docs/** + claudedocs/** + root *.md together is still all docs.
    assert (
        classifier.classify_paths(
            ["docs/a.md", "claudedocs/b.md", "README.md"]
        )
        == LIGHT
    )


# ---------------------------------------------------------------------------
# Fail-closed: anything that is not unambiguously docs-only is heavy
# ---------------------------------------------------------------------------


def test_docs_plus_one_python_file_is_heavy():
    assert classifier.classify_paths(["docs/a.md", "scripts/foo.py"]) == HEAVY


def test_unmatched_path_is_heavy():
    # The fail-closed core: a path no rule recognises gets the heaviest profile.
    assert classifier.classify_paths(["somewhere/unknown.txt"]) == HEAVY
    assert classifier.classify_paths(["scripts/lib/project_root.py"]) == HEAVY


def test_empty_list_is_heavy():
    assert classifier.classify_paths([]) == HEAVY


def test_whitespace_only_input_is_heavy():
    assert classifier.classify_paths(["", "   ", "\t"]) == HEAVY


def test_skill_markdown_is_heavy():
    # skills/** markdown drives agent behaviour — a code change, not docs.
    assert classifier.classify_paths(["skills/horizon/SKILL.md"]) == HEAVY


def test_workflow_yaml_is_heavy():
    assert classifier.classify_paths([".github/workflows/vnx-ci.yml"]) == HEAVY


# ---------------------------------------------------------------------------
# Malformed paths: heavy, never a crash
# ---------------------------------------------------------------------------


def test_path_with_space_is_heavy_not_crash():
    # A leading/trailing/embedded space must not sneak a match past the
    # allowlist, and must not crash the classifier.
    assert classifier.classify_paths(["docs/my file.md"]) == HEAVY
    assert classifier.classify_paths(["my file.md"]) == HEAVY
    assert classifier.classify_paths([" docs/foo.md"]) == HEAVY


def test_path_with_non_ascii_is_heavy_not_crash():
    assert classifier.classify_paths(["docs/résumé.md"]) == HEAVY
    assert classifier.classify_paths(["café.md"]) == HEAVY


# ---------------------------------------------------------------------------
# CLI: the logic is exercised exactly as the workflow invokes it
# ---------------------------------------------------------------------------


def _run_cli(*args: str, stdin_text: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(_CI_DIR / "classify_ci_profile.py"), *args],
        capture_output=True,
        text=True,
        input=stdin_text,
    )


def test_cli_prints_profile_from_stdin():
    result = _run_cli(stdin_text="docs/a.md\ndocs/b.md\n")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == LIGHT


def test_cli_prints_heavy_from_positional_args():
    result = _run_cli("docs/a.md", "scripts/foo.py")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == HEAVY


def test_cli_paths_file_interface(tmp_path: Path):
    paths_file = tmp_path / "changed.txt"
    paths_file.write_text("README.md\nclaudedocs/x.md\n", encoding="utf-8")
    result = _run_cli("--paths-file", str(paths_file))
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == LIGHT


def test_cli_empty_stdin_is_heavy():
    result = _run_cli(stdin_text="")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == HEAVY
