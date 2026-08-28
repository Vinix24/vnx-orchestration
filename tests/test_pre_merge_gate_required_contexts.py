"""pre_merge_gate.check_required_contexts — the merge-gate half of #1691/#1701.

The classifier itself is covered in tests/test_ci_contexts.py. This file
covers the translation from its states into a gate verdict, where the two
mistakes that matter are opposite: reading an unmeasurable answer as GO, and
reading "not created yet" as "never created".
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))
sys.path.insert(0, str(REPO_ROOT / "scripts" / "lib"))

import ci_contexts  # noqa: E402
import pre_merge_gate  # noqa: E402
from pre_merge_gate import SKIPPED_UNVERIFIED, run_gate_checks  # noqa: E402
from required_contexts_gate import check_required_contexts  # noqa: E402
import required_contexts_gate  # noqa: E402
from pre_merge_gate import ResolvedPRRef  # noqa: E402

HEAD = "a" * 40


def test_the_mirrored_sentinel_matches_the_gate_it_mirrors():
    """required_contexts_gate re-declares SKIPPED_UNVERIFIED to avoid an
    import cycle. If the two ever drift, an unverifiable answer stops being
    recognised as blocking by run_gate_checks and reads as an unknown status.
    """
    assert required_contexts_gate.SKIPPED_UNVERIFIED == SKIPPED_UNVERIFIED


def _state(context, state, detail="d"):
    return ci_contexts.ContextState(context=context, state=state, detail=detail)


def _stub_states(monkeypatch, states):
    monkeypatch.setattr(ci_contexts, "evaluate_commit", lambda *a, **k: states)


def test_all_passed_is_go(monkeypatch, tmp_path):
    _stub_states(monkeypatch, [_state("Profile A", ci_contexts.STATE_PASSED)])
    result = check_required_contexts(tmp_path, head_sha=HEAD)
    assert result["status"] == "GO"
    assert "all 1 required contexts passed" in result["detail"]


def test_never_created_holds_and_names_the_context(monkeypatch, tmp_path):
    """The #1691 shape: the gate must say WHICH context is missing."""
    _stub_states(
        monkeypatch,
        [
            _state("Profile A", ci_contexts.STATE_PASSED),
            _state("Profile B", ci_contexts.STATE_NEVER_CREATED, "run finished without it"),
        ],
    )
    result = check_required_contexts(tmp_path, head_sha=HEAD)
    assert result["status"] == "HOLD"
    assert "Profile B" in result["detail"]
    assert "not satisfied" in result["detail"]
    assert result["summary"]["never_created"] == 1


def test_waiting_holds_but_is_reported_as_in_flight(monkeypatch, tmp_path):
    """The #1701 shape: still blocking, but the fix is time, not a re-run."""
    _stub_states(
        monkeypatch,
        [
            _state("Profile A", ci_contexts.STATE_PASSED),
            _state("Profile B", ci_contexts.STATE_WAITING_UPSTREAM, "waiting on Profile A"),
        ],
    )
    result = check_required_contexts(tmp_path, head_sha=HEAD)
    assert result["status"] == "HOLD"
    assert "still in flight" in result["detail"]
    assert "not satisfied" not in result["detail"]


def test_unverified_context_is_skipped_unverified_never_go(monkeypatch, tmp_path):
    _stub_states(
        monkeypatch,
        [
            _state("Profile A", ci_contexts.STATE_PASSED),
            _state("Some App Check", ci_contexts.STATE_UNVERIFIED),
        ],
    )
    result = check_required_contexts(tmp_path, head_sha=HEAD)
    assert result["status"] == SKIPPED_UNVERIFIED
    assert "Some App Check" in result["detail"]


def test_unreadable_branch_protection_is_skipped_unverified(monkeypatch, tmp_path):
    def _boom(*a, **k):
        raise ci_contexts.CIContextsError("gh api failed (rc=1): not a git repository")

    monkeypatch.setattr(ci_contexts, "evaluate_commit", _boom)
    result = check_required_contexts(tmp_path, head_sha=HEAD)
    assert result["status"] == SKIPPED_UNVERIFIED
    assert "not a git repository" in result["detail"]


def test_empty_required_list_is_a_misconfiguration_not_a_pass(monkeypatch, tmp_path):
    """Zero required contexts must never read as "everything required passed"."""
    _stub_states(monkeypatch, [])
    result = check_required_contexts(tmp_path, head_sha=HEAD)
    assert result["status"] == SKIPPED_UNVERIFIED
    assert "misconfiguration" in result["detail"]


@pytest.mark.parametrize(
    "state",
    [
        ci_contexts.STATE_FAILED,
        ci_contexts.STATE_NO_RUN,
        ci_contexts.STATE_NEVER_CREATED,
        ci_contexts.STATE_RUNNING,
        ci_contexts.STATE_WAITING_UPSTREAM,
    ],
)
def test_no_non_passing_state_ever_yields_go(monkeypatch, tmp_path, state):
    _stub_states(monkeypatch, [_state("Profile A", state)])
    assert check_required_contexts(tmp_path, head_sha=HEAD)["status"] != "GO"


# ---------------------------------------------------------------------------
# Orchestrator wiring
# ---------------------------------------------------------------------------


def _quiet_orchestrator(monkeypatch):
    for name, payload in (
        ("check_pr_size", {"check": "pr_size", "status": "GO", "detail": "stub"}),
        ("check_ci_workflow", {"check": "ci_workflow", "status": "GO", "detail": "stub"}),
        ("check_net_deletion", {"check": "net_deletion", "status": "GO", "detail": "stub"}),
    ):
        monkeypatch.setattr(pre_merge_gate, name, lambda project_root, _p=payload, **kw: dict(_p))


def test_orchestrator_omits_the_check_without_a_resolved_pr_head(monkeypatch, tmp_path):
    """A local working copy has no protected-branch contract to compare to."""
    _quiet_orchestrator(monkeypatch)
    called = []
    monkeypatch.setattr(
        pre_merge_gate, "check_required_contexts",
        lambda *a, **kw: called.append(kw) or {"check": "required_contexts", "status": "GO"},
    )
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    dispatch_dir = tmp_path / "dispatches"
    for sub in ("pending", "active", "completed", "staging"):
        (dispatch_dir / sub).mkdir(parents=True)

    result = run_gate_checks(
        pr_id="PR-6", project_root=tmp_path, state_dir=state_dir,
        dispatch_dir=dispatch_dir, skip_pytest=True,
    )
    assert called == []
    assert not any(c["check"] == "required_contexts" for c in result["checks"])


def test_orchestrator_binds_the_check_to_the_pr_head(monkeypatch, tmp_path):
    """Never the local HEAD: the required contexts live on the PR's commit."""
    _quiet_orchestrator(monkeypatch)
    called = []
    monkeypatch.setattr(
        pre_merge_gate, "check_required_contexts",
        lambda *a, **kw: called.append(kw) or {"check": "required_contexts", "status": "GO"},
    )
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    dispatch_dir = tmp_path / "dispatches"
    for sub in ("pending", "active", "completed", "staging"):
        (dispatch_dir / sub).mkdir(parents=True)

    run_gate_checks(
        pr_id="1522", project_root=tmp_path, state_dir=state_dir,
        dispatch_dir=dispatch_dir, skip_pytest=True,
        pr_head=ResolvedPRRef("1522", "b" * 40, "feature/x", "c" * 40),
    )
    assert called and called[0]["head_sha"] == "b" * 40
