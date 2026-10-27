"""Tests for scripts/ci/red_on_base.py: two worktrees, one pytest run per side, one verdict per test."""

from __future__ import annotations

import importlib.util
import json
import os
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
DELETE = object()  # a ``{path: DELETE}`` entry in ``_commit_all`` commits the removal of that path
VALUE_ERROR_BASE = {"tinypkg/__init__.py": "def value():\n    raise ValueError('the base has no value')\n"}
VALUE_ERROR_TEST = "from tinypkg import value\n\n\ndef test_it():\n    assert value() == 2\n"
STALE_CONFTEST = (  # an autouse fixture that makes a head test fail on base only
    "import pytest\n\n\n@pytest.fixture(autouse=True)\ndef old_value(monkeypatch):\n"
    "    import tinypkg\n\n    monkeypatch.setattr(tinypkg, 'value', lambda: 1)\n")
DELETED_CONFTEST_TEST = "import tinypkg\n\n\ndef test_claim():\n    assert tinypkg.value() == 2\n"

spec = importlib.util.spec_from_file_location("red_on_base", SCRIPT)
rl = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = rl
spec.loader.exec_module(rl)


def _git(repo, *args):
    done = subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.com",
                           "-c", "commit.gpgsign=false", *args], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    return done.stdout.strip()


def _commit_all(root, *commits):
    """One commit per ``{path: text}``; a ``Path`` value is committed as a symlink to that path and
    ``DELETE`` as the removal of that path."""
    _git(root, "init")
    for index, sources in enumerate(commits):
        for name, content in sources.items():
            path = root / name
            for link in (path, *(root / parent for parent in Path(name).parents[:-1])):
                if link.is_symlink():  # the previous commit's symlink: never write through it
                    link.unlink()
            if content is DELETE:
                path.unlink()
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            if isinstance(content, Path):
                path.symlink_to(content)
            else:
                path.write_text(content)
        _git(root, "add", "-A")
        _git(root, "commit", "-m", f"commit {index}")
    return root


@pytest.fixture(scope="module")
def repo(tmp_path_factory):
    """A two-commit repo: base returns 1; head returns 2 and adds ``new_helper``."""
    return _commit_all(tmp_path_factory.mktemp("red-on-base"), BASE, HEAD)


def _run(repo, tmp_path, selected, claims=None, extra=(), env=None):
    (tmp_path / "selection.json").write_text(
        json.dumps({"selected": selected, "conftest_changed": ["tests/conftest.py"]}))
    argv = [sys.executable, str(SCRIPT), "--base", _git(repo, "rev-parse", "HEAD^"), "--head",
            _git(repo, "rev-parse", "HEAD"), "--selection", str(tmp_path / "selection.json"),
            "--out", str(tmp_path / "verdict.json"), *extra]
    if claims:
        (tmp_path / "claims.txt").write_text("\n".join(claims) + "\n")
        argv += ["--claims", str(tmp_path / "claims.txt")]
    done = subprocess.run(argv, cwd=str(repo), capture_output=True, text=True, timeout=180,
                          env={**os.environ, **(env or {})})
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
    assert verdict["summary"] == {"behavior": 1, "error_on_base": 0, "not_behavior": 1, "green_on_base": 1,
                                  "fails_on_head": 0, "skipped": 1, "incomplete": 0}


def test_fails_on_head_and_false_claim_exit_one(repo, tmp_path):
    done, verdict = _run(repo, tmp_path, ["tests/test_head_fail.py"],
                         claims=["tests/test_claim_green.py::test_claimed_green"])
    assert done.returncode == 1, done.stdout + done.stderr
    verdicts = {entry["nodeid"]: entry["verdict"] for entry in verdict["tests"]}
    assert verdicts["tests/test_head_fail.py::test_red_on_head"] == "fails_on_head"
    assert verdicts["tests/test_claim_green.py::test_claimed_green"] == "green_on_base"
    assert verdict["false_claims"] == ["tests/test_claim_green.py::test_claimed_green"]


