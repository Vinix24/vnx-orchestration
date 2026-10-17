"""check_xdist_parity.py against real output of the pinned pytest and pytest-xdist (D2).

The synthetic contract tests (tests/test_ci_check_xdist_parity.py) build the
XML with the script's own key function. These tests run the real pytest and
the real pytest-xdist of scripts/ci/profile_a_constraints.txt, with the real
collection-manifest plugin, over a small fixture suite written into a temp
directory: a pass, a skip, a strict xfail, a skip whose reason holds the
test's tmp_path, parametrize ids with a space, '#' and '/', a call failure
plus a teardown error (SEOcrawler M16), a pass plus a teardown error, a
module-level importorskip, and (in a second suite) a file that errors at
collection. Each suite runs serially (the reference) and with -n 2 (the
candidate), and in the two-phase shape with an active serial phase.

What the pinned tools write, measured with them and pinned here:

* the M16 pair is two <testcase> records, the second interleaved under xdist;
* a module-level skip is one collection record in both shapes;
* a collection error reaches the xdist controller once per worker (manifest
  "reports": 2) and lands as one record with one <error> per worker;
* with a collection error the serial run stops before any test (exit 2), the
  xdist run runs every other file (exit 1);
* a skip reason that holds tmp_path differs between the shapes (the worker's
  basetemp has a popen-gw<N> level);
* every run carries the run stamp of its step (GITHUB_RUN_ID and
  GITHUB_RUN_ATTEMPT) in its manifest and, through the plugin, in its XML,
  also the XML the xdist controller writes, so a candidate from an earlier
  attempt is stale evidence.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import xml.etree.ElementTree as ET
from importlib import metadata
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = REPO_ROOT / "scripts" / "ci"
PLUGIN = PLUGIN_DIR / "pytest_collection_manifest.py"
CONSTRAINTS = PLUGIN_DIR / "profile_a_constraints.txt"


def _load(name: str, path: Path):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


cxp = _load("check_xdist_parity", PLUGIN_DIR / "check_xdist_parity.py")
PINS = cxp.read_pins(CONSTRAINTS)


def _installed(dist: str):
    try:
        return metadata.version(dist)
    except metadata.PackageNotFoundError:
        return None


_INSTALLED = {dist: _installed(dist) for dist in PINS}
pytestmark = pytest.mark.skipif(
    _INSTALLED != PINS,
    reason=f"needs the pinned tools of profile_a_constraints.txt {PINS}, installed {_INSTALLED}",
)

XDIST = ["-n", "2", "--dist", "loadfile", "--no-loadscope-reorder", "--max-worker-restart=0"]

SUITE = {
    "tests/test_ok.py": (
        "import pytest\n\n\n"
        "def test_pass():\n    pass\n\n\n"
        "@pytest.mark.skip(reason='plain skip')\n"
        "def test_skipped():\n    pass\n\n\n"
        "@pytest.mark.xfail(reason='known', strict=True)\n"
        "def test_xfail():\n    assert False\n\n\n"
        "def test_skip_with_a_tmp_path(tmp_path):\n"
        "    pytest.skip(f'nothing provisioned under {tmp_path}')\n\n\n"
        "@pytest.mark.parametrize('v', ['a b', 'c#d', 'x.y/z'], ids=['a b', 'c#d', 'x.y/z'])\n"
        "def test_ids(v):\n    pass\n"
    ),
    "tests/test_teardown.py": (
        "import pytest\n\n\n"
        "@pytest.fixture\n"
        "def broken_teardown():\n    yield\n    raise RuntimeError('teardown boom')\n\n\n"
        "def test_call_and_teardown_fail(broken_teardown):\n    assert False, 'call boom'\n\n\n"
        "def test_pass_then_teardown_fail(broken_teardown):\n    pass\n"
    ),
    "tests/test_modskip.py": (
        "import pytest\n\n"
        "pytest.importorskip('module_that_does_not_exist_vnx_d2')\n\n\n"
        "def test_never():\n    pass\n"
    ),
    "tests/test_other.py": (
        "class TestOther:\n"
        "    def test_one(self):\n        pass\n\n"
        "    def test_two(self):\n        pass\n"
    ),
}
BROKEN = {"tests/test_broken.py": "raise RuntimeError('boom at import')\n"}
OTHER = ["tests/test_other.py::TestOther::test_one", "tests/test_other.py::TestOther::test_two"]
M16 = "tests/test_teardown.py::test_call_and_teardown_fail"
TMP_SKIP = "tests/test_ok.py::test_skip_with_a_tmp_path"


def _write_suite(root: Path, files: dict) -> Path:
    for rel, body in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(body, encoding="utf-8")
    (root / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    return root


RUN = {"run_id": "38939122085", "run_attempt": "2"}


def _env(attempt: str = RUN["run_attempt"]) -> dict:
    """The environment of a step in attempt *attempt* of run RUN."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYTEST_")}
    env.update(PYTHONPATH=str(PLUGIN_DIR), GITHUB_RUN_ID=RUN["run_id"], GITHUB_RUN_ATTEMPT=attempt)
    return env


