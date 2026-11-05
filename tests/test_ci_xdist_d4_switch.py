"""Tests for D4 of track ci-profile-a-xdist: Profile A switches to the two-phase xdist shape.

The switch (plan, "D4 — switch Profile A"): Profile A runs the serial list
under `-p no:xdist` and the rest under xdist; a required completeness step
reads the collection manifests and `xdist_expected_skips.txt`; the per-PR
shadow and parity jobs are gone (the nightly `vnx-ci-serial-truth.yml` keeps
the comparison); `vars.VNX_CI_XDIST=0` is the one-variable way back to a
fully serial Profile A. These tests hold each of those to the behaviour.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from importlib import metadata
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
PARITY = REPO_ROOT / "scripts" / "ci" / "check_xdist_parity.py"
ARGS_SCRIPT = REPO_ROOT / "scripts" / "ci" / "profile_a_pytest_args.sh"
CONSTRAINTS = REPO_ROOT / "scripts" / "ci" / "profile_a_constraints.txt"
EXPECTED_SKIPS = REPO_ROOT / "scripts" / "ci" / "xdist_expected_skips.txt"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "vnx-ci.yml"
NIGHTLY = REPO_ROOT / ".github" / "workflows" / "vnx-ci-serial-truth.yml"
WORKFLOW_DIR = REPO_ROOT / ".github" / "workflows"


def _load(name: str, path: Path):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


cxp = _load("check_xdist_parity", PARITY)
KEY = cxp.junit_key_function()

# The installed pytest derives the JUnit keys; the constraints must pin it
# (own_tool_problems), so a temp constraints file pins what is installed.
PINS = {"pytest": metadata.version("pytest"), "pytest-xdist": "3.8.0", "pytest-timeout": "2.4.0"}
RUN = {"run_id": "41119890499", "run_attempt": "1"}

A = "tests/test_a.py::test_one"
B = "tests/test_a.py::TestK::test_two"
C = "tests/test_b.py::test_param[1-x y]"
D = "tests/test_b.py::test_skipped"
E = "tests/test_c.py::test_three"
ALL = [A, B, C, D, E]
PASS: tuple = ()


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


def write_xml(path: Path, testcases: list, stamp: dict | None = None) -> None:
    suite = ET.Element("testsuite", name="pytest", tests=str(len(testcases)))
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


def manifest(phase: str, nodeids: list, *, versions: dict | None = None, workers: int = 2) -> dict:
    mode = cxp.PHASE_MODES[phase]
    xdist = None
    if mode == "xdist":
        xdist = {"dist": "loadfile", "workers_reported": workers, "workers_agree": True, "workers": {}}
    return {
        "schema": cxp.MANIFEST_SCHEMA, "phase": phase, "phase_active": True, "mode": mode,
        "tool_versions": dict(versions or PINS), "worker_count": workers if mode == "xdist" else 0,
        "xdist": xdist, "collection_finished": True, "exitstatus": 0,
        "nodeid_count": len(nodeids), "nodeids": list(nodeids), "collection_outcomes": [],
    }


def inactive(phase: str = "serial") -> dict:
    return {"schema": cxp.MANIFEST_SCHEMA, "phase": phase, "phase_active": False, "tool_versions": dict(PINS),
            "worker_count": 0, "nodeid_count": 0, "nodeids": [], "collection_outcomes": []}


class Completeness:
    """A candidate evidence dir, a constraints file and a skip set, in the shape a Profile A run leaves."""

    def __init__(self, tmp_path: Path) -> None:
        self.tmp = tmp_path
        self.candidate = tmp_path / "candidate"
        self.constraints = tmp_path / "constraints.txt"
        self.constraints.write_text("".join(f"{k}=={v}\n" for k, v in PINS.items()), encoding="utf-8")
        self.expected = tmp_path / "expected.txt"
        self.expected.write_text(f"# recorded from run 41119890499\n{D}  # not here\n", encoding="utf-8")
        self.serial_manifest = inactive()
        self.serial_cases = None
        self.parallel_manifest = manifest("parallel", ALL)
        self.parallel_cases = [(A, PASS), (B, PASS), (C, PASS), (D, skip("not here")), (E, PASS)]
        self.stamp = {**RUN}
        self.constraints_text = None

    def write(self) -> None:
        self.candidate.mkdir(parents=True, exist_ok=True)
        stamp = self.stamp
        for name, document in (("serial", self.serial_manifest), ("parallel", self.parallel_manifest)):
            if document is not None:
                (self.candidate / f"manifest-{name}.json").write_text(
                    json.dumps({**document, **stamp}), encoding="utf-8")
        if self.serial_cases is not None:
            write_xml(self.candidate / "junit-serial.xml", self.serial_cases, stamp=stamp)
        if self.parallel_cases is not None:
            write_xml(self.candidate / "junit-parallel.xml", self.parallel_cases, stamp=stamp)
        if self.constraints_text is not None:
            self.constraints.write_text(self.constraints_text, encoding="utf-8")

    def run(self, *extra: str, write: bool = True) -> tuple:
        if write:
            self.write()
        output = self.tmp / "verdict.json"
        argv = [sys.executable, str(PARITY), "completeness",
                "--candidate", str(self.candidate), "--constraints", str(self.constraints),
                "--expected-skips", str(self.expected), "--output", str(output),
                "--run-id", RUN["run_id"], "--run-attempt", RUN["run_attempt"], *extra]
        proc = subprocess.run(argv, capture_output=True, text=True, cwd=str(REPO_ROOT))
        return proc, (json.loads(output.read_text(encoding="utf-8")) if output.is_file() else None)


@pytest.fixture
def ev(tmp_path) -> Completeness:
    return Completeness(tmp_path)


def _reasons(verdict: dict) -> set:
    return {problem["reason"] for problem in verdict["invalid_evidence"]}


# --- the skip set: recorded keys pass, an unrecorded one fails -------------------------------


def test_a_run_whose_only_skip_is_recorded_is_complete(ev):
    proc, verdict = ev.run()
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert verdict["verdict"] == "complete" and verdict["exit"] == 0
    assert verdict["expected_skips"]["entries"] == 1
    assert all(not entries for entries in verdict["differences"].values()), verdict["differences"]


def test_a_skip_that_is_not_in_the_skip_set_fails_the_run(ev):
    ev.expected.write_text(f"{D}  # not here\n", encoding="utf-8")
    ev.parallel_cases = [(A, PASS), (B, PASS), (C, PASS), (D, skip("not here")), (E, skip("new skip"))]
    proc, verdict = ev.run()
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert verdict["verdict"] == "incomplete" and verdict["exit"] == 1
    [entry] = verdict["differences"]["unexpected_skip"]
    assert (entry["nodeid"], entry["reason"]) == (E, "new skip")
    assert not any(verdict["differences"][k] for k in cxp.COMPLETENESS_KINDS if k != "unexpected_skip")


def test_the_check_reads_a_key_as_a_set_not_as_a_prefix(ev):
    ev.expected.write_text(f"{A}\n", encoding="utf-8")
    ev.parallel_cases = [(A, PASS), (B, PASS), (C, PASS), (D, skip("not here")), (E, PASS)]
    proc, verdict = ev.run()
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert [entry["nodeid"] for entry in verdict["differences"]["unexpected_skip"]] == [D]


# --- fail-closed: missing, absent, malformed and unknown evidence never passes ----------------


def test_a_missing_skip_set_is_invalid_evidence(ev):
    ev.expected.unlink()
    proc, verdict = ev.run()
    assert proc.returncode == 2 and verdict["verdict"] == "invalid_evidence"
    assert "missing_expected_skips" in _reasons(verdict)


@pytest.mark.parametrize("line, problem", [
    ("tests/test_a.txt", "not a test file"),
    ("not-tests/test_a.py::test_one", "not a path under tests/"),
    ("tests/../test_a.py::test_one", "plain"),
    ("tests//test_a.py::test_one", "plain"),
    ("tests\\test_a.py::test_one", "not a path under tests/"),
])
def test_a_malformed_skip_entry_is_invalid_evidence(ev, line, problem):
    ev.expected.write_text(line + "\n", encoding="utf-8")
    proc, verdict = ev.run()
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "malformed_expected_skips" in _reasons(verdict)
    assert problem in verdict["invalid_evidence"][0]["detail"]


def test_a_missing_manifest_is_invalid_evidence(ev):
    ev.parallel_manifest = None
    proc, verdict = ev.run()
    assert proc.returncode == 2 and "missing_manifest" in _reasons(verdict)


def test_an_unparsable_manifest_is_invalid_evidence(ev):
    ev.write()
    (ev.candidate / "manifest-parallel.json").write_text("{not json", encoding="utf-8")
    proc, verdict = ev.run(write=False)
    assert proc.returncode == 2 and "unparsable_manifest" in _reasons(verdict)


def test_evidence_with_no_run_stamp_is_invalid(ev):
    ev.stamp = {}
    proc, verdict = ev.run()
    assert proc.returncode == 2 and "stale_evidence" in _reasons(verdict)


def test_evidence_of_another_attempt_is_invalid(ev):
    ev.stamp = {"run_id": RUN["run_id"], "run_attempt": "2"}
    proc, verdict = ev.run()
    assert proc.returncode == 2 and "stale_evidence" in _reasons(verdict)


def test_without_a_run_to_hold_the_evidence_against_nothing_passes(ev):
    ev.write()
    output = ev.tmp / "verdict.json"
    argv = [sys.executable, str(PARITY), "completeness",
            "--candidate", str(ev.candidate), "--constraints", str(ev.constraints),
            "--expected-skips", str(ev.expected), "--output", str(output)]
    proc = subprocess.run(argv, capture_output=True, text=True, cwd=str(REPO_ROOT))
    assert proc.returncode == 2, proc.stdout + proc.stderr
    verdict = json.loads(output.read_text(encoding="utf-8"))
    assert "stale_evidence" in _reasons(verdict)


def test_an_unreadable_constraints_file_is_invalid_evidence(ev):
    ev.constraints_text = "this is not a constraints file\n"
    proc, verdict = ev.run()
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "parity_tool_version_mismatch" in _reasons(verdict)


def test_a_pinned_pytest_that_is_not_the_installed_one_is_invalid_evidence(ev):
    ev.constraints_text = "".join(f"{k}=={v}\n" for k, v in PINS.items()).replace(
        f"pytest=={PINS['pytest']}", "pytest==99.0.0")
    ev.parallel_manifest = manifest("parallel", ALL, versions={**PINS, "pytest": "99.0.0"})
    proc, verdict = ev.run()
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "parity_tool_version_mismatch" in _reasons(verdict)


# --- the committed skip set -------------------------------------------------------------------


def test_the_committed_skip_set_names_the_run_it_was_measured_on():
    text = EXPECTED_SKIPS.read_text(encoding="utf-8")
    header = "\n".join(text.splitlines()[:12])
    assert "41119890499" in header and "75011469" in header
    assert "50 skips" in header
    assert "OI-2518" in header  # the one key added after that run
    entries, problems = cxp.read_expected_skips(EXPECTED_SKIPS)
    assert problems == []
    assert len(entries) == 51
    assert ("tests/test_report_to_receipt_converter.py::TestFailClosedBranchNotOnOrigin"
            "::test_existing_branch_passes_check") in entries
    for line in text.splitlines():
        if line.strip() and not line.startswith("#"):
            assert "  # " in line, line
            assert cxp.expected_skip_entry_problem(line.split("  # ", 1)[0]) is None


# --- the emission: unset means xdist, VNX_CI_XDIST=0 is the way back ---------------------------


def _emit(tmp_path: Path, xdist: str | None) -> Path:
    out = tmp_path / ("unset" if xdist is None else f"xdist-{xdist}")
    env = {k: v for k, v in os.environ.items() if not k.startswith("VNX_")}
    if xdist is not None:
        env["VNX_CI_XDIST"] = xdist
    proc = subprocess.run(["bash", str(ARGS_SCRIPT), str(out)], env=env, capture_output=True, text=True,
                          cwd=str(REPO_ROOT))
    assert proc.returncode == 0, proc.stderr
    return out


def _read_vector(path: Path) -> list:
    return path.read_bytes().rstrip(b"\0").decode("utf-8").split("\0")


def test_unset_emits_the_xdist_shape_and_vnx_ci_xdist_0_the_serial_one(tmp_path):
    xdist = _emit(tmp_path, None)
    assert (xdist / "parallel.args").is_file()
    assert not (xdist / "full.args").exists()
    parallel = _read_vector(xdist / "parallel.args")
    assert parallel[-6:] == ["-n", "4", "--dist", "loadfile", "--no-loadscope-reorder", "--max-worker-restart=0"]

    serial = _emit(tmp_path, "0")
    assert (serial / "full.args").is_file()
    assert not (serial / "parallel.args").exists() and not (serial / "serial.args").exists()
    full = _read_vector(serial / "full.args")
    assert "-n" not in full and "--dist" not in full
    assert full[:2] == ["-q", str(REPO_ROOT / "tests")]


_PIN_LINE = re.compile(r"^\s*(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*(?:\[[^\]]*\])?)\s*==(?P<version>[^\s;#]+)")


def _mutate_first_pin(constraints_text: str) -> str:
    """Change the version of the first ``==`` pin, and nothing else, in the text given.

    The mutation is derived from the text instead of naming a literal version
    (OI-2543 point 3): when the pin in ``profile_a_constraints.txt`` moves, the
    mutated text moves with it. A literal ``replace("pytest==9.1.1", ...)``
    silently becomes a no-op on that day and turns the fingerprint test red for
    a reason that has nothing to do with the fingerprint.
    """
    lines = constraints_text.splitlines(keepends=True)
    for index, line in enumerate(lines):
        match = _PIN_LINE.match(line)
        if match is None:
            continue
        version = match.group("version")
        head, _, last = version.rpartition(".")
        bumped = f"{head}.{int(last) + 1}" if head and last.isdigit() else f"{version}.post1"
        start, end = match.span("version")
        lines[index] = f"{line[:start]}{bumped}{line[end:]}"
        return "".join(lines)
    raise AssertionError(f"no `==` pin line in the constraints text: {constraints_text!r}")


def test_the_xdist_selection_fingerprint_is_stable_for_one_selection_and_moves_with_its_inputs(tmp_path):
    """What the fingerprint means, instead of one literal sha256 of today's vectors (OI-2543 point 3).

    Same inputs give the same value (two emissions), and each input that
    defines the selection moves it: the ``--ignore`` entries from
    ``scripts/ci/test_exclusions.txt``, the ``-k`` quarantine, and the
    constraints file (pins). A PR that repairs an excluded test or bumps a pin
    therefore changes the value on purpose; it does not turn Profile A red.

    The cutover value of the D4 merge stays on record in
    ``claudedocs/2026-10-24-nachtrun-golf-1-3/evidence/d4-overstapregel-20261105-0251.json``.
    """
    first = _emit(tmp_path / "first", None)
    second = _emit(tmp_path / "second", None)
    fingerprint = cxp.fingerprint_of_args_dir(first, CONSTRAINTS, str(REPO_ROOT))
    assert cxp.fingerprint_of_args_dir(second, CONSTRAINTS, str(REPO_ROOT)) == fingerprint

    serial, parallel = cxp.read_args_dir(first)
    constraints = CONSTRAINTS.read_text(encoding="utf-8")
    quarantine = list(parallel)
    [k_index] = [i for i, arg in enumerate(quarantine) if arg == "-k"]
    quarantine[k_index + 1] = quarantine[k_index + 1] + " and not another_quarantined_test"
    pin_mutation = _mutate_first_pin(constraints)
    assert pin_mutation != constraints, "the pin mutation must change the constraints text"
    inputs = {
        "an exclusion from test_exclusions.txt": (serial, [*parallel, "--ignore=/tmp/tests/test_repaired.py"],
                                                  constraints),
        "the -k quarantine": (serial, quarantine, constraints),
        "a constraint pin": (serial, parallel, pin_mutation),
    }
    for name, (case_serial, case_parallel, case_constraints) in inputs.items():
        assert cxp.selection_fingerprint(case_serial, case_parallel, case_constraints, str(REPO_ROOT)) != fingerprint, name


@pytest.mark.parametrize("constraints_text", [
    "pytest==8.0.0\n",
    "# a comment and a blank line come first\n\npytest==9.2.0\npytest-xdist==3.8.0\n",
], ids=["one-pin", "after-comment-and-blank"])
def test_the_pin_mutation_follows_whatever_version_the_text_pins(constraints_text, tmp_path):
    """A version this test does not name still moves: the mutation reads the text (OI-2543 point 3)."""
    mutated = _mutate_first_pin(constraints_text)
    assert mutated != constraints_text, "the mutation left the constraints text unchanged"

    before = constraints_text.splitlines()
    after = mutated.splitlines()
    assert len(before) == len(after)
    changed = [index for index, (old, new) in enumerate(zip(before, after)) if old != new]
    assert len(changed) == 1, f"the mutation changed more than the pinned line: {changed}"

    before_path = tmp_path / "before.txt"
    before_path.write_text(constraints_text, encoding="utf-8")
    after_path = tmp_path / "after.txt"
    after_path.write_text(mutated, encoding="utf-8")
    before_pins, after_pins = cxp.read_pins(before_path), cxp.read_pins(after_path)
    assert list(after_pins) == list(before_pins), (before_pins, after_pins)
    moved = [name for name, version in before_pins.items() if after_pins[name] != version]
    assert moved == [next(iter(before_pins))], (before_pins, after_pins)


# --- the workflows ----------------------------------------------------------------------------


def _jobs(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))["jobs"]


def _step(job: dict, step_id: str) -> dict:
    [step] = [s for s in job["steps"] if s.get("id") == step_id]
    return step


def test_the_per_pr_workflow_no_longer_runs_a_shadow_or_parity_job():
    jobs = _jobs(WORKFLOW)
    assert "profile-a-xdist-shadow" not in jobs
    assert "xdist-parity" not in jobs
    assert "profile-a" in jobs


def test_the_shadow_job_and_the_test_of_it_are_gone_together():
    """D4 removes the shadow job, so D3c's test of it goes too; its step-sum rule lives here now."""
    assert not (REPO_ROOT / "tests" / "test_ci_xdist_shadow_workflow.py").exists()