@pytest.fixture(scope="module")
def value_error_repo(tmp_path_factory):
    """Base raises ``ValueError``; head returns 2 and adds a test that is green there only."""
    head = {"tinypkg/__init__.py": HEAD["tinypkg/__init__.py"], "tests/test_value.py": VALUE_ERROR_TEST}
    return _commit_all(tmp_path_factory.mktemp("value-error"), VALUE_ERROR_BASE, head)


def test_a_base_value_error_never_confirms_a_claim(value_error_repo, tmp_path):
    done, verdict = _run(value_error_repo, tmp_path, ["tests/test_value.py"],
                         claims=["tests/test_value.py::test_it"])
    assert done.returncode == 1, done.stdout + done.stderr
    entry = {item["nodeid"]: item for item in verdict["tests"]}["tests/test_value.py::test_it"]
    assert (entry["verdict"], entry["base_outcome"], entry["head_outcome"]) == (
        "error_on_base", "failed", "passed")
    assert verdict["false_claims"] == ["tests/test_value.py::test_it"]


@pytest.fixture(scope="module")
def deleted_conftest_repo(tmp_path_factory):
    """Base already returns 2, but a stale autouse conftest fixture hides it; head deletes that conftest
    and adds a test that is green on base as soon as the deletion is mirrored there."""
    base = {"tinypkg/__init__.py": "def value():\n    return 2\n", "tests/conftest.py": STALE_CONFTEST}
    head = {"tests/conftest.py": DELETE, "tests/test_claim.py": DELETED_CONFTEST_TEST}
    return _commit_all(tmp_path_factory.mktemp("deleted-conftest"), base, head)


def test_a_deleted_conftest_is_deleted_on_base_too(deleted_conftest_repo, tmp_path):
    done, verdict = _run(deleted_conftest_repo, tmp_path, ["tests/test_claim.py"],
                         claims=["tests/test_claim.py::test_claim"])
    assert done.returncode == 1, done.stdout + done.stderr
    entry = {item["nodeid"]: item for item in verdict["tests"]}["tests/test_claim.py::test_claim"]
    assert (entry["verdict"], entry["head_outcome"]) == ("green_on_base", "passed")
    assert verdict["false_claims"] == ["tests/test_claim.py::test_claim"]


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
    assert rl.classify_verdict(outcome("failed", "TypeError: bad call"), outcome("passed")) == "error_on_base"
    assert rl.classify_verdict(outcome("failed", "ValueError: boom"), outcome("passed")) == "error_on_base"
    assert rl.classify_verdict(outcome("failed", "RuntimeError: boom"), outcome("passed")) == "error_on_base"
    assert rl.classify_verdict(outcome("failed", ""), outcome("passed")) == "error_on_base"  # unnamed: never behaviour
    assert rl.classify_verdict(outcome("failed", "AssertionError: x", "builtins.AssertionError"),
                               outcome("passed")) == "behavior"
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
        rl.read_manifest(tmp_path / "manifest.json", 0)


ABNORMAL_END = (  # the base run writes complete JUnit XML and a manifest, then does not end normally
    "import os\nimport signal\nimport time\n\nfrom tinypkg import value\n\n\n"
    "def pytest_unconfigure(config):\n"
    "    if value() == 1:\n"
    "        end = os.environ['FAKE_PYTEST_END']\n"
    "        if end == 'timeout':\n            time.sleep(120)\n"
    "        elif end == 'signal':\n            os.kill(os.getpid(), signal.SIGKILL)\n"
    "        else:\n            os._exit(int(end))\n")
TEST_VALUE_2 = "from tinypkg import value\n\n\ndef test_it():\n    assert value() == 2\n"


@pytest.fixture(scope="module")
def abnormal_repo(tmp_path_factory):
    head = {"tinypkg/__init__.py": HEAD["tinypkg/__init__.py"], "tests/conftest.py": ABNORMAL_END,
            "tests/test_abnormal.py": TEST_VALUE_2 + "\n\ndef test_passes():\n    assert value() in (1, 2)\n"}
    return _commit_all(tmp_path_factory.mktemp("abnormal"), BASE, head)


