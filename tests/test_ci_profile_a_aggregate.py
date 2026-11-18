"""Tests for scripts/ci/profile_a_aggregate.py (pr-ci-onder-zes-minuten C3)."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "ci" / "profile_a_aggregate.py"


def _main(argv):
    """Load the script per call, so a missing script fails each test, not collection."""
    spec = importlib.util.spec_from_file_location("profile_a_aggregate", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.main(argv)

SHARDS = ["shard-1", "shard-2", "shard-3", "shard-4"]


def _needs(result="success", profile="light", guards="success"):
    needs = {j: {"result": result, "outputs": {"profile": profile}} for j in SHARDS}
    needs["guards"] = {"result": guards, "outputs": {}}
    return needs


def _evidence(root: Path, ks=(1, 2, 3, 4)) -> None:
    for k in ks:
        (root / f"shard-{k}").mkdir(parents=True, exist_ok=True)
        (root / f"shard-{k}" / "manifest.json").write_text("{}", encoding="utf-8")


def _run(tmp_path, capsys, needs, profile="light", xdist_off=False, raw=None, shard_jobs=None):
    needs_file = tmp_path / "needs.json"
    needs_file.write_text(raw if raw is not None else json.dumps(needs), encoding="utf-8")
    out = tmp_path / "verdict.json"
    summary = tmp_path / "summary.md"
    argv = ["decide", "--needs", str(needs_file),
            "--shard-jobs", shard_jobs if shard_jobs is not None else ",".join(SHARDS),
            "--guards-job", "guards", "--profile", profile,
            "--evidence-root", str(tmp_path / "ev"),
            "--output", str(out), "--summary", str(summary)]
    if xdist_off:
        argv.append("--xdist-off")
    rc = _main(argv)
    stdout = capsys.readouterr().out
    verdict = json.loads(out.read_text(encoding="utf-8")) if out.exists() else None
    return rc, stdout, verdict, summary


# --- green ---------------------------------------------------------------

def test_green_light(tmp_path, capsys):
    rc, stdout, verdict, summary = _run(tmp_path, capsys, _needs())
    assert (rc, stdout) == (0, "mode=light\n")
    assert verdict["schema"] == "vnx-ci-aggregate/1"
    assert verdict["green"] is True and verdict["mode"] == "light" and verdict["reasons"] == []
    assert [s["k"] for s in verdict["shards"]] == [1, 2, 3, 4]
    assert "light" in summary.read_text(encoding="utf-8")


def test_green_sharded(tmp_path, capsys):
    _evidence(tmp_path / "ev")
    rc, stdout, verdict, _ = _run(tmp_path, capsys, _needs(profile="heavy"), profile="heavy")
    assert (rc, stdout) == (0, "mode=sharded\n")
    assert all(s["evidence"] is True for s in verdict["shards"])


def test_green_serial(tmp_path, capsys):
    needs = {j: {"result": "skipped", "outputs": {}} for j in SHARDS}
    needs["guards"] = {"result": "success", "outputs": {}}
    rc, stdout, verdict, _ = _run(tmp_path, capsys, needs, profile="heavy", xdist_off=True)
    assert (rc, stdout) == (0, "mode=serial\n")
    assert verdict["xdist_off"] is True and verdict["mode"] == "serial"


# --- red, one fault each -------------------------------------------------

def _assert_red(rc, stdout, verdict, reasons):
    assert rc == 1
    assert "mode=" not in stdout
    assert verdict["green"] is False and verdict["mode"] is None
    assert verdict["reasons"] == reasons


@pytest.mark.parametrize("result", ["failure", "cancelled", "skipped"])
def test_red_shard_result(tmp_path, capsys, result):
    needs = _needs()
    needs["shard-2"]["result"] = result
    _assert_red(*_run(tmp_path, capsys, needs)[:3], [f"shard 2: result {result}"])


@pytest.mark.parametrize("result", ["failure", "cancelled", "skipped"])
def test_red_guards_result(tmp_path, capsys, result):
    rc, stdout, verdict, summary = _run(tmp_path, capsys, _needs(guards=result))
    _assert_red(rc, stdout, verdict, [f"guards: result {result}"])
    assert f"guards: result {result}" in summary.read_text(encoding="utf-8")


def test_red_guards_result_in_serial_mode(tmp_path, capsys):
    needs = {j: {"result": "skipped"} for j in SHARDS}
    needs["guards"] = {"result": "cancelled"}
    _assert_red(*_run(tmp_path, capsys, needs, xdist_off=True)[:3], ["guards: result cancelled"])


@pytest.mark.parametrize("make", ["missing", "empty", "only_subdir"])
def test_red_shard_without_evidence(tmp_path, capsys, make):
    _evidence(tmp_path / "ev", ks=(1, 2, 4))
    if make == "empty":
        (tmp_path / "ev" / "shard-3").mkdir()
    elif make == "only_subdir":
        (tmp_path / "ev" / "shard-3" / "sub").mkdir(parents=True)
    rc, stdout, verdict, _ = _run(tmp_path, capsys, _needs(profile="heavy"), profile="heavy")
    _assert_red(rc, stdout, verdict, ["shard 3: no evidence"])
    assert verdict["shards"][2]["evidence"] is False


def test_red_profile_mismatch(tmp_path, capsys):
    needs = _needs()
    needs["shard-4"]["outputs"]["profile"] = "heavy"
    _assert_red(*_run(tmp_path, capsys, needs)[:3], ["shard 4: profile heavy, aggregator light"])


def test_red_aggregator_heavy_shards_light(tmp_path, capsys):
    _evidence(tmp_path / "ev")
    rc, stdout, verdict, _ = _run(tmp_path, capsys, _needs(), profile="heavy")
    _assert_red(rc, stdout, verdict, [f"shard {k}: profile light, aggregator heavy" for k in range(1, 5)])


@pytest.mark.parametrize("outputs", [None, {}, {"profile": None}, {"profile": ""}, {"profile": 7}, "light"])
def test_red_no_profile_output(tmp_path, capsys, outputs):
    needs = _needs()
    if outputs is None:
        del needs["shard-1"]["outputs"]
    else:
        needs["shard-1"]["outputs"] = outputs
    _assert_red(*_run(tmp_path, capsys, needs)[:3], ["shard 1: no profile output"])


@pytest.mark.parametrize("result", ["success", "failure", "cancelled"])
def test_red_shard_ran_while_xdist_off(tmp_path, capsys, result):
    needs = {j: {"result": "skipped"} for j in SHARDS}
    needs["guards"] = {"result": "success"}
    needs["shard-3"]["result"] = result
    _assert_red(*_run(tmp_path, capsys, needs, xdist_off=True)[:3],
                [f"shard 3: result {result} while VNX_CI_XDIST=0"])


@pytest.mark.parametrize("missing", ["shard-2", "guards"])
def test_red_job_absent_from_needs(tmp_path, capsys, missing):
    needs = _needs()
    del needs[missing]
    rc, stdout, verdict, _ = _run(tmp_path, capsys, needs)
    _assert_red(rc, stdout, verdict, [f"needs: no job {missing}"])


def test_red_near_identical_job_id_is_not_matched(tmp_path, capsys):
    needs = _needs()
    needs["shard-1x"] = needs.pop("shard-1")
    _assert_red(*_run(tmp_path, capsys, needs)[:3], ["needs: no job shard-1"])


def test_red_several_faults_all_named(tmp_path, capsys):
    _evidence(tmp_path / "ev", ks=(1, 2, 3))
    needs = _needs(profile="heavy", guards="failure")
    needs["shard-1"]["result"] = "cancelled"
    needs["shard-2"]["outputs"]["profile"] = "light"
    rc, stdout, verdict, _ = _run(tmp_path, capsys, needs, profile="heavy")
    _assert_red(rc, stdout, verdict, [
        "shard 1: result cancelled",
        "shard 2: profile light, aggregator heavy",
        "shard 4: no evidence",
        "guards: result failure",
    ])


# --- invalid input (exit 2) ----------------------------------------------

def _assert_invalid(tmp_path, capsys, **kw):
    rc, stdout, verdict, summary = _run(tmp_path, capsys, kw.pop("needs", _needs()), **kw)
    assert rc == 2
    assert "mode=" not in stdout
    assert verdict["green"] is False and verdict["mode"] is None
    assert verdict["reasons"][0].startswith("invalid input: ")
    assert "invalid input" in summary.read_text(encoding="utf-8")


@pytest.mark.parametrize("raw", ["", "{not json", "[1, 2]", "null", '"x"', '{"a": NaN}'])
def test_invalid_needs_file_content(tmp_path, capsys, raw):
    _assert_invalid(tmp_path, capsys, raw=raw)


def test_invalid_unreadable_needs_file(tmp_path, capsys):
    out = tmp_path / "verdict.json"
    rc = _main(["decide", "--needs", str(tmp_path / "absent.json"), "--shard-jobs", "a,b",
                   "--guards-job", "g", "--profile", "light", "--evidence-root", str(tmp_path),
                   "--output", str(out)])
    assert rc == 2 and "mode=" not in capsys.readouterr().out
    assert json.loads(out.read_text(encoding="utf-8"))["green"] is False


@pytest.mark.parametrize("bad", [None, "", "SUCCESS", "neutral", 0, True])
def test_invalid_result_value(tmp_path, capsys, bad):
    needs = _needs()
    needs["shard-2"]["result"] = bad
    _assert_invalid(tmp_path, capsys, needs=needs)


def test_invalid_result_missing_and_entry_not_object(tmp_path, capsys):
    needs = _needs()
    del needs["shard-2"]["result"]
    _assert_invalid(tmp_path, capsys, needs=needs)
    needs = _needs()
    needs["guards"] = "success"
    _assert_invalid(tmp_path, capsys, needs=needs)


@pytest.mark.parametrize("jobs", ["", ",", "shard-1,shard-1,shard-3,shard-4", "shard-1,,shard-3",
                                  "shard-1,shard-2,shard-3,guards"])
def test_invalid_shard_jobs(tmp_path, capsys, jobs):
    _assert_invalid(tmp_path, capsys, shard_jobs=jobs)


def test_unwritable_verdict_is_exit_2_without_mode(tmp_path, capsys):
    (tmp_path / "verdict.json").mkdir()
    needs_file = tmp_path / "needs.json"
    needs_file.write_text(json.dumps(_needs()), encoding="utf-8")
    rc = _main(["decide", "--needs", str(needs_file), "--shard-jobs", ",".join(SHARDS),
                "--guards-job", "guards", "--profile", "light", "--evidence-root", str(tmp_path),
                "--output", str(tmp_path / "verdict.json")])
    assert rc == 2 and "mode=" not in capsys.readouterr().out


def test_invalid_argv_returns_2(capsys):
    assert _main(["decide", "--profile", "medium"]) == 2
    assert "mode=" not in capsys.readouterr().out


# --- the real process -----------------------------------------------------

@pytest.mark.parametrize("guards, rc, line", [("success", 0, "mode=light\n"), ("failure", 1, "")])
def test_subprocess(tmp_path, guards, rc, line):
    (tmp_path / "needs.json").write_text(json.dumps(_needs(guards=guards)), encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, str(_SCRIPT), "decide", "--needs", str(tmp_path / "needs.json"),
         "--shard-jobs", ",".join(SHARDS), "--guards-job", "guards", "--profile", "light",
         "--evidence-root", str(tmp_path / "ev"), "--output", str(tmp_path / "v.json")],
        capture_output=True, text=True, check=False)
    assert (proc.returncode, proc.stdout) == (rc, line)
