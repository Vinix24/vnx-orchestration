"""Tests for scripts/ci/check_xdist_parity.py (track ci-profile-a-xdist, D2): the contract.

Synthetic evidence for every edge case of "The comparison contract (D2)": a
reference set (the serial Profile A) and a candidate set (the shadow's serial
and parallel phases), each a collection manifest plus a JUnit XML in the
shape pytest's junitxml writes. The real-output counterpart, produced by the
pinned pytest and pytest-xdist, is tests/test_ci_xdist_parity_real_output.py.

The JUnit keys of the synthetic XML come from the script's own key function;
that the function matches what pytest really writes is pinned by the
real-output tests.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from importlib import metadata
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "ci" / "check_xdist_parity.py"
ARGS_SCRIPT = REPO_ROOT / "scripts" / "ci" / "profile_a_pytest_args.sh"
RESOLVE_ROOT = REPO_ROOT / "scripts" / "lib" / "vnx_resolve_root.sh"
CONSTRAINTS = REPO_ROOT / "scripts" / "ci" / "profile_a_constraints.txt"


def _load(name: str, path: Path):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


cxp = _load("check_xdist_parity", SCRIPT)
KEY = cxp.junit_key_function()

PYTEST_VERSION = metadata.version("pytest")
PINS = {"pytest": PYTEST_VERSION, "pytest-xdist": "3.8.0", "pytest-timeout": "2.4.0"}
WORKSPACE = "/home/runner/work/vnx-orchestration/vnx-orchestration"

A = "tests/test_a.py::test_one"
B = "tests/test_a.py::TestK::test_two"
C = "tests/test_b.py::test_param[1-x y]"
D = "tests/test_b.py::test_skipped"
E = "tests/test_c.py::test_three"
ALL = [A, B, C, D, E]

PASS: tuple = ()
FAIL = (("failure", None, "AssertionError: boom"),)
XFAIL = (("skipped", "pytest.xfail", "known"),)
TEARDOWN_ERROR = (("error", None, 'failed on teardown with "RuntimeError: teardown boom"'),)
SETUP_ERROR = (("error", None, 'failed on setup with "RuntimeError: setup boom"'),)
COLLECTION_SKIP = (("skipped", None, "collection skipped"),)
COLLECTION_ERROR = (("error", None, "collection failure"),)


def skip(reason: str) -> tuple:
    return (("skipped", "pytest.skip", reason),)


def _testcase(nodeid: str, children: tuple) -> ET.Element:
    classname, name = KEY(nodeid)
    element = ET.Element("testcase", classname=classname, name=name, time="0.001")
    for tag, kind, message in children:
        child = ET.SubElement(element, tag, message=message)
        if kind is not None:
            child.set("type", kind)
        child.text = message
    return element


def write_xml(path: Path, testcases: list, suite_attrs: dict | None = None, stamp: dict | None = None) -> None:
    """A JUnit XML; *stamp* becomes the testsuite properties the manifest plugin writes (vnx_run_id, vnx_run_attempt)."""
    suite = ET.Element("testsuite", name="pytest", **(suite_attrs or {"tests": str(len(testcases))}))
    properties = {f"vnx_{k}": v for k, v in (stamp or {}).items() if v is not None}
    if properties:
        props = ET.SubElement(suite, "properties")
        for name, value in properties.items():
            ET.SubElement(props, "property", name=name, value=str(value))
    for nodeid, children in testcases:
        suite.append(_testcase(nodeid, children))
    root = ET.Element("testsuites", name="pytest tests")
    root.append(suite)
    path.write_text('<?xml version="1.0" encoding="utf-8"?>' + ET.tostring(root, encoding="unicode"), encoding="utf-8")


def manifest(phase: str, nodeids: list, *, mode: str | None = None, outcomes: list = (), versions: dict | None = None,
             workers: int = 2, agree: bool = True, reported: int | None = None, finished: bool = True) -> dict:
    mode = mode or cxp.PHASE_MODES[phase]
    xdist = None
    if mode == "xdist":
        xdist = {"dist": "loadfile", "workers_reported": workers if reported is None else reported,
                 "workers_agree": agree, "workers": {}}
    return {
        "schema": cxp.MANIFEST_SCHEMA, "phase": phase, "phase_active": True, "mode": mode,
        "tool_versions": dict(versions or PINS), "worker_count": workers if mode == "xdist" else 0,
        "xdist": xdist, "collection_finished": finished, "exitstatus": 0,
        "nodeid_count": len(nodeids), "nodeids": list(nodeids), "collection_outcomes": [dict(o) for o in outcomes],
    }


def inactive(phase: str = "serial") -> dict:
    return {"schema": cxp.MANIFEST_SCHEMA, "phase": phase, "phase_active": False, "tool_versions": dict(PINS),
            "worker_count": 0, "nodeid_count": 0, "nodeids": [], "collection_outcomes": []}


def outcome(nodeid: str, state: str, reason: str = "r", reports: int = 1) -> dict:
    return {"nodeid": nodeid, "outcome": state, "reason": reason, "reports": reports}


def write_phase(directory: Path, phase: str, doc: dict | None, testcases: list | None, stamps) -> None:
    """One phase's manifest and XML, each with the run stamp *stamps(name)* gives it."""
    directory.mkdir(parents=True, exist_ok=True)
    if doc is not None:
        (directory / f"manifest-{phase}.json").write_text(json.dumps({**doc, **stamps(f"manifest-{phase}")}), encoding="utf-8")
    if testcases is not None:
        write_xml(directory / f"junit-{phase}.xml", testcases, stamp=stamps(f"junit-{phase}"))


def write_args(directory: Path, parallel: list, serial: list | None = None, stamp: dict | None = None) -> None:
    """An args emission as profile_a_pytest_args.sh writes it, with its run.json when *stamp* is given."""
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "parallel.args").write_bytes(b"".join(a.encode() + b"\0" for a in parallel))
    if serial is not None:
        (directory / "serial.args").write_bytes(b"".join(a.encode() + b"\0" for a in serial))
    if stamp is not None:
        (directory / "run.json").write_text(json.dumps({"schema": "vnx-run-stamp/1", **stamp}), encoding="utf-8")


PARALLEL_ARGS = ["-q", f"{WORKSPACE}/tests", "-n", "4", "--dist", "loadfile"]


def baseline() -> list:
    return [(A, PASS), (B, PASS), (C, PASS), (D, skip("not here")), (E, PASS)]


