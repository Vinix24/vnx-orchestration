"""Tests for scripts/ci/red_on_base.py: two worktrees, one pytest run per side, one verdict per test."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "ci" / "red_on_base.py"
BASE = {"tinypkg/__init__.py": "def value():\n    return 1\n"}
HEAD = {
    "tinypkg/__init__.py": "def value():\n    return 2\n\n\ndef new_helper():\n    return 'hi'\n",
    "tests/conftest.py": "import pytest\n\n\n@pytest.fixture\ndef expected():\n    return 2\n",
    "tests/test_behavior.py": "from tinypkg import value\n\n\ndef test_claim(expected):\n    assert value() == expected\n\n\ndef test_green():\n    assert True\n",
    "tests/test_not_behavior.py": "from tinypkg import new_helper\n\n\ndef test_new():\n    assert new_helper() == 'hi'\n",
    "tests/test_head_fail.py": "def test_red_on_head():\n    assert False\n",
    "tests/test_skip.py": "import pytest\n\n\ndef test_skipped():\n    pytest.skip('later')\n",
    "tests/test_claim_green.py": "def test_claimed_green():\n    assert True\n",
}

spec = importlib.util.spec_from_file_location("red_on_base", SCRIPT)
rl = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = rl
spec.loader.exec_module(rl)


def _git(repo, *args):
    done = subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.com",
                           "-c", "commit.gpgsign=false", *args], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    return done.stdout.strip()


@pytest.fixture(scope="module")
def repo(tmp_path_factory):
    """A two-commit repo: base returns 1; head returns 2 and adds ``new_helper``."""
    root = tmp_path_factory.mktemp("red-on-base")
    _git(root, "init")
    for index, sources in enumerate((BASE, HEAD)):
        for name, text in sources.items():
            (root / name).parent.mkdir(parents=True, exist_ok=True)
            (root / name).write_text(text)
        _git(root, "add", "-A")
        _git(root, "commit", "-m", f"commit {index}")
    return root


def _run(repo, tmp_path, selected, claims=None):
    (tmp_path / "selection.json").write_text(
        json.dumps({"selected": selected, "conftest_changed": ["tests/conftest.py"]}))
    argv = [sys.executable, str(SCRIPT), "--base", _git(repo, "rev-parse", "HEAD^"), "--head",
            _git(repo, "rev-parse", "HEAD"), "--selection", str(tmp_path / "selection.json"),
            "--out", str(tmp_path / "verdict.json")]
    if claims:
        (tmp_path / "claims.txt").write_text("\n".join(claims) + "\n")
        argv += ["--claims", str(tmp_path / "claims.txt")]
    done = subprocess.run(argv, cwd=str(repo), capture_output=True, text=True, timeout=180)
    out = tmp_path / "verdict.json"
    return done, (json.loads(out.read_text()) if out.is_file() else None)


def test_one_verdict_per_case_and_exit_zero(repo, tmp_path):
    done, verdict = _run(repo, tmp_path, ["tests/test_behavior.py", "tests/test_not_behavior.py",
                                          "tests/test_skip.py"],
                         claims=["tests/test_behavior.py::test_claim"])
    assert done.returncode == 0, done.stdout + done.stderr
    verdicts = {entry["nodeid"]: entry["verdict"] for entry in verdict["tests"]}
    assert verdicts["tests/test_behavior.py::test_claim"] == "behavior"
    assert verdicts["tests/test_behavior.py::test_green"] == "green_on_base"
    assert verdicts["tests/test_not_behavior.py::test_new"] == "not_behavior"
    assert verdicts["tests/test_skip.py::test_skipped"] == "skipped"
    assert verdict["false_claims"] == [] and verdict["no_test"] is False
    assert verdict["summary"] == {"behavior": 1, "not_behavior": 1, "green_on_base": 1,
                                  "fails_on_head": 0, "skipped": 1, "incomplete": 0}


def test_fails_on_head_and_false_claim_exit_one(repo, tmp_path):
    done, verdict = _run(repo, tmp_path, ["tests/test_head_fail.py"],
                         claims=["tests/test_claim_green.py::test_claimed_green"])
    assert done.returncode == 1, done.stdout + done.stderr
    verdicts = {entry["nodeid"]: entry["verdict"] for entry in verdict["tests"]}
    assert verdicts["tests/test_head_fail.py::test_red_on_head"] == "fails_on_head"
    assert verdicts["tests/test_claim_green.py::test_claimed_green"] == "green_on_base"
    assert verdict["false_claims"] == ["tests/test_claim_green.py::test_claimed_green"]


def test_empty_selection_is_no_test(repo, tmp_path):
    done, verdict = _run(repo, tmp_path, [])
    assert done.returncode == 0, done.stderr
    assert verdict["no_test"] is True and verdict["tests"] == [] and verdict["false_claims"] == []


def test_classifier_and_unreadable_evidence(tmp_path):
    def case(body):
        return rl.outcome_of(ET.fromstring(f'<testcase classname="tests.test_x" name="test_y">{body}</testcase>'))

    outcome = rl.Outcome
    assert case("").kind == "passed"
    assert case('<failure type="AssertionError" message="boom">a</failure>').message == "AssertionError boom a"
    assert case('<error message="collection failure">x</error>').kind == "error"
    assert case('<skipped message="later"/>').kind == "skipped"
    assert case("<properties/><failure/>").kind == "failed"
    assert case("<unexpected/>").kind == "unknown"  # fail-closed: never a pass
    assert rl.classify_verdict(outcome("failed", "AssertionError: 1 != 2"), outcome("passed")) == "behavior"
    assert rl.classify_verdict(outcome("failed", "TypeError: bad call"), outcome("passed")) == "not_behavior"
    assert rl.classify_verdict(outcome("error", "ImportError: no name"), outcome("passed")) == "not_behavior"
    assert rl.classify_verdict(outcome("collection_error", "ModuleNotFoundError"), outcome("passed")) == "not_behavior"
    assert rl.classify_verdict(outcome("passed"), outcome("passed")) == "green_on_base"
    assert rl.classify_verdict(outcome("passed"), outcome("failed", "AssertionError")) == "fails_on_head"
    assert rl.classify_verdict(outcome("skipped"), outcome("passed")) == "skipped"
    assert rl.classify_verdict(outcome("collection_skipped"), outcome("passed")) == "incomplete"
    assert rl.classify_verdict(outcome("missing", "no evidence"), outcome("passed")) == "incomplete"
    assert rl.classify_verdict(outcome("failed", "AssertionError"), outcome("unknown")) == "incomplete"
    (tmp_path / "junit.xml").write_text("<testsuite><testcase")
    with pytest.raises(ValueError):
        rl.read_junit(tmp_path / "junit.xml")
    (tmp_path / "manifest.json").write_text(json.dumps({"nodeids": None}))
    with pytest.raises(ValueError):
        rl.read_manifest(tmp_path / "manifest.json")
