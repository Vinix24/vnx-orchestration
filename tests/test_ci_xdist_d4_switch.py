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

import ci_workflow_shape as shape

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
    assert "OI-2518" in header  # the first key added after that run
    assert "OI-2564" in header  # the second key added after that run
    assert "OI-2589" in header  # the third to fifth keys added after that run
    entries, problems = cxp.read_expected_skips(EXPECTED_SKIPS)
    assert problems == []
    assert len(entries) == 56
    assert ("tests/test_report_to_receipt_converter.py::TestFailClosedBranchNotOnOrigin"
            "::test_existing_branch_passes_check") in entries
    assert ("tests/test_launchd_interpreter_pin.py"
            "::test_without_a_venv_the_pinned_312_is_taken") in entries
    assert ("tests/integration/test_gemini_streaming_gated.py::TestGeminiLiveStreaming"
            "::test_stream_events_yields_events") in entries
    assert ("tests/integration/test_gemini_streaming_gated.py::TestGeminiLiveStreaming"
            "::test_all_real_events_have_tier_1") in entries
    assert ("tests/test_w_init_project_id_threading.py::TestMigrationLinter"
            "::test_passes_actual_repo_in_normal_mode") in entries
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


def _profile_a(path: Path = WORKFLOW) -> shape.ProfileA:
    return shape.profile_a(path)


def _step(job: dict, step_id: str) -> dict:
    [step] = [s for s in job["steps"] if s.get("id") == step_id]
    return step


def test_the_per_pr_workflow_no_longer_runs_a_shadow_or_parity_job():
    jobs = _jobs(WORKFLOW)
    assert "profile-a-xdist-shadow" not in jobs
    assert "xdist-parity" not in jobs
    assert _profile_a().required_job.name == shape.REQUIRED_CHECK_NAME


def test_the_shadow_job_and_the_test_of_it_are_gone_together():
    """D4 removes the shadow job, so D3c's test of it goes too; its step-sum rule lives here now."""
    assert not (REPO_ROOT / "tests" / "test_ci_xdist_shadow_workflow.py").exists()


def _profile_a_job_text() -> str:
    """The `profile-a` job block of the workflow as text — the comments included, the steps too."""
    lines = WORKFLOW.read_text(encoding="utf-8").splitlines()
    try:
        start = lines.index(f"  {_profile_a().required_job.job_id}:")
    except ValueError:
        raise AssertionError("vnx-ci.yml has no job block for the required Profile A check") from None
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
                            r"|(?P<name>[A-Za-z_][A-Za-z0-9_.]*)|(?P<operator>&&|\|\||==|!|\(|\))")


