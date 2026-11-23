"""impact_backtest: S rebuilt for old PR runs from the Profile A evidence (fake gh, fake git, a tiny tree in tmp_path)."""

from __future__ import annotations

import io
import json
import re
import sys
import tarfile
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts" / "lib"))

import impact_backtest as bt  # noqa: E402
import impact_ledger as ledger  # noqa: E402
from test_impact_shadow import junit, manifest  # noqa: E402

TREE = {
    "scripts/lib/core.py": "X = 1\n", "scripts/lib/other.py": "Y = 1\n",
    "scripts/ci/test_exclusions.txt": "", "scripts/ci/xdist_expected_skips.txt": "",
    "tests/test_core.py": "import core\n", "tests/test_other.py": "import other\n", "tests/test_plain.py": "import os\n",
}
NODEIDS = ["tests/test_core.py::t", "tests/test_other.py::t", "tests/test_plain.py::t"]
BASE = "b" * 40
MAIN_AT_RUN = "a" * 40


def sha(run_id) -> str:
    return format(run_id, "040x")


def run(run_id, attempt=1, conclusion="failure"):
    return {"databaseId": run_id, "attempt": attempt, "headSha": sha(run_id), "headBranch": "feat", "event": "pull_request",
            "conclusion": conclusion, "createdAt": "2026-11-10T09:00:00Z"}


def jobs(conclusion="failure"):
    return {"jobs": [{"name": ledger.PROFILE_A_JOB, "started_at": "2026-11-10T09:00:00Z",
                      "completed_at": "2026-11-10T09:40:00Z", "conclusion": conclusion}]}


def shards(target: Path, cases: dict, stamp: tuple, count: int = 2) -> None:
    """The C3 artifact: shard-k/ per shard with its plan and phase, shard 1 also the serial phase (NODEIDS[0])."""
    split = {1: NODEIDS[1:2], 2: NODEIDS[2:]}
    for k in range(1, count + 1):
        directory = target / f"shard-{k}"
        (directory / "args").mkdir(parents=True)
        (directory / "args" / "shard.json").write_text(json.dumps({"shard": k, "shards": 2}), encoding="utf-8")
        manifest(directory / "manifest-shard.json", "shard", split[k], stamp=stamp)
        junit(directory / "junit-shard.xml", {n: cases[n] for n in split[k]}, stamp=stamp)
    manifest(target / "shard-1" / "manifest-serial.json", "serial", NODEIDS[:1], stamp=stamp)
    junit(target / "shard-1" / "junit-serial.xml", {NODEIDS[0]: cases[NODEIDS[0]]}, stamp=stamp)


class Gh:
    """gh calls of the backtest; ``failing`` maps a run id to the stderr its download answers with, ``cases`` run id -> {nodeid: outcome}."""

    def __init__(self, runs, cases, failing=None, sharded=False, jobs=None):
        self.runs, self.cases, self.failing, self.calls, self.sharded = runs, cases, failing or {}, [], sharded
        self.jobs = jobs

    def __call__(self, argv):
        self.calls.append(list(argv))
        if argv[:3] == ["gh", "repo", "view"]:
            return json.dumps({"nameWithOwner": "owner/repo"})
        if argv[:3] == ["gh", "run", "list"]:
            return json.dumps(self.runs)
        if argv[:3] == ["gh", "run", "download"]:
            run_id, name, target = int(argv[3]), argv[5], Path(argv[7])
            assert name == "profile-a-evidence"
            if run_id in self.failing:
                raise ledger.RunnerError(argv, 1, self.failing[run_id])
            if run_id not in self.cases:
                raise ledger.RunnerError(argv, 1, "no valid artifacts found to download")
            if self.sharded:
                shards(target, self.cases[run_id], (str(run_id), "1"))
                return ""
            manifest(target / "manifest-serial.json", "serial", NODEIDS, stamp=(str(run_id), "1"))
            junit(target / "junit-serial.xml", self.cases[run_id], stamp=(str(run_id), "1"))
            return ""
        if argv[:2] == ["gh", "api"]:
            return json.dumps(self.jobs[int(argv[2].split('/')[5])] if self.jobs else jobs())
        raise AssertionError(f"unexpected gh call {argv}")