class Evidence:
    """A reference and a candidate directory, written from the parts a test changes.

    Every input carries the run stamp of the run the parity job runs in
    (``self.run``), as the writers in CI give it: the manifests and the phase
    result as fields, the XML as testsuite properties, the args emission as
    ``args/run.json``, the shadow record as fields. ``self.stamp[name]``
    overrides the stamp of one input.
    """

    def __init__(self, tmp_path: Path) -> None:
        self.tmp = tmp_path
        self.reference = tmp_path / "reference"
        self.candidate = tmp_path / "candidate"
        self.constraints = tmp_path / "constraints.txt"
        self.constraints.write_text("".join(f"{k}=={v}\n" for k, v in PINS.items()), encoding="utf-8")
        self.ref_manifest = manifest("full", ALL)
        self.ref_cases = baseline()
        self.serial_manifest = inactive()
        self.serial_cases = None
        self.par_manifest = manifest("parallel", ALL)
        self.par_cases = baseline()
        self.steps = {name: "success" for name in cxp.PARITY_STEPS}
        self.upstream = {"profile_a": "success", "shadow": "success"}
        self.profile = "heavy"
        self.shadow_record = {"verdict": "shadow_passed", "profile": "heavy",
                              "durations_s": {"serial": None, "parallel": 612}, "steps": {}}
        self.reference_result = {"schema": "vnx-phase-result/1", "phase": "full", "exit": 0, "duration_s": 2110}
        self.run = {"run_id": "99", "run_attempt": "1"}
        self.stamp = {}

    def stamp_of(self, name: str) -> dict:
        return {**self.run, **self.stamp.get(name, {})}

    def attempt(self, value: str) -> None:
        """The parity job and, unless a test overrides one, every input run in attempt *value*."""
        self.run["run_attempt"] = value

    def write(self) -> None:
        write_phase(self.reference, "full", self.ref_manifest, self.ref_cases, self.stamp_of)
        write_phase(self.candidate, "serial", self.serial_manifest, self.serial_cases, self.stamp_of)
        write_phase(self.candidate, "parallel", self.par_manifest, self.par_cases, self.stamp_of)
        write_args(self.candidate / "args", PARALLEL_ARGS, stamp=self.stamp_of("args"))
        if self.reference_result is not None:
            (self.reference / "result-full.json").write_text(
                json.dumps({**self.reference_result, **self.stamp_of("result-full")}), encoding="utf-8")
        if self.shadow_record is not None:
            (self.candidate / "xdist-shadow.json").write_text(
                json.dumps({**self.shadow_record, **self.stamp_of("shadow")}), encoding="utf-8")

    def own_args(self, directory: Path, vector: list = PARALLEL_ARGS) -> Path:
        """The parity job's own emission, stamped like the rest."""
        write_args(directory, vector, stamp=self.stamp_of("own-args"))
        return directory

    def inputs(self, **overrides):
        values = dict(
            reference_dir=self.reference, candidate_dir=self.candidate, constraints=self.constraints,
            args_dir=None, workspace_root=WORKSPACE, profile=self.profile,
            meta={"head_sha": "abc123", "tested_sha": "def456", **self.run, "event": "pull_request", "ref": "dispatch/x"},
            steps=self.steps, upstream=self.upstream,
        )
        values.update(overrides)
        return cxp.CompareInputs(**values)

    def verdict(self, **overrides) -> dict:
        self.write()
        return cxp.evaluate(self.inputs(**overrides))


@pytest.fixture
def ev(tmp_path) -> Evidence:
    return Evidence(tmp_path)


def _reasons(verdict: dict) -> set:
    return {p["reason"] for p in verdict["invalid_evidence"]}


def _only(verdict: dict, kind: str) -> list:
    """The entries of *kind*, after asserting that every other difference list is empty."""
    others = {k: v for k, v in verdict["differences"].items() if k != kind and v}
    assert not others, others
    return verdict["differences"][kind]


# --- exit 0 -----------------------------------------------------------------------------------


def test_identical_sets_are_parity(ev):
    verdict = ev.verdict()
    assert verdict["exit"] == 0 and verdict["verdict"] == "parity", verdict["invalid_evidence"]
    assert all(not entries for entries in verdict["differences"].values())
    assert verdict["counts"]["reference"]["classes"] == {"fail": 0, "xfail": 0, "skip": 1, "pass": 4}
    assert verdict["counts"]["parallel"]["executed"] == 5


def test_a_red_test_red_in_both_sets_is_parity(ev):
    ev.ref_cases[0] = (A, FAIL)
    ev.par_cases[0] = (A, FAIL)
    assert ev.verdict()["exit"] == 0


def test_the_summary_attributes_of_the_xml_are_never_read(ev):
    ev.write()
    write_xml(ev.candidate / "junit-parallel.xml", ev.par_cases,
              {"tests": "26145", "skipped": "0", "errors": "7", "failures": "3"}, stamp=ev.stamp_of("junit-parallel"))
    assert cxp.evaluate(ev.inputs())["exit"] == 0


# --- executions ---------------------------------------------------------------------------------


def test_a_call_failure_plus_teardown_error_is_one_execution(ev):
    ev.ref_cases[1] = (B, FAIL)
    ev.ref_cases.append((B, TEARDOWN_ERROR))
    ev.par_cases[1] = (B, FAIL)
    ev.par_cases.insert(3, (B, TEARDOWN_ERROR))  # xdist interleaves the second record
    verdict = ev.verdict()
    assert verdict["exit"] == 0, verdict["differences"]
    assert verdict["counts"]["parallel"]["records"] == 6
    assert verdict["counts"]["parallel"]["executed"] == 5
    assert verdict["counts"]["parallel"]["classes"]["fail"] == 1


def test_the_m16_pair_has_the_class_of_a_plain_failure(ev):
    ev.ref_cases[1] = (B, FAIL)
    ev.par_cases[1] = (B, FAIL)
    ev.par_cases.append((B, TEARDOWN_ERROR))
    assert ev.verdict()["exit"] == 0


def test_a_pass_with_a_teardown_error_is_one_failing_execution(ev):
    ev.par_cases[1] = (B, TEARDOWN_ERROR)
    entries = _only(ev.verdict(), "outcome_class_differs")
    assert entries == [{"nodeid": B, "key": list(KEY(B)), "reference": "pass", "candidate": "fail"}]


def test_two_call_failures_each_with_a_teardown_error_are_two_executions(ev):
    ev.par_cases[1] = (B, FAIL)
    ev.par_cases += [(B, FAIL), (B, TEARDOWN_ERROR), (B, TEARDOWN_ERROR)]
    ev.ref_cases[1] = (B, FAIL)
    entries = _only(ev.verdict(), "run_more_than_once")
    assert entries == [{"nodeid": B, "key": list(KEY(B)), "executions": {"parallel": 2}}]


def test_two_call_records_of_one_key_in_one_phase_is_run_more_than_once(ev):
    ev.par_cases.append((A, PASS))
    verdict = ev.verdict()
    assert verdict["exit"] == 1
    assert _only(verdict, "run_more_than_once") == [{"nodeid": A, "key": list(KEY(A)), "executions": {"parallel": 2}}]


def test_a_setup_error_is_a_failing_execution(ev):
    ev.par_cases[0] = (A, SETUP_ERROR)
    assert _only(ev.verdict(), "outcome_class_differs")[0]["candidate"] == "fail"


