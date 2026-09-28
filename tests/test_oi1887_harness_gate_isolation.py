#!/usr/bin/env python3
"""Tests for OI-1887 — harness-lane review-gates must not touch the orchestrator's
own checkout.

Measured 27-09 on PR #1950 (glm_gate via gate_runner's harness-lane path): the
provider worktree ``_prepare_provider_workdir`` created was isolated on
``origin/main``, not the PR branch — so the dispatched agent had no PR content
of its own to read and fetched/checked out the PR branch itself, 96 times, IN
THE ORCHESTRATOR'S OWN CHECKOUT (reflog: "checkout: moving from main to
FETCH_HEAD"; a stray branch ``pr1950`` appeared in the operator's repo).

These tests use a REAL, disposable git repo under tmp_path (never the actual
vnx-orchestration checkout) as the "orchestrator's own checkout"
(``GateRunner(project_root=...)``), with a fake dispatcher standing in for the
governed provider lane — real enough to prove the fix end to end without
starting a real provider or claude process:

  (a) a normal harness-lane run completes and leaves the main checkout's HEAD,
      branch list, and status exactly as they were;
  (b) the dispatcher is handed ``origin/<branch>`` as ``base_ref`` — the PR
      branch, never the default ``origin/main``;
  (c) a fake dispatcher that behaves like the 27-09 agent (git checkout in the
      orchestrator's own checkout) is caught and booked
      ``unavailable``/``harness_lane_touched_main_checkout`` — never a PASS;
  (d) a PR branch that does not exist on the remote books ``unavailable``
      before any dispatch — the dispatcher is never even called, so there is
      no run against main or anything else.

Fix-forward (OI-1887 round 2): the initial vangnet compared the FULL output
of ``git branch --list``, which is shared across every worktree of the repo
and changes on every real run — the gate's own provider worktree
(``git worktree add <path> -b dispatch/<safe_id> ...``) leaves its branch
behind, and any sibling dispatch running concurrently mints its own. That
made every real kimi/glm/deepseek review book
``unavailable``/``harness_lane_touched_main_checkout``. The fix excludes the
``dispatch/`` prefix from the comparison (by name AND sha, not name alone),
covered by:

  (e) a fake dispatcher that runs the REAL ``git worktree add ... -b
      dispatch/<id>`` command and leaves the branch behind still books
      ``completed``;
  (f) a sibling ``dispatch/other`` branch appearing mid-run (a different,
      concurrent dispatch) still books ``completed``;
  (g) the exclusion is narrow — a bare ``git branch pr1950`` (no checkout
      needed) OUTSIDE the ``dispatch/`` prefix still books ``unavailable``.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

VNX_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = VNX_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(SCRIPTS_DIR / "lib"))

from gate_runner import GateRunner  # noqa: E402


def _run_git(args, cwd):
    result = subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 0, f"git {args} failed: {result.stderr}"
    return result.stdout


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    _run_git(["init", "-b", "main"], path)
    _run_git(["config", "user.email", "test@example.invalid"], path)
    _run_git(["config", "user.name", "Test"], path)


@pytest.fixture
def orchestrator_repo(tmp_path):
    """An 'origin' with a PR branch, and a 'checkout' cloned from it — the
    orchestrator's own repo, passed to ``GateRunner`` as ``project_root``,
    mirroring the production shape where the gate runner executes from within
    its own checkout of the repo the reviewed PR lives in.
    """
    origin = tmp_path / "origin"
    _init_repo(origin)
    (origin / "marker.txt").write_text("BASE\n")
    _run_git(["add", "marker.txt"], origin)
    _run_git(["commit", "-m", "base"], origin)

    checkout = tmp_path / "checkout"
    _run_git(["clone", str(origin), str(checkout)], tmp_path)
    _run_git(["config", "user.email", "test@example.invalid"], checkout)
    _run_git(["config", "user.name", "Test"], checkout)

    _run_git(["checkout", "-b", "feature/pr-1950"], origin)
    (origin / "marker.txt").write_text("PR_BRANCH_CONTENT\n")
    _run_git(["add", "marker.txt"], origin)
    _run_git(["commit", "-m", "pr work"], origin)
    _run_git(["checkout", "main"], origin)

    return {"origin": origin, "checkout": checkout}


def _gate_env(tmp_path):
    data_dir = tmp_path / ".vnx-data"
    state_dir = data_dir / "state"
    reports_dir = data_dir / "unified_reports"
    (state_dir / "review_gates" / "requests").mkdir(parents=True, exist_ok=True)
    (state_dir / "review_gates" / "results").mkdir(parents=True, exist_ok=True)
    reports_dir.mkdir(parents=True, exist_ok=True)
    return {"state_dir": state_dir, "reports_dir": reports_dir}


def _payload(branch, report_path, pr_number=1950):
    return {
        "gate": "kimi_gate",
        "status": "requested",
        "provider": "kimi",
        "branch": branch,
        "pr_number": pr_number,
        "report_path": str(report_path),
        # A prompt is supplied directly so the test never needs `gh`/network
        # to build the diff-review prompt — orthogonal to what this file proves.
        "prompt": "Review this diff for correctness and security",
    }


def _pass_report_text():
    return (
        "Reviewed the diff.\nNo blocking findings.\n\n"
        "```json\n"
        '{"verdict": "pass", "findings": [], "residual_risk": null}\n'
        "```\n"
    )


def _make_runner(env, checkout):
    return GateRunner(
        state_dir=env["state_dir"], reports_dir=env["reports_dir"], project_root=checkout,
    )


class TestNormalRunIsolatesAndLeavesMainCheckoutUntouched:
    """(a) + (b): the intended-path proof."""

    def test_normal_run_completes_and_leaves_main_checkout_untouched(
        self, orchestrator_repo, monkeypatch, tmp_path,
    ):
        checkout = orchestrator_repo["checkout"]
        env = _gate_env(tmp_path)

        def factory(data_dir, timeout_seconds, *, role="plan-reviewer", base_ref=None):
            def dispatch(provider, model, instruction, dispatch_id):
                return _pass_report_text()
            return dispatch

        monkeypatch.setattr("plan_gate_panel._make_default_dispatcher", factory)
        monkeypatch.delenv("VNX_KIMI_GATE_MODEL", raising=False)

        head_before = _run_git(["rev-parse", "--abbrev-ref", "HEAD"], checkout).strip()
        branches_before = _run_git(["branch", "--list"], checkout)
        status_before = _run_git(["status", "--porcelain"], checkout)

        runner = _make_runner(env, checkout)
        payload = _payload("feature/pr-1950", env["reports_dir"] / "kimi-gate-pr1950.md")
        result = runner.run(gate="kimi_gate", request_payload=payload, pr_number=1950)

        assert result["status"] == "completed", result.get("reason_detail")

        assert _run_git(["rev-parse", "--abbrev-ref", "HEAD"], checkout).strip() == head_before
        assert _run_git(["branch", "--list"], checkout) == branches_before
        assert _run_git(["status", "--porcelain"], checkout) == status_before

    def test_dispatcher_receives_pr_branch_as_base_ref(
        self, orchestrator_repo, monkeypatch, tmp_path,
    ):
        checkout = orchestrator_repo["checkout"]
        env = _gate_env(tmp_path)
        calls = []

        def factory(data_dir, timeout_seconds, *, role="plan-reviewer", base_ref=None):
            calls.append({"base_ref": base_ref})

            def dispatch(provider, model, instruction, dispatch_id):
                return _pass_report_text()
            return dispatch

        monkeypatch.setattr("plan_gate_panel._make_default_dispatcher", factory)
        monkeypatch.delenv("VNX_KIMI_GATE_MODEL", raising=False)

        runner = _make_runner(env, checkout)
        payload = _payload("feature/pr-1950", env["reports_dir"] / "kimi-gate-pr1950.md")
        result = runner.run(gate="kimi_gate", request_payload=payload, pr_number=1950)

        assert result["status"] == "completed", result.get("reason_detail")
        assert len(calls) == 1
        assert calls[0]["base_ref"] == "origin/feature/pr-1950"


class TestAgentTouchingMainCheckoutIsCaught:
    """(c): the vangnet fires even when the model ignores every instruction
    meant to prevent it — reproducing the exact PR #1950 incident shape."""

    def test_agent_git_checkout_in_main_repo_books_unavailable_never_pass(
        self, orchestrator_repo, monkeypatch, tmp_path,
    ):
        checkout = orchestrator_repo["checkout"]
        env = _gate_env(tmp_path)
        calls = []

        def factory(data_dir, timeout_seconds, *, role="plan-reviewer", base_ref=None):
            def dispatch(provider, model, instruction, dispatch_id):
                calls.append(1)
                # Reproduces the measured 27-09 incident: the agent fetches
                # and checks out the PR branch IN THE ORCHESTRATOR'S OWN
                # CHECKOUT instead of staying in its isolated provider
                # worktree — `git fetch` + `git checkout -b <name> FETCH_HEAD`,
                # verbatim the reflog line from the incident.
                _run_git(["fetch", "origin", "feature/pr-1950"], checkout)
                _run_git(["checkout", "-b", "pr1950", "FETCH_HEAD"], checkout)
                return _pass_report_text()
            return dispatch

        monkeypatch.setattr("plan_gate_panel._make_default_dispatcher", factory)
        monkeypatch.delenv("VNX_KIMI_GATE_MODEL", raising=False)

        runner = _make_runner(env, checkout)
        payload = _payload("feature/pr-1950", env["reports_dir"] / "kimi-gate-pr1950.md")
        result = runner.run(gate="kimi_gate", request_payload=payload, pr_number=1950)

        # The dispatch DID run (the agent produced a PASS) — the vangnet must
        # override that outcome, not merely fail to record one.
        assert len(calls) == 1
        assert result["status"] == "unavailable", result
        assert result["reason"] == "harness_lane_touched_main_checkout"
        assert "pr1950" in result["reason_detail"]

        result_file = env["state_dir"] / "review_gates" / "results" / "pr-1950-kimi_gate.json"
        saved = json.loads(result_file.read_text(encoding="utf-8"))
        assert saved["status"] == "unavailable"
        assert saved["reason"] == "harness_lane_touched_main_checkout"