def _profile_a_job_text() -> str:
    """The `profile-a` job block of the workflow as text — the comments included, the steps too."""
    lines = WORKFLOW.read_text(encoding="utf-8").splitlines()
    try:
        start = lines.index("  profile-a:")
    except ValueError:
        raise AssertionError("vnx-ci.yml has no `profile-a` job") from None
    body = lines[start + 1:]
    end = next((i for i, line in enumerate(body) if line.strip() and not line.startswith("    ")), len(body))
    return "\n".join(body[:end])


def _timeout_index() -> list:
    """(lines, index) of the job-level `timeout-minutes` line: 4 spaces, not a step's 8."""
    lines = _profile_a_job_text().splitlines()
    [index] = [i for i, line in enumerate(lines) if line.startswith("    timeout-minutes:")]
    return lines, index


def _timeout_expression() -> str:
    lines, index = _timeout_index()
    return lines[index].split("timeout-minutes:", 1)[1].strip()


def _timeout_comment() -> str:
    """The comment block directly above the job-level timeout — where its sizing is argued."""
    lines, index = _timeout_index()
    comment = []
    for line in reversed(lines[:index]):
        if not line.strip().startswith("#"):
            break
        comment.append(line.strip())
    return "\n".join(reversed(comment))


_ACTIONS_TOKEN = re.compile(r"(?P<space>\s+)|(?P<string>'(?:[^']|'')*')|(?P<number>\d+)"
                            r"|(?P<name>[A-Za-z_][A-Za-z0-9_.]*)|(?P<operator>&&|\|\||==|\(|\))")


