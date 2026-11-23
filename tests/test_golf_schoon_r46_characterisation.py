#!/usr/bin/env python3
"""Characterisation of ``merge_preflight_ci_check.check_ci_run_for_head`` (golf schoon R46).

Pins the full verdict dict of every return path (every ``REASON_CODES`` member),
the exact argv/timeout/cwd of each external call and their order, so the
extract-method refactor can be shown to keep behaviour byte-for-byte. Only the
external processes are replaced: ``subprocess.run`` answers with recorded
``gh``/``git`` output and ``shutil.which`` decides whether ``gh`` exists.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_MODULE_PATH = Path(__file__).resolve().parent.parent / "scripts" / "lib" / "merge_preflight_ci_check.py"
_spec = importlib.util.spec_from_file_location("_golf_r46_merge_preflight_ci_check", _MODULE_PATH)
mpci = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = mpci
_spec.loader.exec_module(mpci)

SHA = "a" * 40
WF = "VNX CI"


class FakeRunner:
    """Records every subprocess.run call and answers from a script keyed on argv[:3]."""

    def __init__(self, answers):
        self.answers = answers
        self.calls = []

    def __call__(self, argv, capture_output, text, timeout, cwd):
        assert capture_output is True and text is True
        self.calls.append((list(argv), timeout, cwd))
        key = argv[1] if argv[0] == "git" else " ".join(argv[1:3])
        answer = self.answers[key]
        if isinstance(answer, BaseException):
            raise answer
        return answer


def _proc(returncode=0, stdout="", stderr=""):
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


def _runs(*runs):
    return _proc(stdout=json.dumps(list(runs)))


def _run(conclusion="success", status="completed", db=1, created="2026-01-01T00:00:00Z", sha=SHA):
    return {"conclusion": conclusion, "headSha": sha, "status": status, "databaseId": db, "createdAt": created}


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setattr(mpci.shutil, "which", lambda name: "/usr/bin/" + name)

    def install(answers):
        runner = FakeRunner(answers)
        monkeypatch.setattr(subprocess, "run", runner)
        return runner

    return SimpleNamespace(root=tmp_path, install=install, monkeypatch=monkeypatch)


def _no_go(reason, message, **extra):
    base = {"verdict": "NO-GO", "reason": reason, "message": message, "ci_conclusion": None,
            "ran_on_sha": False, "head_sha": None, "ci_run_id": None, "overridden": False,
            "override_reason": None}
    base.update(extra)
    return base


def _auth_ok(run_list):
    return {"auth status": _proc(), "run list": run_list}


def _expected_calls(root, head_resolved=False):
    calls = []
    if head_resolved:
        calls.append((["git", "rev-parse", "HEAD"], mpci.GIT_TIMEOUT, str(root)))
    calls.append((["gh", "auth", "status"], mpci.GH_AUTH_TIMEOUT, str(root)))
    calls.append((["gh", "run", "list", "--commit", SHA, "--workflow", WF, "--limit", "10",
                   "--json", "conclusion,headSha,status,databaseId,createdAt"],
                  mpci.GH_RUN_LIST_TIMEOUT, str(root)))
    return calls


# ── Escape hatch and pre-query refusals ─────────────────────────────────────

def test_override_with_reason_is_visible_go_and_runs_nothing(env):
    runner = env.install({})
    result = mpci.check_ci_run_for_head(env.root, head_sha=SHA, override_reason="  hotfix  ")
    assert result == {"verdict": "GO", "reason": "overridden",
                      "message": "OVERRIDE: VNX CI-check overgeslagen voor merge (hotfix)",
                      "ci_conclusion": None, "ran_on_sha": False, "head_sha": SHA, "ci_run_id": None,
                      "workflow_name": WF, "overridden": True, "override_reason": "hotfix"}
    assert runner.calls == []


def test_override_with_a_blank_reason_is_refused(env):
    runner = env.install({})
    result = mpci.check_ci_run_for_head(env.root, head_sha=SHA, override_reason="   ")
    assert result == _no_go(
        "override_without_reason",
        "override zonder reden geweigerd: een override vereist een niet-lege reden (geen stille bypass)",
        workflow_name=WF, overridden=True, override_reason="")
    assert runner.calls == []


def test_override_is_checked_before_gh_availability(env):
    env.monkeypatch.setattr(mpci.shutil, "which", lambda name: None)
    env.install({})
    result = mpci.check_ci_run_for_head(env.root, head_sha=SHA, override_reason="x")
    assert result["reason"] == "overridden"


def test_gh_missing_on_which(env):
    env.monkeypatch.setattr(mpci.shutil, "which", lambda name: None)
    runner = env.install({})
    result = mpci.check_ci_run_for_head(env.root, head_sha=SHA, workflow_name="CI")
    assert result == _no_go("gh_missing", "gh CLI niet beschikbaar: deze merge is niet toetsbaar",
                            workflow_name="CI")
    assert runner.calls == []


def test_head_unresolved(env):
    runner = env.install({"rev-parse": _proc(returncode=128)})
    result = mpci.check_ci_run_for_head(env.root)
    assert result == _no_go("head_unresolved",
                            "HEAD-SHA kon niet worden bepaald: deze merge is niet toetsbaar",
                            workflow_name=WF)
    assert runner.calls == [(["git", "rev-parse", "HEAD"], mpci.GIT_TIMEOUT, str(env.root))]


def test_short_sha_is_refused_before_gh(env):
    runner = env.install({})
    result = mpci.check_ci_run_for_head(env.root, head_sha="abc1234")
    assert result == _no_go(
        "short_sha",
        "head_sha 'abc1234' is afgekort (7 tekens): gh run list --commit vereist de volle 40-teken "
        "sha, anders komt er stil nul rijen terug: deze merge is niet toetsbaar",
        head_sha="abc1234", workflow_name=WF)
    assert runner.calls == []


# ── gh auth ─────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("answer, reason, message", [
    (FileNotFoundError(), "gh_missing", "gh CLI niet beschikbaar: deze merge is niet toetsbaar"),
    (subprocess.TimeoutExpired("gh", 10), "gh_auth_timeout",
     "gh auth status liep vast: deze merge is niet toetsbaar"),
    (_proc(returncode=1), "gh_unauthenticated",
     "gh is niet geauthenticeerd (gh auth status faalde): deze merge is niet toetsbaar"),
])
def test_gh_auth_failures(env, answer, reason, message):
    runner = env.install({"auth status": answer})
    result = mpci.check_ci_run_for_head(env.root, head_sha=SHA)
    assert result == _no_go(reason, message, head_sha=SHA, workflow_name=WF)
    assert runner.calls == _expected_calls(env.root)[:1]


# ── gh run list ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("answer, reason, message", [
    (FileNotFoundError(), "gh_missing", "gh CLI niet beschikbaar: deze merge is niet toetsbaar"),
    (subprocess.TimeoutExpired("gh", 15), "gh_run_list_timeout",
     "gh run list liep vast voor workflow 'VNX CI': deze merge is niet toetsbaar"),
    (_proc(returncode=1, stderr="  " + "e" * 200 + "  "), "gh_run_list_failed",
     "gh run list faalde voor workflow 'VNX CI': deze merge is niet toetsbaar (" + "e" * 120 + ")"),
    (_proc(returncode=1, stderr="   "), "gh_run_list_failed",
     "gh run list faalde voor workflow 'VNX CI': deze merge is niet toetsbaar"),
    (_proc(stdout="not json"), "unparseable",
     "gh-uitvoer niet te parsen voor workflow 'VNX CI': deze merge is niet toetsbaar"),
    (_runs(), "no_run", "Geen VNX CI-run gevonden voor aaaaaaaaaaaa: deze merge is niet toetsbaar"),
    (_runs(_run(sha="b" * 40)), "no_run",
     "Geen VNX CI-run gevonden voor aaaaaaaaaaaa: deze merge is niet toetsbaar"),
])
def test_run_list_failures(env, answer, reason, message):
    runner = env.install(_auth_ok(answer))
    result = mpci.check_ci_run_for_head(env.root, head_sha=SHA)
    assert result == _no_go(reason, message, head_sha=SHA, workflow_name=WF)
    assert runner.calls == _expected_calls(env.root)


def test_resolved_head_flows_into_the_query_in_order(env):
    answers = _auth_ok(_runs(_run(db=7)))
    answers["rev-parse"] = _proc(stdout=SHA + "\n")
    runner = env.install(answers)
    result = mpci.check_ci_run_for_head(env.root, branch="feature/x")
    assert result["reason"] == "go"
    assert runner.calls == _expected_calls(env.root, head_resolved=True)


# ── Verdicts from the runs ──────────────────────────────────────────────────

def test_running_blocks_even_next_to_success(env):
    env.install(_auth_ok(_runs(_run(db=1), _run(status="queued", conclusion="", db=2),
                               _run(status="in_progress", conclusion="", db=3))))
    result = mpci.check_ci_run_for_head(env.root, head_sha=SHA)
    assert result == _no_go(
        "running",
        "VNX CI draait nog op aaaaaaaaaaaa (status: queued): deze merge is niet toetsbaar tot alle "
        "runs op deze commit op 'success' eindigen",
        ran_on_sha=True, head_sha=SHA, ci_run_id=2, workflow_name=WF)


def test_single_success_is_go(env):
    env.install(_auth_ok(_runs(_run(db=42))))
    result = mpci.check_ci_run_for_head(env.root, head_sha=SHA)
    assert result == {"verdict": "GO", "reason": "go", "message": "VNX CI geslaagd op aaaaaaaaaaaa (run 42)",
                      "ci_conclusion": "success", "ran_on_sha": True, "head_sha": SHA, "ci_run_id": 42,
                      "workflow_name": WF, "overridden": False, "override_reason": None}


@pytest.mark.parametrize("conclusion, expected_ci", [("failure", "failure"), (None, None), ("", None)])
def test_single_non_success_is_conclusion_no_go(env, conclusion, expected_ci):
    env.install(_auth_ok(_runs(_run(conclusion=conclusion, db=42))))
    result = mpci.check_ci_run_for_head(env.root, head_sha=SHA)
    shown = conclusion or ""
    assert result == _no_go(
        "conclusion",
        f"VNX CI conclusion is '{shown}' op aaaaaaaaaaaa (run 42): deze merge is niet toetsbaar",
        ci_conclusion=expected_ci, ran_on_sha=True, head_sha=SHA, ci_run_id=42, workflow_name=WF)


def test_latest_of_many_decides_go(env):
    env.install(_auth_ok(_runs(_run(conclusion="failure", db=1, created="2026-01-01T00:00:00Z"),
                               _run(db=2, created="2026-01-02T00:00:00Z"),
                               _run(conclusion="cancelled", db=3, created="2025-12-31T00:00:00Z"))))
    result = mpci.check_ci_run_for_head(env.root, head_sha=SHA)
    assert result["reason"] == "go"
    assert result["ci_run_id"] == 2
    assert result["message"] == ("VNX CI geslaagd op aaaaaaaaaaaa "
                                 "(laatste van 3 runs: 2, createdAt 2026-01-02T00:00:00Z)")


def test_latest_of_many_decides_fail(env):
    env.install(_auth_ok(_runs(_run(db=1, created="2026-01-01T00:00:00Z"),
                               _run(conclusion="failure", db=2, created="2026-01-02T00:00:00Z"))))
    result = mpci.check_ci_run_for_head(env.root, head_sha=SHA)
    assert result == _no_go(
        "conclusion",
        "VNX CI conclusion is 'failure' op aaaaaaaaaaaa (laatste van 2 runs: 2, createdAt "
        "2026-01-02T00:00:00Z): deze merge is niet toetsbaar",
        ci_conclusion="failure", ran_on_sha=True, head_sha=SHA, ci_run_id=2, workflow_name=WF)


@pytest.mark.parametrize("created", [None, "", "garbage", 12345])
def test_order_undeterminable_missing_timestamp(env, created):
    env.install(_auth_ok(_runs(_run(db=1), _run(db=2, created=created))))
    result = mpci.check_ci_run_for_head(env.root, head_sha=SHA)
    assert result == _no_go(
        "order_undeterminable",
        "kan de volgorde van 2 klare VNX CI-runs op aaaaaaaaaaaa niet vaststellen (createdAt "
        "ontbreekt of is onparsebaar bij minstens een run): deze merge is niet toetsbaar",
        ran_on_sha=True, head_sha=SHA, workflow_name=WF)


def test_order_undeterminable_tied_latest(env):
    env.install(_auth_ok(_runs(_run(db=1, created="2026-01-02T00:00:00Z"),
                               _run(conclusion="failure", db=2, created="2026-01-02T00:00:00Z"),
                               _run(db=3, created="2026-01-01T00:00:00Z"))))
    result = mpci.check_ci_run_for_head(env.root, head_sha=SHA)
    assert result == _no_go(
        "order_undeterminable",
        "kan de volgorde van 3 klare VNX CI-runs op aaaaaaaaaaaa niet vaststellen (twee runs delen "
        "exact dezelfde createdAt): deze merge is niet toetsbaar",
        ran_on_sha=True, head_sha=SHA, workflow_name=WF)


def test_workflow_resolution_reaches_the_query(env):
    runner = env.install(_auth_ok(_runs(_run())))
    env.monkeypatch.setenv("VNX_CI_WORKFLOW_NAME", "Env CI")
    result = mpci.check_ci_run_for_head(env.root, head_sha=SHA, project_workflow="Project CI")
    assert result["workflow_name"] == "Project CI"
    assert runner.calls[1][0][6] == "Project CI"


def test_every_reason_code_is_covered_here():
    source = Path(__file__).read_text(encoding="utf-8")
    missing = [code for code in mpci.REASON_CODES if f'"{code}"' not in source]
    assert missing == []
