"""OI-2784: a path without a symbol provider forces a full round, loud in the record and in the prompt (golf schoon F2)."""
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "lib"))

import review_changekind as rc
import review_delta as rd

FEAT = "scripts/lib/feature.py"
NON_PY = [("web/a.ts", ".ts"), ("web/a.js", ".js"), ("scripts/run.sh", ".sh"), ("docs/a.md", ".md"),
          ("conf/a.yaml", ".yaml"), ("conf/a.json", ".json"), ("Makefile", "(no suffix)"), ("img/a.png", ".png")]
LOUD = "symbols not computed for this file type ({})"


def git(repo: Path, *args: str) -> str:
    ident = {f"GIT_{k}_{f}": v for k in ("AUTHOR", "COMMITTER") for f, v in (("NAME", "t"), ("EMAIL", "t@t"))}
    env = {**ident, "PATH": "/usr/bin:/bin:/opt/homebrew/bin", "HOME": str(repo)}
    return subprocess.run(["git", *args], cwd=repo, env=env, check=True, capture_output=True, text=True).stdout.strip()


def commit(repo: Path, files: dict, msg: str) -> str:
    for name, body in files.items():
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_bytes(body if isinstance(body, bytes) else body.encode())
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", msg)
    return git(repo, "rev-parse", "HEAD")


def body(path: str, n: int):
    return b"\x89PNG\r\n\x1a\n" + bytes([n]) if path.endswith(".png") else f"value = {n}\n"


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    git(tmp_path, "init", "-q", "-b", "main")
    commit(tmp_path, {"scripts/lib/lib.py": "def helper():\n    return 1\n", **{p: body(p, 0) for p, _ in NON_PY}}, "base")
    git(tmp_path, "checkout", "-q", "-b", "pr")
    return tmp_path


def prompt_line(path: str, kind: rc.ChangeKindRecord, symbols=frozenset()) -> str:
    """The interdiff line of ``path`` in the delta prompt, built on a record a delta may judge."""
    record = rd.DeltaInput(7, "codex_gate", "a" * 40, "fail", "rounds#1", "b" * 40, kind,
                           rc.Interdiff(frozenset({path}), {path: symbols}), (), ())
    text = rd.build_delta_prompt(record)
    return next(line for line in text.splitlines() if line.startswith(f"  - {path}:"))


@pytest.mark.parametrize("path,suffix", NON_PY)
def test_non_python_path_in_the_own_diff_and_interdiff_is_rework_no_symbol_provider(repo, path, suffix):
    parent = commit(repo, {path: body(path, 1)}, "edit")
    head = commit(repo, {path: body(path, 2)}, "fix")
    rec = rc.classify_change(repo, "main", parent, head, rc.seat_input_set(repo, head, {path}))
    assert (rec.changekind, rec.full_review_reason) == (rc.REWORK, rc.REASON_NO_SYMBOL_PROVIDER)
    assert rec.no_provider_paths == {path: suffix}
    with pytest.raises(rd.DeltaRefused) as refused:
        rd.build_delta_prompt(rd.DeltaInput(7, "codex_gate", parent, "fail", "r#1", head, rec,
                                            rc.interdiff(repo, "main", parent, head), (), ()))
    assert refused.value.full_review_reason == rc.REASON_NO_SYMBOL_PROVIDER
    line = prompt_line(path, rc.ChangeKindRecord(rc.REWORK, "p", "q", {}, "v1"))
    assert line == f"  - {path}: " + LOUD.format(suffix) and "no symbol-level change found" not in line


@pytest.mark.parametrize("path,suffix", NON_PY)
def test_non_python_path_in_the_base_move_never_yields_trivial_rebase(repo, path, suffix):
    parent = commit(repo, {FEAT: "def feature():\n    return 1\n"}, "feature")
    git(repo, "checkout", "-q", "main")
    commit(repo, {path: body(path, 3)}, "main moves")
    git(repo, "checkout", "-q", "--detach")
    git(repo, "cherry-pick", parent)
    head = git(repo, "rev-parse", "HEAD")
    reach = rc.seat_input_set(repo, head, {FEAT})
    assert path not in reach  # callers of a non-Python path are not known, so the input-set check alone would pass
    rec = rc.classify_change(repo, "main", parent, head, reach)
    assert (rec.changekind, rec.full_review_reason, rec.no_provider_paths) == (rc.REWORK, rc.REASON_NO_SYMBOL_PROVIDER,
                                                                               {path: suffix})