# --- differences --------------------------------------------------------------------------------


def test_a_test_not_run_in_the_candidate(ev):
    ev.par_cases = [c for c in ev.par_cases if c[0] != E]
    verdict = ev.verdict()
    assert verdict["exit"] == 1 and verdict["verdict"] == "differences"
    assert _only(verdict, "not_run_in_candidate") == [{"nodeid": E, "key": list(KEY(E))}]


def test_a_test_run_in_the_candidate_and_absent_from_the_reference(ev):
    ev.ref_cases = [c for c in ev.ref_cases if c[0] != C]
    assert _only(ev.verdict(), "extra_in_candidate") == [{"nodeid": C, "key": list(KEY(C))}]


@pytest.mark.parametrize(
    ("reference", "candidate", "expected"),
    [
        (PASS, FAIL, ("pass", "fail")),
        (FAIL, PASS, ("fail", "pass")),
        (XFAIL, PASS, ("xfail", "pass")),
        (PASS, XFAIL, ("pass", "xfail")),
    ],
    ids=["pass-to-fail", "fail-to-pass", "xfail-to-pass", "pass-to-xfail"],
)
def test_an_outcome_class_change(ev, reference, candidate, expected):
    ev.ref_cases[0] = (A, reference)
    ev.par_cases[0] = (A, candidate)
    [entry] = _only(ev.verdict(), "outcome_class_differs")
    assert (entry["reference"], entry["candidate"]) == expected


def test_a_test_skipped_only_in_the_candidate_is_a_skip_set_difference(ev):
    ev.par_cases[0] = (A, skip("port 8080 in use"))
    [entry] = _only(ev.verdict(), "skip_set_differs")
    assert entry == {"nodeid": A, "key": list(KEY(A)), "reference": "pass", "candidate": "skip",
                     "reference_reason": None, "candidate_reason": "port 8080 in use"}


def test_a_test_skipped_only_in_the_reference_is_a_skip_set_difference(ev):
    ev.par_cases[3] = (D, PASS)
    [entry] = _only(ev.verdict(), "skip_set_differs")
    assert (entry["reference"], entry["candidate"]) == ("skip", "pass")


def test_skip_reasons_are_carried_but_not_compared(ev):
    # Measured on run 38951854817: reasons differ per process only in temp paths.
    ev.ref_cases[3] = (D, skip("real receipt trail not found at /tmp/vnx_conftest_i9bnmeu0/state"))
    ev.par_cases[3] = (D, skip("real receipt trail not found at /tmp/vnx_conftest_qzioc9vr/state"))
    assert ev.verdict()["exit"] == 0


def test_a_collection_time_skip_in_both_sets_is_no_difference(ev):
    skipped = outcome("tests/test_mod.py", "skipped", "Skipped: could not import 'x'")
    ev.ref_manifest["collection_outcomes"] = [skipped]
    ev.par_manifest["collection_outcomes"] = [skipped]
    ev.ref_cases.append(("tests/test_mod.py", COLLECTION_SKIP))
    ev.par_cases.append(("tests/test_mod.py", COLLECTION_SKIP))
    verdict = ev.verdict()
    assert verdict["exit"] == 0, verdict
    assert verdict["counts"]["reference"]["collection_records"] == 1


def test_a_collection_time_skip_in_one_set_only(ev):
    ev.ref_manifest["collection_outcomes"] = [outcome("tests/test_mod.py", "skipped")]
    ev.ref_cases.append(("tests/test_mod.py", COLLECTION_SKIP))
    assert _only(ev.verdict(), "collection_outcome_differs") == [
        {"nodeid": "tests/test_mod.py", "reference": ["skipped"], "candidate": []}
    ]


def test_a_collection_outcome_that_changes_from_skip_to_error(ev):
    ev.ref_manifest["collection_outcomes"] = [outcome("tests/test_mod.py", "skipped")]
    ev.par_manifest["collection_outcomes"] = [outcome("tests/test_mod.py", "failed")]
    ev.ref_cases.append(("tests/test_mod.py", COLLECTION_SKIP))
    ev.par_cases.append(("tests/test_mod.py", COLLECTION_ERROR))
    assert _only(ev.verdict(), "collection_outcome_differs")[0]["candidate"] == ["failed"]


@pytest.mark.parametrize("shape", ["one-record-two-errors", "two-records"])
def test_a_collection_error_once_per_worker_is_one_collection_outcome(ev, shape):
    ev.ref_manifest["collection_outcomes"] = [outcome("tests/test_broken.py", "failed", reports=1)]
    ev.par_manifest["collection_outcomes"] = [outcome("tests/test_broken.py", "failed", reports=2)]
    ev.ref_cases.append(("tests/test_broken.py", COLLECTION_ERROR))
    if shape == "two-records":
        ev.par_cases += [("tests/test_broken.py", COLLECTION_ERROR)] * 2
    else:
        ev.par_cases.append(("tests/test_broken.py", COLLECTION_ERROR * 2))
    verdict = ev.verdict()
    assert verdict["exit"] == 0, verdict
    assert verdict["counts"]["parallel"]["collection_records"] == (2 if shape == "two-records" else 1)


def test_two_phases_merged_with_a_key_in_both(ev):
    ev.serial_manifest = manifest("serial", [A, B])
    ev.serial_cases = [(A, PASS), (B, PASS)]
    verdict = ev.verdict()
    assert verdict["exit"] == 1
    assert {e["nodeid"]: e["executions"] for e in verdict["differences"]["run_more_than_once"]} == {
        A: {"serial": 1, "parallel": 1}, B: {"serial": 1, "parallel": 1}
    }
    assert {(e["nodeid"], e["problem"]) for e in verdict["differences"]["partition"]} == {
        (A, "in both candidate phases"), (B, "in both candidate phases")
    }


def test_a_serial_phase_that_partitions_the_selection_is_parity(ev):
    ev.serial_manifest = manifest("serial", [C, D])
    ev.serial_cases = [(C, PASS), (D, skip("not here"))]
    ev.par_manifest = manifest("parallel", [A, B, E])
    ev.par_cases = [(A, PASS), (B, PASS), (E, PASS)]
    verdict = ev.verdict()
    assert verdict["exit"] == 0, verdict["differences"]
    assert verdict["counts"]["serial"]["executed"] == 2


def test_a_broken_partition_names_the_test_in_no_phase(ev):
    ev.par_manifest = manifest("parallel", [A, B, C, D])
    ev.par_cases = [c for c in ev.par_cases if c[0] != E]
    differences = ev.verdict()["differences"]
    assert differences["partition"] == [{"nodeid": E, "problem": "in no candidate phase"}]
    assert [e["nodeid"] for e in differences["not_run_in_candidate"]] == [E]