class ActionsExpression:
    """The subset of the GitHub Actions expression language a job timeout uses.

    `||`, `&&` and `==` over string and number literals, `vars.<name>` and
    parentheses, with GitHub's operand-returning `&&`/`||` and its truthiness
    (null, 0 and the empty string are false, every other value — `'0'` included —
    is true). A token or a form it does not know raises, so a rewritten
    expression can never be read as a number it does not produce.
    """

    def __init__(self, expression: str, variables: dict) -> None:
        match = re.fullmatch(r"\$\{\{\s*(.*?)\s*\}\}", expression, re.DOTALL)
        if match is None:
            raise AssertionError(f"not one `${{{{ ... }}}}` expression: {expression!r}")
        self.variables = variables
        self.tokens = self._tokenize(match.group(1))
        self.position = 0

    @staticmethod
    def _tokenize(source: str) -> list:
        tokens, position = [], 0
        while position < len(source):
            token = _ACTIONS_TOKEN.match(source, position)
            if token is None:
                raise AssertionError(f"unsupported expression syntax: {source[position:]!r}")
            position = token.end()
            if token.lastgroup != "space":
                tokens.append((token.lastgroup, token.group()))
        return tokens

    def value(self):
        result = self._parse_or()
        if self.position != len(self.tokens):
            raise AssertionError(f"trailing tokens: {self.tokens[self.position:]}")
        return result

    def _peek(self) -> tuple:
        return self.tokens[self.position] if self.position < len(self.tokens) else (None, None)

    def _take(self) -> tuple:
        token = self._peek()
        if token == (None, None):
            raise AssertionError("the expression ended early")
        self.position += 1
        return token

    def _parse_or(self):
        value = self._parse_and()
        while self._peek() == ("operator", "||"):
            self._take()
            right = self._parse_and()
            value = value if self._truthy(value) else right
        return value

    def _parse_and(self):
        value = self._parse_comparison()
        while self._peek() == ("operator", "&&"):
            self._take()
            right = self._parse_comparison()
            value = right if self._truthy(value) else value
        return value

    def _parse_comparison(self):
        left = self._parse_primary()
        if self._peek() == ("operator", "=="):
            self._take()
            return self._equal(left, self._parse_primary())
        return left

    def _parse_primary(self):
        kind, text = self._take()
        if (kind, text) == ("operator", "("):
            value = self._parse_or()
            if self._take() != ("operator", ")"):
                raise AssertionError("unbalanced parenthesis")
            return value
        if kind == "string":
            return text[1:-1].replace("''", "'")
        if kind == "number":
            return int(text)
        if kind == "name":
            scope, _, name = text.partition(".")
            if scope != "vars" or not name:
                raise AssertionError(f"only `vars.<name>` is supported, not {text!r}")
            return self.variables.get(name)
        raise AssertionError(f"unexpected token {text!r}")

    @staticmethod
    def _truthy(value) -> bool:
        if value is None or value is False:
            return False
        if isinstance(value, int) and value == 0:
            return False
        return value != ""

    @staticmethod
    def _equal(left, right) -> bool:
        if left is None or right is None:
            return left is right
        return str(left) == str(right)