class Git:
    """git calls of the backtest; ``paths`` is the changed files of every run, ``fail`` names the git commands that exit 1
    (``fetch`` of main, ``fetch-head`` of the PR head).  ``rev_list`` answers the main-at-run lookup, ``merge_base`` the
    merge-base of main-at-run and the head (default ``BASE``).  A merge-base against today's ``origin/main`` answers the head
    itself: today's main already contains every PR head of these runs."""

    def __init__(self, paths, fail=(), tree=None, rev_list=MAIN_AT_RUN, merge_base=BASE):
        self.paths, self.fail, self.tree, self.calls = paths, set(fail), tree or TREE, []
        self.rev_list, self.merge_base = rev_list, merge_base

    def __call__(self, argv):
        self.calls.append(list(argv))
        command = "fetch-head" if argv[1] == "fetch" and argv[-1] != "main" else argv[1]
        if command in self.fail:
            raise ledger.RunnerError(argv, 1, f"fatal: {command}")
        if command == "rev-list":
            return self.rev_list
        if command == "merge-base":
            return argv[-1] + "\n" if argv[2] == "origin/main" else self.merge_base + "\n"
        if command == "diff":
            return "\n".join(self.paths) + "\n"
        if command == "archive":
            with tarfile.open(argv[3], "w") as tar:
                for rel, text in self.tree.items():
                    info = tarfile.TarInfo(rel)
                    info.size = len(text.encode())
                    tar.addfile(info, io.BytesIO(text.encode()))
        return ""


def lines(state):
    return ledger.read_ledger(bt.backtest_path(state))


def one(tmp_path, cases, paths=("scripts/lib/core.py",), **kwargs):
    gh = Gh([run(1)], {1: cases}, **kwargs)
    result = bt.backtest(tmp_path, gh, Git(paths))
    return result, lines(tmp_path)


def test_red_run_outside_s_is_one_miss_counted_under_backtest(tmp_path):
    result, (entry,) = one(tmp_path, {"tests/test_other.py::t": "fail", "tests/test_core.py::t": "pass", "tests/test_plain.py::t": "pass"})
    assert result == {"added": ["1/1"], "skipped": [], "failed": []}
    assert entry["source"] == "backtest" and entry["candidate_kind"] == "pr_head" and entry["schema"] == "impact_ledger/1"
    assert entry["inputs"]["base"] == BASE and entry["inputs"]["candidate"] == sha(1) and entry["inputs"]["paths"] == ["scripts/lib/core.py"]
    assert entry["inputs"]["selector_version"] and entry["inputs"]["graph_version"]
    assert entry["shadow"]["sets"]["S"] == ["tests/test_core.py"] and entry["shadow"]["reconstructible"] is True
    rep = ledger.report(lines(tmp_path))
    assert rep["misses"] == [{"test_id": "tests/test_other.py::t", "run_id": 1, "run_attempt": 1}]
    assert len(rep["by_source"]["backtest"]["misses"]) == 1 and rep["by_source"]["live"]["counted"] == []
    assert rep["by_source"]["backtest"]["red_runs"] == {"counted": 1, "reduced": 1}


def test_red_run_inside_s_is_no_miss(tmp_path):
    _, (entry,) = one(tmp_path, {"tests/test_core.py::t": "fail", "tests/test_other.py::t": "pass", "tests/test_plain.py::t": "pass"})
    rep = ledger.report([entry])
    assert rep["misses"] == [] and rep["red_runs"] == {"counted": 1, "reduced": 1}


