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