# The merge window D4 round 5 sizes the xdist bound from: the runs
# `xdist_parity_history.py --last-d3-merge 983c944f` reads on 2026-11-05.
WINDOW_LONGEST_WALL_S = 1134


def test_profile_a_timeout_follows_the_switch_on_every_branch():
    """The serial shape needs 80; the two-phase shape gets 38 (OI-2523, OI-2525)."""
    expression = _timeout_expression()
    xdist = ActionsExpression(expression, {}).value()
    serial = ActionsExpression(expression, {"VNX_CI_XDIST": "0"}).value()
    assert xdist == 38, f"xdist branch of {expression!r} is {xdist!r}"
    # Twice the longest shadow suite wall time of the merge window (1134 s, run
    # 41827919472 on 2026-11-04), rounded up to whole minutes.
    assert xdist >= 2 * WINDOW_LONGEST_WALL_S / 60
    assert serial == 80, f"serial branch of {expression!r} is {serial!r}"
    # The serial Profile A takes 52, 52 and 54.8 minutes on main (OI-2523).
    assert serial >= 55
    assert ActionsExpression(expression, {"VNX_CI_XDIST": "1"}).value() == xdist


def test_every_profile_a_step_timeout_fires_inside_both_job_bounds():
    """D3c's rule (#2378), carried into D4: a step timeout must fire before the job's.

    The two job bounds are read out of the workflow's own expression, so a
    branch whose number is edited shows up here without a second copy of it.
    """
    expression = _timeout_expression()
    bounds = {branch: ActionsExpression(expression, variables).value()
              for branch, variables in (("xdist", {}), ("serial", {"VNX_CI_XDIST": "0"}))}
    job = _jobs(WORKFLOW)["profile-a"]
    bounded = [step["timeout-minutes"] for step in job["steps"] if "timeout-minutes" in step]
    assert bounded, "no step of profile-a carries its own timeout"
    assert all(isinstance(bound, int) for bound in bounded), bounded
    step_sum = sum(bounded)
    for branch, bound in bounds.items():
        assert isinstance(bound, int), f"the {branch} branch of {expression!r} is not a number: {bound!r}"
        assert step_sum < bound, f"{step_sum} min of step timeouts cannot fire inside the {branch} bound {bound}"
    # A bound that does not even cover the window it was sized on guards nothing.
    assert bounds["xdist"] >= 2 * WINDOW_LONGEST_WALL_S / 60, bounds


