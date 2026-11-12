"""DR-2: changekind of a follow-up head, on real temporary git repos (plan "Klaar per levering" 3)."""

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
FEAT, APP, UNRELATED = "scripts/lib/feature.py", "scripts/lib/app.py", "scripts/lib/unrelated.py"
FEATURE = "def feature():\n    return 'a'\n"
BLOCK = "".join(f"    x{i} = {i}\n" for i in range(10)) + "    return x5\n"
APP_SRC = "def first():\n" + BLOCK + "\n\ndef second():\n" + BLOCK
CUT, EDIT = APP_SRC.index("def second"), ("x5 = 5", "x5 = 50")  # the same one-line edit, in first() or in second()
IN_FIRST, IN_SECOND = APP_SRC[:CUT].replace(*EDIT) + APP_SRC[CUT:], APP_SRC[:CUT] + APP_SRC[CUT:].replace(*EDIT)
OID, CANNOT = "a" * 40, (rc.REWORK, rc.REASON_CANNOT_PROVE)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    git(tmp_path, "init", "-q", "-b", "main")
    commit(tmp_path, {"scripts/lib/lib.py": LIB, "scripts/lib/user.py": USER, UNRELATED: "X = 1\n", APP: APP_SRC}, "base")
    git(tmp_path, "checkout", "-q", "-b", "pr")
    return tmp_path


def move_main(repo: Path, main_files: dict, pick: str = "") -> str:
    """Main moves; HEAD is left detached on the new main, with ``pick`` cherry-picked (a rebase of one commit)."""
    git(repo, "checkout", "-q", "main")
    commit(repo, main_files, "main moves")
    git(repo, "checkout", "-q", "--detach")
    if pick:
        git(repo, "cherry-pick", pick)
    return git(repo, "rev-parse", "HEAD")


@pytest.mark.parametrize("main_files,want", [({UNRELATED: "X = 2\n"}, (rc.TRIVIAL_REBASE, None)),
                                              ({"scripts/lib/üser.py": "Y = 1\n"}, (rc.REWORK, rc.REASON_INPUT_SET))])  # unquoted path
def test_clean_rebase_is_trivial_rebase_unless_the_base_move_touches_the_input_set(repo, main_files, want):
    parent = commit(repo, {FEAT: FEATURE}, "feature")
    head = move_main(repo, main_files, pick=parent)
    rec = rc.classify_change(repo, "main", parent, head, {FEAT, "scripts/lib/üser.py"})
    assert (rec.changekind, rec.full_review_reason) == want
    assert rec.parent_patch_id == rec.head_patch_id != rc.EMPTY_PATCH_ID == rc.patch_id(repo, "main", "main")
    assert rc.interdiff(repo, "main", parent, head).paths == frozenset()


@pytest.mark.parametrize("base_moves,reason", [(False, None), (True, rc.REASON_REBASE_REWORK)])
def test_relocated_edit_is_rework_and_interdiff_names_both_functions(repo, base_moves, reason):
    parent = commit(repo, {APP: IN_FIRST}, "edit first")
    if base_moves:
        move_main(repo, {UNRELATED: "X = 2\n"})
    head = commit(repo, {APP: IN_SECOND}, "the same edit, in second")
    rec = rc.classify_change(repo, "main", parent, head, {APP})
    assert (rec.changekind, rec.full_review_reason) == (rc.REWORK, reason)
    assert rec.parent_patch_id == rec.head_patch_id  # one identity of the diff, two different codes
    assert rc.interdiff(repo, "main", parent, head).symbols == {APP: {("app", "first"), ("app", "second")}}


@pytest.mark.parametrize("old,new", [
    ("def feature():\n    return 'a b'\n", "def feature():\n    return 'ab'\n"),  # whitespace inside a string
    ("@allow\ndef feature():\n    return 1\n", "@deny\ndef feature():\n    return 1\n"),  # decorator only
])
def test_whitespace_or_decorator_change_is_rework_with_the_function_named(repo, old, new):
    parent = commit(repo, {FEAT: old}, "feature")
    head = commit(repo, {FEAT: new}, "change")
    rec = rc.classify_change(repo, "main", parent, head, set())
    assert (rec.changekind, rec.full_review_reason) == (rc.REWORK, None)
    assert rc.interdiff(repo, "main", parent, head).symbols == {FEAT: {("feature", "feature")}}


def test_replay_with_a_conflict_is_rework_and_names_the_conflicted_path(repo):
    parent = commit(repo, {UNRELATED: "X = 2\n"}, "parent edit")
    move_main(repo, {UNRELATED: "X = 3\n"})
    head = commit(repo, {UNRELATED: "X = 2\n"}, "conflict resolved to the parent's side")
    rec = rc.classify_change(repo, "main", parent, head, set())
    assert (rec.changekind, rec.full_review_reason) == (rc.REWORK, rc.REASON_REBASE_REWORK)
    assert rc.interdiff(repo, "main", parent, head).paths == {UNRELATED}


@pytest.mark.parametrize("cmd,result", [("merge-tree", r) for r in (
    "", "not-a-tree\0", f"{OID}\0{UNRELATED}\0", (None, ""), (128, ""), (1, f"{OID}\0"), (1, f"bad\0{UNRELATED}\0")
)] + [("patch-id", (128, "")), ("merge-base", "\n"), ("rev-parse", "not-a-tree\n")])
def test_git_without_positive_evidence_is_rework_cannot_prove(repo, cmd, result):
    parent = commit(repo, {FEAT: FEATURE}, "feature")
    head = move_main(repo, {UNRELATED: "X = 2\n"}, pick=parent)

    def run(r, args, stdin=None):
        if args[0] == cmd and isinstance(result, tuple):
            raise rc.ChangeKindError(cmd, *result)
        return result if args[0] == cmd else rc._run_git(r, args, stdin)
    rec = rc.classify_change(repo, "main", parent, head, {FEAT}, run=run)
    assert (rec.changekind, rec.full_review_reason) == CANNOT


