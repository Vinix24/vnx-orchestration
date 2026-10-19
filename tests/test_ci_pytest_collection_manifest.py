"""Tests for scripts/ci/pytest_collection_manifest.py (track ci-profile-a-xdist, D1).

The plugin records what one pytest run collected, from that same run, so the
parity check can compare a serial sweep with an xdist run. These tests run the
real pytest (and the real pytest-xdist, pinned in
scripts/ci/profile_a_constraints.txt) over a small fixture suite written into
tmp_path: a passing test, a skip, an xfail, a module-level importorskip and a
file that errors at collection, serially and with -n 2.

What xdist forwards to the controller was measured with pytest 9.1.1 and
pytest-xdist 3.8.0: every collection report that did not pass. A module-level
skip arrives once (deduplicated by its longrepr tuple), a collection error once
per worker. The xdist tests below pin that, through the manifest's
"reports" count.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = REPO_ROOT / "scripts" / "ci"
PLUGIN = PLUGIN_DIR / "pytest_collection_manifest.py"
CONSTRAINTS = PLUGIN_DIR / "profile_a_constraints.txt"

_SPEC = importlib.util.spec_from_file_location("pytest_collection_manifest_under_test", PLUGIN)
pcm = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(pcm)

OK_FILE = (
    "import pytest\n\n\n"
    "def test_pass():\n    pass\n\n\n"
    "@pytest.mark.skip(reason='plain skip')\n"
    "def test_skipped():\n    pass\n\n\n"
    "@pytest.mark.xfail(reason='known', strict=True)\n"
    "def test_xfail():\n    assert False\n"
)
MODSKIP_FILE = (
    "import pytest\n\n"
    "pytest.importorskip('module_that_does_not_exist_vnx_d1')\n\n\n"
    "def test_never():\n    pass\n"
)
BROKEN_FILE = "raise RuntimeError('boom at import')\n"
OK_NODEIDS = ["tests/test_ok.py::test_pass", "tests/test_ok.py::test_skipped", "tests/test_ok.py::test_xfail"]


def _suite(tmp_path: Path, broken: bool) -> Path:
    root = tmp_path / "suite"
    (root / "tests").mkdir(parents=True)
    (root / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    (root / "tests" / "test_ok.py").write_text(OK_FILE, encoding="utf-8")
    (root / "tests" / "test_modskip.py").write_text(MODSKIP_FILE, encoding="utf-8")
    if broken:
        (root / "tests" / "test_broken.py").write_text(BROKEN_FILE, encoding="utf-8")
    return root


RUN = {"GITHUB_RUN_ID": "38939122085", "GITHUB_RUN_ATTEMPT": "2"}


def _env(run: dict | None = RUN) -> dict:
    """The environment of a step in run *run* (the GITHUB_* variables), or outside CI when *run* is None."""
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("PYTEST_") and k not in ("GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT")}
    env.update(run or {})
    env["PYTHONPATH"] = str(PLUGIN_DIR)
    return env


def _run(root: Path, *extra: str, phase: str = "t", run: dict | None = RUN) -> tuple:
    manifest = root / f"manifest-{phase}.json"
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-p", "pytest_collection_manifest",
         f"--collection-manifest={manifest}", f"--collection-manifest-phase={phase}", *extra, "tests"],
        cwd=str(root),
        env=_env(run),
        capture_output=True,
        text=True,
    )
    data = json.loads(manifest.read_text(encoding="utf-8")) if manifest.exists() else None
    return proc, data


def _outcomes(manifest: dict) -> dict:
    return {(o["nodeid"], o["outcome"]): o for o in manifest["collection_outcomes"]}


def test_serial_manifest_records_nodeids_and_collection_outcomes(tmp_path):
    proc, manifest = _run(_suite(tmp_path, broken=True), "-p", "no:xdist")
    assert proc.returncode == 2, proc.stdout
    assert manifest["schema"] == "vnx-collection-manifest/1"
    assert manifest["phase"] == "t" and manifest["phase_active"] is True
    assert manifest["mode"] == "serial" and manifest["worker_count"] == 0 and manifest["xdist"] is None
    assert manifest["exitstatus"] == 2 and manifest["collection_finished"] is True
    assert manifest["nodeids"] == OK_NODEIDS and manifest["nodeid_count"] == 3
    outcomes = _outcomes(manifest)
    assert set(outcomes) == {("tests/test_broken.py", "failed"), ("tests/test_modskip.py", "skipped")}
    assert "module_that_does_not_exist_vnx_d1" in outcomes[("tests/test_modskip.py", "skipped")]["reason"]
    assert "boom at import" in outcomes[("tests/test_broken.py", "failed")]["reason"]
    assert all(o["reports"] == 1 for o in outcomes.values())


def test_serial_manifest_of_a_clean_run(tmp_path):
    proc, manifest = _run(_suite(tmp_path, broken=False), "-p", "no:xdist")
    assert proc.returncode == 0, proc.stdout
    assert manifest["nodeids"] == OK_NODEIDS
    assert set(_outcomes(manifest)) == {("tests/test_modskip.py", "skipped")}


def test_xdist_manifest_matches_the_serial_one(tmp_path):
    pytest.importorskip("xdist")
    root = _suite(tmp_path, broken=False)
    _, serial = _run(root, "-p", "no:xdist", phase="serial")
    proc, par = _run(root, "-n", "2", "--dist", "loadfile", phase="parallel")
    assert proc.returncode == 0, proc.stdout
    assert par["mode"] == "xdist" and par["worker_count"] == 2
    assert par["xdist"]["dist"] == "loadfile"
    assert par["xdist"]["workers_reported"] == 2 and par["xdist"]["workers_agree"] is True
    assert set(par["xdist"]["workers"]) == {"gw0", "gw1"}
    assert all(w["nodeid_count"] == 3 for w in par["xdist"]["workers"].values())
    assert par["nodeids"] == serial["nodeids"] == OK_NODEIDS
    assert set(_outcomes(par)) == set(_outcomes(serial)) == {("tests/test_modskip.py", "skipped")}
    assert _outcomes(par)[("tests/test_modskip.py", "skipped")]["reports"] == 1


def test_xdist_forwards_the_skip_once_and_the_error_once_per_worker(tmp_path):
    pytest.importorskip("xdist")
    proc, manifest = _run(_suite(tmp_path, broken=True), "-n", "2")
    assert proc.returncode == 1, proc.stdout
    assert manifest["mode"] == "xdist" and manifest["exitstatus"] == 1
    assert manifest["nodeids"] == OK_NODEIDS
    outcomes = _outcomes(manifest)
    assert set(outcomes) == {("tests/test_broken.py", "failed"), ("tests/test_modskip.py", "skipped")}
    assert outcomes[("tests/test_modskip.py", "skipped")]["reports"] == 1
    assert outcomes[("tests/test_broken.py", "failed")]["reports"] == 2


def test_manifest_records_the_pinned_tool_versions(tmp_path):
    _, manifest = _run(_suite(tmp_path, broken=False), "-p", "no:xdist")
    assert set(manifest["tool_versions"]) == set(pcm.PINNED_TOOLS)
    assert manifest["tool_versions"]["pytest"] == pytest.__version__


def test_constraints_pin_exactly_the_recorded_tools():
    pins = {}
    for line in CONSTRAINTS.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            name, sep, version = line.partition("==")
            assert sep == "==" and version, f"not an exact pin: {line}"
            pins[name] = version
    assert set(pins) == set(pcm.PINNED_TOOLS)


def test_the_phase_is_required_with_a_manifest_path(tmp_path):
    root = _suite(tmp_path, broken=False)
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "pytest_collection_manifest",
         f"--collection-manifest={root / 'm.json'}", "tests"],
        cwd=str(root), env=_env(), capture_output=True, text=True,
    )
    assert proc.returncode == 4
    assert "--collection-manifest-phase is required" in proc.stderr
    assert not (root / "m.json").exists()


def test_the_plugin_without_a_path_writes_nothing(tmp_path):
    root = _suite(tmp_path, broken=False)
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-p", "pytest_collection_manifest", "tests"],
        cwd=str(root), env=_env(), capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stdout
    assert not list(root.rglob("*.json"))


def _junit_stamp(path: Path) -> dict:
    """The vnx_* testsuite properties of a JUnit XML."""
    root = ET.parse(str(path)).getroot()
    return {prop.get("name"): prop.get("value") for suite in root.iter("testsuite")
            for prop in suite.findall("properties/property") if (prop.get("name") or "").startswith("vnx_")}


def test_the_manifest_and_the_xml_of_a_run_carry_its_run_stamp(tmp_path):
    root = _suite(tmp_path, broken=False)
    proc, manifest = _run(root, "-p", "no:xdist", f"--junitxml={root / 'junit-t.xml'}")
    assert proc.returncode == 0, proc.stdout
    assert (manifest["run_id"], manifest["run_attempt"]) == ("38939122085", "2")
    assert _junit_stamp(root / "junit-t.xml") == {"vnx_run_id": "38939122085", "vnx_run_attempt": "2"}


def test_under_xdist_the_controller_stamps_the_xml_it_writes(tmp_path):
    pytest.importorskip("xdist")
    root = _suite(tmp_path, broken=False)
    proc, manifest = _run(root, "-n", "2", f"--junitxml={root / 'junit-t.xml'}")
    assert proc.returncode == 0, proc.stdout
    assert manifest["mode"] == "xdist" and manifest["run_attempt"] == "2"
    assert _junit_stamp(root / "junit-t.xml") == {"vnx_run_id": "38939122085", "vnx_run_attempt": "2"}


def test_outside_ci_neither_the_manifest_nor_the_xml_names_a_run(tmp_path):
    root = _suite(tmp_path, broken=False)
    proc, manifest = _run(root, "-p", "no:xdist", f"--junitxml={root / 'junit-t.xml'}", run=None)
    assert proc.returncode == 0, proc.stdout
    assert manifest["run_id"] is None and manifest["run_attempt"] is None
    assert _junit_stamp(root / "junit-t.xml") == {}


def test_a_run_without_junitxml_still_writes_its_stamped_manifest(tmp_path):
    proc, manifest = _run(_suite(tmp_path, broken=False), "-p", "no:xdist")
    assert proc.returncode == 0, proc.stdout
    assert manifest["run_attempt"] == "2"


def test_inactive_cli_writes_an_inactive_manifest(tmp_path):
    out = tmp_path / "evidence" / "manifest-serial.json"
    assert pcm.main(["inactive", "--phase", "serial", "--output", str(out)]) == 0
    manifest = json.loads(out.read_text(encoding="utf-8"))
    assert manifest["phase"] == "serial" and manifest["phase_active"] is False
    assert manifest["nodeids"] == [] and manifest["collection_outcomes"] == []
    assert set(manifest["tool_versions"]) == set(pcm.PINNED_TOOLS)
    assert not [p for p in out.parent.iterdir() if p.name != out.name], "temp file left behind"


def test_the_inactive_manifest_carries_the_run_stamp_of_its_step(tmp_path):
    out = tmp_path / "manifest-serial.json"
    subprocess.run([sys.executable, str(PLUGIN), "inactive", "--phase", "serial", "--output", str(out)],
                   env=_env(), check=True)
    manifest = json.loads(out.read_text(encoding="utf-8"))
    assert (manifest["run_id"], manifest["run_attempt"]) == ("38939122085", "2")


def test_nodeids_cli_prints_one_per_line(tmp_path, capsys):
    path = tmp_path / "m.json"
    path.write_text(json.dumps({"nodeids": ["a.py::t1", "a.py::t[x y]"]}), encoding="utf-8")
    assert pcm.main(["nodeids", str(path)]) == 0
    assert capsys.readouterr().out.splitlines() == ["a.py::t1", "a.py::t[x y]"]


@pytest.mark.parametrize("body", [None, "", "not json", '{"no_nodeids": []}'])
def test_nodeids_cli_refuses_an_unreadable_manifest(tmp_path, capsys, body):
    path = tmp_path / "m.json"
    if body is not None:
        path.write_text(body, encoding="utf-8")
    assert pcm.main(["nodeids", str(path)]) == 2
    assert "unreadable manifest" in capsys.readouterr().err