def test_the_timeout_reads_the_same_variable_the_switch_reads():
    """One variable answers both questions: which shape runs, and how long the job may take."""
    timeout = _timeout_expression()
    switch = re.fullmatch(r"\$\{\{\s*vars\.([A-Za-z_][A-Za-z0-9_]*)\s*\}\}",
                          _step(_jobs(WORKFLOW)["profile-a"], "suite")["env"]["VNX_CI_XDIST"])
    assert switch is not None, "the suite step no longer reads its switch from vars.<name>"
    read = set(re.findall(r"\bvars\.([A-Za-z_][A-Za-z0-9_]*)", timeout))
    assert read == {switch.group(1)}


def test_the_profile_a_timeout_comment_names_the_serial_wall_time():
    comment = _timeout_comment()
    assert "1134" in comment, "the xdist sizing argument (2 x 1134 s) is gone"
    assert "41827919472" in comment, "the window's longest run is gone from the comment"
    assert "37.8" in comment and "983c944f" in comment, comment
    assert "52" in comment and "54.8" in comment, comment
    assert "OI-2523" in comment and "OI-2525" in comment, comment
    assert "#2378" in comment, "the comment no longer says who set the 80-minute bridge"


def test_the_completeness_check_is_a_required_step_after_both_phases():
    profile_a = _jobs(WORKFLOW)["profile-a"]
    step = _step(profile_a, "completeness")
    assert not step.get("continue-on-error"), "a failing completeness check must fail the job"
    assert not profile_a.get("continue-on-error")
    run = step["run"]
    assert "check_xdist_parity.py\" completeness" in run or "check_xdist_parity.py completeness" in run
    assert "xdist_expected_skips.txt" in run and "xdist-completeness-verdict.json" in run
    assert "steps.suite.outputs.mode == 'xdist'" in step["if"]
    ids = [s.get("id") for s in profile_a["steps"]]
    assert ids.index("serial") < ids.index("completeness")
    assert ids.index("parallel") < ids.index("completeness")