def _phase(root: Path, out: Path, phase: str, *args: str, attempt: str = RUN["run_attempt"]) -> int:
    """One real pytest run of *phase* that writes junit-<phase>.xml and manifest-<phase>.json into *out*."""
    out.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", *args,
         f"--junitxml={out / f'junit-{phase}.xml'}", "-p", "pytest_collection_manifest",
         f"--collection-manifest={out / f'manifest-{phase}.json'}", f"--collection-manifest-phase={phase}"],
        cwd=str(root), env=_env(attempt), capture_output=True, text=True,
    )
    assert (out / f"manifest-{phase}.json").is_file(), proc.stdout + proc.stderr
    return proc.returncode


def _inactive_serial(out: Path, attempt: str = RUN["run_attempt"]) -> None:
    subprocess.run([sys.executable, str(PLUGIN), "inactive", "--phase", "serial", "--output", str(out / "manifest-serial.json")],
                   env=_env(attempt), check=True)


def _args(out: Path, vector: list, attempt: str = RUN["run_attempt"]) -> None:
    """The shadow's emission with its run.json, as profile_a_pytest_args.sh writes them."""
    (out / "args").mkdir(parents=True, exist_ok=True)
    (out / "args" / "parallel.args").write_bytes(b"".join(a.encode() + b"\0" for a in vector))
    (out / "args" / "run.json").write_text(json.dumps({"schema": "vnx-run-stamp/1", "run_id": RUN["run_id"],
                                                       "run_attempt": attempt}), encoding="utf-8")


@pytest.fixture(scope="module")
def runs(tmp_path_factory) -> dict:
    base = tmp_path_factory.mktemp("parity-real")
    suite = _write_suite(base / "suite", SUITE)
    broken = _write_suite(base / "broken", {**SUITE, **BROKEN})
    exits = {}
    exits["reference"] = _phase(suite, base / "reference", "full", "-p", "no:xdist", "tests")
    exits["shadow"] = _phase(suite, base / "shadow", "parallel", *XDIST, "tests")
    _inactive_serial(base / "shadow")
    _args(base / "shadow", [*XDIST, "tests"])
    exits["two_phase_serial"] = _phase(suite, base / "two-phase", "serial", "-p", "no:xdist", "tests/test_teardown.py")
    exits["two_phase_parallel"] = _phase(suite, base / "two-phase", "parallel", *XDIST, "--ignore=tests/test_teardown.py", "tests")
    _args(base / "two-phase", [*XDIST, "--ignore=tests/test_teardown.py", "tests"])
    exits["dropped"] = _phase(suite, base / "dropped", "parallel", *XDIST, "--ignore=tests/test_other.py", "tests")
    _inactive_serial(base / "dropped")
    _args(base / "dropped", [*XDIST, "--ignore=tests/test_other.py", "tests"])
    exits["broken_reference"] = _phase(broken, base / "broken-reference", "full", "-p", "no:xdist", "tests")
    exits["broken_shadow"] = _phase(broken, base / "broken-shadow", "parallel", *XDIST, "tests")
    _inactive_serial(base / "broken-shadow")
    _args(base / "broken-shadow", [*XDIST, "tests"])
    exits["attempt_1_shadow"] = _phase(suite, base / "attempt-1-shadow", "parallel", *XDIST, "tests", attempt="1")
    _inactive_serial(base / "attempt-1-shadow", attempt="1")
    _args(base / "attempt-1-shadow", [*XDIST, "tests"], attempt="1")
    return {"base": base, "exits": exits}


