"""blast-radius-en-afhankelijkheden B3: the pure blast-radius core (ADR-096 decisions 5-8, 10)."""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts" / "lib"))

import blast_radius as br  # noqa: E402
import config_registry as cr  # noqa: E402

GENERIC = ("tests", "changelog.d", "docs")


@pytest.fixture
def repo(tmp_path):
    return tmp_path


def S(did, paths, branch=None, preds=()):
    return br.Subject(did, paths, branch, tuple(preds))


# --- normalisation -------------------------------------------------------------------------
def test_absolute_inside_becomes_relative(repo):
    assert br.normalise([str(repo / "scripts/lib/a.py")], repo) == ("scripts/lib/a.py",)


def test_dot_segments_normalise(repo):
    assert br.normalise(["./scripts//lib/a.py"], repo) == ("scripts/lib/a.py",)


def test_trailing_slash_means_directory(repo):
    assert br.normalise(["scripts/lib/"], repo) == ("scripts/lib/",)
    assert br.normalise([str(repo / "scripts") + "/"], repo) == ("scripts/",)


@pytest.mark.parametrize("bad", ["../x.py", "a/../../x.py", "scripts/../../x", "/etc/passwd",
                                 "", "  ", ".", None])
def test_invalid_is_refused(repo, bad):
    with pytest.raises(br.ScopeInvalid, match="scope_invalid"):
        br.normalise([bad], repo)


def test_scope_invalid_names_the_path(repo):
    with pytest.raises(br.ScopeInvalid, match=r"\.\./x\.py"):
        br.normalise(["ok.py", "../x.py"], repo)


def test_sibling_prefix_outside_repo_is_refused(repo):
    with pytest.raises(br.ScopeInvalid):
        br.normalise([str(repo) + "-other/x.py"], repo)


# --- generic dirs and incomplete scope -----------------------------------------------------
def test_whole_generic_dir_is_dropped_file_inside_counts(repo):
    sc = br.scope_of(["tests/", "docs/", "tests/test_x.py"], repo, GENERIC)
    assert sc.paths == ("tests/test_x.py",) and not sc.incomplete


@pytest.mark.parametrize("paths", [[], None, ["tests/"], ["tests/", "changelog.d/", "docs/"]])
def test_incomplete_scope(repo, paths):
    assert br.scope_of(paths, repo, GENERIC).incomplete is True


def test_a_generic_name_without_slash_is_a_file_and_counts(repo):
    assert br.scope_of(["docs"], repo, GENERIC).paths == ("docs",)


# --- overlap -------------------------------------------------------------------------------
def test_file_file_overlap():
    assert br.overlaps(["a/b.py", "c.py"], ["a/b.py"]) == ("a/b.py",)
    assert br.overlaps(["a/b.py"], ["a/c.py"]) == ()


def test_dir_contains_file_both_directions():
    assert br.overlaps(["scripts/lib/"], ["scripts/lib/x.py"]) == ("scripts/lib/",)
    assert br.overlaps(["scripts/lib/x.py"], ["scripts/lib/"]) == ("scripts/lib/x.py",)


def test_directory_prefix_is_not_containment():
    assert br.overlaps(["scripts/lib/"], ["scripts/library/x.py"]) == ()
    assert br.overlaps(["scripts/lib"], ["scripts/lib/x.py"]) == ()


# --- check ---------------------------------------------------------------------------------
def run(cand, others, repo):
    return {r.other: r for r in br.check(cand, others, repo, GENERIC)}


def test_overlap_clear_and_unknown(repo):
    cand = S("dlv-c", ["scripts/a.py"])
    res = run(cand, [S("dlv-1", ["scripts/a.py"]), S("dlv-2", ["scripts/b.py"]),
                     S("dlv-3", None), S("dlv-4", ["tests/"]), S("dlv-5", ["../x"])], repo)
    assert (res["dlv-1"].outcome, res["dlv-1"].paths) == (br.OVERLAP, ("scripts/a.py",))
    assert res["dlv-2"].outcome == br.CLEAR
    assert (res["dlv-3"].outcome, res["dlv-3"].reason) == (br.UNKNOWN, br.REASON_OTHER_UNREADABLE)
    assert (res["dlv-4"].outcome, res["dlv-4"].reason) == (br.UNKNOWN, br.REASON_OTHER_SCOPE_INCOMPLETE)
    assert (res["dlv-5"].outcome, res["dlv-5"].reason) == (br.UNKNOWN, br.REASON_OTHER_UNREADABLE)