def test_profile_a_reads_the_switch_from_the_repository_variable():
    profile_a = _jobs(WORKFLOW)["profile-a"]
    suite = _step(profile_a, "suite")
    assert suite["env"]["VNX_CI_XDIST"] == "${{ vars.VNX_CI_XDIST }}"
    assert "profile_a_pytest_args.sh" in suite["run"]


def test_the_nightly_is_not_named_vnx_ci():
    nightly = yaml.safe_load(NIGHTLY.read_text(encoding="utf-8"))
    assert nightly["name"] == "VNX CI serial truth"
    assert nightly["name"] != "VNX CI"
    assert "schedule" in nightly[True] and "workflow_dispatch" in nightly[True]
    assert nightly[True]["schedule"][0]["cron"] == "23 3 * * *"


def test_only_one_workflow_is_named_vnx_ci():
    """The merge readers select by the exact name "VNX CI"; a second workflow of that name breaks them."""
    names = []
    for path in sorted(WORKFLOW_DIR.glob("*.yml")) + sorted(WORKFLOW_DIR.glob("*.yaml")):
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
        if isinstance(document, dict) and document.get("name"):
            names.append(document["name"])
    assert names.count("VNX CI") == 1, names


def test_the_nightly_keeps_the_full_serial_truth_and_the_comparison():
    jobs = _jobs(NIGHTLY)
    truth = jobs["serial-truth"]
    emit = [s for s in truth["steps"] if "profile_a_pytest_args.sh" in (s.get("run") or "")][0]
    assert emit["env"]["VNX_CI_XDIST"] == "0"
    assert any("profile_a_run_phase.sh\" full" in (s.get("run") or "")
               or "profile_a_run_phase.sh full" in (s.get("run") or "") for s in truth["steps"])

    parity = jobs["parity"]
    assert set(parity["needs"]) == {"serial-truth", "xdist-shape"}
    assert "check_xdist_parity.py\" compare" in _step(parity, "compare")["run"]
    assert "record_xdist_parity_verdict.py" in _step(parity, "record")["run"]
    assert _step(parity, "report")["run"].rstrip().endswith("exit 1")