def test_a_candidate_phase_outside_the_selection_breaks_the_partition(ev):
    extra = "tests/test_d.py::test_new"
    ev.par_manifest = manifest("parallel", ALL + [extra])
    ev.par_cases.append((extra, PASS))
    differences = ev.verdict()["differences"]
    assert differences["partition"] == [{"nodeid": extra, "problem": "not in the reference selection"}]
    assert [e["nodeid"] for e in differences["extra_in_candidate"]] == [extra]


def test_a_collection_outcome_in_both_candidate_phases_breaks_the_partition(ev):
    shared = outcome("tests", "failed")
    ev.ref_manifest["collection_outcomes"] = [shared]
    ev.serial_manifest = manifest("serial", [], outcomes=[shared])
    ev.serial_cases = [("tests", COLLECTION_ERROR)]
    ev.par_manifest["collection_outcomes"] = [shared]
    ev.ref_cases.append(("tests", COLLECTION_ERROR))
    ev.par_cases.append(("tests", COLLECTION_ERROR))
    assert ev.verdict()["differences"]["partition"] == [
        {"nodeid": "tests", "problem": "collection outcome in both candidate phases"}
    ]


def test_an_inactive_serial_phase_needs_no_xml(ev):
    verdict = ev.verdict()
    assert not (ev.candidate / "junit-serial.xml").exists()
    assert verdict["exit"] == 0
    assert verdict["counts"]["serial"]["active"] is False


def test_records_under_an_inactive_serial_phase_are_invalid(ev):
    ev.serial_cases = [(A, PASS)]
    assert "record_outside_manifest" in _reasons(ev.verdict())


# --- invalid evidence (exit 2) -----------------------------------------------------------------------


def test_two_nodeids_on_one_junit_key_is_a_collision(ev):
    twin_a, twin_b = "tests/a/b.py::test_x", "tests/a.b.py::test_x"
    assert KEY(twin_a) == KEY(twin_b)
    ev.ref_manifest = manifest("full", ALL + [twin_a, twin_b])
    ev.par_manifest = manifest("parallel", ALL + [twin_a, twin_b])
    verdict = ev.verdict()
    assert verdict["exit"] == 2 and verdict["verdict"] == "invalid_evidence"
    assert "key_collision" in _reasons(verdict)


def test_a_nodeid_listed_twice_in_one_manifest_is_a_collision(ev):
    ev.par_manifest = manifest("parallel", ALL + [A])
    assert "key_collision" in _reasons(ev.verdict())


def test_a_collection_outcome_on_a_test_key_is_a_collision(ev):
    ev.par_manifest["collection_outcomes"] = [outcome(A, "failed")]
    assert "key_collision" in _reasons(ev.verdict())


def _drop(path: Path) -> None:
    path.unlink()


def _empty(path: Path) -> None:
    path.write_text("", encoding="utf-8")


def _garble(path: Path) -> None:
    path.write_text("<testsuites><testcase", encoding="utf-8")


def _not_a_manifest(path: Path) -> None:
    path.write_text(json.dumps({"schema": "something-else/1"}), encoding="utf-8")


@pytest.mark.parametrize(
    ("relative", "damage", "reason"),
    [
        ("reference/junit-full.xml", _drop, "missing_xml"),
        ("candidate/junit-parallel.xml", _empty, "empty_xml"),
        ("candidate/junit-parallel.xml", _garble, "unparsable_xml"),
        ("reference/manifest-full.json", _drop, "missing_manifest"),
        ("candidate/manifest-serial.json", _drop, "missing_manifest"),
        ("candidate/manifest-parallel.json", _empty, "empty_manifest"),
        ("candidate/manifest-parallel.json", _garble, "unparsable_manifest"),
        ("reference/manifest-full.json", _not_a_manifest, "unparsable_manifest"),
    ],
    ids=["ref-xml-missing", "par-xml-empty", "par-xml-unparsable", "ref-manifest-missing",
         "serial-manifest-missing", "par-manifest-empty", "par-manifest-unparsable", "ref-manifest-foreign"],
)
def test_missing_empty_or_unparsable_evidence(ev, relative, damage, reason):
    ev.write()
    damage(ev.tmp / relative)
    verdict = cxp.evaluate(ev.inputs())
    assert verdict["exit"] == 2
    assert reason in _reasons(verdict)
    assert verdict["differences"] is None


def test_an_active_serial_phase_without_xml_is_invalid(ev):
    ev.serial_manifest = manifest("serial", [C])
    ev.par_manifest = manifest("parallel", [A, B, D, E])
    assert "missing_xml" in _reasons(ev.verdict())


def test_a_record_outside_the_manifest(ev):
    ev.par_cases.append(("tests/test_zz.py::test_ghost", PASS))
    verdict = ev.verdict()
    assert verdict["exit"] == 2
    [problem] = [p for p in verdict["invalid_evidence"] if p["reason"] == "record_outside_manifest"]
    assert "tests.test_zz::test_ghost" in problem["detail"]


def test_an_internal_error_record_is_outside_the_manifest(ev):
    ev.write()
    path = ev.candidate / "junit-parallel.xml"
    path.write_text(path.read_text(encoding="utf-8").replace(
        "</testsuite>", '<testcase classname="pytest" name="internal"><error message="internal error" /></testcase></testsuite>'
    ), encoding="utf-8")
    assert "record_outside_manifest" in _reasons(cxp.evaluate(ev.inputs()))


def test_workers_that_disagree(ev):
    ev.par_manifest = manifest("parallel", ALL, agree=False)
    assert "workers_disagree" in _reasons(ev.verdict())


def test_a_worker_that_reported_no_collection(ev):
    ev.par_manifest = manifest("parallel", ALL, workers=4, reported=3)
    assert "workers_missing" in _reasons(ev.verdict())


def test_a_tool_version_that_differs_from_the_constraints(ev):
    ev.par_manifest = manifest("parallel", ALL, versions={**PINS, "pytest-xdist": "3.7.0"})
    verdict = ev.verdict()
    assert verdict["exit"] == 2
    [problem] = verdict["invalid_evidence"]
    assert problem["reason"] == "tool_version_mismatch" and "3.7.0" in problem["detail"]


def test_the_parity_check_itself_must_run_the_pinned_pytest(ev):
    ev.constraints.write_text("pytest==0.0.1\npytest-xdist==3.8.0\npytest-timeout==2.4.0\n", encoding="utf-8")
    assert "parity_tool_version_mismatch" in _reasons(ev.verdict())


@pytest.mark.parametrize(
    ("which", "change", "reason"),
    [
        ("ref", {"mode": "xdist"}, "manifest_mode_mismatch"),
        ("par", {"mode": "serial", "xdist": None}, "manifest_mode_mismatch"),
        ("ref", {"phase": "parallel"}, "manifest_phase_mismatch"),
        ("par", {"collection_finished": False}, "collection_unfinished"),
        ("par", {"phase_active": False}, "inactive_phase"),
    ],
    ids=["ref-under-xdist", "par-serial", "ref-wrong-phase", "par-unfinished", "par-inactive"],
)
def test_a_manifest_of_the_wrong_shape(ev, which, change, reason):
    target = ev.ref_manifest if which == "ref" else ev.par_manifest
    target.update(change)
    assert reason in _reasons(ev.verdict())