class TestDispatchBranchChurnIsExcludedFromTheVangnet:
    """OI-1887 fix-forward: ``refs/heads`` is shared across every worktree of
    the repo, and a real, correct harness-lane run creates ``dispatch/<id>``
    branches as a side effect (its own provider worktree, or a sibling
    dispatch running concurrently) — those must never trip the vangnet.
    Anything outside the ``dispatch/`` prefix still must."""

    def test_real_worktree_add_dispatch_branch_left_behind_does_not_trip_vangnet(
        self, orchestrator_repo, monkeypatch, tmp_path,
    ):
        """Reproduces the actual mechanism, not a stand-in for it: the fake
        dispatcher runs the SAME command the provider lane runs
        (``dispatch_worktree_isolation.create_dispatch_worktree`` ->
        ``git worktree add <path> -b dispatch/<safe_id> origin/<branch>``),
        and leaves the branch behind exactly as teardown does in production
        (branch removal is not part of worktree teardown). This must stay
        ``completed`` — the branch is under ``dispatch/`` and its existence
        is expected fabric churn, not evidence of a touched checkout.
        """
        checkout = orchestrator_repo["checkout"]
        env = _gate_env(tmp_path)
        provider_worktree = tmp_path / "provider-worktree"

        def factory(data_dir, timeout_seconds, *, role="plan-reviewer", base_ref=None):
            def dispatch(provider, model, instruction, dispatch_id):
                _run_git(
                    ["worktree", "add", str(provider_worktree), "-b",
                     "dispatch/kimi-gate-pr1950", base_ref],
                    checkout,
                )
                return _pass_report_text()
            return dispatch

        monkeypatch.setattr("plan_gate_panel._make_default_dispatcher", factory)
        monkeypatch.delenv("VNX_KIMI_GATE_MODEL", raising=False)

        runner = _make_runner(env, checkout)
        payload = _payload("feature/pr-1950", env["reports_dir"] / "kimi-gate-pr1950.md")
        result = runner.run(gate="kimi_gate", request_payload=payload, pr_number=1950)

        assert result["status"] == "completed", result.get("reason_detail")
        assert "dispatch/kimi-gate-pr1950" in _run_git(["branch", "--list"], checkout)

    def test_concurrent_sibling_dispatch_branch_does_not_trip_vangnet(
        self, orchestrator_repo, monkeypatch, tmp_path,
    ):
        """A DIFFERENT dispatch (a sibling worker, running in parallel in its
        own worktree of the same repo) creates ``dispatch/other`` mid-run.
        ``refs/heads`` is shared, so this shows up in the after-snapshot too
        — and must not be mistaken for the gate's own dispatch touching the
        checkout."""
        checkout = orchestrator_repo["checkout"]
        env = _gate_env(tmp_path)

        def factory(data_dir, timeout_seconds, *, role="plan-reviewer", base_ref=None):
            def dispatch(provider, model, instruction, dispatch_id):
                # Simulates a sibling dispatch's worktree branch appearing
                # in the shared refs/heads while THIS gate run is in flight.
                _run_git(["branch", "dispatch/other-sibling-dispatch"], checkout)
                return _pass_report_text()
            return dispatch

        monkeypatch.setattr("plan_gate_panel._make_default_dispatcher", factory)
        monkeypatch.delenv("VNX_KIMI_GATE_MODEL", raising=False)

        runner = _make_runner(env, checkout)
        payload = _payload("feature/pr-1950", env["reports_dir"] / "kimi-gate-pr1950.md")
        result = runner.run(gate="kimi_gate", request_payload=payload, pr_number=1950)

        assert result["status"] == "completed", result.get("reason_detail")

    def test_agent_creates_non_dispatch_branch_in_main_repo_still_caught(
        self, orchestrator_repo, monkeypatch, tmp_path,
    ):
        """The exclusion is narrow: a branch OUTSIDE the ``dispatch/`` prefix
        (e.g. the exact ``pr1950`` shape from the 27-09 incident, created
        here with a bare ``git branch`` — no checkout involved) must still
        trip the vangnet."""
        checkout = orchestrator_repo["checkout"]
        env = _gate_env(tmp_path)
        calls = []

        def factory(data_dir, timeout_seconds, *, role="plan-reviewer", base_ref=None):
            def dispatch(provider, model, instruction, dispatch_id):
                calls.append(1)
                _run_git(["branch", "pr1950"], checkout)
                return _pass_report_text()
            return dispatch

        monkeypatch.setattr("plan_gate_panel._make_default_dispatcher", factory)
        monkeypatch.delenv("VNX_KIMI_GATE_MODEL", raising=False)

        runner = _make_runner(env, checkout)
        payload = _payload("feature/pr-1950", env["reports_dir"] / "kimi-gate-pr1950.md")
        result = runner.run(gate="kimi_gate", request_payload=payload, pr_number=1950)

        assert len(calls) == 1
        assert result["status"] == "unavailable", result
        assert result["reason"] == "harness_lane_touched_main_checkout"
        assert "pr1950" in result["reason_detail"]


