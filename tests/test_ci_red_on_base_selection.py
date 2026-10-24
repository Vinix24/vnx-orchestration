"""Tests for the ``--tests-only`` selection of scripts/ci/classify_ci_profile.py.

Track deterministische-testverificatie PR-1: a PR's test selection is the pytest
files it adds or changes.  Pinned here: the exact JSON of the real CLI (sorted,
unique, conftests and deletions under their own keys); an excluded-but-touched
file staying in ``selected`` *and* landing in ``excluded_touched`` (Profile A
ignores it, so red-on-base must still run it); ``deleted`` from the checkout;
fail closed on anything that cannot be read (exit 2, no JSON — never a smaller
selection); and no change to the profile classification, compared against
origin/main.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import types
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
_CI_DIR = REPO_ROOT / "scripts" / "ci"
if str(_CI_DIR) not in sys.path:
    sys.path.insert(0, str(_CI_DIR))

import classify_ci_profile as selector  # noqa: E402

SCRIPT = _CI_DIR / "classify_ci_profile.py"
KEYS = ["selected", "excluded_touched", "deleted", "conftest_changed"]
EMPTY = {key: [] for key in KEYS}

# The prelude's changed-paths file (git diff --name-only), deliberately unsorted with a
# duplicate and a blank line.  tests/test_removed.py is absent from disk: that is a
# deleted file in the head checkout.
CHANGED_PATHS = """\
tests/test_changed.py
tests/test_added.py