@pytest.mark.parametrize("step", ["download_reference", "download_candidate"])
def test_a_failed_download_is_invalid_evidence(ev, step):
    ev.steps[step] = "failure"
    verdict = ev.verdict()
    assert verdict["exit"] == 2
    assert {"reason": "download_failed", "detail": f"{step}=failure"} in verdict["invalid_evidence"]
    assert f"parity.{step}=failure" in verdict["step_failures"]


def test_without_any_emitted_args_the_fingerprint_is_unavailable(ev):
    ev.write()
    shutil.rmtree(ev.candidate / "args")
    verdict = cxp.evaluate(ev.inputs())
    assert verdict["selection_fingerprint"] is None
    assert "fingerprint_unavailable" in _reasons(verdict)


def test_the_shadow_ran_another_selection_than_this_commit_emits(ev, tmp_path):
    verdict = ev.verdict(args_dir=ev.own_args(tmp_path / "own-args", PARALLEL_ARGS + ["--deselect=tests/test_c.py"]))
    assert "fingerprint_mismatch" in _reasons(verdict)


# --- stale evidence: every input proves the run and attempt it is counted for -----------------------------------


def _stale_details(verdict: dict) -> list:
    return [p["detail"] for p in verdict["invalid_evidence"] if p["reason"] == "stale_evidence"]


def test_evidence_that_proves_attempt_2_is_parity(ev, tmp_path):
    ev.attempt("2")
    verdict = ev.verdict(args_dir=ev.own_args(tmp_path / "own-args"))
    assert verdict["exit"] == 0 and verdict["invalid_evidence"] == [] and verdict["step_failures"] == []


def test_a_shadow_record_of_an_earlier_attempt_is_stale_evidence(ev):
    """codex round 2: parity attempt 2 with the shadow record of attempt 1 gave exit 0."""
    ev.attempt("2")
    ev.stamp["shadow"] = {"run_attempt": "1"}
    verdict = ev.verdict()
    assert verdict["exit"] == 2 and verdict["verdict"] == "invalid_evidence"
    assert _stale_details(verdict) == ["shadow record xdist-shadow.json of attempt 1, not of attempt 2"]
    assert "shadow.record=stale" in verdict["step_failures"]
    assert verdict["shadow"].get("suite_wall_s") is None, "nothing is read from a stale shadow record"


# input -> the name its stale_evidence detail carries
STAMPED_INPUTS = {
    "shadow": "shadow record xdist-shadow.json",
    "result-full": "result-full.json",
    "manifest-full": "manifest-full.json",
    "junit-full": "junit-full.xml",
    "manifest-serial": "manifest-serial.json",
    "junit-serial": "junit-serial.xml",
    "manifest-parallel": "manifest-parallel.json",
    "junit-parallel": "junit-parallel.xml",
    "args": "shadow args/run.json",
    "own-args": "parity-job args/run.json",
}
STALE_STAMPS = {
    "earlier-attempt": ({"run_attempt": "1"}, "of attempt 1, not of attempt 2"),
    "no-attempt": ({"run_attempt": None}, "names no run_attempt"),
    "other-run": ({"run_id": "98"}, "of run 98, not of run 99"),
}


def _two_phase(ev: Evidence) -> None:
    """An active serial phase, so all six phase files are evidence."""
    ev.serial_manifest = manifest("serial", [C, D])
    ev.serial_cases = [(C, PASS), (D, skip("not here"))]
    ev.par_manifest = manifest("parallel", [A, B, E])
    ev.par_cases = [(A, PASS), (B, PASS), (E, PASS)]


@pytest.mark.parametrize("stale", list(STALE_STAMPS))
@pytest.mark.parametrize("name", list(STAMPED_INPUTS))
def test_every_input_must_prove_its_run_and_attempt(ev, tmp_path, name, stale):
    _two_phase(ev)
    ev.attempt("2")
    stamp, phrase = STALE_STAMPS[stale]
    ev.stamp[name] = stamp
    verdict = ev.verdict(args_dir=ev.own_args(tmp_path / "own-args"))
    assert verdict["exit"] == 2 and verdict["verdict"] == "invalid_evidence", verdict["invalid_evidence"]
    [detail] = _stale_details(verdict)
    assert STAMPED_INPUTS[name] in detail and phrase in detail, detail


def test_the_two_phase_shape_proves_attempt_2_when_every_input_does(ev, tmp_path):
    _two_phase(ev)
    ev.attempt("2")
    verdict = ev.verdict(args_dir=ev.own_args(tmp_path / "own-args"))
    assert verdict["exit"] == 0, verdict["invalid_evidence"]


def test_a_stale_phase_is_not_compared(ev):
    ev.attempt("2")
    ev.stamp["manifest-parallel"] = {"run_attempt": "1"}
    ev.par_cases[0] = (A, FAIL)
    verdict = ev.verdict()
    assert verdict["differences"] is None and verdict["counts"]["parallel"] is None


def test_a_stale_shadow_record_is_not_read_for_the_profile(ev):
    """Without its own profile the job takes the shadow's, but only from a shadow record of this attempt."""
    ev.profile = ""
    ev.attempt("2")
    ev.shadow_record["profile"] = "light"
    ev.stamp["shadow"] = {"run_attempt": "1"}
    verdict = ev.verdict()
    assert verdict["verdict"] != "not_applicable" and verdict["profile"] == "unknown"
    assert verdict["exit"] == 2


def test_a_stale_reference_result_shows_no_exit(ev):
    ev.attempt("2")
    ev.stamp["result-full"] = {"run_attempt": "1"}
    ev.reference_result["exit"] = 1
    verdict = ev.verdict()
    assert verdict["reference"] == {"job_result": "success", "result": "stale", "exit": None, "duration_s": None}


def test_a_stale_shadow_emission_does_not_give_the_fingerprint(ev):
    ev.attempt("2")
    ev.stamp["args"] = {"run_attempt": "1"}
    verdict = ev.verdict()
    assert verdict["selection_fingerprint"] is None and verdict["selection_fingerprint_source"] is None
    assert "fingerprint_unavailable" in _reasons(verdict)


@pytest.mark.parametrize("own", [{"run_attempt": ""}, {"run_id": ""}, {"run_attempt": "0"}],
                         ids=["no-attempt", "no-run-id", "attempt-0"])
def test_a_parity_job_that_names_no_run_proves_no_evidence_current(ev, own):
    verdict = ev.verdict(meta={"head_sha": "abc123", **ev.run, **own})
    assert verdict["exit"] == 2
    details = _stale_details(verdict)
    assert details and all(d.startswith("no run to hold") for d in details), details


# --- malformed evidence is exit 2 with a reason, never a crash -------------------------------------------------------