def test_interdiff_on_security_path_names_path_kind_and_rule_version(repo):
    parent = commit(repo, {FEAT: FEATURE}, "feature")
    head = commit(repo, {".github/workflows/cî.yml": "name: ci\n"}, "workflow")
    rec = rc.classify_change(repo, "main", parent, head, set())
    assert (rec.changekind, rec.full_review_reason) == (rc.REWORK, rc.REASON_SECURITY_PATH)
    assert rec.security_paths == {".github/workflows/cî.yml": "ci-workflows"}  # a non-ASCII path, unquoted
    assert rec.plan_scope_rule_version == plan_scope_rule_version()


def test_identical_head_after_a_no_op_merge_commit_is_no_code_change(repo):
    parent = commit(repo, {FEAT: FEATURE}, "feature")
    head = git(repo, "commit-tree", f"{parent}^{{tree}}", "-p", parent, "-p", git(repo, "rev-parse", "main"), "-m", "merge")
    rec = rc.classify_change(repo, "main", parent, head, {FEAT})
    assert (rec.changekind, rec.full_review_reason) == (rc.NO_CODE_CHANGE, None)


def test_seat_input_set_adds_importers_and_fails_on_an_unknown_import_edge(repo):
    assert rc.seat_input_set(repo, "main", {"scripts/lib/lib.py"}) == {"scripts/lib/lib.py", "scripts/lib/user.py"}
    commit(repo, {"scripts/lib/broken.py": "def (:\n"}, "broken")
    with pytest.raises(rc.ChangeKindError):
        rc.seat_input_set(repo, "pr", {"scripts/lib/lib.py"})


@pytest.mark.parametrize("facts,security,want", [
    ((True, True, True), {}, (rc.NO_CODE_CHANGE, None)), ((True, False, True), {}, (rc.REWORK, None)),
    ((False, True, True), {}, (rc.TRIVIAL_REBASE, None)), ((False, False, True), {}, (rc.REWORK, rc.REASON_REBASE_REWORK)),
    ((False, True, False), {}, (rc.REWORK, rc.REASON_REBASE_REWORK)),  # tree equal, yet not merged cleanly
    ((True, True, True), {"ci.yml": "ci-workflows"}, (rc.REWORK, rc.REASON_SECURITY_PATH)),
    ((True, None, True), {}, CANNOT), ((None, True, True), {}, CANNOT), ((True, 1, True), {}, CANNOT),
    ((False, True, "yes"), {}, CANNOT), ((True, True, True), None, CANNOT),
])
def test_decide_pure_and_fail_closed_on_unknown_facts(facts, security, want):
    assert rc.decide(*facts, ["x.py"], {"in.py"}, security) == want


def test_vanished_function_is_unverifiable_and_forces_full_review(repo):
    head = commit(repo, {"scripts/lib/lib.py": "def other():\n    return 2\n"}, "drop helper")
    found = [{"file_path": "scripts/lib/lib.py", "symbolic_context": "other"},
             {"file_path": "scripts/lib/lib.py", "symbolic_context": "helper"},
             {"file_path": "scripts/lib/gone.py", "symbolic_context": ""}]
    out = rc.anchors_present(repo, head, found)
    assert [a["status"] for a in out["anchors"]] == ["present", "unverifiable", "unverifiable"]
    assert out["full_review_reason"] == rc.REASON_ANCHOR
    assert rc.anchors_present(repo, head, found[:1])["full_review_reason"] is None


def test_a_clean_rebase_onto_a_base_that_added_a_security_path_is_rework_and_names_it(repo):
    parent = commit(repo, {FEAT: FEATURE}, "feature")
    head = move_main(repo, {".github/workflows/ci.yml": "name: ci\n"}, pick=parent)
    rec = rc.classify_change(repo, "main", parent, head, {FEAT})
    assert (rec.changekind, rec.full_review_reason) == (rc.REWORK, rc.REASON_SECURITY_PATH)
    assert list(rec.security_paths) == [".github/workflows/ci.yml"]
    assert rec.security_paths[".github/workflows/ci.yml"] == rc.security_path_kind(".github/workflows/ci.yml")


def test_a_clean_rebase_onto_a_base_that_touched_an_ordinary_file_stays_trivial(repo):
    parent = commit(repo, {FEAT: FEATURE}, "feature")
    head = move_main(repo, {UNRELATED: "X = 3\n"}, pick=parent)
    rec = rc.classify_change(repo, "main", parent, head, {FEAT})
    assert (rec.changekind, rec.full_review_reason, dict(rec.security_paths)) == (rc.TRIVIAL_REBASE, None, {})


@pytest.mark.parametrize("name", ["a b.py", "üser.py"])
def test_seat_input_set_keeps_an_importer_whose_path_has_a_space_or_a_quoted_character(repo, name):
    path = f"scripts/lib/{name}"
    commit(repo, {path: USER}, "importer with an awkward name")
    assert rc.seat_input_set(repo, "pr", {"scripts/lib/lib.py"}) == {"scripts/lib/lib.py", "scripts/lib/user.py", path}