class TestMissingBranchNeverDispatches:
    """(d): a PR branch that cannot be fetched fails BEFORE any dispatch —
    no silent fallback to main, no run at all."""

    def test_missing_branch_books_unavailable_and_never_calls_the_dispatcher(
        self, orchestrator_repo, monkeypatch, tmp_path,
    ):
        checkout = orchestrator_repo["checkout"]
        env = _gate_env(tmp_path)
        calls = []

        def factory(data_dir, timeout_seconds, *, role="plan-reviewer", base_ref=None):
            def dispatch(provider, model, instruction, dispatch_id):
                calls.append(1)
                return _pass_report_text()
            return dispatch

        monkeypatch.setattr("plan_gate_panel._make_default_dispatcher", factory)
        monkeypatch.delenv("VNX_KIMI_GATE_MODEL", raising=False)

        head_before = _run_git(["rev-parse", "--abbrev-ref", "HEAD"], checkout).strip()

        runner = _make_runner(env, checkout)
        payload = _payload("does/not/exist", env["reports_dir"] / "kimi-gate-pr1951.md", pr_number=1951)
        result = runner.run(gate="kimi_gate", request_payload=payload, pr_number=1951)

        assert result["status"] == "unavailable", result
        assert result["reason"] == "harness_lane_branch_unavailable"
        assert len(calls) == 0, "the dispatcher must never run against an unfetchable branch"
        assert _run_git(["rev-parse", "--abbrev-ref", "HEAD"], checkout).strip() == head_before