def _verdict(runs: dict, reference: str, candidate: str) -> dict:
    base = runs["base"]
    return cxp.evaluate(cxp.CompareInputs(
        reference_dir=base / reference, candidate_dir=base / candidate, constraints=CONSTRAINTS, args_dir=None,
        workspace_root=str(base), profile="heavy", meta=dict(RUN), steps={}, upstream={},
    ))


def _testcases(path: Path) -> list:
    return [(tc.get("classname"), tc.get("name"), [c.tag for c in tc if c.tag in ("failure", "error", "skipped")])
            for tc in ET.parse(str(path)).getroot().iter("testcase")]


def _manifest(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_the_runs_end_as_the_pinned_tools_end_them(runs):
    assert runs["exits"] == {
        "reference": 1, "shadow": 1, "two_phase_serial": 1, "two_phase_parallel": 0, "dropped": 1,
        "broken_reference": 2, "broken_shadow": 1, "attempt_1_shadow": 1,
    }


def test_a_serial_and_an_xdist_run_of_one_suite_are_parity(runs):
    verdict = _verdict(runs, "reference", "shadow")
    assert verdict["invalid_evidence"] == [] and verdict["exit"] == 0, verdict["differences"]
    for side in ("reference", "parallel"):
        counts = verdict["counts"][side]
        assert counts["collected"] == counts["executed"] == 11
        assert counts["classes"] == {"fail": 2, "xfail": 1, "skip": 2, "pass": 6}
        assert counts["records"] == 13, "11 tests, the second record of the M16 pair, one collection record"


def test_the_key_function_matches_every_real_record(runs):
    key = cxp.junit_key_function()
    for directory, phase in (("reference", "full"), ("shadow", "parallel")):
        recorded = {(c, n) for c, n, _ in _testcases(runs["base"] / directory / f"junit-{phase}.xml")}
        nodeids = _manifest(runs["base"] / directory / f"manifest-{phase}.json")["nodeids"]
        assert {key(n) for n in nodeids} <= recorded
        assert ("tests.test_ok", "test_ids[x.y/z]") in recorded and ("tests.test_ok", "test_ids[c#d]") in recorded


def test_the_m16_pair_is_two_records_and_one_execution(runs):
    key = cxp.junit_key_function()(M16)
    for directory, phase in (("reference", "full"), ("shadow", "parallel")):
        records = [kids for c, n, kids in _testcases(runs["base"] / directory / f"junit-{phase}.xml") if (c, n) == key]
        assert records == [["failure"], ["error"]]
    assert _verdict(runs, "reference", "shadow")["differences"]["run_more_than_once"] == []


def test_a_module_level_importorskip_is_a_collection_outcome_in_both_shapes(runs):
    for directory, phase in (("reference", "full"), ("shadow", "parallel")):
        outcomes = _manifest(runs["base"] / directory / f"manifest-{phase}.json")["collection_outcomes"]
        assert [(o["nodeid"], o["outcome"], o["reports"]) for o in outcomes] == [("tests/test_modskip.py", "skipped", 1)]
    verdict = _verdict(runs, "reference", "shadow")
    assert verdict["counts"]["reference"]["collection_records"] == verdict["counts"]["parallel"]["collection_records"] == 1


def test_a_skip_reason_with_tmp_path_differs_between_the_shapes_and_does_not_count(runs):
    key = cxp.junit_key_function()(TMP_SKIP)

    def reason(directory: str, phase: str) -> str:
        root = ET.parse(str(runs["base"] / directory / f"junit-{phase}.xml")).getroot()
        [tc] = [t for t in root.iter("testcase") if (t.get("classname"), t.get("name")) == key]
        return tc.find("skipped").get("message")

    serial, xdist = reason("reference", "full"), reason("shadow", "parallel")
    assert serial != xdist and "popen-gw" in xdist and "popen-gw" not in serial
    assert _verdict(runs, "reference", "shadow")["differences"]["skip_set_differs"] == []


def test_the_two_phase_shape_with_an_active_serial_phase_is_parity(runs):
    verdict = _verdict(runs, "reference", "two-phase")
    assert verdict["exit"] == 0, (verdict["invalid_evidence"], verdict["differences"])
    assert verdict["counts"]["serial"]["executed"] == 2 and verdict["counts"]["parallel"]["executed"] == 9
    assert verdict["differences"]["partition"] == []


def test_a_candidate_that_silently_dropped_a_file(runs):
    verdict = _verdict(runs, "reference", "dropped")
    assert verdict["exit"] == 1 and verdict["invalid_evidence"] == []
    differences = verdict["differences"]
    assert [e["nodeid"] for e in differences["not_run_in_candidate"]] == OTHER
    assert differences["partition"] == [{"nodeid": n, "problem": "in no candidate phase"} for n in OTHER]
    assert not any(differences[k] for k in cxp.DIFFERENCE_KINDS if k not in ("not_run_in_candidate", "partition"))


def test_a_collection_error_is_one_outcome_in_both_shapes(runs):
    base = runs["base"]
    ref = _manifest(base / "broken-reference" / "manifest-full.json")["collection_outcomes"]
    par = _manifest(base / "broken-shadow" / "manifest-parallel.json")["collection_outcomes"]
    assert ("tests/test_broken.py", "failed", 1) in [(o["nodeid"], o["outcome"], o["reports"]) for o in ref]
    assert ("tests/test_broken.py", "failed", 2) in [(o["nodeid"], o["outcome"], o["reports"]) for o in par]
    broken_records = [kids for c, n, kids in _testcases(base / "broken-shadow" / "junit-parallel.xml") if n == "tests.test_broken"]
    assert broken_records == [["error", "error"]], "one record, one <error> per worker"
    verdict = _verdict(runs, "broken-reference", "broken-shadow")
    assert verdict["invalid_evidence"] == []
    differences = verdict["differences"]
    assert differences["collection_outcome_differs"] == [] and differences["run_more_than_once"] == []


def test_with_a_collection_error_serial_runs_nothing_and_xdist_runs_the_rest(runs):
    verdict = _verdict(runs, "broken-reference", "broken-shadow")
    assert verdict["counts"]["reference"]["executed"] == 0 and verdict["counts"]["reference"]["collected"] == 11
    assert verdict["counts"]["parallel"]["executed"] == 11
    assert verdict["exit"] == 1
    assert len(verdict["differences"]["extra_in_candidate"]) == 11


def test_every_real_run_carries_the_run_stamp_of_its_step(runs):
    base = runs["base"]
    for directory, phase in (("reference", "full"), ("shadow", "parallel"), ("two-phase", "serial")):
        manifest = _manifest(base / directory / f"manifest-{phase}.json")
        assert (manifest["run_id"], manifest["run_attempt"]) == (RUN["run_id"], RUN["run_attempt"])
        root = ET.parse(str(base / directory / f"junit-{phase}.xml")).getroot()
        assert cxp.junit_stamp(root) == RUN, f"{directory}/junit-{phase}.xml"


def test_a_real_shadow_of_an_earlier_attempt_is_stale_evidence(runs):
    """The same suite, the same commit, run in attempt 1: in attempt 2 it is not this attempt's candidate."""
    verdict = _verdict(runs, "reference", "attempt-1-shadow")
    assert verdict["exit"] == 2 and verdict["differences"] is None
    stale = sorted(p["detail"] for p in verdict["invalid_evidence"] if p["reason"] == "stale_evidence")
    assert stale == [
        "manifest-parallel.json of attempt 1, not of attempt 2",
        "manifest-serial.json of attempt 1, not of attempt 2",
        "shadow args/run.json of attempt 1, not of attempt 2",
    ]