@pytest.mark.parametrize("paths", [[], ["tests/"]])
def test_incomplete_candidate_is_unknown_never_clear(repo, paths):
    res = run(S("dlv-c", paths), [S("dlv-1", ["scripts/a.py"])], repo)
    assert (res["dlv-1"].outcome, res["dlv-1"].reason) == (br.UNKNOWN, br.REASON_SCOPE_INCOMPLETE)


def test_invalid_candidate_scope_raises(repo):
    with pytest.raises(br.ScopeInvalid):
        br.check(S("dlv-c", ["../x"]), [S("dlv-1", ["a"])], repo, GENERIC)


def test_self_exclusion_by_id(repo):
    assert run(S("dlv-c", ["a.py"]), [S("dlv-c", ["a.py"])], repo) == {}


def test_self_exclusion_by_round_on_the_same_branch(repo):
    cand = S("dlv-c", ["a.py"])
    assert run(cand, [S("", ["a.py"], branch="dispatch/dlv-c-r2")], repo) == {}
    assert run(cand, [S("dlv-c-r3", ["a.py"], branch="dispatch/dlv-c-r3")], repo) == {}


def test_self_exclusion_by_pr_branch(repo):
    cand = S("dlv-c", ["a.py"], branch="dispatch/dlv-c")
    assert run(cand, [S("", ["a.py"], branch="dispatch/dlv-c")], repo) == {}


def test_similar_branch_is_not_self(repo):
    cand = S("dlv-c", ["a.py"])
    res = run(cand, [S("dlv-c9", ["a.py"], branch="dispatch/dlv-c9"),
                     S("dlv-cx", ["a.py"], branch="dispatch/dlv-c-rx")], repo)
    assert set(res) == {"dlv-c9", "dlv-cx"} and all(r.outcome == br.OVERLAP for r in res.values())


@pytest.mark.parametrize("other", [S("dlv-p", ["a.py"]), S("", ["a.py"], branch="dispatch/dlv-p"),
                                   S("", ["a.py"], branch="dispatch/dlv-p-r2")])
def test_declared_predecessor_is_excluded(repo, other):
    assert run(S("dlv-c", ["a.py"], preds=["dlv-p"]), [other], repo) == {}


def test_undeclared_other_still_overlaps(repo):
    res = run(S("dlv-c", ["a.py"], preds=["dlv-p"]), [S("dlv-q", ["a.py"])], repo)
    assert res["dlv-q"].outcome == br.OVERLAP


# --- registry key via config_runtime -------------------------------------------------------
def test_registry_row_shape():
    e = cr.CONFIG_REGISTRY[br.GENERIC_DIRS_KEY]
    assert (e.type, e.default, e.scope) == ("string", "tests,changelog.d,docs", "project")


@pytest.fixture
def clean_config(monkeypatch, tmp_path):
    monkeypatch.setenv("VNX_GLOBAL_CONFIG_FILE", str(tmp_path / "none.json"))
    for k in (br.GENERIC_DIRS_KEY, "VNX_OVERRIDE_BLAST_RADIUS_GENERIC_DIRS"):
        monkeypatch.delenv(k, raising=False)
    cr.set_db_resolver(None)
    yield
    cr.set_db_resolver(None)


def test_default_read_through_config_runtime(clean_config):
    assert br.generic_dirs() == GENERIC


def test_stored_value_overrides_the_default(clean_config, monkeypatch):
    monkeypatch.setattr(cr, "_default_project_id", "proj-a")
    monkeypatch.setattr("config_runtime.autowire", lambda *a, **k: True)
    cr.set_db_resolver(lambda project_id, key: " vendor/ , tests " if key == br.GENERIC_DIRS_KEY else None)
    assert br.generic_dirs() == ("vendor", "tests")


def test_unresolved_value_is_a_failure_not_an_empty_list():
    with pytest.raises(ValueError, match=br.GENERIC_DIRS_KEY):
        br.generic_dirs(lambda key: None)