@pytest.mark.parametrize(("field", "value", "reason"), [
    ("worker_count", "four", "malformed_manifest"),
    ("worker_count", None, "malformed_manifest"),
    ("worker_count", True, "malformed_manifest"),
    ("xdist", ["gw0", "gw1"], "malformed_manifest"),
    ("tool_versions", ["pytest"], "malformed_manifest"),
    ("collection_outcomes", ["tests/test_mod.py"], "unparsable_manifest"),
], ids=["worker-count-text", "worker-count-null", "worker-count-bool", "xdist-list", "versions-list", "outcome-text"])
def test_a_malformed_manifest_field_is_invalid_evidence(ev, field, value, reason):
    ev.par_manifest[field] = value
    verdict = ev.verdict()
    assert verdict["exit"] == 2
    assert reason in _reasons(verdict), verdict["invalid_evidence"]


def test_a_non_numeric_worker_count_exits_2_from_the_cli_not_1_with_a_traceback(ev):
    """deepseek round 2: int(worker_count) raised ValueError out of _xdist_problems: exit 1, a traceback, no verdict."""
    ev.par_manifest["worker_count"] = "four"
    ev.write()
    output = ev.tmp / "out" / cxp.VERDICT_FILE
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "compare", "--reference", str(ev.reference), "--candidate", str(ev.candidate),
         "--constraints", str(ev.constraints), "--output", str(output), "--workspace-root", WORKSPACE,
         "--profile", "heavy", "--run-id", ev.run["run_id"], "--run-attempt", ev.run["run_attempt"]],
        capture_output=True, text=True,
    )
    assert proc.returncode == 2, proc.stderr
    assert "Traceback" not in proc.stderr
    problems = json.loads(output.read_text(encoding="utf-8"))["invalid_evidence"]
    assert {"reason": "malformed_manifest", "detail": "parallel: worker_count 'four' is not a whole number"} in problems


_ABSENT = object()


@pytest.mark.parametrize(("field", "value"), [
    ("nodeid", ["tests/test_mod.py"]),
    ("nodeid", _ABSENT),
    ("nodeid", 7),
    ("outcome", _ABSENT),
    ("outcome", "passed"),
    ("outcome", ["skipped"]),
    ("reason", _ABSENT),
    ("reason", 3),
    ("reports", _ABSENT),
    ("reports", "1"),
    ("reports", 0),
    ("reports", True),
], ids=["nodeid-list", "nodeid-missing", "nodeid-number", "outcome-missing", "outcome-passed", "outcome-list",
        "reason-missing", "reason-number", "reports-missing", "reports-text", "reports-0", "reports-bool"])
def test_every_field_of_a_collection_outcome_is_validated_before_any_comparison(ev, field, value):
    """codex/deepseek round 3: a collection outcome passed validation with any field; the comparison read it raw.

    The same outcome in the reference and the candidate: nothing differs, so
    only the validation can keep this from being parity.
    """
    bad = outcome("tests/test_mod.py", "skipped")
    if value is _ABSENT:
        del bad[field]
    else:
        bad[field] = value
    ev.ref_manifest["collection_outcomes"] = [bad]
    ev.par_manifest["collection_outcomes"] = [bad]
    verdict = ev.verdict()
    assert verdict["exit"] == 2 and verdict["differences"] is None, verdict["invalid_evidence"]
    details = [p["detail"] for p in verdict["invalid_evidence"] if p["reason"] == "malformed_manifest"]
    assert len(details) == 2 and all(f"collection outcome 0: {field} " in d for d in details), details
    assert verdict["counts"]["reference"] is None and verdict["counts"]["parallel"] is None


def test_a_nodeid_that_is_not_a_string_is_malformed(ev):
    ev.par_manifest["nodeids"] = [A, B, C, D, ["tests/test_c.py::test_three"]]
    verdict = ev.verdict()
    assert verdict["exit"] == 2 and verdict["differences"] is None
    assert any(p["reason"] == "malformed_manifest" and "nodeid 4" in p["detail"] for p in verdict["invalid_evidence"])


def test_a_list_valued_collection_nodeid_exits_2_from_main_with_a_verdict(ev):
    """codex round 3: a list-valued nodeid raised TypeError in compare_partition: exit 1 and no verdict file."""
    ev.par_manifest["collection_outcomes"] = [{**outcome("tests/test_mod.py", "skipped"), "nodeid": ["tests/test_mod.py"]}]
    code, verdict, summary = _cli(ev)
    assert code == 2 and verdict["exit"] == 2
    assert "malformed_manifest" in summary


def test_a_collection_outcome_without_nodeid_exits_2_from_main_with_a_verdict(ev):
    """deepseek round 3: no nodeid in both candidate phases put None next to a str in the partition sort: TypeError."""
    _two_phase(ev)
    ev.par_manifest = manifest("parallel", [A, B])
    ev.par_cases = [(A, PASS), (B, PASS)]
    nameless = {k: v for k, v in outcome("tests/test_mod.py", "skipped").items() if k != "nodeid"}
    ev.serial_manifest["collection_outcomes"] = [nameless]
    ev.par_manifest["collection_outcomes"] = [nameless]
    code, verdict, _ = _cli(ev)
    assert code == 2 and verdict["exit"] == 2 and verdict["differences"] is None
    assert {p["reason"] for p in verdict["invalid_evidence"]} == {"malformed_manifest"}


def test_constraints_that_are_not_utf8_are_invalid_evidence(ev):
    ev.write()
    ev.constraints.write_bytes(b"pytest==9.1.1\n\xff\xfe\n")
    verdict = cxp.evaluate(ev.inputs())
    assert verdict["exit"] == 2 and "unreadable_constraints" in _reasons(verdict)


@pytest.mark.parametrize(("durations", "wall"), [
    ("fast", None),
    ({"serial": "40", "parallel": 600}, None),
    ({"serial": None, "parallel": "600"}, None),
    ({"serial": 40, "parallel": 600}, 640),
], ids=["not-an-object", "serial-text", "parallel-text", "whole-numbers"])
def test_shadow_durations_of_the_wrong_type_give_no_wall_time(ev, durations, wall):
    ev.shadow_record["durations_s"] = durations
    assert ev.verdict()["shadow"]["suite_wall_s"] == wall


# --- the verdict -----------------------------------------------------------------------------------


def test_the_verdict_carries_the_run_and_the_selection(ev, tmp_path):
    verdict = ev.verdict(args_dir=ev.own_args(tmp_path / "own-args"))
    assert verdict["schema"] == "vnx-xdist-parity/1"
    for key, value in {"head_sha": "abc123", "tested_sha": "def456", "run_id": "99", "run_attempt": "1",
                       "event": "pull_request", "ref": "dispatch/x", "profile": "heavy"}.items():
        assert verdict[key] == value
    assert verdict["selection_fingerprint"] == cxp.selection_fingerprint(None, PARALLEL_ARGS, ev.constraints.read_text(), WORKSPACE)
    assert verdict["selection_fingerprint_source"] == "parity_job"
    assert verdict["steps"] == ev.steps
    assert verdict["tool_versions"] == PINS
    assert verdict["shadow"]["suite_wall_s"] == 612
    assert verdict["step_failures"] == []