def test_full_suite_fallback_counts_but_is_not_reduced(tmp_path):
    cases = {"tests/test_other.py::t": "fail", "tests/test_core.py::t": "pass", "tests/test_plain.py::t": "pass"}
    _, (entry,) = one(tmp_path, cases, paths=("config/settings.yaml",))
    rep = ledger.report([entry])
    assert rep["counted"] == ["1/1"] and rep["reduced_share"]["reduced"] == 0
    assert rep["misses"] == [] and rep["red_runs"] == {"counted": 1, "reduced": 0}


def test_absent_evidence_artifact_is_a_line_with_missing_and_not_counted(tmp_path):
    result = bt.backtest(tmp_path, Gh([run(1)], {}), Git([]))
    (entry,) = lines(tmp_path)
    assert result["added"] == ["1/1"] and entry["missing"] == ["artifact:profile-a-evidence"] and entry["shadow"] is None
    rep = ledger.report([entry])
    assert rep["counted"] == [] and rep["excluded"] == [{"attempt": "1/1", "reason": "missing:profile-a-evidence"}]


def test_merge_base_failing_is_onreconstrueerbaar_naming_base(tmp_path):
    gh, git = Gh([run(1)], {1: {"tests/test_core.py::t": "pass"}}), Git(["scripts/lib/core.py"], fail=["merge-base"])
    bt.backtest(tmp_path, gh, git)
    (entry,) = lines(tmp_path)
    assert entry["shadow"] == {"schema": "impact_shadow/1", "reconstructible": False, "unreadable_inputs": ["base"]}
    assert entry["inputs"]["base"] is None and entry["inputs"]["selector_version"] is None
    assert ledger.report([entry])["excluded"][0]["reason"] == "onreconstrueerbaar:base"
    assert not any(call[1] == "archive" for call in git.calls)


def test_base_is_merge_base_of_main_at_run_creation_and_the_head_never_of_today_main(tmp_path):
    git = Git(["scripts/lib/core.py"])
    bt.backtest(tmp_path, Gh([run(1)], {1: {"tests/test_core.py::t": "pass"}}), git)
    (entry,) = lines(tmp_path)
    (rev_list,) = [c for c in git.calls if c[1] == "rev-list"]
    assert rev_list == ["git", "rev-list", "-1", "--first-parent", "--before=2026-11-10T09:00:00+00:00", "origin/main"]
    merge_bases = [c for c in git.calls if c[1] == "merge-base"]
    assert merge_bases == [["git", "merge-base", MAIN_AT_RUN, sha(1)]]
    assert entry["inputs"]["base"] == BASE and entry["shadow"]["reconstructible"] is True


def test_a_head_that_today_main_already_contains_keeps_the_paths_of_its_historical_diff(tmp_path):
    git = Git(["scripts/lib/core.py", "tests/test_core.py"])
    bt.backtest(tmp_path, Gh([run(1)], {1: {"tests/test_core.py::t": "fail"}}), git)
    (entry,) = lines(tmp_path)
    assert entry["inputs"]["base"] == BASE != entry["inputs"]["candidate"]
    assert entry["inputs"]["paths"] == ["scripts/lib/core.py", "tests/test_core.py"]
    assert entry["shadow"]["reconstructible"] is True
    diff = next(c for c in git.calls if c[1] == "diff")
    assert diff == ["git", "diff", "--name-only", BASE, sha(1)]


def test_a_run_without_a_readable_created_at_is_onreconstrueerbaar_naming_base(tmp_path):
    for label, created in (("missing", None), ("empty", ""), ("garbage", "yesterday"), ("number", 5)):
        broken = dict(run(1), createdAt=created)
        if created is None:
            del broken["createdAt"]
        git = Git(["scripts/lib/core.py"])
        bt.backtest(tmp_path / label, Gh([broken], {1: {"tests/test_core.py::t": "pass"}}), git)
        (entry,) = lines(tmp_path / label)
        assert entry["shadow"] == {"schema": "impact_shadow/1", "reconstructible": False, "unreadable_inputs": ["base"]}, label
        assert entry["inputs"]["base"] is None
        assert not any(c[1] in ("rev-list", "merge-base", "archive") for c in git.calls), label