tests/test_changed.py
tests/test_excluded.py
tests/test_nodeid.py
tests/helper_utils.py
tests/conftest.py
tests/test_removed.py
conftest.py
scripts/tool.py
docs/ci.md
"""

# Every entry shape Profile A's parser accepts; the comment names a file that must not be excluded.
EXCLUSIONS = """\
# whole-line comment: tests/test_commented.py is not an entry
tests/test_excluded.py  # OI-1: red solo on main
tests/test_nodeid.py::TestNodeid::test_one  # OI-2: only this node is red
"""

EXPECTED = {
    "selected": ["tests/test_added.py", "tests/test_changed.py",
                 "tests/test_excluded.py", "tests/test_nodeid.py"],
    "excluded_touched": ["tests/test_excluded.py", "tests/test_nodeid.py"],
    "deleted": ["tests/test_removed.py"],
    "conftest_changed": ["tests/conftest.py"],
}


@pytest.fixture
def tree(tmp_path: Path) -> types.SimpleNamespace:
    """A head checkout with every changed file that still exists, plus both input files."""
    root = tmp_path / "repo"
    for rel in CHANGED_PATHS.splitlines():
        if rel.startswith("tests/") and not rel.endswith("test_removed.py"):
            (root / rel).parent.mkdir(parents=True, exist_ok=True)
            (root / rel).write_text("def test_placeholder():\n    pass\n", encoding="utf-8")
    (root / "changed-paths.txt").write_text(CHANGED_PATHS, encoding="utf-8")
    (root / "exclusions.txt").write_text(EXCLUSIONS, encoding="utf-8")
    return types.SimpleNamespace(root=root, paths=root / "changed-paths.txt", exclusions=root / "exclusions.txt")


def _run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True)


def _cli(tree: types.SimpleNamespace) -> dict:
    result = _run("--tests-only", "--paths-file", str(tree.paths),
                  "--exclusions", str(tree.exclusions), "--root", str(tree.root))
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_cli_prints_the_exact_selection(tree):
    result = _run("--tests-only", "--paths-file", str(tree.paths),
                  "--exclusions", str(tree.exclusions), "--root", str(tree.root))
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload == EXPECTED
    assert list(payload) == KEYS
    assert result.stdout.endswith("\n")


def test_excluded_but_touched_file_is_selected_and_marked(tree):
    payload = _cli(tree)
    for path in ("tests/test_excluded.py", "tests/test_nodeid.py"):
        assert path in payload["selected"], f"{path} must still be selected to run"
        assert path in payload["excluded_touched"]


def test_profile_output_is_unchanged_against_origin_main(tree, tmp_path):
    old = tmp_path / "old" / "scripts"
    shutil.copytree(_CI_DIR, old / "ci")
    shutil.copytree(_CI_DIR.parent / "release", old / "release")
    old_script = old / "ci" / "classify_ci_profile.py"
    old_script.write_text(subprocess.run(
        ["git", "-C", str(REPO_ROOT), "show", "origin/main:scripts/ci/classify_ci_profile.py"],
        capture_output=True, text=True, check=True).stdout, encoding="utf-8")
    docs_only = tree.root / "docs-only.txt"
    docs_only.write_text("docs/a.md\nchangelog.d/x.md\n", encoding="utf-8")
    for paths_file in (tree.paths, docs_only):
        before = subprocess.run([sys.executable, str(old_script), "--paths-file", str(paths_file)],
                                capture_output=True, text=True)
        after = _run("--paths-file", str(paths_file))
        assert before.returncode == after.returncode == 0, after.stderr
        assert after.stdout == before.stdout
        assert after.stdout.strip() in ("light", "heavy")


def test_a_root_that_is_not_a_directory_is_refused():
    # A root that is not there would report every listed test as deleted: "nothing to run".
    with pytest.raises(selector.MalformedPathsError, match="not a directory"):
        selector.select_test_paths(["tests/test_a.py"], [], root=Path("/nonexistent-checkout"))


def test_empty_paths_file_is_an_empty_selection(tree, tmp_path):
    (tmp_path / "empty.txt").write_text("\n\n", encoding="utf-8")
    result = _run("--tests-only", "--paths-file", str(tmp_path / "empty.txt"),
                  "--exclusions", str(tree.exclusions), "--root", str(tree.root))
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == EMPTY


TRICKY_EXCLUSIONS = (
    "# header comment\n\n"
    "tests/test_a.py  # OI-1: red solo\n"
    "   # indented comment naming tests/test_not_excluded.py\n"
    "tests/test_b.py\t# OI-2: tab before the reason\n"
    "   \n"
    "tests/test_c.py   \n"
    "tests/test_d.py::TestD::test_one  # OI-3: nodeid entry\r\n"
    "\ttests/test_e.py # OI-4: leading tab\n"
    "tests/test_f.py # OI-5: a reason with # inside\n"
)


def test_exclusion_entries_follow_the_consumer_format():
    assert selector.read_exclusion_entries(TRICKY_EXCLUSIONS) == [
        "tests/test_a.py", "tests/test_b.py", "tests/test_c.py",
        "tests/test_d.py", "tests/test_e.py", "tests/test_f.py"]


UNREADABLE = [
    None,                             # the field is missing
    42,                               # not text at all
    "/tests/test_abs.py",             # absolute
    "tests/../tests/test_escape.py",  # escapes tests/
    "tests//test_empty_part.py",      # '//'
    "tests\\test_win.py",             # backslash
    '"tests/test_quoted.py"',         # git's C-quoted form
    "tests/test_a.py\tduplicate",     # a tab inside a path line
    "D\ttests/test_gone.py",          # a --name-status row, not --name-only
]


@pytest.mark.parametrize("entry", UNREADABLE)
def test_unreadable_entry_refuses_the_selection(entry):
    with pytest.raises(selector.MalformedPathsError):
        selector.select_test_paths([entry], [], root=Path("/nonexistent-checkout"))


def test_missing_inputs_are_refused_not_read_as_empty():
    # None paths, one flat string and no exclusions list are not "nothing
    # changed" / "nothing excluded": each is refused.
    for args in ((None, []), ("tests/test_a.py", []), ([], None)):
        with pytest.raises(selector.MalformedPathsError):
            selector.select_test_paths(*args)


def test_cli_exits_2_without_json_when_it_cannot_read_its_input(tree, capsys):
    missing = str(tree.root / "nope.txt")
    broken = tree.root / "broken.txt"
    broken.write_text("tests/test_added.py\ntests/../escape.py\n", encoding="utf-8")
    cases = [
        ["--paths-file", missing],
        ["--paths-file", str(tree.paths), "--exclusions", missing],
        ["--paths-file", str(broken), "--exclusions", str(tree.exclusions)],
        ["--paths-file", str(tree.paths), "--exclusions"],
        ["--paths-file", str(tree.paths), "--root"],
        ["--paths-file", str(tree.paths), "--exclusions", "a", "--exclusions", "b"],
    ]
    for argv in cases:
        assert selector.main(["--tests-only", *argv]) == 2, argv
        captured = capsys.readouterr()
        assert captured.out == "", argv
        assert captured.err.startswith("classify_ci_profile --tests-only:"), argv