@pytest.mark.parametrize("end, reason", [("2", "exit status 2"), ("3", "exit status 3"),
                                         ("signal", "exit status -9"), ("timeout", "did not complete")])
def test_evidence_of_an_abnormal_pytest_end_is_incomplete(abnormal_repo, tmp_path, end, reason):
    done, verdict = _run(abnormal_repo, tmp_path, ["tests/test_abnormal.py"],
                         claims=["tests/test_abnormal.py::test_it"], extra=["--timeout", "6"],
                         env={"FAKE_PYTEST_END": end})
    assert done.returncode == 1, done.stdout + done.stderr
    assert {entry["verdict"] for entry in verdict["tests"]} == {"incomplete"}
    assert all(reason in entry["base_message"] for entry in verdict["tests"]), verdict["tests"]
    assert verdict["false_claims"] == ["tests/test_abnormal.py::test_it"]


@pytest.fixture(scope="module")
def symlink_repo(tmp_path_factory):
    """Base: a test file linked to an outside file, a test dir linked to an outside dir. Head: real files
    there, plus a test dir linked to an outside dir holding a test."""
    outside = tmp_path_factory.mktemp("outside")
    (outside / "sub").mkdir()
    (outside / "ext").mkdir()
    (outside / "ext" / "test_ext.py").write_text(TEST_VALUE_2)
    (outside / "victim.py").write_text("VICTIM\n")
    base = {**BASE, "tests/test_dest.py": outside / "victim.py", "tests/sub": outside / "sub"}
    head = {"tinypkg/__init__.py": HEAD["tinypkg/__init__.py"], "tests/test_dest.py": TEST_VALUE_2,
            "tests/sub/test_sub.py": TEST_VALUE_2, "tests/ext": outside / "ext", "tests/test_ok.py": TEST_VALUE_2}
    return _commit_all(tmp_path_factory.mktemp("symlinks"), base, head), outside


def test_copy_never_goes_through_a_symlink_or_outside_a_tree(symlink_repo, tmp_path):
    repo, outside = symlink_repo
    refused = ["tests/ext/test_ext.py", "tests/sub/test_sub.py", "tests/test_dest.py"]
    done, verdict = _run(repo, tmp_path, refused + ["tests/test_ok.py"], claims=["tests/ext/test_ext.py::test_it"])
    assert done.returncode == 1, done.stdout + done.stderr
    assert (outside / "victim.py").read_text() == "VICTIM\n"
    assert sorted(path.relative_to(outside).as_posix() for path in outside.rglob("*")) == [
        "ext", "ext/test_ext.py", "sub", "victim.py"]
    verdicts = {entry["nodeid"]: (entry["verdict"], entry["base_message"] or "") for entry in verdict["tests"]}
    assert verdicts["tests/test_ok.py::test_it"][0] == "behavior"
    for rel in refused:
        assert verdicts[rel][0] == "incomplete" and verdicts[rel][1].startswith(f"refused to copy {rel}")
    assert verdict["false_claims"] == ["tests/ext/test_ext.py::test_it"]


def _evidence(tmp_path, run_exit, **fields):
    """JUnit XML and a manifest of a run that exited *run_exit*; *fields* override manifest fields."""
    (tmp_path / "side.xml").write_text('<testsuite><testcase classname="tests.test_x" name="test_y"/></testsuite>')
    (tmp_path / "side.json").write_text(json.dumps({
        "schema": "vnx-collection-manifest/1", "phase_active": True, "collection_finished": True,
        "exitstatus": run_exit, "nodeids": ["tests/test_x.py::test_y"], "collection_outcomes": [], **fields}))
    return tmp_path / "side.xml", tmp_path / "side.json", run_exit


def test_evidence_of_a_completed_run_is_used(tmp_path):
    side = rl.read_side(*_evidence(tmp_path, 1))
    assert side.problems == [] and side.outcomes[("tests.test_x", "test_y")].kind == "passed"