class ActionsExpression:
    """The subset of the GitHub Actions expression language a job timeout uses.

    `||`, `&&`, `==` and `!` over string and number literals, `vars.<name>`,
    `inputs.<name>` (keyed ``inputs.<name>`` in the variables) and parentheses, with GitHub's operand-returning `&&`/`||` and its truthiness
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
        if (kind, text) == ("operator", "!"):
            return not self._truthy(self._parse_primary())
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
            if scope not in ("vars", "inputs") or not name:
                raise AssertionError(f"only `vars.<name>` and `inputs.<name>` are supported, not {text!r}")
            return self.variables.get(name if scope == "vars" else text)
        raise AssertionError(f"unexpected token {text!r}")

    @staticmethod
    def _truthy(value) -> bool:
        if value is None or value is False:
            return False
        if isinstance(value, int) and value == 0:
            return False
        return value != ""

    @staticmethod
    def _kind(value) -> str:
        return "null" if value is None else "bool" if isinstance(value, bool) else "number" if isinstance(value, int) else "string"

    @staticmethod
    def _number(value) -> float:
        if value is None or value == "":
            return 0.0
        try:
            return float(value)
        except ValueError:
            return float("nan")

    @classmethod
    def _equal(cls, left, right) -> bool:
        """GitHub's loose `==`: same kind compares as is (strings case-insensitively), mixed kinds as numbers,
        so an unset ``vars.<name>`` (null) equals ``'0'`` (run 44234967120)."""
        if cls._kind(left) == cls._kind(right):
            return left.lower() == right.lower() if isinstance(left, str) else left == right
        return cls._number(left) == cls._number(right)


def test_the_expression_reader_compares_like_github():
    """null == '0' is true on GitHub; that is why "xdist off" reads the variable through `|| '1'`."""
    assert ActionsExpression("${{ vars.X == '0' }}", {}).value() is True
    assert ActionsExpression("${{ (vars.X || '1') == '0' }}", {}).value() is False
    assert ActionsExpression("${{ (vars.X || '1') == '0' }}", {"X": "0"}).value() is True
    assert ActionsExpression("${{ vars.X == 'abc' }}", {"X": "ABC"}).value() is True
    assert ActionsExpression("${{ vars.X == '1' }}", {"X": "x"}).value() is False


# The merge window D4 round 5 sizes the xdist bound from: the runs
# `xdist_parity_history.py --last-d3-merge 983c944f` reads on 2026-11-05.
WINDOW_LONGEST_WALL_S = 1134


# The branches of "xdist off" (C3): the repository variable, the workflow_dispatch input, or neither.
OFF = ({"VNX_CI_XDIST": "0"}, {"inputs.xdist_off": True}, {"VNX_CI_XDIST": "0", "inputs.xdist_off": False})
ON = ({}, {"VNX_CI_XDIST": "1"}, {"inputs.xdist_off": False}, {"VNX_CI_XDIST": ""})
C5_LONGEST_SHARD_MIN = 12.73  # 30 heavy runs to 44713236371, see the C5 PR
XDIST_OFF = "((vars.VNX_CI_XDIST || '1') == '0' || inputs.xdist_off)"


def test_profile_a_timeout_follows_the_switch_on_every_branch():
    """The aggregator: 80 for the serial sweep (OI-2523, OI-2525), 15 otherwise; each shard 26 (C5: twice the longest shard of 30 heavy runs)."""
    expression = _timeout_expression()
    assert [ActionsExpression(expression, v).value() for v in OFF] == [80, 80, 80], expression
    assert [ActionsExpression(expression, v).value() for v in ON] == [15, 15, 15, 15], expression
    # The serial Profile A takes 52, 52 and 54.8 minutes on main (OI-2523).
    assert ActionsExpression(expression, OFF[0]).value() >= 55
    for job in _profile_a().jobs_with_role(shape.ROLE_SHARD):
        # C5: twice the longest shard of the last 30 heavy runs (12.73 min, to run 44713236371), rounded up.
        assert job.timeout == 26 and job.timeout >= 2 * C5_LONGEST_SHARD_MIN, job.name


def test_every_profile_a_step_timeout_fires_inside_both_job_bounds():
    """D3c's rule (#2378), carried into D4 and C3: in every job, the step timeouts fire before the job's."""
    expression = _timeout_expression()
    bounds = {"xdist": ActionsExpression(expression, ON[0]).value(), "serial": ActionsExpression(expression, OFF[0]).value()}
    for job in _profile_a().jobs:
        bounded = [step["timeout-minutes"] for step in job.steps if "timeout-minutes" in step]
        assert all(isinstance(bound, int) for bound in bounded), (job.name, bounded)
        for branch, bound in (bounds.items() if job.role == shape.ROLE_AGGREGATOR else [("job", job.timeout)]):
            assert isinstance(bound, int), f"{job.name}: the {branch} bound is not a number: {bound!r}"
            assert sum(bounded) < bound, f"{job.name}: {sum(bounded)} min of step timeouts cannot fire inside {bound}"
    assert bounded and _profile_a().required_job.steps, "no step of Profile A carries its own timeout"


def test_the_profile_a_timeout_comments_name_the_measured_wall_times():
    comment = _timeout_comment()
    for token in ("52", "54.8", "OI-2523", "OI-2525", "#2378"):
        assert token in comment, (token, comment)
    shard_comment = WORKFLOW.read_text(encoding="utf-8").split("\n  profile-a-shard-1:\n", 1)[0].rsplit("\n\n", 1)[1]
    assert "763.6" in shard_comment and "44713236371" in shard_comment, shard_comment


def _xdist_off_places() -> dict:
    a = _profile_a()
    places = {f"{job.job_id} if": job.body.get("if") for job in a.jobs_with_role(shape.ROLE_SHARD)}
    places["aggregator timeout"] = a.required_job.timeout
    places["--xdist-off flag"] = _step(a.required_job.body, "aggregate")["env"]["XDIST_OFF"]
    return places


def test_xdist_off_is_one_expression_in_the_shard_if_the_timeout_and_the_flag():
    """Criterion 7 without the repository variable: the workflow_dispatch input is in each place the variable is."""
    places = _xdist_off_places()
    assert len(places) == 6, places
    for where, text in places.items():
        assert isinstance(text, str) and XDIST_OFF in text, f"{where}: {text!r}"
        assert set(re.findall(r"\b(vars|inputs)\.([A-Za-z_]+)", text)) == {("vars", "VNX_CI_XDIST"), ("inputs", "xdist_off")}, where
    flag = places["--xdist-off flag"]
    assert [ActionsExpression(flag, v).value() for v in OFF + ON] == ["true"] * 3 + ["false"] * 4
    trigger = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))[True]["workflow_dispatch"]["inputs"]["xdist_off"]
    assert trigger["type"] == "boolean" and trigger["default"] is False


def test_a_shard_runs_only_when_xdist_is_on():
    for job in _profile_a().jobs_with_role(shape.ROLE_SHARD):
        assert [ActionsExpression(job.body["if"], v).value() for v in OFF] == [False] * 3, job.name
        assert [ActionsExpression(job.body["if"], v).value() for v in ON] == [True] * 4, job.name


def test_the_aggregator_needs_all_five_and_always_runs():
    a = _profile_a().required_job
    assert a.job_id == "profile-a" and a.body["if"] == "${{ always() }}"
    assert a.needs == [f"profile-a-shard-{k}" for k in range(1, 5)] + ["profile-a-guards"]
    assert not a.body.get("continue-on-error")
    decide = _step(a.body, "aggregate")
    assert "--shard-jobs " + ",".join(a.needs[:4]) in decide["run"] and "--guards-job profile-a-guards" in decide["run"]
    assert decide["env"]["NEEDS_JSON"] == "${{ toJSON(needs) }}" and "continue-on-error" not in decide


def test_the_decision_comes_before_every_mode_step_and_each_mode_step_reads_its_mode():
    a = _profile_a().required_job
    ids = [s.get("id") for s in a.steps]
    for mode_step, mode in (("light", "light"), ("completeness", "sharded"), ("serial-sweep", "serial")):
        step = _step(a.body, mode_step)
        assert ids.index("aggregate") < ids.index(mode_step), mode_step
        assert f"steps.aggregate.outputs.mode == '{mode}'" in step["if"], mode_step
        assert "always()" not in step["if"] and "continue-on-error" not in step, mode_step
    assert _step(a.body, "completeness")["if"] == "${{ steps.aggregate.outputs.mode == 'sharded' }}"


def test_the_completeness_check_is_a_required_step_over_the_four_shards():
    step = _step(_profile_a().required_job.body, "completeness")
    assert not step.get("continue-on-error"), "a failing completeness check must fail the job"
    run = step["run"]
    assert "check_xdist_parity.py\" completeness" in run and "--candidate" not in run
    assert re.findall(r'--shard-dir "\$PROFILE_A_EVIDENCE/(shard-\d)"', run) == [f"shard-{k}" for k in range(1, 5)]
    for arg in ("--constraints", "xdist_expected_skips.txt", "xdist-completeness-verdict.json", "--summary",
                "--head-sha", "--tested-sha", "--run-id", "--run-attempt", "--event", "--ref"):
        assert arg in run, arg


def test_no_shard_has_needs_and_only_the_uploads_of_shards_and_guards_are_non_blocking():
    a = _profile_a()
    for job in a.jobs_with_role(shape.ROLE_SHARD) + a.jobs_with_role(shape.ROLE_GUARDS):
        assert job.needs == [] and not job.body.get("continue-on-error"), job.name
        soft = [s.get("name") for s in job.steps if s.get("continue-on-error") is not None]
        assert all("actions/upload-artifact@" in (s.get("uses") or "") for s in job.steps if s.get("continue-on-error") is not None), soft
    assert a.jobs_with_role(shape.ROLE_GUARDS)[0].timeout == 15 and "if" not in a.jobs_with_role(shape.ROLE_GUARDS)[0].body


def test_every_shard_exports_its_profile_and_uploads_under_its_own_job_id():
    shards = _profile_a().jobs_with_role(shape.ROLE_SHARD)
    assert [j.job_id for j in shards] == [f"profile-a-shard-{k}" for k in range(1, 5)]
    for job in shards:
        assert job.body["outputs"] == {"profile": "${{ steps.prelude.outputs.profile }}"}, job.name
        assert job.body["permissions"] == {"contents": "read", "actions": "read"} and job.body["runs-on"] == "ubuntu-24.04"
        assert job.steps == shards[0].steps, f"{job.name} does not run the same steps as shard 1"
    [upload] = [s for s in shards[0].steps if "actions/upload-artifact@" in (s.get("uses") or "")]
    assert upload["with"]["name"] == "${{ github.job }}" and "always()" in upload["if"]
    gh_steps = [s for s in shards[0].steps if "gh " in (s.get("run") or "")]
    assert gh_steps and all(s["env"]["GH_TOKEN"] == "${{ github.token }}" for s in gh_steps)


def test_profile_a_reads_the_switch_from_the_repository_variable():
    """The shard selection reads the repository variable; the serial sweep sets 0 itself, so the input reaches it too."""
    a = _profile_a()
    args = _step(a.jobs_with_role(shape.ROLE_SHARD)[0].body, "args")
    assert args["env"]["VNX_CI_XDIST"] == "${{ vars.VNX_CI_XDIST }}" and "profile_a_pytest_args.sh" in args["run"]
    sweep = _step(a.required_job.body, "serial-sweep")
    assert sweep["env"]["VNX_CI_XDIST"] == "0"
    assert "profile_a_pytest_args.sh" in sweep["run"] and "profile_a_run_phase.sh\" full" in sweep["run"]


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


def test_the_serial_mode_s_skipped_shard_checks_pass_the_closure_verifier_and_the_ci_gate():
    """Xdist off: the four shard check runs conclude `skipped` on the PR head (C3).

    ``closure_verifier._rollup_all_green`` reads a skipped Profile A shard or guards check as green next to a green
    aggregator, so the serial mode no longer blocks a closure that the run itself passes (OI-2993).
    The CI gate of ``deterministic_gate`` and the run-level readers (``merge_preflight_ci_check``, ``pre_merge_gate``)
    read a skipped check as advisory and the run's own conclusion.
    """
    sys.path.insert(0, str(REPO_ROOT / "scripts" / "lib"))
    try:
        gate = _load("deterministic_gate", REPO_ROOT / "scripts" / "lib" / "deterministic_gate.py")
    finally:
        sys.path.pop(0)
    closure = _load("closure_verifier", REPO_ROOT / "scripts" / "closure_verifier.py")
    names = [shape.REQUIRED_CHECK_NAME, "Profile A guards"] + [f"Profile A shard ({k}/4)" for k in range(1, 5)]
    serial = {n: "SKIPPED" if "shard" in n else "SUCCESS" for n in names}
    rollup = [{"__typename": "CheckRun", "name": n, "status": "COMPLETED", "conclusion": c} for n, c in serial.items()]
    assert closure._rollup_all_green(rollup) is True
    assert closure._rollup_all_green([dict(r, conclusion="SUCCESS") for r in rollup]) is True
    checks = [{"name": n, "bucket": "skipping" if c == "SKIPPED" else "pass"} for n, c in serial.items()]
    assert gate.ci_classification(gate.LocalContext(checks=checks))["verdict"] == "pass"


# --- the nightly runs only on main, and the serial truth gets room for its measured wall time (OI-2521, OI-2523)


def _nightly_text() -> str:
    return NIGHTLY.read_text(encoding="utf-8")


def test_every_nightly_job_runs_only_on_main():
    """A workflow_dispatch on a PR head would put these check runs on that SHA, where the merge readers look (OI-2521)."""
    jobs = _jobs(NIGHTLY)
    for job_id in ("serial-truth", "xdist-shape", "parity", "shard-shape", "shard-parity"):
        assert "github.ref == 'refs/heads/main'" in (jobs[job_id].get("if") or ""), job_id
    assert all("!cancelled()" in jobs[j]["if"] for j in ("parity", "shard-parity")), "a parity job runs after a failed need"


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
    assert [jobs[j]["timeout-minutes"] for j in ("shard-parity", "shard-shape")] == [20, 26] == [20, _jobs(WORKFLOW)["profile-a-shard-1"]["timeout-minutes"]]


def test_the_serial_truth_timeout_comment_names_the_measured_wall_time():
    text = _nightly_text()
    job = text.split("  serial-truth:\n", 1)[1].split("\n  xdist-shape:\n", 1)[0]
    assert "52" in job and "54.8" in job, job
    assert "OI-2523" in job
    assert "35.4" not in job, "the comment still cites the pre-switch measurement"



# --- C4b: the nightly also compares the serial truth with the four Profile A shards ----------------


def test_the_nightly_runs_the_four_profile_a_shards_through_the_pr_shard_scripts():
    shard = _jobs(NIGHTLY)["shard-shape"]
    assert shard["name"] == "Shard shape (the four Profile A shards)" and "needs" not in shard
    assert shard["strategy"] == {"fail-fast": False, "matrix": {"k": [1, 2, 3, 4]}}
    nightly, pr = ("\n".join(s.get("run") or "" for s in job["steps"]) for job in (shard, _jobs(WORKFLOW)["profile-a-shard-1"]))
    for call in ("test_shard_plan.py\" select-source", "--before \"$created\" --artifact profile-a-evidence",
                 "VNX_CI_SHARD_SOURCE=", "profile_a_pytest_args.sh", "pytest_collection_manifest.py\" inactive",
                 "profile_a_run_phase.sh\" serial", "profile_a_run_phase.sh\" shard"):
        assert call in nightly and call in pr, call
    plan = _step(shard, "plan")
    assert 'VNX_CI_SHARD="$SHARD_K/4"' in plan["run"] and 'elif [ "$SHARD_K" = 1 ]' in plan["run"]
    assert plan["env"]["SHARD_K"] == "${{ matrix.k }}" and plan["env"]["VNX_CI_XDIST"] == "1"
    assert _step(shard, "parallel")["run"].endswith('profile_a_run_phase.sh" shard "$XDIST_TRUTH_EVIDENCE"')
    assert shard["steps"][-1]["with"]["name"] == "vnx-shard-truth-evidence-${{ matrix.k }}"
    assert shard["env"] == {"XDIST_PHASE": "shard"} and all(
        _step(shard, s) == _step(_jobs(NIGHTLY)["xdist-shape"], s) for s in ("serial", "record"))


def test_the_shard_parity_job_shares_every_step_but_the_download_with_the_two_phase_parity_job():
    jobs = _jobs(NIGHTLY)  # one compare, record and report: criterion 6 covers both shapes under one label
    job, parity = jobs["shard-parity"], jobs["parity"]
    assert job["name"] == "Serial truth vs shard shape" and set(job["needs"]) == {"serial-truth", "shard-shape"}
    assert job["env"] == {"RESULT_PROFILE_A": "${{ needs.serial-truth.result }}",
                          "RESULT_SHADOW": "${{ needs.shard-shape.result }}", "PARITY_SHAPE": "shards"}
    assert parity["env"]["RESULT_SHADOW"] == "${{ needs.xdist-shape.result }}" and parity["env"]["PARITY_SHAPE"] == "two_phase"
    differ = [i for i, (a, b) in enumerate(zip(job["steps"], parity["steps"], strict=True)) if a != b]
    assert [job["steps"][i]["id"] for i in differ] == ["download_candidate"]
    assert job["steps"][differ[0]]["with"]["pattern"] == "vnx-shard-truth-evidence-*"
    assert parity["steps"][-1]["with"]["name"] == "vnx-serial-truth-${{ github.job }}-verdict"
    report = _step(parity, "report")["run"]
    assert report.count("--label xdist-parity") == 2 and "candidate_shape" in report
    names = [j["name"] for j in jobs.values()]  # never merge evidence: no name the merge readers select
    assert not [n for n in names if n.startswith(("VNX CI", "Profile A", shape.REQUIRED_CHECK_NAME))], names


def _bash_step(job: str, step_id: str, env: dict, written: Path) -> tuple:
    base = {k: v for k, v in os.environ.items() if not k.startswith(("OUTCOME_", "XDIST_", "GITHUB_", "PARITY_", "RESULT_"))}
    proc = subprocess.run(["bash", "-c", _step(_jobs(NIGHTLY)[job], step_id)["run"]], capture_output=True, text=True,
                          env={**base, "GITHUB_WORKSPACE": str(REPO_ROOT), "GITHUB_RUN_ID": "7",
                               "GITHUB_RUN_ATTEMPT": "1", "GITHUB_EVENT_NAME": "schedule", **env})
    return proc, json.loads(written.read_text(encoding="utf-8")) if written.exists() else None


def _compare(tmp_path: Path, shape_env: dict) -> tuple:
    env = {"PARITY_WORK": str(tmp_path), "PARITY_PROFILE": "heavy", "PARITY_RUN_ID": "7", "PARITY_RUN_ATTEMPT": "1",
           "PARITY_HEAD_SHA": "h", "PARITY_TESTED_SHA": "t", "PARITY_EVENT": "schedule", "PARITY_REF": "main",
           "RESULT_PROFILE_A": "success", "RESULT_SHADOW": "success", **shape_env,
           **{f"OUTCOME_{k}": "success" for k in ("CHECKOUT", "PRELUDE", "SELECTION", "DOWNLOAD_REFERENCE", "DOWNLOAD_CANDIDATE")}}
    tmp_path.mkdir(exist_ok=True)
    return _bash_step("shard-parity", "compare", env, tmp_path / "verdict" / "xdist-parity-verdict.json")


def test_the_shards_shape_compares_four_shard_dirs_and_missing_shards_stay_invalid(tmp_path):
    proc, verdict = _compare(tmp_path, {"PARITY_SHAPE": "shards"})
    assert proc.returncode == 2 and (verdict["exit"], verdict["verdict"]) == (2, "invalid_evidence")
    assert (verdict["candidate_shape"], verdict["shards"]) == ("shards", 4) and "shadow.record=missing" in verdict["step_failures"]


@pytest.mark.parametrize("value", ["", "shard", "two-phase", "SHARDS"])
def test_an_unknown_shape_writes_no_verdict_and_unset_is_the_two_phase_shape(tmp_path, value):
    assert _compare(tmp_path / "unset", {})[1]["candidate_shape"] == "two_phase"
    proc, verdict = _compare(tmp_path, {"PARITY_SHAPE": value})
    assert proc.returncode == 2 and "is not two_phase or shards" in proc.stderr and verdict is None


def _record(tmp_path: Path, phase: str | None, files: dict, outcomes: dict) -> tuple:
    for name, text in files.items():
        (tmp_path / name).write_text(text, encoding="utf-8")
    env = {"XDIST_TRUTH_EVIDENCE": str(tmp_path), "XDIST_HEAD_SHA": "h", "XDIST_TESTED_SHA": "t",
           **({"XDIST_PHASE": phase} if phase is not None else {}),
           **{f"OUTCOME_{k.upper()}": v for k, v in outcomes.items()}}
    return _bash_step("shard-shape", "record", env, tmp_path / "xdist-shadow.json")


GREEN = {"plan": "success", "serial": "success", "parallel": "success"}


def test_the_shard_record_names_the_shard_phase_and_the_two_phase_record_keeps_parallel(tmp_path):
    proc, record = _record(tmp_path, "shard", {"exit-shard": "0\n", "duration-shard-s": "301\n"}, GREEN)
    assert proc.returncode == 0 and record["verdict"] == "shadow_passed" and record["run_attempt"] == "1"
    assert record["steps"] == {"plan": "success", "serial": "success", "shard": "success"}
    assert record["exit_codes"] == {"serial": None, "shard": 0} and record["durations_s"] == {"serial": None, "shard": 301}
    proc, record = _record(tmp_path, None, {"exit-parallel": "1\n"}, {**GREEN, "serial": ""})
    assert (record["steps"]["serial"], record["exit_codes"]) == ("skipped", {"serial": None, "parallel": 1})


@pytest.mark.parametrize("change", [{"parallel": "failure"}, {"parallel": ""}, {"parallel": "skipped"},
                                    {"parallel": "bogus"}, {"plan": "cancelled"}, {"plan": ""},
                                    {"serial": "failure"}, {"serial": "cancelled"}])
def test_a_shard_step_that_did_not_succeed_records_a_failed_shadow(tmp_path, change):
    proc, record = _record(tmp_path, "shard", {}, {**GREEN, **change})
    assert proc.returncode == 0 and record["verdict"] == "shadow_failed"


@pytest.mark.parametrize("phase", ["", "full", "../x", "shard/../parallel"])
def test_an_unknown_phase_writes_no_record(tmp_path, phase):
    proc, record = _record(tmp_path, phase, {}, GREEN)
    assert proc.returncode == 2 and "is not parallel or shard" in proc.stderr and record is None
