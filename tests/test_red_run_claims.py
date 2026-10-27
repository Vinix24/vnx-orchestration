"""Tests for scripts/ci/red_run_claims.py: a PR body's claimed red runs become a claims file.

Track deterministische-testverificatie PR-3. ``parse_claims`` reads the label forms
``RED_RUN_LABELS`` names (``**Red run** on <sha>:``, ``Rode run:``, ``Before the fix:``) and the
list lines directly below them. End to end, a claim on a ``green_on_base`` test makes
``red_on_base.py --claims`` exit 1 and a claim on a ``behavior`` test exits 0.

A parametrize part may hold whitespace, and a token inside a claim that looks like a nodeid
but cannot be read exits 3 with the token on stderr and no claims file: an unreadable claim
must never turn into "no claim".
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

BODY = """\
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
"""
EXPECTED = ["tests/test_a.py::test_one", "tests/test_a.py::test_two", "tests/test_b.py::test_x",
            "tests/test_b.py::test_y", "tests/test_c.py::TestK::test_z"]
BASE = {"tinypkg/__init__.py": "def value():\n    return 1\n"}
HEAD = {"tinypkg/__init__.py": "def value():\n    return 2\n",
        "tests/test_claim.py": "from tinypkg import value\n\n\ndef test_claim():\n    assert value() == 2\n",
        "tests/test_green.py": "def test_always():\n    assert True\n"}


def test_the_four_claim_forms_and_a_non_red_label():
    claims = rrc.parse_claims(BODY)
    assert claims == EXPECTED  # deduplicated and in body order
    assert "tests/test_d.py::test_never" not in claims


def test_a_label_outside_red_run_labels_claims_nothing():
    assert rrc.parse_claims("**Groene run**:\n- tests/test_d.py::test_never\n") == []
    assert rrc.parse_claims("red run: tests/test_a.py::test_one\n") == []


SPACED = "tests/test_claim.py::test_always[hello world]"


def test_a_parameter_id_with_a_space_is_read_exactly():
    body = f"**Red run** on abc123:\n- `{SPACED}`\n- `tests/test_a.py::test_one`\n"
    assert rrc.parse_claims(body) == [SPACED, "tests/test_a.py::test_one"]
    # The same claim twice is still one claim, and a plain id keeps its place in body order.
    twice = f"**Red run**:\n- `{SPACED}`\n- `tests/test_a.py::test_one`\n- `{SPACED}`\n"
    assert rrc.parse_claims(twice) == [SPACED, "tests/test_a.py::test_one"]


def test_whitespace_in_the_path_or_the_name_is_not_read_as_a_nodeid():
    # Whitespace is allowed only inside [...]: a backticked id with a space elsewhere is
    # refused, never silently kept as a claim.
    for token in ("tests/my test.py::test_one", "tests/test_a.py::test one", "tests/test_a.py::test one[x]"):
        body = f"**Red run**:\n- `{token}`\n"
        with pytest.raises(rrc.UnreadableClaim) as raised:
            rrc.parse_claims(body)
        assert raised.value.tokens == [token]


def test_a_parameter_id_with_a_space_goes_through_main(tmp_path):
    body = tmp_path / "body.md"
    body.write_text(f"**Red run** on abc123:\n- `{SPACED}`\n", encoding="utf-8")
    out = tmp_path / "claims.txt"
    assert rrc.main([str(body), "--out", str(out)]) == 0
    assert out.read_text(encoding="utf-8") == f"{SPACED}\n"


@pytest.mark.parametrize("token", [
    "tests/test_claim.py::test_always[hello world] extra",  # junk after the parameter part
    "tests/test claim.py::test_always[hello world]",        # whitespace in the path
    "tests/test_claim.py::",                                # no test name at all
    "tests/test_claim.py::test_always[hello world",         # parameter list left open
])
def test_a_backticked_claim_that_cannot_be_read_is_refused(token, tmp_path, capsys):
    body = tmp_path / "body.md"
    body.write_text(f"**Red run** on abc123:\n- `{token}`\n", encoding="utf-8")
    out = tmp_path / "claims.txt"
    assert rrc.main([str(body), "--out", str(out)]) == 3
    captured = capsys.readouterr()
    assert captured.out == ""
    assert f"red_run_claims: unreadable claim: {token}" in captured.err
    assert not out.exists()


def test_a_bare_claim_with_an_unclosed_parameter_list_is_refused(tmp_path, capsys):
    # Bare ids are whitespace-split, so "hello world" arrives as "test_always[hello" + "world]".
    body = tmp_path / "body.md"
    body.write_text("**Red run** on abc123:\n- tests/test_claim.py::test_always[hello world]\n",
                   encoding="utf-8")
    assert rrc.main([str(body)]) == 3
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "red_run_claims: unreadable claim: tests/test_claim.py::test_always[hello" in captured.err
    assert "world]" not in captured.err


def test_a_refused_claim_writes_no_claims_file_and_keeps_exit_two_for_a_body(tmp_path, capsys):
    # An unreadable body is still exit 2; the claim refusal does not take its place.
    assert rrc.main([str(tmp_path / "missing.md"), "--out", str(tmp_path / "claims.txt")]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "cannot read the PR body" in captured.err
    assert not (tmp_path / "claims.txt").exists()


def _cli(tmp_path: Path, body: str) -> tuple:
    (tmp_path / "body.md").write_text(body, encoding="utf-8")
    out = tmp_path / "claims.txt"
    done = subprocess.run([sys.executable, str(SCRIPT), str(tmp_path / "body.md"), "--out", str(out)],
                          capture_output=True, text=True, timeout=60)
    return done, out


def test_a_body_without_claims_gives_an_empty_claims_file(tmp_path):
    done, out = _cli(tmp_path, "## Summary\n\nNothing claimed here.\n")
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
            (root / name).parent.mkdir(parents=True, exist_ok=True)
            (root / name).write_text(content, encoding="utf-8")
        _git(root, "add", "-A")
        _git(root, "commit", "-m", f"commit {index}")
    return root


@pytest.fixture(scope="module")
def repo(tmp_path_factory):
    """Base returns 1; head returns 2 and adds a behaviour test and an always-green test."""
    return _commit_all(tmp_path_factory.mktemp("red-run-claims"), BASE, HEAD)


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
    _, claims = _cli(tmp_path, "**Red run** on abc123:\n- tests/test_green.py::test_always\n")
    done, verdict = _red_on_base(repo, tmp_path, claims)
    assert done.returncode == 1, done.stdout + done.stderr
    verdicts = {entry["nodeid"]: entry["verdict"] for entry in verdict["tests"]}
    assert verdicts["tests/test_green.py::test_always"] == "green_on_base"
    assert verdict["false_claims"] == ["tests/test_green.py::test_always"]


def test_a_claim_on_a_behavior_test_exits_zero(repo, tmp_path):
    _, claims = _cli(tmp_path, "Rode run:\n- tests/test_claim.py::test_claim\n")
    done, verdict = _red_on_base(repo, tmp_path, claims)
    assert done.returncode == 0, done.stdout + done.stderr
    verdicts = {entry["nodeid"]: entry["verdict"] for entry in verdict["tests"]}
    assert verdicts["tests/test_claim.py::test_claim"] == "behavior"
    assert verdict["false_claims"] == []