def test_the_shadow_suite_wall_time_adds_both_phases(ev):
    ev.shadow_record["durations_s"] = {"serial": 40, "parallel": 600}
    assert ev.verdict()["shadow"]["suite_wall_s"] == 640


def test_step_failures_name_every_step_that_was_not_success(ev):
    ev.steps["prelude"] = "failure"
    ev.shadow_record["verdict"] = "shadow_failed"
    ev.upstream["shadow"] = "failure"
    verdict = ev.verdict()
    assert verdict["exit"] == 0
    assert verdict["step_failures"] == ["parity.prelude=failure", "shadow.verdict=shadow_failed", "shadow.job=failure"]


def test_a_missing_shadow_record_is_a_step_failure(ev):
    ev.shadow_record = None
    assert "shadow.record=missing" in ev.verdict()["step_failures"]


def test_a_light_profile_is_not_applicable(ev):
    ev.profile = "light"
    verdict = ev.verdict()
    assert verdict["verdict"] == "not_applicable" and verdict["exit"] is None
    assert verdict["differences"] is None and verdict["invalid_evidence"] == []


def test_without_its_own_profile_the_job_takes_the_shadows(ev):
    ev.profile = ""
    ev.shadow_record["profile"] = "light"
    assert ev.verdict()["verdict"] == "not_applicable"


def test_without_any_profile_the_comparison_still_runs(ev):
    ev.profile = ""
    ev.shadow_record = None
    verdict = ev.verdict()
    assert verdict["profile"] == "unknown" and verdict["exit"] == 0


def test_the_reference_block_reads_profile_as_exit_and_duration_from_its_phase_result(ev):
    ev.reference_result["exit"] = 1
    verdict = ev.verdict()
    assert verdict["reference"] == {"job_result": "success", "result": "present", "exit": 1, "duration_s": 2110}


def test_a_reference_without_a_phase_result_shows_no_exit(ev):
    ev.reference_result = None
    verdict = ev.verdict()
    assert verdict["reference"] == {"job_result": "success", "result": "missing", "exit": None, "duration_s": None}
    assert verdict["exit"] == 0


@pytest.mark.parametrize(("change", "failure"), [
    (lambda ev: ev.upstream.update(profile_a="failure"), "reference.job=failure"),
    (lambda ev: ev.upstream.update(profile_a="cancelled"), "reference.job=cancelled"),
    (lambda ev: ev.upstream.pop("profile_a"), "reference.job=unknown"),
    (lambda ev: ev.reference_result.update(exit=1), "reference.exit=1"),
    (lambda ev: ev.reference_result.update(exit=False), "reference.exit=None"),
    (lambda ev: setattr(ev, "reference_result", None), "reference.result=missing"),
], ids=["audit-after-upload", "cancelled", "no-job-result", "suite-red", "exit-bool", "no-phase-result"])
def test_a_failed_reference_run_is_a_step_failure(ev, change, failure):
    """codex round 3: Profile A failed an audit after uploading passing evidence: exit 0 and no step failure."""
    change(ev)
    verdict = ev.verdict()
    assert verdict["exit"] == 0, "the comparison itself is parity"
    assert failure in verdict["step_failures"], verdict["step_failures"]


def test_a_clean_reference_and_shadow_give_no_step_failure(ev):
    verdict = ev.verdict()
    assert verdict["reference"] == {"job_result": "success", "result": "present", "exit": 0, "duration_s": 2110}
    assert verdict["step_failures"] == []


# --- the CLI -----------------------------------------------------------------------------------------


def _cli(ev: Evidence, *extra: str) -> tuple:
    ev.write()
    output = ev.tmp / "out" / cxp.VERDICT_FILE
    summary = ev.tmp / "summary.md"
    code = cxp.main([
        "compare", "--reference", str(ev.reference), "--candidate", str(ev.candidate),
        "--constraints", str(ev.constraints), "--output", str(output), "--summary", str(summary),
        "--workspace-root", WORKSPACE, "--profile", ev.profile, "--head-sha", "abc123",
        "--run-id", ev.run["run_id"], "--run-attempt", ev.run["run_attempt"],
        "--step", "checkout=success", "--upstream", "shadow=success", *extra,
    ])
    return code, json.loads(output.read_text(encoding="utf-8")), summary.read_text(encoding="utf-8")


def test_the_cli_exits_with_the_verdict_and_writes_a_summary(ev):
    ev.par_cases[0] = (A, FAIL)
    code, verdict, summary = _cli(ev)
    assert code == 1 and verdict["exit"] == 1
    assert verdict["steps"] == {"checkout": "success"}
    assert verdict["shadow"]["job_result"] == "success" and verdict["head_sha"] == "abc123"
    assert "Verdict: **differences** (exit 1, profile heavy)" in summary
    assert "| outcome_class_differs | 1 |" in summary and A in summary


def test_the_cli_exits_0_on_a_light_run(ev):
    ev.profile = "light"
    code, verdict, summary = _cli(ev)
    assert code == 0 and verdict["verdict"] == "not_applicable"
    assert "not_applicable" in summary


def test_the_cli_exits_2_on_invalid_evidence(ev):
    code, verdict, summary = _cli(ev, "--step", "download_candidate=failure")
    assert code == 2 and "download_failed" in summary


@pytest.mark.parametrize("flag", ["--step", "--upstream"])
@pytest.mark.parametrize("value", ["no-equals-sign", "=success"], ids=["no-equals", "no-name"])
def test_the_cli_refuses_a_malformed_step_or_upstream_with_exit_2(ev, capsys, flag, value):
    """Invalid input exits 2 with a reason; it was SystemExit(<message>), which exits 1."""
    with pytest.raises(SystemExit) as exc:
        _cli(ev, flag, value)
    assert exc.value.code == 2
    assert f"argument {flag}: expects NAME=VALUE, got {value!r}" in capsys.readouterr().err


def test_the_cli_exits_2_with_a_reason_when_the_verdict_cannot_be_written(ev, capsys):
    ev.write()
    blocker = ev.tmp / "not-a-directory"
    blocker.write_text("", encoding="utf-8")
    code = cxp.main(["compare", "--reference", str(ev.reference), "--candidate", str(ev.candidate),
                     "--constraints", str(ev.constraints), "--output", str(blocker / cxp.VERDICT_FILE),
                     "--workspace-root", WORKSPACE, "--profile", "heavy",
                     "--run-id", ev.run["run_id"], "--run-attempt", ev.run["run_attempt"]])
    assert code == 2
    assert capsys.readouterr().err.startswith("check_xdist_parity: cannot write the verdict")


