"""Tests for scripts/ci/impact_shadow.py: the shadow record built from the selection and the Profile A evidence."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

_CI_DIR = Path(__file__).resolve().parents[1] / "scripts" / "ci"
if str(_CI_DIR) not in sys.path:
    sys.path.insert(0, str(_CI_DIR))

import impact_shadow as shadow  # isort: skip

RUN = ("100", "1")
FILES = ["tests/test_a.py", "tests/test_b.py", "tests/test_c.py"]


def junit(path: Path, cases: dict, stamp: tuple = RUN) -> None:
    """cases: nodeid -> 'pass' | 'fail'; the real pytest junitxml shape."""
    body = ""
    for nodeid, outcome in cases.items():
        file, name = nodeid.split("::")
        inner = '<failure message="boom">x</failure>' if outcome == "fail" else ""
        body += f'<testcase classname="{file[:-3].replace("/", ".")}" name="{name}">{inner}</testcase>'
    props = f'<property name="vnx_run_id" value="{stamp[0]}"/><property name="vnx_run_attempt" value="{stamp[1]}"/>'
    path.write_text(f'<testsuites><testsuite name="p"><properties>{props}</properties>{body}</testsuite></testsuites>', encoding="utf-8")


def manifest(path: Path, phase: str, nodeids: list, outcomes: list | None = None, stamp: tuple = RUN) -> None:
    doc = {"schema": "vnx-collection-manifest/1", "phase": phase, "phase_active": True, "run_id": stamp[0],
           "run_attempt": stamp[1], "mode": "serial", "collection_finished": True, "nodeids": nodeids,
           "collection_outcomes": outcomes or []}
    path.write_text(json.dumps(doc), encoding="utf-8")


def evidence(tmp_path: Path, cases: dict) -> Path:
    directory = tmp_path / "evidence"
    directory.mkdir()
    manifest(directory / "manifest-serial.json", "serial", list(cases))
    junit(directory / "junit-serial.xml", cases)
    return directory


def selection(selected: list, reached: bool = False) -> dict:
    return {"selected": selected, "selector_version": "sv", "graph_version": "gv",
            "conftest_closure_version": "cv", "conftest_reached": reached}


def build(tmp_path: Path, cases: dict, sel: dict, known: set | None = None, exclusions: set | None = None,
          paths: list | None = None, evidence_dir: Path | None = None) -> dict:
    ev = shadow.read_evidence(evidence_dir or evidence(tmp_path, cases), RUN)
    identity = {"run_id": "100", "run_attempt": "1", "base_sha": "b", "candidate_sha": "c",
                "changed_paths": ["scripts/x.py"] if paths is None else paths}
    return shadow.build_record(sel, ev, exclusions or set(), known or set(), identity)


CASES = {"tests/test_a.py::test_one": "pass", "tests/test_b.py::test_two": "fail", "tests/test_c.py::test_three": "pass"}


def test_failing_test_outside_selection_is_a_miss_by_id(tmp_path):
    rec = build(tmp_path, CASES, selection(["tests/test_a.py"]))
    assert rec["sets"]["misses"] == ["tests/test_b.py::test_two"]
    assert rec["sets"]["U"] == FILES and rec["sets"]["F"] == ["tests/test_b.py::test_two"]
    assert rec["attempt_valid"] is True and rec["invalid_reasons"] == []


def test_failing_test_inside_selection_is_not_a_miss(tmp_path):
    rec = build(tmp_path, CASES, selection(["tests/test_b.py"]))
    assert rec["sets"]["misses"] == []


def test_conftest_reached_turns_the_selection_into_the_universe(tmp_path):
    rec = build(tmp_path, CASES, selection(["tests/test_a.py"], reached=True))
    assert rec["sets"]["misses"] == ["tests/test_b.py::test_two"]
    assert rec["conftest_variant"]["misses"] == [] and rec["conftest_variant"]["selected_as"] == "universe"
    assert rec["conftest_variant"]["reduced"] is False and rec["reduced"] is True


def test_conftest_not_reached_keeps_the_selection(tmp_path):
    rec = build(tmp_path, CASES, selection(["tests/test_a.py"]))
    assert rec["conftest_variant"]["misses"] == rec["sets"]["misses"]
    assert rec["conftest_variant"]["selected_as"] == "selection"


def test_known_failures_split_on_the_selection_with_the_label(tmp_path):
    known = {"tests/test_a.py", "tests/test_gone.py"}
    rec = build(tmp_path, CASES, selection(["tests/test_a.py"]), known=known, exclusions={"tests/test_a.py"})
    kf = rec["known_failures"]
    assert kf["in_selection"] == ["tests/test_a.py"] and kf["outside_selection"] == ["tests/test_gone.py"]
    assert kf["label_outside_selection"] == "niet onderzocht"
    assert rec["sets"]["X"] == ["tests/test_a.py"]


def test_reduced_true_and_false(tmp_path):
    assert build(tmp_path, CASES, selection(["tests/test_a.py"]))["reduced"] is True


def test_reduced_false_when_the_selection_is_the_whole_non_excluded_universe(tmp_path):
    rec = build(tmp_path, CASES, selection(FILES), exclusions={"tests/test_c.py"})
    assert rec["reduced"] is False


def test_missing_junit_is_an_invalid_attempt_and_still_a_record(tmp_path):
    directory = evidence(tmp_path, CASES)
    (directory / "junit-serial.xml").unlink()
    rec = build(tmp_path, CASES, selection(FILES), evidence_dir=directory)
    assert rec["attempt_valid"] is False
    assert "missing_xml" in {r["reason"] for r in rec["invalid_reasons"]}


def test_collection_error_is_an_invalid_attempt(tmp_path):
    directory = tmp_path / "evidence"
    directory.mkdir()
    manifest(directory / "manifest-serial.json", "serial", list(CASES),
             [{"nodeid": "tests/test_bad.py", "outcome": "failed", "reason": "ImportError", "reports": 1}])
    junit(directory / "junit-serial.xml", CASES)
    rec = build(tmp_path, CASES, selection(FILES), evidence_dir=directory)
    assert rec["attempt_valid"] is False
    assert {"reason": "collection_error", "detail": "tests/test_bad.py"} in rec["invalid_reasons"]
    assert "tests/test_bad.py" in rec["sets"]["U"]


def test_junit_that_does_not_cover_every_file_is_invalid(tmp_path):
    directory = tmp_path / "evidence"
    directory.mkdir()
    manifest(directory / "manifest-serial.json", "serial", list(CASES))
    junit(directory / "junit-serial.xml", {"tests/test_a.py::test_one": "pass"})
    rec = build(tmp_path, CASES, selection(FILES), evidence_dir=directory)
    assert rec["attempt_valid"] is False
    assert "junit_does_not_cover_e" in {r["reason"] for r in rec["invalid_reasons"]}
    assert rec["sets"]["E"] == ["tests/test_a.py"]


def test_evidence_of_another_attempt_is_invalid(tmp_path):
    directory = tmp_path / "evidence"
    directory.mkdir()
    manifest(directory / "manifest-serial.json", "serial", list(CASES), stamp=("100", "2"))
    junit(directory / "junit-serial.xml", CASES, stamp=("100", "2"))
    rec = build(tmp_path, CASES, selection(FILES), evidence_dir=directory)
    assert rec["attempt_valid"] is False
    assert "stale_evidence" in {r["reason"] for r in rec["invalid_reasons"]}


def test_no_manifest_at_all_is_invalid(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    rec = build(tmp_path, CASES, selection(FILES), evidence_dir=empty)
    assert rec["attempt_valid"] is False and rec["invalid_reasons"][0]["reason"] == "missing_manifest"


@pytest.mark.parametrize("field", ["base_sha", "candidate_sha", "changed_paths", "selector_version", "graph_version"])
def test_a_missing_reconstruction_datum_is_not_reconstructible(tmp_path, field):
    sel = selection(FILES)
    ev = shadow.read_evidence(evidence(tmp_path, CASES), RUN)
    identity = {"run_id": "100", "run_attempt": "1", "base_sha": "b", "candidate_sha": "c", "changed_paths": ["a.py"]}
    if field in identity:
        identity[field] = "" if field != "changed_paths" else []
    else:
        sel[field] = ""
    assert shadow.build_record(sel, ev, set(), set(), identity)["reconstructible"] is False
    ok = {"run_id": "100", "run_attempt": "1", "base_sha": "b", "candidate_sha": "c", "changed_paths": ["a.py"]}
    assert shadow.build_record(selection(FILES), ev, set(), set(), ok)["reconstructible"] is True


def test_a_selection_without_a_field_is_refused(tmp_path):
    sel = selection(FILES)
    del sel["conftest_reached"]
    with pytest.raises(ValueError):
        build(tmp_path, CASES, sel)


def test_summary_line(tmp_path):
    rec = build(tmp_path, CASES, selection(["tests/test_a.py"], reached=True))
    assert shadow.summary_line(rec) == (
        "impact shadow: selected 1 of 3, misses 1 (conftest variant: selected 3, misses 0), attempt_valid true")


def test_cli_writes_a_byte_identical_record_and_exits_zero_on_invalid(tmp_path):
    directory = evidence(tmp_path, CASES)
    (directory / "junit-serial.xml").unlink()
    (tmp_path / "sel.json").write_text(json.dumps(selection(["tests/test_a.py"])), encoding="utf-8")
    (tmp_path / "paths.txt").write_text("scripts/x.py\n", encoding="utf-8")
    cmd = [sys.executable, str(_CI_DIR / "impact_shadow.py"), "--selection", str(tmp_path / "sel.json"), "--evidence", str(directory),
           "--run-id", "100", "--run-attempt", "1", "--base", "b", "--candidate", "c", "--paths-file", str(tmp_path / "paths.txt")]
    outputs = []
    for name in ("one.json", "two.json"):
        done = subprocess.run([*cmd, "--output", str(tmp_path / name), "--summary", str(tmp_path / "s.txt")], capture_output=True, text=True)
        assert done.returncode == 0, done.stderr
        outputs.append((tmp_path / name).read_bytes())
    assert outputs[0] == outputs[1]
    record = json.loads(outputs[0])
    assert record["schema"] == "impact_shadow/1" and record["scope"] == "impact" and record["suite_complete"] == "niet gedekt"
    assert record["attempt_valid"] is False
    assert (tmp_path / "s.txt").read_text().startswith("impact shadow: selected 1 of 3")


def test_cli_crashes_nonzero_on_an_unreadable_selection(tmp_path):
    (tmp_path / "sel.json").write_text("not json", encoding="utf-8")
    (tmp_path / "paths.txt").write_text("a.py\n", encoding="utf-8")
    done = subprocess.run([sys.executable, str(_CI_DIR / "impact_shadow.py"), "--selection", str(tmp_path / "sel.json"),
                           "--evidence", str(tmp_path), "--run-id", "1", "--run-attempt", "1", "--base", "b", "--candidate", "c",
                           "--paths-file", str(tmp_path / "paths.txt"), "--output", str(tmp_path / "o.json")], capture_output=True, text=True)
    assert done.returncode != 0 and not (tmp_path / "o.json").exists()


# --- the shadow job that runs only S: plan, shape and the job record ------------------------------

def impact_selection(selected: list, excluded: list | None = None, universe: int = 10, excluded_count: int = 1) -> dict:
    return {"scope": "impact", "expand_reason": None, "selected": selected, "selected_excluded": excluded or [],
            "universe_count": universe, "excluded_count": excluded_count, "selector_version": "sv", "graph_version": "gv"}


def job(tmp_path: Path, sel: dict | None, cases: dict | None = None, extra: list | None = None, **identity) -> dict:
    out = tmp_path / "job.json"
    cmd = [sys.executable, str(_CI_DIR / "impact_shadow.py"), "job", "--output", str(out), "--run-id", RUN[0], "--run-attempt", RUN[1],
           "--base", identity.get("base", "b"), "--candidate", "c", "--evidence", str(tmp_path / "evidence")]
    if sel is not None:
        (tmp_path / "sel.json").write_text(json.dumps(sel), encoding="utf-8")
        cmd += ["--selection", str(tmp_path / "sel.json")]
    if cases is not None:
        evidence(tmp_path, cases)
    done = subprocess.run([*cmd, *(extra or [])], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    return json.loads(out.read_text(encoding="utf-8"))


def test_job_complete_when_every_file_of_s_ran_and_nothing_else(tmp_path):
    rec = job(tmp_path, impact_selection(["tests/test_a.py", "tests/test_b.py"]), {"tests/test_a.py::t": "pass", "tests/test_b.py::t": "pass"},
              ["--wall-seconds", "42", "--phase-exit", "serial=0", "--phase-exit", "parallel=1"])
    assert rec["schema"] == "impact_shadow_job/1"
    assert rec["ran"] is True and rec["skip_reason"] is None and rec["complete"] is True
    assert rec["not_run"] == [] and rec["outside_selection"] == []
    assert rec["wall_seconds"] == 42 and rec["phase_exit_codes"] == {"parallel": 1, "serial": 0}
    assert rec["sets"]["E"] == ["tests/test_a.py", "tests/test_b.py"]
    assert rec["base_sha"] == "b" and rec["selector_version"] == "sv" and rec["graph_version"] == "gv"


def test_job_names_a_file_of_s_without_a_junit_record(tmp_path):
    rec = job(tmp_path, impact_selection(["tests/test_a.py", "tests/test_b.py"]), {"tests/test_a.py::t": "pass"})
    assert rec["not_run"] == ["tests/test_b.py"]
    assert rec["complete"] is False


def test_job_names_a_file_outside_s_and_a_failing_test(tmp_path):
    rec = job(tmp_path, impact_selection(["tests/test_a.py"]), {"tests/test_a.py::t": "fail", "tests/test_c.py::t": "pass"})
    assert rec["outside_selection"] == ["tests/test_c.py"]
    assert rec["sets"]["F"] == ["tests/test_a.py::t"]
    assert rec["complete"] is False


def test_job_excluded_files_are_not_expected_to_run(tmp_path):
    sel = impact_selection(["tests/test_a.py", "tests/test_x.py"], excluded=["tests/test_x.py"])
    rec = job(tmp_path, sel, {"tests/test_a.py::t": "pass"})
    assert rec["sets"]["S_minus_X"] == ["tests/test_a.py"] and rec["complete"] is True


def test_job_is_incomplete_without_readable_evidence(tmp_path):
    rec = job(tmp_path, impact_selection(["tests/test_a.py"]), None)
    assert rec["not_run"] == ["tests/test_a.py"] and rec["complete"] is False
    assert rec["invalid_reasons"][0]["reason"] == "missing_manifest"


@pytest.mark.parametrize("sel,reason", [
    (None, "no_selection"),
    ({"scope": "full", "expand_reason": "conftest", "selected": []}, "expand:conftest"),
    ({"scope": "full", "selected": []}, "expand:unknown"),
    (impact_selection(["tests/test_a.py"], universe=2, excluded_count=1), "not_reduced"),
    (impact_selection(["tests/test_a.py", "tests/test_b.py", "tests/test_c.py"], universe=3, excluded_count=0), "not_reduced"),
    (impact_selection([]), "empty_selection"),
    (impact_selection(["tests/test_x.py"], excluded=["tests/test_x.py"]), "empty_selection"),
])
def test_job_does_not_run_pytest_and_names_the_reason(tmp_path, sel, reason):
    rec = job(tmp_path, sel)
    assert rec["ran"] is False and rec["skip_reason"] == reason
    assert rec["complete"] is False and rec["sets"] == {"E": [], "F": [], "S_minus_X": []}


@pytest.mark.parametrize("broken", [
    {"scope": "impact"},
    {**impact_selection(["tests/test_a.py"]), "scope": "partial"},
    {**impact_selection(["tests/test_a.py"]), "scope": None},
    {**impact_selection(["tests/test_a.py"]), "selected": "tests/test_a.py"},
    {**impact_selection(["tests/test_a.py"]), "selected": [1]},
    {**impact_selection(["tests/test_a.py"]), "selected_excluded": None},
    {**impact_selection(["tests/test_a.py"]), "universe_count": None},
    {**impact_selection(["tests/test_a.py"]), "universe_count": "10"},
    {**impact_selection(["tests/test_a.py"]), "excluded_count": True},
])
def test_job_with_a_malformed_selection_stays_blocking_as_no_selection(tmp_path, broken):
    rec = job(tmp_path, broken)
    assert rec["ran"] is False and rec["skip_reason"] == "no_selection" and rec["complete"] is False


def test_job_with_an_unreadable_selection_file_is_no_selection(tmp_path):
    (tmp_path / "sel.json").write_text("{not json", encoding="utf-8")
    out = tmp_path / "job.json"
    done = subprocess.run([sys.executable, str(_CI_DIR / "impact_shadow.py"), "job", "--output", str(out), "--run-id", "1", "--run-attempt", "1",
                           "--base", "", "--candidate", "", "--selection", str(tmp_path / "sel.json")], capture_output=True, text=True)
    assert done.returncode == 0
    assert json.loads(out.read_text())["skip_reason"] == "no_selection"


def test_job_failed_reason_wins_and_the_job_did_not_run(tmp_path):
    rec = job(tmp_path, impact_selection(["tests/test_a.py"]), {"tests/test_a.py::t": "pass"}, ["--failed-reason", "args_failed"])
    assert rec["ran"] is False and rec["skip_reason"] == "args_failed" and rec["complete"] is False


def test_job_record_is_byte_identical_for_the_same_input(tmp_path):
    sel = impact_selection(["tests/test_a.py", "tests/test_b.py"])
    cases = {"tests/test_a.py::t": "pass", "tests/test_b.py::t": "fail"}
    first = job(tmp_path, sel, cases, ["--wall-seconds", "7"])
    text = (tmp_path / "job.json").read_bytes()
    (tmp_path / "job.json").unlink()
    job(tmp_path, sel, None, ["--wall-seconds", "7"])
    assert (tmp_path / "job.json").read_bytes() == text
    assert text.endswith(b"\n") and list(first) == sorted(first)


def test_record_cli_still_works_without_a_subcommand(tmp_path):
    cases = {"tests/test_a.py::t": "pass"}
    (tmp_path / "sel.json").write_text(json.dumps(selection(["tests/test_a.py"])), encoding="utf-8")
    (tmp_path / "paths.txt").write_text("scripts/x.py\n", encoding="utf-8")
    done = subprocess.run([sys.executable, str(_CI_DIR / "impact_shadow.py"), "--selection", str(tmp_path / "sel.json"), "--evidence", str(evidence(tmp_path, cases)),
                           "--run-id", RUN[0], "--run-attempt", RUN[1], "--base", "b", "--candidate", "c", "--paths-file", str(tmp_path / "paths.txt"),
                           "--output", str(tmp_path / "record.json")], capture_output=True, text=True)
    assert done.returncode == 0 and json.loads((tmp_path / "record.json").read_text())["schema"] == "impact_shadow/1"


def vector(path: Path, args: list) -> None:
    path.write_bytes("".join(f"{a}\0" for a in args).encode())


def read_vector(path: Path) -> list:
    return path.read_bytes().decode().split("\0")[:-1]


def shaped(tmp_path: Path, runnable: list):
    root, args_dir = "/r", tmp_path / "args"
    args_dir.mkdir()
    vector(args_dir / "serial.args", ["-q", "-p", "no:xdist", "--timeout=180", "-k", "not Q", "--ignore=/r/tests/test_x.py",
                                      "/r/tests/test_s.py", "/r/tests/test_n.py::TestN::test_one"])
    vector(args_dir / "parallel.args", ["-q", "/r/tests", "-k", "not Q", "--ignore=/r/tests/test_x.py", "--ignore=/r/tests/test_s.py",
                                        "--deselect=/r/tests/test_n.py::TestN::test_one", "-n", "4"])
    sel = tmp_path / "sel.json"
    sel.write_text(json.dumps(impact_selection(runnable, universe=50)), encoding="utf-8")
    done = subprocess.run([sys.executable, str(_CI_DIR / "impact_shadow.py"), "shape", "--selection", str(sel), "--args-dir", str(args_dir), "--root", root],
                          capture_output=True, text=True)
    return done, args_dir


def test_shape_replaces_only_the_test_tree_and_keeps_every_option(tmp_path):
    done, args_dir = shaped(tmp_path, ["tests/test_a.py", "tests/test_s.py", "tests/test_n.py"])
    assert done.returncode == 0 and done.stdout.split() == ["serial", "parallel"]
    parallel = read_vector(args_dir / "parallel.args")
    assert "/r/tests" not in parallel and parallel[1:3] == ["/r/tests/test_a.py", "/r/tests/test_n.py"]
    assert "--ignore=/r/tests/test_s.py" in parallel and "-n" in parallel
    serial = read_vector(args_dir / "serial.args")
    assert serial[-2:] == ["/r/tests/test_s.py", "/r/tests/test_n.py::TestN::test_one"]
    assert "--timeout=180" in serial and "not Q" in serial and "--ignore=/r/tests/test_x.py" in serial


def test_shape_drops_a_phase_without_files_instead_of_running_the_whole_tree(tmp_path):
    done, args_dir = shaped(tmp_path, ["tests/test_a.py"])
    assert done.stdout.split() == ["parallel"] and not (args_dir / "serial.args").exists()
    (tmp_path / "two").mkdir()
    done, args_dir = shaped(tmp_path / "two", ["tests/test_s.py"])
    assert done.stdout.split() == ["serial"] and not (args_dir / "parallel.args").exists()


def test_shape_refuses_a_vector_without_the_test_tree_and_a_selection_that_does_not_run(tmp_path):
    args_dir = tmp_path / "args"
    args_dir.mkdir()
    vector(args_dir / "parallel.args", ["-q", "/elsewhere/tests"])
    sel = tmp_path / "sel.json"
    sel.write_text(json.dumps(impact_selection(["tests/test_a.py"], universe=50)), encoding="utf-8")
    cmd = [sys.executable, str(_CI_DIR / "impact_shadow.py"), "shape", "--selection", str(sel), "--args-dir", str(args_dir), "--root"]
    assert subprocess.run([*cmd, "/r"], capture_output=True, text=True).returncode != 0
    sel.write_text(json.dumps(impact_selection(["tests/test_a.py"], universe=1, excluded_count=0)), encoding="utf-8")
    assert subprocess.run([*cmd, "/elsewhere"], capture_output=True, text=True).returncode != 0


def test_plan_prints_run_or_the_reason(tmp_path):
    sel = tmp_path / "sel.json"
    plan = [sys.executable, str(_CI_DIR / "impact_shadow.py"), "plan", "--selection", str(sel)]
    assert subprocess.run(plan, capture_output=True, text=True).stdout.strip() == "no_selection"
    sel.write_text(json.dumps(impact_selection(["tests/test_a.py"], universe=50)), encoding="utf-8")
    assert subprocess.run(plan, capture_output=True, text=True).stdout.strip() == "run"