def test_python_only_keeps_its_behaviour(repo):
    parent = commit(repo, {FEAT: "def feature():\n    return 1\n"}, "feature")
    head = commit(repo, {FEAT: "def feature():\n    return 2\n"}, "fix")
    rec = rc.classify_change(repo, "main", parent, head, rc.seat_input_set(repo, head, {FEAT}))
    assert (rec.changekind, rec.full_review_reason, rec.no_provider_paths) == (rc.REWORK, None, {})
    diff = rc.interdiff(repo, "main", parent, head)
    assert diff.symbols == {FEAT: {("feature", "feature")}}
    assert prompt_line(FEAT, rec, diff.symbols[FEAT]) == f"  - {FEAT}: feature"
    assert prompt_line(FEAT, rec) == f"  - {FEAT}: no symbol-level change found"


@pytest.mark.parametrize("no_provider", [None, ["docs/a.md"], "docs/a.md"])
def test_decide_without_a_known_provider_set_cannot_prove(no_provider):
    assert rc.decide(False, True, True, [], [], {}, no_provider) == (rc.REWORK, rc.REASON_CANNOT_PROVE)
    assert rc.decide(False, True, True, [], [], {}) == (rc.REWORK, rc.REASON_CANNOT_PROVE)


def test_registry_is_the_one_place_that_names_a_provider():
    assert set(rc.SYMBOL_PROVIDERS) == {".py", rc.INERT_CHANGELOG}
    assert rc.no_provider_paths(["a.py", "b.ts", "Makefile"]) == {"b.ts": ".ts", "Makefile": "(no suffix)"} and rc.NO_SUFFIX == "(no suffix)"


FRAG = "changelog.d/x.md"
NOT_INERT = [("docs/x.md", ".md"), ("changelog.d/sub/x.md", ".md"), ("changelog.d/x.txt", ".txt"),
             ("x/changelog.d/y.md", ".md")]


def test_changelog_fragment_beside_python_keeps_the_python_changekind(repo):
    parent = commit(repo, {FEAT: "def feature():\n    return 1\n", FRAG: "- one\n"}, "feature")
    git(repo, "commit", "-q", "--allow-empty", "-m", "no code")
    head = git(repo, "rev-parse", "HEAD")
    rec = rc.classify_change(repo, "main", parent, head, rc.seat_input_set(repo, head, {FEAT, FRAG}))
    assert (rec.changekind, rec.full_review_reason, rec.no_provider_paths) == (rc.NO_CODE_CHANGE, None, {})
    fix = commit(repo, {FEAT: "def feature():\n    return 2\n", FRAG: "- two\n"}, "fix")
    rec = rc.classify_change(repo, "main", head, fix, rc.seat_input_set(repo, fix, {FEAT, FRAG}))
    assert (rec.changekind, rec.full_review_reason, rec.no_provider_paths) == (rc.REWORK, None, {})


def test_pure_changelog_fragment_change_is_not_no_symbol_provider(repo):
    parent = commit(repo, {FRAG: "- one\n"}, "fragment")
    head = commit(repo, {FRAG: "- two\n"}, "fragment again")
    rec = rc.classify_change(repo, "main", parent, head, rc.seat_input_set(repo, head, {FRAG}))
    assert (rec.changekind, rec.full_review_reason, rec.no_provider_paths) == (rc.REWORK, None, {})


def test_prompt_line_of_a_changelog_fragment_says_it_is_inert():
    line = prompt_line(FRAG, rc.ChangeKindRecord(rc.REWORK, "p", "q", {}, "v1"))
    assert line == f"  - {FRAG}: changelog fragment (no callers)"


@pytest.mark.parametrize("path,suffix", NOT_INERT)
def test_only_a_md_directly_under_root_changelog_d_is_inert(repo, path, suffix):
    parent = commit(repo, {path: "one\n"}, "edit")
    head = commit(repo, {path: "two\n"}, "fix")
    rec = rc.classify_change(repo, "main", parent, head, rc.seat_input_set(repo, head, {path}))
    assert (rec.changekind, rec.full_review_reason, rec.no_provider_paths) == (rc.REWORK, rc.REASON_NO_SYMBOL_PROVIDER,
                                                                               {path: suffix})
    assert not rc.is_inert(path)


def test_changelog_fragment_in_the_base_move_never_touches_the_input_set(repo):
    assert rc.decide(False, True, True, [FRAG], [FRAG], {}, {}) == (rc.TRIVIAL_REBASE, None)
    assert rc.decide(False, True, True, [FRAG, FEAT], [FRAG, FEAT], {}, {}) == (rc.REWORK, rc.REASON_INPUT_SET)
    parent = commit(repo, {FEAT: "def feature():\n    return 1\n"}, "feature")
    git(repo, "checkout", "-q", "main")
    commit(repo, {"changelog.d/other.md": "- other\n"}, "main moves")
    git(repo, "checkout", "-q", "--detach")
    git(repo, "cherry-pick", parent)
    head = git(repo, "rev-parse", "HEAD")
    rec = rc.classify_change(repo, "main", parent, head, rc.seat_input_set(repo, head, {FEAT}))
    assert (rec.changekind, rec.full_review_reason, rec.no_provider_paths) == (rc.TRIVIAL_REBASE, None, {})