def test_the_cli_exits_2_with_a_reason_when_the_summary_cannot_be_appended(ev, capsys):
    ev.write()
    output = ev.tmp / "out" / cxp.VERDICT_FILE
    code = cxp.main(["compare", "--reference", str(ev.reference), "--candidate", str(ev.candidate),
                     "--constraints", str(ev.constraints), "--output", str(output),
                     "--summary", str(ev.tmp / "no-such-dir" / "summary.md"),
                     "--workspace-root", WORKSPACE, "--profile", "heavy",
                     "--run-id", ev.run["run_id"], "--run-attempt", ev.run["run_attempt"]])
    assert code == 2
    assert capsys.readouterr().err.startswith("check_xdist_parity: cannot append the summary")
    assert json.loads(output.read_text(encoding="utf-8"))["exit"] == 0, "the verdict itself is written first"


# --- the selection fingerprint ---------------------------------------------------------------------------


def test_strip_root_replaces_whole_path_components_only():
    root = "/home/runner/work/x"
    assert cxp.strip_root(f"--ignore={root}/tests/a.py", root) == "--ignore=<workspace>/tests/a.py"
    assert cxp.strip_root(root, root) == "<workspace>"
    assert cxp.strip_root(f"{root}x/tests", root) == f"{root}x/tests"
    assert cxp.strip_root("-k", root) == "-k"


def test_two_roots_give_one_fingerprint():
    def vectors(root):
        return [f"{root}/tests/s.py"], ["-q", f"{root}/tests", f"--ignore={root}/tests/x.py", "-n", "4"]

    first = cxp.selection_fingerprint(*vectors("/home/runner/work/a/a"), "pytest==9.1.1\n", "/home/runner/work/a/a")
    second = cxp.selection_fingerprint(*vectors("/Users/v/dev/a"), "pytest==9.1.1\n", "/Users/v/dev/a/")
    assert first == second


def test_the_fingerprint_covers_the_serial_phase_and_the_constraints():
    parallel = ["-q", "/r/tests"]
    base = cxp.selection_fingerprint(None, parallel, "pytest==9.1.1\n", "/r")
    assert base != cxp.selection_fingerprint(["/r/tests/s.py"], parallel, "pytest==9.1.1\n", "/r")
    assert base != cxp.selection_fingerprint(None, parallel + ["--ignore=/r/tests/x.py"], "pytest==9.1.1\n", "/r")
    assert base != cxp.selection_fingerprint(None, parallel, "pytest==9.1.2\n", "/r")


def _env(**extra: str) -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("PYTEST_", "GIT_")) and k != "VNX_CI_XDIST"}
    env.update(extra)
    return env


def _root(tmp_path: Path, name: str, exclusions: str = "tests/test_gone.py  # OI-0\n", serial: str = "",
          script: str | None = None) -> Path:
    """A throwaway git repo with copies of the args script, its root helper and the selection files."""
    root = tmp_path / name
    (root / "scripts" / "ci").mkdir(parents=True)
    (root / "scripts" / "lib").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "scripts" / "ci" / ARGS_SCRIPT.name).write_text(script or ARGS_SCRIPT.read_text(encoding="utf-8"), encoding="utf-8")
    shutil.copy2(RESOLVE_ROOT, root / "scripts" / "lib" / RESOLVE_ROOT.name)
    shutil.copy2(CONSTRAINTS, root / "scripts" / "ci" / CONSTRAINTS.name)
    (root / "scripts" / "ci" / "test_exclusions.txt").write_text(exclusions, encoding="utf-8")
    (root / "scripts" / "ci" / "serial_tests.txt").write_text(serial, encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(root)], env=_env(), check=True)
    return root.resolve()


def _emitted_fingerprint(root: Path) -> str:
    out = root.parent / f"{root.name}-args"
    proc = subprocess.run(["bash", str(root / "scripts" / "ci" / ARGS_SCRIPT.name), str(out)],
                          env=_env(VNX_CI_XDIST="1"), capture_output=True, text=True, cwd=str(root))
    assert proc.returncode == 0, proc.stderr
    assert str(root) in (out / "parallel.args").read_text(encoding="utf-8"), "the vector carries the absolute root"
    return cxp.fingerprint_of_args_dir(out, root / "scripts" / "ci" / CONSTRAINTS.name, str(root))


def test_the_args_script_in_two_roots_gives_one_fingerprint(tmp_path):
    first = _emitted_fingerprint(_root(tmp_path, "first-checkout"))
    second = _emitted_fingerprint(_root(tmp_path / "elsewhere" / "deeper", "second-checkout"))
    assert first == second


def test_an_exclusion_entry_changes_the_fingerprint(tmp_path):
    plain = _emitted_fingerprint(_root(tmp_path, "plain"))
    more = _emitted_fingerprint(_root(tmp_path, "more", exclusions="tests/test_gone.py\ntests/test_also.py\n"))
    assert plain != more


def test_a_script_change_that_emits_the_same_vectors_keeps_the_fingerprint(tmp_path):
    edited = ARGS_SCRIPT.read_text(encoding="utf-8").replace(
        "set -euo pipefail\n", "set -euo pipefail\n# D4 adds the serial path here; the xdist vectors stay.\n", 1)
    assert _emitted_fingerprint(_root(tmp_path, "orig")) == _emitted_fingerprint(_root(tmp_path, "edit", script=edited))


def test_a_changed_k_filter_changes_the_fingerprint(tmp_path):
    edited = ARGS_SCRIPT.read_text(encoding="utf-8").replace(
        'QUARANTINE_K="not TestFlagGateClaude', 'QUARANTINE_K="not TestOtherQuarantine and not TestFlagGateClaude', 1)
    assert _emitted_fingerprint(_root(tmp_path, "orig")) != _emitted_fingerprint(_root(tmp_path, "edit", script=edited))


def test_the_fingerprint_cli(tmp_path, capsys):
    write_args(tmp_path / "args", PARALLEL_ARGS)
    code = cxp.main(["fingerprint", "--args-dir", str(tmp_path / "args"), "--constraints", str(CONSTRAINTS),
                     "--workspace-root", WORKSPACE])
    assert code == 0
    assert capsys.readouterr().out.strip() == cxp.selection_fingerprint(
        None, PARALLEL_ARGS, CONSTRAINTS.read_text(encoding="utf-8"), WORKSPACE)


def test_the_fingerprint_cli_refuses_a_missing_directory(tmp_path, capsys):
    code = cxp.main(["fingerprint", "--args-dir", str(tmp_path / "none"), "--constraints", str(CONSTRAINTS),
                     "--workspace-root", WORKSPACE])
    assert code == 2 and "cannot fingerprint" in capsys.readouterr().err


def test_read_pins_reads_the_real_constraints_file():
    assert cxp.read_pins(CONSTRAINTS) == {"pytest": "9.1.1", "pytest-xdist": "3.8.0", "pytest-timeout": "2.4.0"}
