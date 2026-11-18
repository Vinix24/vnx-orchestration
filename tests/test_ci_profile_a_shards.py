"""C2b (pr-ci-onder-zes-minuten): the shard mode of profile_a_pytest_args.sh and completeness over N shards."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from test_ci_check_xdist_parity import PASS, PINS, A, B, C, D, E, cxp, inactive, manifest, skip, write_phase
from test_ci_profile_a_pytest_args import SCRIPT, SUITE_FILES, _clean_env, _read_vector, _run_script, _suite_root

RUN = {"run_id": "7", "run_attempt": "1"}
F = "tests/test_d.py::test_four"
SPLIT = {1: [A, B], 2: [C, D], 3: [E], 4: [F]}
ASSIGN = {"tests/test_a.py": 1, "tests/test_b.py": 2, "tests/test_c.py": 3, "tests/test_d.py": 4}


def _json(path: Path, doc: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc), encoding="utf-8")


def _shards(tmp_path: Path) -> list:
    dirs = [tmp_path / f"shard-{k}" for k in SPLIT]
    for (k, nodeids), d in zip(SPLIT.items(), dirs):
        _json(d / "args/shard.json", {"schema": "vnx-ci-shard/1", **RUN, "shard": k, "shards": 4, "source": "junit:1",
                                      "assignment": ASSIGN})
        _json(d / "args/collected.json", {**manifest("serial", sorted(sum(SPLIT.values(), []))), "phase": "collected", **RUN})
        write_phase(d, "shard", manifest("shard", nodeids), [(n, PASS) for n in nodeids], lambda _: RUN)
    write_phase(dirs[0], "serial", inactive(), None, lambda _: RUN)
    return dirs


def _shard_root(tmp_path: Path) -> Path:
    root = _suite_root(tmp_path, "")
    for name in ("profile_a_shard.py", "check_xdist_parity.py", "test_shard_plan.py", "profile_a_run_phase.sh"):
        shutil.copy2(SCRIPT.parent / name, root / "scripts" / "ci" / name)
    return root


def _verdict(tmp_path: Path, dirs: list) -> dict:
    constraints, skips = tmp_path / "constraints.txt", tmp_path / "skips.txt"
    constraints.write_text("".join(f"{k}=={v}\n" for k, v in PINS.items()), encoding="utf-8")
    skips.write_text("# none\n", encoding="utf-8")
    return cxp.evaluate_completeness(cxp.CompletenessInputs(None, constraints, skips, dict(RUN), shard_dirs=dirs))


def _plan(d: Path, **change) -> None:
    doc = json.loads((d / "args/shard.json").read_text(encoding="utf-8"))
    _json(d / "args/shard.json", {k: v for k, v in {**doc, **change}.items() if v is not ...})


def _phase(d: Path, nodeids: list, cases: list, stamp: dict = RUN) -> None:
    write_phase(d, "shard", manifest("shard", nodeids), cases, lambda _: stamp)


CASES = {
    "collected_differs": ("collected_differs", lambda ds: _json(ds[2] / "args/collected.json", {**manifest("serial", [A, B, C, D, E]), **RUN})),
    "source_differs": ("source_differs", lambda ds: _plan(ds[1], source="hash")),
    "in no shard": ("in no shard", lambda ds: [_phase(ds[2], [], [])] + [_plan(d, assignment={f: n for f, n in ASSIGN.items() if n != 3}) for d in ds]),
    "in more than one phase": ("in more than one phase", lambda ds: write_phase(ds[0], "serial", manifest("serial", [E]), [(E, PASS)], lambda _: RUN)),
    "not_run": ("not_run", lambda ds: _phase(ds[1], [C, D], [(C, PASS)])),
    "run_more_than_once": ("run_more_than_once", lambda ds: _phase(ds[1], [C, D], [(C, PASS), (C, PASS), (D, PASS)])),
    "unexpected_skip": ("unexpected_skip", lambda ds: _phase(ds[1], [C, D], [(C, PASS), (D, skip("no db"))])),
    "missing_shard_plan": ("missing_shard_plan", lambda ds: (ds[3] / "args/shard.json").unlink()),
    "missing_xml": ("missing_xml", lambda ds: (ds[2] / "junit-shard.xml").unlink()),
    "missing_manifest": ("missing_manifest", lambda ds: (ds[0] / "manifest-serial.json").unlink()),
    "unparsable_manifest": ("unparsable_manifest", lambda ds: (ds[1] / "args/collected.json").write_text("{", encoding="utf-8")),
    "stale run": ("stale_evidence", lambda ds: _plan(ds[1], run_id="6")),
    "stale attempt": ("stale_evidence", lambda ds: _phase(ds[2], [E], [(E, PASS)], {**RUN, "run_attempt": "2"})),
    "stale no attempt": ("stale_evidence", lambda ds: _plan(ds[3], run_attempt=...)),
    "shard missing": ("malformed_shard_plan", lambda ds: _plan(ds[0], shard=...)),
    "shard as string": ("malformed_shard_plan", lambda ds: _plan(ds[0], shard="1")),
    "shards wrong": ("malformed_shard_plan", lambda ds: _plan(ds[2], shards=3)),
    "source None": ("malformed_shard_plan", lambda ds: _plan(ds[2], source=None)),
    "assignment malformed": ("malformed_shard_plan", lambda ds: _plan(ds[2], assignment=[])),
    "serial_outside_shard_one": ("serial_outside_shard_one", lambda ds: write_phase(ds[1], "serial", inactive(), None, lambda _: RUN)),
}


def test_four_complete_shards_are_green_and_three_of_them_are_invalid(tmp_path):
    dirs = _shards(tmp_path)
    verdict, short = _verdict(tmp_path, dirs), _verdict(tmp_path, dirs[:3])
    assert (verdict["exit"], verdict["shards"], verdict["invalid_evidence"]) == (0, 4, [])
    assert short["exit"] == 2 and {p["reason"] for p in short["invalid_evidence"]} == {"malformed_shard_plan"}


@pytest.mark.parametrize("case", sorted(CASES))
def test_every_gap_in_the_shard_evidence_turns_completeness_red(tmp_path, case):
    dirs = _shards(tmp_path)
    token, change = CASES[case]
    change(dirs)
    verdict = _verdict(tmp_path, dirs)
    assert verdict["exit"] in (1, 2) and token in json.dumps([verdict["differences"], verdict["invalid_evidence"]])


@pytest.mark.parametrize("assignment", [{}, {**ASSIGN, "tests/test_d.py": 99}, {**ASSIGN, "tests/test_d.py": 0},
                                        {**ASSIGN, "/tests/test_e.py": 4}, {**ASSIGN, "tests/test_c.py": 4}])
def test_a_malformed_assignment_in_every_plan_is_refused(tmp_path, assignment):
    dirs = _shards(tmp_path)
    for d in dirs:
        _plan(d, assignment=assignment)
    verdict = _verdict(tmp_path, dirs)
    assert verdict["exit"] == 2 and "malformed_shard_plan" in {p["reason"] for p in verdict["invalid_evidence"]}


@pytest.mark.parametrize("value", ["0/4", "5/4", "4", "1/4/2", "a/4", "1/100", "/4", "1/", "01/4"])
def test_a_malformed_shard_value_is_refused(tmp_path, value):
    proc = _run_script(SCRIPT, tmp_path / "out", "1", run={"VNX_CI_SHARD": value})
    assert proc.returncode == 2 and "VNX_CI_SHARD must be k/N" in proc.stderr and not (tmp_path / "out").exists()


def test_the_shard_mode_needs_the_xdist_shape(tmp_path):
    proc = _run_script(SCRIPT, tmp_path / "out", "0", run={"VNX_CI_SHARD": "1/4"})
    assert proc.returncode == 2 and "needs the xdist shape" in proc.stderr


@pytest.mark.parametrize("source", ['{"run_id": 1, "junit": [], "reason": null}', "{", '{"run_id": null}', None])
def test_an_unusable_duration_source_refuses_the_shard(tmp_path, source):
    root = _shard_root(tmp_path)
    path = tmp_path / "source.json"
    if source is not None:
        path.write_text(source, encoding="utf-8")
    proc = _run_script(root / "scripts/ci" / SCRIPT.name, tmp_path / "out", "1",
                       run={"VNX_CI_SHARD": "1/1", "VNX_CI_SHARD_SOURCE": str(path)})
    assert proc.returncode == 2 and "not a select-source output" in proc.stderr
    assert not (tmp_path / "out" / "shard.json").exists() and not (tmp_path / "out" / "run.json").exists()


def test_end_to_end_four_shards_of_the_args_script_are_complete(tmp_path):
    pytest.importorskip("xdist")
    root = _shard_root(tmp_path)
    ci = root / "scripts" / "ci"
    (ci / "xdist_expected_skips.txt").write_text("tests/test_modskip.py\n", encoding="utf-8")
    (ci / "profile_a_constraints.txt").write_text("".join(f"{k}=={v}\n" for k, v in PINS.items()), encoding="utf-8")
    xml = tmp_path / "src" / "junit-parallel.xml"
    xml.parent.mkdir()
    xml.write_text("<testsuite>" + "".join(f'<testcase classname="tests.test_{n}" name="t" time="{s}"/>' for n, s in
                                           (("alpha", 4), ("beta", 3), ("gamma", 2), ("space", 1))) + "</testsuite>")
    _json(tmp_path / "source.json", {"run_id": 42, "junit": [str(xml)], "reason": None})
    env = {"GITHUB_RUN_ID": "7", "GITHUB_RUN_ATTEMPT": "1", "VNX_CI_SHARD_SOURCE": str(tmp_path / "source.json")}
    assert _run_script(ci / SCRIPT.name, tmp_path / "default", "1", run=env).returncode == 0
    parallel, files = _read_vector(tmp_path / "default" / "parallel.args"), []
    dirs = [tmp_path / f"shard-{k}" for k in range(1, 5)]
    for k, d in enumerate(dirs, start=1):
        proc = _run_script(ci / SCRIPT.name, d / "args", "1", run={**env, "VNX_CI_SHARD": f"{k}/4"})
        assert proc.returncode == 0, proc.stderr
        assert sorted(p.name for p in (d / "args").iterdir()) == ["collected.json", "run.json", "shard.args", "shard.json"]
        vector, plan = _read_vector(d / "args" / "shard.args"), json.loads((d / "args" / "shard.json").read_text())
        assert plan["source"] == "junit:42" and vector == ["-q", *(f"{root}/{f}" for f in plan["files"]), *parallel[2:]]
        files += plan["files"]
        subprocess.run(["bash", str(ci / "profile_a_run_phase.sh"), "shard", str(d)], env={**_clean_env(), **env}, cwd=root)
    subprocess.run(["python3", str(ci / "pytest_collection_manifest.py"), "inactive", "--phase", "serial", "--output",
                    str(dirs[0] / "manifest-serial.json")], env={**_clean_env(), **env}, check=True)
    assert sorted(files) == sorted(f for f in SUITE_FILES if f != "tests/test_excluded.py")
    proc = subprocess.run(["python3", str(ci / "check_xdist_parity.py"), "completeness", "--constraints",
                           str(ci / "profile_a_constraints.txt"), "--expected-skips", str(ci / "xdist_expected_skips.txt"),
                           "--output", str(tmp_path / "verdict.json"), "--run-id", "7", "--run-attempt", "1",
                           *(a for d in dirs for a in ("--shard-dir", str(d)))], env=_clean_env(), capture_output=True, text=True)
    assert proc.returncode == 0, (proc.stdout, (tmp_path / "verdict.json").read_text())
