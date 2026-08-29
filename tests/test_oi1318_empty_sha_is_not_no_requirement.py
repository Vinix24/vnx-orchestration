"""An unresolvable head is a refusal, not a dropped requirement (OI-1318).

Two halves of one asymmetry.

``pr_merge._run_ci_gate`` resolves the PR head and refuses when it cannot:
"PR-head (sha/branch) kon niet worden bepaald ... deze merge is niet toetsbaar".
Its sibling ``_run_review_gate`` did not. It read the same two fields, got empty
strings, and carried on into ``closure_verifier.check_review_gate_for_merge``.

There the constraint was applied as ``if head_sha:``. That truthy test collapses
two different callers into one behaviour: a caller that passes nothing is saying
"I am not scoping by sha", and a caller that passes an empty string is saying it
tried and could not. Both took the branch that skips the check.

So the one path that could not establish which commit it was merging was also
the path that stopped asking, and any result for the PR — including one recorded
against a commit that is no longer there — satisfied the merge door. The failure
is silent by construction: nothing is logged, and the verdict is GO.

``branch`` carried the identical shape and is fixed with it.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

VNX_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = VNX_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(SCRIPTS_DIR / "lib"))

import pr_merge

HEAD_SHA = "6c925a1b2ee8b0cded8728d4f2c792fcf64be4d4"
OTHER_SHA = "0f5e881a2b2761f73b7664ee7ea2e03ee4ee63b9"


# --------------------------------------------------------------------------
# The matcher: three buckets, not two.
# --------------------------------------------------------------------------

def test_no_constraint_accepts_anything():
    from closure_verifier import _matches_required_field

    assert _matches_required_field(None, OTHER_SHA) is True
    assert _matches_required_field(None, None) is True
    assert _matches_required_field(None, "") is True


def test_a_supplied_value_must_match():
    from closure_verifier import _matches_required_field

    assert _matches_required_field(HEAD_SHA, HEAD_SHA) is True
    assert _matches_required_field(HEAD_SHA, OTHER_SHA) is False


def test_a_record_without_the_field_fails_a_real_constraint():
    """A result with no commit_sha is stale evidence, never a wildcard."""
    from closure_verifier import _matches_required_field

    assert _matches_required_field(HEAD_SHA, None) is False
    assert _matches_required_field(HEAD_SHA, "") is False


def test_an_empty_constraint_accepts_nothing():
    """The bucket the truthy test did not have.

    The caller asked for a sha match and could not supply the sha. That is an
    unverifiable state, and an unverifiable state is a refusal — the same answer
    the sibling CI gate already gives when gh hands it no head.
    """
    from closure_verifier import _matches_required_field

    assert _matches_required_field("", HEAD_SHA) is False, (
        "an empty constraint accepted a record — the merge door that could not "
        "determine its own head silently stopped requiring one"
    )
    assert _matches_required_field("", OTHER_SHA) is False
    assert _matches_required_field("", "") is False


# --------------------------------------------------------------------------
# The merge door: the sibling gates now refuse alike.
# --------------------------------------------------------------------------

def _seed_merge_door(monkeypatch, tmp_path, *, head_oid, record_sha):
    """A merge door with everything in place except the head it is merging.

    An obligation exists, a terminal + evidenced PASS is on disk, and its report
    file is real — so nothing else can be the reason for the verdict. The only
    variable is whether the door could resolve its own head.
    """
    import json

    state_dir = tmp_path / "state"
    results_dir = state_dir / "review_gates" / "results"
    results_dir.mkdir(parents=True, exist_ok=True)
    report = tmp_path / "kimi-gate-1710.md"
    report.write_text(
        "# kimi_gate\n\nReviewed the diff. No blocking findings.\n", encoding="utf-8"
    )
    (results_dir / "pr-1710-kimi_gate.json").write_text(json.dumps({
        "gate": "kimi_gate", "pr_id": "1710", "pr_number": 1710,
        "status": "pass", "commit_sha": record_sha, "branch": "fix/x",
        "contract_hash": "183bed973031720a", "report_path": str(report),
        "dispatch_id": "kimi-gate-pr1710-old",
        "blocking_findings": [], "advisory_findings": [],
    }), encoding="utf-8")

    monkeypatch.setattr(
        pr_merge, "_query_pr",
        lambda _n: {"headRefName": "fix/x", "headRefOid": head_oid},
    )
    monkeypatch.setattr(pr_merge, "_resolve_declared_gate",
                        lambda *_a, **_k: "kimi_gate")
    monkeypatch.setattr(pr_merge, "ensure_env",
                        lambda *_a, **_k: {"VNX_STATE_DIR": str(state_dir)})
    monkeypatch.setattr(pr_merge, "_resolve_override_reason", lambda _r: None)


def test_an_unresolvable_head_does_not_green_light_a_stale_result(monkeypatch, tmp_path):
    """The harm, end to end.

    The result on disk is a real, evidenced PASS — for a commit that is not the
    head. With the head resolvable, the sha constraint rejects it. With the head
    unresolvable, the constraint was skipped rather than enforced, and the door
    said GO on a review of code that is no longer there.
    """
    _seed_merge_door(monkeypatch, tmp_path, head_oid="", record_sha=OTHER_SHA)

    gate, _ = pr_merge._run_review_gate(1710)

    assert gate["verdict"] == "NO-GO", (
        "the merge door could not determine its own head, therefore stopped "
        "requiring one, and green-lit a PASS recorded against "
        f"{OTHER_SHA[:8]} — the failure is silent by construction: verdict GO, "
        "nothing logged"
    )
    assert "niet toetsbaar" in gate["message"]
    assert gate["overridden"] is False


def test_the_same_stale_result_is_rejected_when_the_head_is_known(monkeypatch, tmp_path):
    """Contrast: with a head, the constraint does its job on main too.

    This is what isolates the defect to the empty-head path rather than to sha
    matching in general.
    """
    _seed_merge_door(monkeypatch, tmp_path, head_oid=HEAD_SHA, record_sha=OTHER_SHA)

    gate, _ = pr_merge._run_review_gate(1710)

    assert gate["verdict"] == "NO-GO"


def test_a_matching_result_still_passes_the_door(monkeypatch, tmp_path):
    """And the door is not simply refusing everything."""
    _seed_merge_door(monkeypatch, tmp_path, head_oid=HEAD_SHA, record_sha=HEAD_SHA)

    gate, _ = pr_merge._run_review_gate(1710)

    assert gate["verdict"] == "GO", (
        f"a matching, evidenced PASS was refused: {gate['message']}"
    )


def test_ci_gate_refuses_the_same_input(monkeypatch):
    """The control: the asymmetry is gone because both now refuse, not because
    the test asserts one of them in isolation."""
    monkeypatch.setattr(pr_merge, "_query_pr", lambda _n: {"headRefName": "", "headRefOid": ""})

    gate, _ = pr_merge._run_ci_gate(1710)

    assert gate["verdict"] == "NO-GO"
    assert "niet toetsbaar" in gate["message"]


def test_a_contract_without_a_branch_is_not_treated_as_unresolvable(tmp_path):
    """glm_gate advisory on 5480324a.

    ``ReviewContract.branch`` defaults to "" and one live contract in this store
    carries that default. A contract that never had a branch is "no constraint";
    a merge door that asked ``gh`` and got nothing is "I tried and failed".
    Same empty string, opposite meanings, so the caller has to say which — the
    closure verifier's internal sites pass ``or None``, the merge door passes
    what it got.

    Without this, tightening the matcher would have made every result for such
    a contract unverifiable, in a code path the merge fix was not about.
    """
    import json

    import closure_verifier
    from review_contract import ReviewContract

    results_dir = tmp_path / "results"
    results_dir.mkdir(parents=True, exist_ok=True)
    report = tmp_path / "r.md"
    report.write_text("# gate\n\nNo blocking findings.\n", encoding="utf-8")
    (results_dir / "pr-77-kimi_gate.json").write_text(json.dumps({
        "gate": "kimi_gate", "pr_id": "77", "status": "pass",
        "branch": "feature/whatever", "commit_sha": HEAD_SHA,
        "contract_hash": "abc123", "report_path": str(report),
        "blocking_findings": [], "advisory_findings": [],
    }), encoding="utf-8")

    contract = ReviewContract(
        pr_id="77", branch="", risk_class="medium",
        review_stack=["kimi_gate"], changed_files=[], content_hash="e" * 16,
    )

    found = closure_verifier._find_gate_result(
        "kimi_gate", contract.pr_id, results_dir, branch=contract.branch or None,
    )
    assert found is not None, (
        "a contract with no branch rejected a valid result — the default empty "
        "string was read as an unresolvable constraint"
    )
    assert found["status"] == "pass"