def test_an_empty_or_failing_rev_list_is_onreconstrueerbaar_naming_base_and_never_falls_back_to_today(tmp_path):
    cases = {"empty": Git(["scripts/lib/core.py"], rev_list=""), "blank": Git(["scripts/lib/core.py"], rev_list="\n"),
             "failing": Git(["scripts/lib/core.py"], fail=["rev-list"]), "not-a-sha": Git(["scripts/lib/core.py"], rev_list="fatal")}
    for label, git in cases.items():
        bt.backtest(tmp_path / label, Gh([run(1)], {1: {"tests/test_core.py::t": "pass"}}), git)
        (entry,) = lines(tmp_path / label)
        assert entry["shadow"]["unreadable_inputs"] == ["base"] and entry["inputs"]["base"] is None, label
        assert not any(c[1] == "merge-base" for c in git.calls), label
        assert ledger.report([entry])["excluded"][0]["reason"] == "onreconstrueerbaar:base"


def test_a_base_equal_to_the_head_or_an_empty_path_list_makes_paths_unreadable(tmp_path):
    equal = Git(["scripts/lib/core.py"], merge_base=sha(1))
    empty = Git([])
    for label, git in (("equal", equal), ("empty", empty)):
        bt.backtest(tmp_path / label, Gh([run(1)], {1: {"tests/test_core.py::t": "pass"}}), git)
        (entry,) = lines(tmp_path / label)
        assert entry["shadow"]["reconstructible"] is False and entry["shadow"]["unreadable_inputs"] == ["paths"], label
        assert ledger.report([entry])["excluded"][0]["reason"] == "onreconstrueerbaar:paths"
        assert not any(c[1] == "archive" for c in git.calls), label
    assert not any(c[1] == "diff" for c in equal.calls)


def test_each_unreadable_input_is_named_never_guessed(tmp_path):
    for fail, wanted in (("fetch-head", "candidate"), ("diff", "paths"), ("archive", "tree")):
        state = tmp_path / fail
        bt.backtest(state, Gh([run(1)], {1: {"tests/test_core.py::t": "pass"}}), Git(["scripts/lib/core.py"], fail=[fail]))
        assert lines(state)[0]["shadow"]["unreadable_inputs"] == [wanted]
    bare = {k: v for k, v in TREE.items() if not k.endswith("test_exclusions.txt")}
    bt.backtest(tmp_path / "x", Gh([run(1)], {1: {"tests/test_core.py::t": "pass"}}), Git(["scripts/lib/core.py"], tree=bare))
    assert lines(tmp_path / "x")[0]["shadow"]["unreadable_inputs"] == ["exclusions"]


def test_a_gh_failure_other_than_absent_writes_no_line_and_the_rest_goes_on(tmp_path):
    cases = {2: {"tests/test_core.py::t": "pass", "tests/test_other.py::t": "pass", "tests/test_plain.py::t": "pass"}}
    result = bt.backtest(tmp_path, Gh([run(1), run(2)], cases, failing={1: "HTTP 403: Attention Required"}), Git(["scripts/lib/core.py"]))
    assert [f["attempt"] for f in result["failed"]] == ["1/1"] and result["added"] == ["2/1"]
    assert [e["run_id"] for e in lines(tmp_path)] == [2]


def test_an_attempt_in_the_live_ledger_is_skipped_and_counts_once_as_live(tmp_path):
    live = {"schema": "impact_ledger/1", "run_id": 1, "run_attempt": 1, "run_conclusion": "failure", "missing": [], "shadow": None}
    ledger.append_entry(ledger.ledger_path(tmp_path), live)
    gh = Gh([run(1)], {1: {"tests/test_core.py::t": "pass"}})
    result = bt.backtest(tmp_path, gh, Git(["scripts/lib/core.py"]))
    assert result["skipped"] == ["1/1"] and result["added"] == [] and not bt.backtest_path(tmp_path).exists()
    assert not any(call[:3] == ["gh", "run", "download"] for call in gh.calls)
    both = [live, dict(live, source="backtest")]
    rep = ledger.report(both)
    assert rep["attempts_in_ledger"] == 1 and rep["backtest_superseded_by_live"] == ["1/1"]
    assert rep["by_source"]["live"]["attempts_in_ledger"] == 1 and rep["by_source"]["backtest"]["attempts_in_ledger"] == 0