@pytest.mark.parametrize("exitstatus, fields", [
    (2, {}), (3, {}), (4, {}), (5, {}), (-9, {}),
    (0, {"phase_active": False}), (0, {"collection_finished": None}), (0, {"exitstatus": 1}),
    (1, {"exitstatus": True}), (0, {"exitstatus": "0"}), (0, {"exitstatus": None}),
    (0, {"nodeids": ["tests/test_x.py::test_y", "tests/test_x.py::test_z"]}),  # collected, no JUnit result
])
def test_evidence_of_an_unfinished_run_is_never_used(tmp_path, exitstatus, fields):
    side = rl.read_side(*_evidence(tmp_path, exitstatus, **fields))
    assert side.outcomes == {} and side.nodeids == {} and side.collection == [] and side.problems


def test_a_refused_conftest_copy_leaves_no_file_comparable(tmp_path):
    head_tree, base_tree, outside = tmp_path / "head", tmp_path / "base", tmp_path / "outside.py"
    for rel in ("tests/conftest.py", "tests/test_a.py"):
        (head_tree / rel).parent.mkdir(parents=True, exist_ok=True)
        (head_tree / rel).write_text("HEAD\n")
    (base_tree / "tests").mkdir(parents=True)
    outside.write_text("OUTSIDE\n")
    (base_tree / "tests" / "conftest.py").symlink_to(outside)
    reasons = rl.copy_head_tests(head_tree, base_tree, ["tests/test_a.py"], ["tests/conftest.py"])
    assert list(reasons) == ["tests/test_a.py"] and reasons["tests/test_a.py"].startswith(
        "refused to copy tests/conftest.py") and reasons["tests/test_a.py"].endswith("is a symlink")
    assert outside.read_text() == "OUTSIDE\n"


def _deletion_trees(tmp_path, conftest_rel):
    """A head tree holding only ``tests/test_a.py`` and a base tree holding *conftest_rel* as well."""
    head_tree, base_tree = tmp_path / "head", tmp_path / "base"
    (head_tree / "tests").mkdir(parents=True)
    (head_tree / "tests" / "test_a.py").write_text("HEAD\n")
    (base_tree / conftest_rel).parent.mkdir(parents=True)
    (base_tree / conftest_rel).write_text("OLD\n")
    return head_tree, base_tree


def test_a_deleted_conftest_is_deleted_from_the_base_copy(tmp_path):
    head_tree, base_tree = _deletion_trees(tmp_path, "tests/conftest.py")
    assert rl.copy_head_tests(head_tree, base_tree, ["tests/test_a.py"], ["tests/conftest.py"]) == {}
    assert not (base_tree / "tests" / "conftest.py").exists()
    assert (base_tree / "tests" / "test_a.py").read_text() == "HEAD\n"


def test_a_deleted_conftest_that_is_a_symlink_leaves_no_file_comparable(tmp_path):
    head_tree, base_tree = _deletion_trees(tmp_path, "tests/conftest.py")
    outside = tmp_path / "outside.py"
    outside.write_text("OUTSIDE\n")
    (base_tree / "tests" / "conftest.py").unlink()
    (base_tree / "tests" / "conftest.py").symlink_to(outside)
    reasons = rl.copy_head_tests(head_tree, base_tree, ["tests/test_a.py"], ["tests/conftest.py"])
    assert reasons["tests/test_a.py"].startswith("refused to delete tests/conftest.py")
    assert reasons["tests/test_a.py"].endswith("is a symlink")
    assert outside.read_text() == "OUTSIDE\n"


def test_a_deleted_conftest_under_a_symlinked_dir_is_refused(tmp_path):
    head_tree, base_tree = _deletion_trees(tmp_path, "tests/sub/conftest.py")
    outside = tmp_path / "outside"
    (base_tree / "tests" / "sub").rename(outside)
    (base_tree / "tests" / "sub").symlink_to(outside)
    reasons = rl.copy_head_tests(head_tree, base_tree, ["tests/test_a.py"], ["tests/sub/conftest.py"])
    assert reasons["tests/test_a.py"].startswith("refused to delete tests/sub/conftest.py")
    assert "outside" in reasons["tests/test_a.py"]
    assert (outside / "conftest.py").read_text() == "OLD\n"