def test_the_merge_readers_see_every_check_run_not_only_the_vnx_ci_workflow():
    sys.path.insert(0, str(REPO_ROOT / "scripts" / "lib"))
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    try:
        preflight = _load("merge_preflight_ci_check", REPO_ROOT / "scripts" / "lib" / "merge_preflight_ci_check.py")
        gate = _load("deterministic_gate", REPO_ROOT / "scripts" / "lib" / "deterministic_gate.py")
        pre_merge = _load("pre_merge_gate", REPO_ROOT / "scripts" / "pre_merge_gate.py")
    finally:
        sys.path.pop(0)
        sys.path.pop(0)
    assert preflight.DEFAULT_CI_WORKFLOW_NAME == "VNX CI"
    assert preflight._resolve_workflow_name(None) == "VNX CI"
    assert pre_merge.DEFAULT_CI_WORKFLOW_NAME == "VNX CI"
    assert pre_merge._resolve_ci_workflow_name(None) == "VNX CI"

    closure = _load("closure_verifier", REPO_ROOT / "scripts" / "closure_verifier.py")
    assert closure._rollup_all_green([
        {"__typename": "CheckRun", "name": "VNX CI", "status": "COMPLETED", "conclusion": "SUCCESS"},
        {"__typename": "CheckRun", "name": "Serial truth (full serial sweep)",
         "status": "COMPLETED", "conclusion": "FAILURE"},
    ]) is False

    context = gate.LocalContext(checks=[
        {"name": "VNX CI", "bucket": "pass"},
        {"name": "Serial truth vs xdist shape", "bucket": "fail"},
    ])
    assert gate.ci_classification(context)["verdict"] == "fail"
    assert gate.ci_classification(gate.LocalContext(checks=[{"name": "VNX CI", "bucket": "pass"}]))["verdict"] == "pass"


