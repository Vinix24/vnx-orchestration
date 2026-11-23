"""Tests for scripts/ci/test_shard_plan.py (track pr-ci-onder-zes-minuten, C2a)."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import random
import sys
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parents[1] / "scripts" / "ci" / "test_shard_plan.py"
_spec = importlib.util.spec_from_file_location("test_shard_plan", _PATH)
tsp = importlib.util.module_from_spec(_spec)
sys.modules["test_shard_plan"] = tsp
_spec.loader.exec_module(tsp)

REPO, WF, BR, ART, NOW = "o/r", "vnx-ci.yml", "main", "profile-a-evidence", "2026-11-17T12:00:00Z"
GOOD_ART = {"artifacts": [{"name": ART, "expired": False}]}
SRC = ["--repo", REPO, "--workflow", WF, "--branch", BR, "--before", NOW, "--artifact", ART]


def _hash(path: str, n: int) -> int:
    return int(hashlib.sha256(path.encode()).hexdigest(), 16) % n + 1


def _xml(tmp_path: Path, cases: list[tuple]) -> Path:
    rows = "".join(
        f'<testcase classname="{c}" name="t" time="{s}"' + (f' file="{f}"' if f else "") + "/>"
        for c, s, f in cases
    )
    p = tmp_path / "j.xml"
    p.write_text(f"<testsuites><testsuite>{rows}</testsuite></testsuites>")
    return p


def _runs(*rows):
    return {"workflow_runs": [{"id": i, "created_at": c, "updated_at": c, "conclusion": k} for i, c, k in rows]}


def _select(tmp_path, runs, arts=GOOD_ART, before=NOW, download=None):
    def api(path):
        return arts if "/artifacts" in path else runs

    def dl(repo, run_id, name, dest):
        (dest / "junit.xml").write_text("<testsuite/>")

    return tsp.select_source(REPO, WF, BR, before, ART, tmp_path, api, download or dl)


def test_greedy_heaviest_first_into_lightest_shard_by_hand():
    d = {"a.py": 10.0, "b.py": 7.0, "c.py": 5.0, "d.py": 4.0, "e.py": 4.0}
    assignment, loads = tsp.plan(list(d), 2, d)
    # a->1 (10/0), b->2 (10/7), c->2 (10/12), d->1 (14/12), e->2 (14/16)
    assert assignment == {"a.py": 1, "b.py": 2, "c.py": 2, "d.py": 1, "e.py": 2}
    assert loads == [14.0, 16.0]
    ties = {"x.py": 2.0, "y.py": 2.0, "z.py": 2.0}  # path order, then lowest shard
    assert tsp.plan(["y.py", "x.py", "z.py"], 3, ties)[0] == {"x.py": 1, "y.py": 2, "z.py": 3}


def test_file_without_duration_goes_to_hash_shard_stable_across_shuffles():
    files = [f"tests/test_{i}.py" for i in range(40)]
    first, _ = tsp.plan(files, 4, {files[0]: 9.0})
    shuffled = random.Random(7).sample(files, len(files))
    second, _ = tsp.plan(shuffled, 4, {files[0]: 9.0})
    assert json.dumps(first) == json.dumps(second)
    assert all(first[f] == _hash(f, 4) for f in files[1:])


def test_every_file_once_unknown_junit_entry_ignored_duplicates_collapse(tmp_path):
    files = ["tests/test_a.py", "tests/test_b.py", "tests/test_c.py", "tests/test_a.py"]
    xml = _xml(tmp_path, [("tests.test_a", 3, None), ("tests.test_ghost", 99, None)])
    r = tsp.build_result(files, 2, [xml], "5", None)
    assert sorted(r["assignment"]) == ["tests/test_a.py", "tests/test_b.py", "tests/test_c.py"]
    assert all(1 <= k <= 2 for k in r["assignment"].values())
    assert (r["unmapped"], r["files_without_duration"], sum(r["load_seconds"])) == (1, 2, 3.0)
    assert r["source"] == "junit:5" and r["source_reason"] is None


def test_classname_mapping_module_class_nested_and_file_attribute(tmp_path):
    files = ["tests/test_foo.py", "tests/sub/test_bar.py", "tests/test_baz.py"]
    xml = _xml(tmp_path, [("tests.test_foo", 1.0, None), ("tests.test_foo.TestBar", 2.0, None),
                          ("tests.sub.test_bar.TestA.Inner", 4.0, None), ("x.y", 8.0, "tests/test_baz.py")])
    assert tsp.durations_from_junit([xml], files) == (
        {"tests/test_foo.py": 3.0, "tests/sub/test_bar.py": 4.0, "tests/test_baz.py": 8.0}, 0)


@pytest.mark.parametrize("bad", ['time="nan"', 'time="inf"', 'time="-1"', 'time="x"', ""])
def test_malformed_testcase_time_falls_back_to_hash(tmp_path, bad):
    p = tmp_path / "j.xml"
    p.write_text(f'<testsuite><testcase classname="tests.test_a" name="t" {bad}/></testsuite>')
    with pytest.raises(tsp.SourceError):
        tsp.durations_from_junit([p], ["tests/test_a.py"])
    r = tsp.build_result(["tests/test_a.py"], 3, [p], "1", None)
    assert r["source"] == "hash" and r["source_reason"]
    assert r["assignment"] == {"tests/test_a.py": _hash("tests/test_a.py", 3)}


@pytest.mark.parametrize("content", ["<not xml", "", '<!DOCTYPE x [<!ENTITY a "b">]><r/>', None, "<r/>"])
def test_unreadable_missing_or_unmapped_xml_falls_back_to_hash(tmp_path, content):
    p = tmp_path / "j.xml"
    if content is not None:
        p.write_text(content)
    r = tsp.build_result(["tests/test_a.py"], 2, [p], "1", None)
    assert r["source"] == "hash" and r["source_reason"]


def test_select_source_newest_successful_run_before_timestamp(tmp_path):
    runs = _runs((9, "2026-11-17T12:30:00Z", "success"), (8, "2026-11-17T11:50:00Z", "failure"),
                 (7, "2026-11-17T11:00:00Z", "success"), (6, "2026-11-17T10:00:00Z", "success"), (5, NOW, "success"))
    seen = []
    run_id, xmls = _select(tmp_path, runs, download=lambda *a: (seen.append(a), (a[3] / "j.xml").write_text("<a/>")))
    assert run_id == 7 and seen[0][:3] == (REPO, 7, ART) and xmls == [seen[0][3] / "j.xml"]
    assert seen[0][3].parent == tmp_path


def test_select_source_returns_only_the_xml_of_this_download(tmp_path):
    (tmp_path / "old-run.xml").write_text("<testsuite/>")  # OI-2979: left by an earlier download into --dest
    run_id, xmls = _select(tmp_path, _runs(OK_RUN))
    assert [p.name for p in xmls] == ["junit.xml"] and xmls[0].parent != tmp_path


def test_select_source_asks_the_api_for_runs_created_before_the_cutoff(tmp_path):
    def api(path):  # OI-2979: 100 newer runs fill the unfiltered first page, the older green run is behind it
        if "/artifacts" in path:
            return GOOD_ART
        if "created=%3C%3D2026-11-17T12:00:00Z" in path:
            return _runs(OK_RUN)
        return _runs(*((i, "2026-11-17T13:00:00Z", "success") for i in range(100, 200)))
    run_id, _ = tsp.select_source(REPO, WF, BR, NOW, ART, tmp_path, api, lambda *a: (a[3] / "j.xml").write_text("<a/>"))
    assert run_id == 1


def _run(i, created, updated):
    return {"id": i, "created_at": created, "updated_at": updated, "conclusion": "success"}


def _pick(tmp_path, rows):
    def api(path):
        return GOOD_ART if "/artifacts" in path else {"workflow_runs": rows}
    return tsp.select_source(REPO, WF, BR, NOW, ART, tmp_path, api, lambda *a: (a[3] / "j.xml").write_text("<a/>"))[0]


def test_two_shards_pick_the_same_run_when_a_run_completes_between_them(tmp_path):
    five = _run(5, "2026-11-17T10:00:00Z", "2026-11-17T10:10:00Z")
    six_done = _run(6, "2026-11-17T11:00:00Z", "2026-11-17T12:05:00Z")  # created before, finished after the cutoff
    assert _pick(tmp_path, [five]) == 5  # shard 1: run 6 still running, not in the success answer
    assert _pick(tmp_path, [six_done, five]) == 5  # shard 3: run 6 turned green after the cutoff


def test_completed_before_cutoff_wins_and_missing_updated_at_is_skipped(tmp_path):
    older = _run(4, "2026-11-17T09:00:00Z", "2026-11-17T09:10:00Z")
    newer = _run(6, "2026-11-17T11:00:00Z", "2026-11-17T11:30:00Z")
    assert _pick(tmp_path, [older, newer]) == 6
    no_stamp = {"id": 7, "created_at": "2026-11-17T11:40:00Z", "conclusion": "success"}
    assert _pick(tmp_path, [older, newer, no_stamp]) == 6
    for bad in (None, "not a time", "2026-11-17T11:45:00"):
        assert _pick(tmp_path, [older, {**no_stamp, "updated_at": bad}]) == 4


OK_RUN = (1, "2026-11-17T01:00:00Z", "success")


@pytest.mark.parametrize("runs", [
    {}, {"workflow_runs": "x"}, {"workflow_runs": []}, _runs((1, "2026-11-17T13:00:00Z", "success")),
    *[_runs((1, OK_RUN[1], k)) for k in ("failure", None, "cancelled")],
    *[_runs((1, c, "success")) for c in (None, "not a time", "2026-11-17T01:00:00")],
])
def test_select_source_without_a_proven_run_raises(tmp_path, runs):
    with pytest.raises(tsp.SourceError):
        _select(tmp_path, runs)


@pytest.mark.parametrize("arts", [
    {"artifacts": []}, {"artifacts": "x"}, {}, {"artifacts": [{"name": "other", "expired": False}]},
    *[{"artifacts": [{"name": ART, **e}]} for e in ({"expired": True}, {}, {"expired": None})],
])
def test_select_source_missing_or_expired_artifact_raises(tmp_path, arts):
    with pytest.raises(tsp.SourceError):
        _select(tmp_path, _runs(OK_RUN), arts)


def test_select_source_bad_timestamp_empty_download_and_errors_raise(tmp_path):
    for kw in ({"before": "garbage"}, {"download": lambda *a: None}):
        with pytest.raises(tsp.SourceError):
            _select(tmp_path, _runs(OK_RUN), **kw)
    with pytest.raises(tsp.SourceError, match="disk"):
        _select(tmp_path, _runs(OK_RUN), download=lambda *a: (_ for _ in ()).throw(OSError("disk")))

    def boom(path):
        raise RuntimeError("api down")

    with pytest.raises(tsp.SourceError, match="api down"):
        tsp.select_source(REPO, WF, BR, NOW, ART, tmp_path, boom, None)


def _cli(tmp_path, capsys, files, *args):
    f = tmp_path / "f.txt"
    f.write_text(files)
    rc = tsp.main(["plan", "--files", str(f), *args])
    return rc, capsys.readouterr().out


def test_cli_without_source_falls_back_to_hash_with_the_given_reason(tmp_path, capsys):
    rc, out = _cli(tmp_path, capsys, "tests/test_a.py\n", "--shards", "3", "--source-reason", "no run")
    r = json.loads(out)
    assert rc == 0 and r["source"] == "hash" and r["source_reason"] == "no run"
    assert r["assignment"] == {"tests/test_a.py": _hash("tests/test_a.py", 3)}


def test_cli_with_junit_is_byte_identical_across_input_order(tmp_path, capsys):
    xml = _xml(tmp_path, [("tests.test_a", 5, None), ("tests.test_b", 3, None)])
    args = ("--shards", "2", "--junit", str(xml))
    one = _cli(tmp_path, capsys, "tests/test_a.py\ntests/test_b.py\ntests/test_c.py\n", *args)
    two = _cli(tmp_path, capsys, "tests/test_c.py\n\ntests/test_b.py\ntests/test_a.py", *args)
    assert one == two and json.loads(one[1])["source"] == "junit:local"


def test_cli_select_source_reports_run_or_reason_and_exits_zero(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(tsp, "select_source", lambda *a, **k: (7, [tmp_path / "j.xml"]))
    assert tsp.main(["select-source", *SRC, "--dest", str(tmp_path)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["run_id"] == 7 and out["junit"] == [str(tmp_path / "j.xml")]
    monkeypatch.setattr(tsp, "select_source", lambda *a, **k: (_ for _ in ()).throw(tsp.SourceError("expired")))
    assert tsp.main(["select-source", *SRC]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["run_id"] is None and out["reason"] == "expired"


@pytest.mark.parametrize("files,args", [("a.py\n", ["--shards", "0"]), ("a.py\n", ["--shards", "-1"]),
                                        ("a.py\n", []), ("\n  \n", ["--shards", "2"])])
def test_cli_usage_errors_exit_2(tmp_path, capsys, files, args):
    assert _cli(tmp_path, capsys, files, *args)[0] == 2


def test_cli_missing_list_exits_2_and_plan_rejects_bad_input(tmp_path):
    assert tsp.main(["plan", "--files", str(tmp_path / "none.txt"), "--shards", "2"]) == 2
    for files, n in ((["a.py"], 0), ([], 2)):
        with pytest.raises(ValueError):
            tsp.plan(files, n)