def test_running_twice_writes_no_duplicate_lines(tmp_path):
    cases = {"tests/test_core.py::t": "pass", "tests/test_other.py::t": "pass", "tests/test_plain.py::t": "pass"}
    one(tmp_path, cases)
    before = bt.backtest_path(tmp_path).read_bytes()
    result, entries = one(tmp_path, cases)
    assert result["skipped"] == ["1/1"] and result["added"] == [] and len(entries) == 1
    assert bt.backtest_path(tmp_path).read_bytes() == before


def test_only_finished_pull_request_runs_are_read_and_discovery_asks_for_the_event(tmp_path):
    gh = Gh([run(1, conclusion="cancelled"), run(2, conclusion=None)], {})
    result = bt.backtest(tmp_path, gh, Git([]))
    assert result == {"added": [], "skipped": [], "failed": []}
    listing = next(call for call in gh.calls if call[:3] == ["gh", "run", "list"])
    assert listing[listing.index("--event") + 1] == "pull_request"


def test_discovery_failure_writes_no_line(tmp_path):
    result = bt.backtest(tmp_path, Gh([run(1)], {}), Git([], fail=["fetch"]))
    assert result["failed"][0]["attempt"] == "discovery" and not bt.backtest_path(tmp_path).exists()


def test_temp_dirs_are_cleaned_on_success_and_on_failure(tmp_path, monkeypatch):
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))
    cases = {"tests/test_core.py::t": "pass"}
    bt.backtest(tmp_path / "a", Gh([run(1)], {1: cases}), Git(["scripts/lib/core.py"]))
    bt.backtest(tmp_path / "b", Gh([run(1)], {}, failing={1: "HTTP 403"}), Git([]))
    bt.backtest(tmp_path / "c", Gh([run(1)], {1: cases}), Git(["scripts/lib/core.py"], fail=["archive"]))
    assert list(scratch.iterdir()) == []


def test_links_in_the_archive_are_left_out_of_the_old_tree(tmp_path):
    archive = tmp_path / "t.tar"
    with tarfile.open(archive, "w") as tar:
        link = tarfile.TarInfo("scripts/escape")
        link.type, link.linkname = tarfile.SYMTYPE, "/etc"
        tar.addfile(link)
        tar.addfile(tarfile.TarInfo("tests"))
    tree = tmp_path / "tree"
    tree.mkdir()
    with tarfile.open(archive) as tar:
        tar.extractall(tree, filter=bt._regular_only)
    assert not (tree / "scripts" / "escape").exists()


RED_OUTSIDE_S = {"tests/test_other.py::t": "fail", "tests/test_core.py::t": "pass", "tests/test_plain.py::t": "pass"}


def test_a_sharded_artifact_gives_the_record_of_the_single_job_layout(tmp_path):
    bt.backtest(tmp_path / "single", Gh([run(1)], {1: RED_OUTSIDE_S}), Git(["scripts/lib/core.py"]))
    bt.backtest(tmp_path / "sharded", Gh([run(1)], {1: RED_OUTSIDE_S}, sharded=True), Git(["scripts/lib/core.py"]))
    (single,), (sharded,) = lines(tmp_path / "single"), lines(tmp_path / "sharded")
    assert sharded["shadow"]["reconstructible"] is True and sharded["shadow"]["attempt_valid"] is True
    assert sharded["shadow"]["sets"] == single["shadow"]["sets"]
    assert sharded["shadow"]["sets"]["misses"] == ["tests/test_other.py::t"]


