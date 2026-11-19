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
# CHANGELOG fragments: exactly the fragment rule plus the README are light
# ---------------------------------------------------------------------------


def test_changelog_fragment_alone_is_light():
    assert classifier.classify_paths(["changelog.d/20261017-clf-f1.md"]) == LIGHT


def test_changelog_fragment_readme_alone_is_light():
    assert classifier.classify_paths(["changelog.d/README.md"]) == LIGHT


def test_fragments_with_docs_are_light():
    assert classifier.classify_paths(["changelog.d/a.md", "changelog.d/README.md", "docs/a.md"]) == LIGHT


def test_non_markdown_file_in_changelog_dir_is_heavy():
    assert classifier.classify_paths(["changelog.d/notes.txt"]) == HEAVY


def test_non_fragment_markdown_in_changelog_dir_is_heavy():
    # Nested, hidden or a name outside the rule: the classifier fails closed.
    assert classifier.classify_paths(["changelog.d/sub/a.md"]) == HEAVY
    assert classifier.classify_paths(["changelog.d/.hidden.md"]) == HEAVY
    assert classifier.classify_paths(["changelog.d/-dash.md"]) == HEAVY


def test_fragment_plus_code_is_heavy():
    assert classifier.classify_paths(["changelog.d/a.md", "scripts/foo.py"]) == HEAVY


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


# Composed at run time: a contiguous literal here would be a test naming the path,
# which makes it heavy on the real checkout (the OI-3035 rule).
_UNNAMED = "docs/" + "cli-probe-"


def test_cli_prints_profile_from_stdin():
    result = _run_cli(stdin_text=f"{_UNNAMED}a.md\n{_UNNAMED}b.md\n")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == LIGHT


def test_cli_prints_heavy_from_positional_args():
    result = _run_cli("docs/a.md", "scripts/foo.py")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == HEAVY


def test_cli_paths_file_interface(tmp_path: Path):
    paths_file = tmp_path / "changed.txt"
    paths_file.write_text(f"{_UNNAMED}c.md\nclaudedocs/" + "cli-probe.md\n", encoding="utf-8")
    result = _run_cli("--paths-file", str(paths_file))
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == LIGHT


def test_cli_empty_stdin_is_heavy():
    result = _run_cli(stdin_text="")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == HEAVY


# ---------------------------------------------------------------------------
# A light path that a test names is heavy (OI-3035)
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent.parent


def _layout(tmp_path: Path, test_text: str) -> Path:
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_reader.py").write_text(test_text, encoding="utf-8")
    return tmp_path


def _classify(root: Path, paths: list[str]) -> str:
    return classifier.classify_paths(paths, classifier.scan_test_read_paths(root, paths))


def test_claude_md_only_diff_is_heavy_on_this_checkout():
    paths = ["CLAUDE.md"]
    assert classifier.classify_paths(paths) == LIGHT  # the old call shape: the OI-3035 bug
    assert _classify(REPO_ROOT, paths) == HEAVY


def test_root_md_that_no_test_names_stays_light(tmp_path: Path):
    root = _layout(tmp_path, "def test_x():\n    assert open('CLAUDE.md')\n")
    assert _classify(root, ["NOTES.md"]) == LIGHT
    assert _classify(root, ["CLAUDE.md"]) == HEAVY


def test_docs_file_a_test_names_is_heavy_and_others_stay_light(tmp_path: Path):
    root = _layout(tmp_path, "P = 'docs/core/READ_BY_TEST.md'\n")
    assert _classify(root, ["docs/core/READ_BY_TEST.md"]) == HEAVY
    assert _classify(root, ["docs/core/UNREAD.md"]) == LIGHT
    assert _classify(root, ["docs/core/UNREAD.md", "docs/core/READ_BY_TEST.md"]) == HEAVY


def test_nested_test_file_counts(tmp_path: Path):
    root = _layout(tmp_path, "")
    (root / "tests" / "sub").mkdir()
    (root / "tests" / "sub" / "helper.py").write_text("'docs/x.md'", encoding="utf-8")
    assert _classify(root, ["docs/x.md"]) == HEAVY


def test_scan_missing_tests_dir_raises(tmp_path: Path):
    with pytest.raises(OSError):
        classifier.scan_test_read_paths(tmp_path, ["docs/a.md"])


def test_scan_unreadable_test_file_raises(tmp_path: Path):
    root = _layout(tmp_path, "")
    (root / "tests" / "test_bad.py").write_bytes(b"\xff\xfe\x00bad")
    with pytest.raises(UnicodeDecodeError):
        classifier.scan_test_read_paths(root, ["docs/a.md"])


def test_main_prints_heavy_when_the_scan_raises(monkeypatch, capsys):
    def boom(root, paths):
        raise OSError("tests unreadable")

    monkeypatch.setattr(classifier, "scan_test_read_paths", boom)
    assert classifier.main(["docs/a.md"]) == 0
    captured = capsys.readouterr()
    assert captured.out.strip() == HEAVY
    assert "tests unreadable" in captured.err
