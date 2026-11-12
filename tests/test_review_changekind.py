"""DR-2: changekind of a follow-up head, on real temporary git repos (plan "Klaar per levering" 3)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts" / "lib"))

import review_changekind as rc  # noqa: E402
from smart_router import plan_scope_rule_version  # noqa: E402


def git(repo: Path, *args: str) -> str:
    ident = {f"GIT_{k}_{f}": v for k in ("AUTHOR", "COMMITTER") for f, v in (("NAME", "t"), ("EMAIL", "t@t"))}
    env = {**ident, "PATH": "/usr/bin:/bin:/opt/homebrew/bin", "HOME": str(repo)}
    return subprocess.run(["git", *args], cwd=repo, env=env, check=True, capture_output=True, text=True).stdout.strip()


def commit(repo: Path, files: dict, msg: str) -> str:
    for name, text in files.items():
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_text(text)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", msg)
    return git(repo, "rev-parse", "HEAD")


LIB = "def helper():\n    return 1\n\n\ndef other():\n    return 2\n"
USER = "from lib import helper\n\n\ndef use():\n    return helper()\n"
FEATURE = "def feature():\n    return 'a'\n"


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    git(tmp_path, "init", "-q", "-b", "main")
    commit(tmp_path, {"scripts/lib/lib.py": LIB, "scripts/lib/user.py": USER, "scripts/lib/unrelated.py": "X = 1\n"}, "base")
    git(tmp_path, "checkout", "-q", "-b", "pr")
    return tmp_path


def parent_on_pr(repo: Path) -> str:
    return commit(repo, {"scripts/lib/feature.py": FEATURE}, "feature")


def rebase_onto_new_main(repo: Path, main_files: dict) -> str:
    git(repo, "checkout", "-q", "main")
    commit(repo, main_files, "main moves")
    git(repo, "checkout", "-q", "pr")
    git(repo, "rebase", "-q", "main")
    return git(repo, "rev-parse", "HEAD")


def test_rebase_without_content_change_is_trivial_rebase(repo):
    parent = parent_on_pr(repo)
    head = rebase_onto_new_main(repo, {"scripts/lib/unrelated.py": "X = 2\n"})
    rec = rc.classify_change(repo, "main", parent, head, {"scripts/lib/feature.py"})
    assert rec.changekind == rc.TRIVIAL_REBASE and rec.full_review_reason is None
    assert rec.parent_patch_id == rec.head_patch_id != rc.EMPTY_PATCH_ID
    assert rc.patch_id(repo, "main", "main") == rc.EMPTY_PATCH_ID


def test_rebase_that_moves_a_file_of_the_input_set_is_rework(repo):
    parent = parent_on_pr(repo)
    head = rebase_onto_new_main(repo, {"scripts/lib/user.py": USER + "\nY = 1\n"})
    rec = rc.classify_change(repo, "main", parent, head, {"scripts/lib/feature.py", "scripts/lib/user.py"})
    assert (rec.changekind, rec.full_review_reason) == (rc.REWORK, rc.REASON_INPUT_SET)


def test_changed_line_in_own_diff_is_rework_and_reports_symbols(repo):
    parent = parent_on_pr(repo)
    head = commit(repo, {"scripts/lib/feature.py": FEATURE.replace("'a'", "'b'")}, "fix")
    rec = rc.classify_change(repo, "main", parent, head, set())
    assert (rec.changekind, rec.full_review_reason) == (rc.REWORK, None)
    assert rec.parent_patch_id != rec.head_patch_id
    inter = rc.interdiff(repo, "main", parent, head)
    assert inter.paths == {"scripts/lib/feature.py"}
    assert inter.symbols["scripts/lib/feature.py"] == {("feature", "feature")}


def test_whitespace_only_change_in_a_string_is_rework_with_the_function_named(repo):
    parent = commit(repo, {"scripts/lib/feature.py": "def feature():\n    return 'a b'\n"}, "feature")
    head = commit(repo, {"scripts/lib/feature.py": "def feature():\n    return 'ab'\n"}, "squash space")
    rec = rc.classify_change(repo, "main", parent, head, set())
    assert rec.changekind == rc.REWORK and rec.parent_patch_id != rec.head_patch_id
    inter = rc.interdiff(repo, "main", parent, head)
    assert inter.symbols == {"scripts/lib/feature.py": {("feature", "feature")}}


def test_decorator_only_change_marks_the_function_as_changed(repo):
    src = "@{}\ndef feature():\n    return 1\n\n\ndef other():\n    return 2\n"
    parent = commit(repo, {"scripts/lib/feature.py": src.format("allow")}, "feature")
    head = commit(repo, {"scripts/lib/feature.py": src.format("deny")}, "swap decorator")
    inter = rc.interdiff(repo, "main", parent, head)
    assert inter.symbols["scripts/lib/feature.py"] == {("feature", "feature")}


def test_interdiff_on_security_path_names_path_kind_and_rule_version(repo):
    parent = parent_on_pr(repo)
    head = commit(repo, {".github/workflows/ci.yml": "name: ci\n"}, "workflow")
    rec = rc.classify_change(repo, "main", parent, head, set())
    assert (rec.changekind, rec.full_review_reason) == (rc.REWORK, rc.REASON_SECURITY_PATH)
    assert rec.security_paths == {".github/workflows/ci.yml": "ci-workflows"}
    assert rec.plan_scope_rule_version == plan_scope_rule_version()


def test_merge_commit_only_change_is_no_code_change(repo):
    parent = parent_on_pr(repo)
    head = git(repo, "commit-tree", f"{parent}^{{tree}}", "-p", parent, "-p", git(repo, "rev-parse", "main"), "-m", "merge")
    rec = rc.classify_change(repo, "main", parent, head, {"scripts/lib/feature.py"})
    assert (rec.changekind, rec.full_review_reason) == (rc.NO_CODE_CHANGE, None)


def test_seat_input_set_adds_importers_of_a_changed_module(repo):
    got = rc.seat_input_set(repo, "main", {"scripts/lib/lib.py"})
    assert got == {"scripts/lib/lib.py", "scripts/lib/user.py"}


def test_seat_input_set_fails_when_an_import_edge_is_unknown(repo):
    commit(repo, {"scripts/lib/broken.py": "def (:\n"}, "broken")
    with pytest.raises(rc.ChangeKindError):
        rc.seat_input_set(repo, "pr", {"scripts/lib/lib.py"})


def test_git_failure_is_rework_cannot_prove_never_trivial_rebase(repo):
    parent = parent_on_pr(repo)
    rec = rc.classify_change(repo, "main", parent, "0" * 40, set())
    assert (rec.changekind, rec.full_review_reason) == (rc.REWORK, rc.REASON_CANNOT_PROVE)


@pytest.mark.parametrize("pids,equal,moved,kind", [
    (("a", "a"), True, [], rc.NO_CODE_CHANGE),
    (("a", "a"), False, ["x.py"], rc.TRIVIAL_REBASE),
    (("a", "b"), False, [], rc.REWORK),
])
def test_decide_pure(pids, equal, moved, kind):
    assert rc.decide(*pids, equal, moved, {"in.py"}, {})[0] == kind


def test_vanished_function_is_unverifiable_and_forces_full_review(repo):
    head = commit(repo, {"scripts/lib/lib.py": "def other():\n    return 2\n"}, "drop helper")
    found = [{"file_path": "scripts/lib/lib.py", "symbolic_context": "other"},
             {"file_path": "scripts/lib/lib.py", "symbolic_context": "helper"},
             {"file_path": "scripts/lib/gone.py", "symbolic_context": ""}]
    out = rc.anchors_present(repo, head, found)
    assert [a["status"] for a in out["anchors"]] == ["present", "unverifiable", "unverifiable"]
    assert out["full_review_reason"] == rc.REASON_ANCHOR
    assert rc.anchors_present(repo, head, found[:1])["full_review_reason"] is None