def _break_shards(how: str):
    def mutate(target: Path) -> None:
        if how == "shard-gone":
            for path in sorted((target / "shard-2").rglob("*"), reverse=True):
                path.rmdir() if path.is_dir() else path.unlink()
            (target / "shard-2").rmdir()
            (target / "shard-3").mkdir()
        elif how == "last-shard-gone":
            (target / "shard-1" / "args" / "shard.json").write_text(json.dumps({"shards": 3}), encoding="utf-8")
        elif how == "plan-unreadable":
            (target / "shard-2" / "args" / "shard.json").write_text("{not json", encoding="utf-8")
        elif how == "plan-shards-not-a-number":
            (target / "shard-2" / "args" / "shard.json").write_text(json.dumps({"shards": "2"}), encoding="utf-8")
        elif how == "no-shard-manifest":
            (target / "shard-2" / "manifest-shard.json").unlink()
        elif how == "serial-twice":
            manifest(target / "shard-2" / "manifest-serial.json", "serial", NODEIDS[:1])
    return mutate


@pytest.mark.parametrize("how, why", [
    ("shard-gone", "not shard-1..shard-N"), ("last-shard-gone", "plan of 3 shards"),
    ("plan-unreadable", "no readable args/shard.json"), ("plan-shards-not-a-number", "has shards '2'"),
    ("no-shard-manifest", "no manifest-shard.json"), ("serial-twice", "also in an earlier shard"),
])
def test_incomplete_shard_evidence_is_refused_by_name_and_the_backtest_line_names_the_evidence(tmp_path, how, why):
    evidence = tmp_path / "ev"
    shards(evidence, RED_OUTSIDE_S, ("1", "1"))
    _break_shards(how)(evidence)
    with pytest.raises(ValueError, match=re.escape(why)):
        bt.shard_view(evidence, tmp_path / "flat")
    gh = Gh([run(1)], {1: RED_OUTSIDE_S}, sharded=True)
    original = gh.__call__

    def broken(argv):
        out = original(argv)
        if argv[:3] == ["gh", "run", "download"]:
            _break_shards(how)(Path(argv[7]))
        return out
    bt.backtest(tmp_path / "state", broken, Git(["scripts/lib/core.py"]))
    (entry,) = lines(tmp_path / "state")
    assert entry["shadow"] == {"schema": "impact_shadow/1", "reconstructible": False, "unreadable_inputs": ["evidence"]}


def test_the_single_job_layout_and_a_symlinked_shard_dir_are_read_in_place(tmp_path):
    evidence = tmp_path / "ev"
    evidence.mkdir()
    assert bt.shard_view(evidence, tmp_path / "flat") == evidence
    shards(tmp_path / "elsewhere", RED_OUTSIDE_S, ("1", "1"))
    (evidence / "shard-1").symlink_to(tmp_path / "elsewhere" / "shard-1", target_is_directory=True)
    assert bt.shard_view(evidence, tmp_path / "flat") == evidence and not (tmp_path / "flat").exists()


def test_a_sharded_profile_a_run_gets_the_span_over_the_six_jobs(tmp_path):
    spans = {ledger.PROFILE_A_JOB: ("09:07:00", "09:08:00"), "Profile A guards": ("09:00:20", "09:03:00")}
    spans.update({f"Profile A shard ({k}/4)": ("09:00:30", "09:06:00") for k in range(1, 5)})
    sharded = {"jobs": [{"name": n, "started_at": f"2026-11-10T{a}Z", "completed_at": f"2026-11-10T{b}Z",
                         "conclusion": "failure"} for n, (a, b) in spans.items()]}
    bt.backtest(tmp_path, Gh([run(1)], {1: RED_OUTSIDE_S}, jobs={1: sharded}), Git(["scripts/lib/core.py"]))
    (entry,) = lines(tmp_path)
    assert entry["profile_a_job"]["seconds"] == 460 and entry["profile_a_job"]["shape"] == "sharded"
    assert entry["profile_a_job"]["started_at"] == "2026-11-10T09:00:20Z" and entry["missing"] == []