# --- the nightly runs only on main, and the serial truth gets room for its measured wall time (OI-2521, OI-2523)


def _nightly_text() -> str:
    return NIGHTLY.read_text(encoding="utf-8")


def test_every_nightly_job_runs_only_on_main():
    """A workflow_dispatch on a PR head would put these check runs on that SHA, where the merge readers look (OI-2521)."""
    jobs = _jobs(NIGHTLY)
    for job_id in ("serial-truth", "xdist-shape", "parity"):
        assert "github.ref == 'refs/heads/main'" in (jobs[job_id].get("if") or ""), job_id
    assert "!cancelled()" in jobs["parity"]["if"], "the parity job still runs when a needed job failed"


def test_every_job_of_the_nightly_carries_the_main_guard():
    """Every job, not only the three named above: a job added without the guard is a skipped check run on a PR head.

    A dispatch on another ref skips these jobs, and GitHub still creates check
    runs with conclusion ``skipped`` on that SHA; ``closure_verifier._rollup_all_green``
    reads that as not green, so it blocks rather than passes (OI-2543 point 2).
    """
    jobs = _jobs(NIGHTLY)
    assert jobs, "the nightly has no jobs"
    missing = [job_id for job_id, job in jobs.items()
               if "github.ref == 'refs/heads/main'" not in (job.get("if") or "")]
    assert missing == [], missing


def test_the_nightly_header_says_why_the_main_guard_exists():
    header = _nightly_text().split("\non:\n", 1)[0]
    assert "workflow_dispatch" in header and "refs/heads/main" in header
    assert "skipped" in header and "_rollup_all_green" in header, "the header names what the guard really does"


def test_the_serial_truth_job_allows_eighty_minutes_and_the_others_keep_theirs():
    jobs = _jobs(NIGHTLY)
    assert jobs["serial-truth"]["timeout-minutes"] == 80
    assert jobs["xdist-shape"]["timeout-minutes"] == 35
    assert jobs["parity"]["timeout-minutes"] == 20


def test_the_serial_truth_timeout_comment_names_the_measured_wall_time():
    text = _nightly_text()
    job = text.split("  serial-truth:\n", 1)[1].split("\n  xdist-shape:\n", 1)[0]
    assert "52" in job and "54.8" in job, job
    assert "OI-2523" in job
    assert "35.4" not in job, "the comment still cites the pre-switch measurement"
