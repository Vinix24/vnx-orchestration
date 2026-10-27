"""Tests for scripts/ci/red_run_claims.py: a PR body's claimed red runs become a claims file.

Track deterministische-testverificatie PR-3. ``parse_claims`` reads the label forms
``RED_RUN_LABELS`` names (``**Red run** on <sha>:``, ``Rode run:``, ``Before the fix:``) and the
list lines directly below them. End to end, a claim on a ``green_on_base`` test makes
``red_on_base.py --claims`` exit 1 and a claim on a ``behavior`` test exits 0.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "ci" / "red_run_claims.py"
RED_ON_BASE = REPO_ROOT / "scripts" / "ci" / "red_on_base.py"

spec = importlib.util.spec_from_file_location("red_run_claims", SCRIPT)
rrc = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = rrc
spec.loader.exec_module(rrc)

BASE = {"tinypkg/__init__.py": "def value():\n    return 1\n"}
HEAD = {
    "tinypkg/__init__.py": "def value():\n    return 2\n",
    "tests/test_claim.py": "from tinypkg import value\n\n\ndef test_claim():\n    assert value() == 2\n",
    "tests/test_green.py": "def test_always():\n    assert True\n",
}
BODY = """\
## Summary

**Red run** on abc123:
- `tests/test_a.py::test_one`
* tests/test_a.py::test_two

Prose in between.

Rode run: `tests/test_b.py::test_x`, tests/test_b.py::test_y

Before the fix:
- `tests/test_c.py::TestK::test_z`

**Groene run** on abc123:
- tests/test_d.py::test_never

Green: 3 passed

Groen:
- tests/test_e.py::test_green
"""
EXPECTED = [
    "tests/test_a.py::test_one",
    "tests/test_a.py::test_two",
    "tests/test_b.py::test_x",
    "tests/test_b.py::test_y",
    "tests/test_c.py::TestK::test_z",
]


def test_the_four_claim_forms_and_a_non_red_label():
    claims = rrc.parse_claims(BODY)
    assert claims == EXPECTED
    assert "tests/test_d.py::test_never" not in claims
    assert "tests/test_e.py::test_green" not in claims


def test_a_label_outside_red_run_labels_claims_nothing():
    assert rrc.parse_claims("**Groene run**:\n- tests/test_d.py::test_never\n") == []
    assert rrc.parse_claims("red run: tests/test_a.py::test_one\n") == []


def test_claims_are_deduplicated_and_keep_their_order():
    body = "Rode run:\n- tests/test_b.py::test_y\n- tests/test_a.py::test_one\n\nRode run:\n- tests/test_b.py::test_y\n"
    assert rrc.parse_claims(body) == ["tests/test_b.py::test_y", "tests/test_a.py::test_one"]


def test_a_body_without_claims_gives_an_empty_claims_file(tmp_path):
    body = tmp_path / "body.md"
    body.write_text("## Summary\n\nNothing claimed here.\n", encoding="utf-8")
    out = tmp_path / "claims.txt"
    done = subprocess.run([sys.executable, str(SCRIPT), str(body), "--out", str(out)],
                          capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    assert out.read_text(encoding="utf-8") == ""


def test_the_cli_refuses_a_body_it_cannot_read(tmp_path):
    out = tmp_path / "claims.txt"
    done = subprocess.run([sys.executable, str(SCRIPT), str(tmp_path / "missing.md"), "--out", str(out)],
                          capture_output=True, text=True, timeout=60)
    assert done.returncode == 2
    assert not out.exists()


def _git(repo, *args):
    done = subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.com",
                           "-c", "commit.gpgsign=false", *args], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    return done.stdout.strip()


def _commit_all(root, *commits):
    _git(root, "init")
    for index, sources in enumerate(commits):
        for name, content in sources.items():
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        _git(root, "add", "-A")
        _git(root, "commit", "-m", f"commit {index}")
    return root


@pytest.fixture(scope="module")
def repo(tmp_path_factory):
    """Base returns 1; head returns 2 and adds a behaviour test and an always-green test."""
    return _commit_all(tmp_path_factory.mktemp("red-run-claims"), BASE, HEAD)


def _claims_file(tmp_path: Path, body: str) -> Path:
    (tmp_path / "body.md").write_text(body, encoding="utf-8")
    out = tmp_path / "claims.txt"
    done = subprocess.run([sys.executable, str(SCRIPT), str(tmp_path / "body.md"), "--out", str(out)],
                          capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    return out


def _red_on_base(repo, tmp_path: Path, claims: Path) -> tuple:
    selection = tmp_path / "selection.json"
    selection.write_text(json.dumps({"selected": [], "conftest_changed": []}), encoding="utf-8")
    verdict_path = tmp_path / "verdict.json"
    done = subprocess.run(
        [sys.executable, str(RED_ON_BASE), "--base", _git(repo, "rev-parse", "HEAD^"),
         "--head", _git(repo, "rev-parse", "HEAD"), "--selection", str(selection),
         "--claims", str(claims), "--out", str(verdict_path)],
        cwd=str(repo), capture_output=True, text=True, timeout=180)
    verdict = json.loads(verdict_path.read_text(encoding="utf-8")) if verdict_path.is_file() else None
    return done, verdict


def test_a_claim_on_a_green_on_base_test_exits_one(repo, tmp_path):
    claims = _claims_file(tmp_path, "**Red run** on abc123:\n- tests/test_green.py::test_always\n")
    done, verdict = _red_on_base(repo, tmp_path, claims)
    assert done.returncode == 1, done.stdout + done.stderr
    verdicts = {entry["nodeid"]: entry["verdict"] for entry in verdict["tests"]}
    assert verdicts["tests/test_green.py::test_always"] == "green_on_base"
    assert verdict["false_claims"] == ["tests/test_green.py::test_always"]


def test_a_claim_on_a_behavior_test_exits_zero(repo, tmp_path):
    claims = _claims_file(tmp_path, "Rode run:\n- tests/test_claim.py::test_claim\n")
    done, verdict = _red_on_base(repo, tmp_path, claims)
    assert done.returncode == 0, done.stdout + done.stderr
    verdicts = {entry["nodeid"]: entry["verdict"] for entry in verdict["tests"]}
    assert verdicts["tests/test_claim.py::test_claim"] == "behavior"
    assert verdict["false_claims"] == []
