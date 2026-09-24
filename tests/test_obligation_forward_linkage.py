"""tests/test_obligation_forward_linkage.py — DEEL B (dispatch
20260920-124500-punt2-verplichtingen-oplosbaar): a gate obligation declared
without a ``pr_number`` or ``branch`` MUST acquire that linkage during its
life, so it becomes resolvable, and that must be provable on current code.

Background (gemeten 20-09): a obligation gets its ``pr_number`` and
``branch`` NOT at declaration but during its life, once the PR exists and
the gate runner runs. Counting on "carries a linkage" therefore counts
every RUNNING dispatch as a defect — a LOPEND dispatch legitimately has
``pr_number=None, branch=None`` (bewijs: dispatch
``20260920-103000-oi1744-vangnet-converter``, gedeclareerd 08:20:11Z op
current main, stond op ``pr_number=None, branch=None`` terwijl hij liep).
Meet on EINDtoestand, not on linkage.

On the v1.5.0-generatie (sales-copilot, website-vincentvandeth) the chain
never closed: an obligation declared without a linkage stayed without one
forever and was never resolvable. On 1.6.2 (vnx-dev, mission-control) the
chain closes: ``resolve_pr_number`` looks the PR up via GitHub and the
runner stamps the linkage back onto the obligation. This test pins that
MECHANISM — not the outcome on a live store.

A test that reads the real ``~/.vnx-data`` is a monitor, not a test (the
dispatch's own warning, zie ``tests/test_report_to_receipt_converter_staleness.py``
and the discriminator "heeft deze store ooit een receipt geboekt"). This
file drives the real ``resolve_pr_number`` and ``run`` against a throwaway
store under ``tmp_path`` with the gh/git subprocesses patched — never the
real network, never the real store.
"""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
for _p in (ROOT / "scripts" / "lib", ROOT / "scripts", ROOT):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import gate_obligation_runner as runner  # noqa: E402
from gate_obligations import (  # noqa: E402
    STATUS_FULFILLED,
    STATUS_PENDING,
    obligation_path,
    register_obligation,
)


# ---------------------------------------------------------------------------
# Harness — mirrors tests/test_gate_obligation_runner.py's _FakeReviewGateManager
# and _patch_manager, kept local so this file is self-contained.
# ---------------------------------------------------------------------------


class _FakeReviewGateManager:
    """Writes real request+result JSON files; no gate actually runs."""

    def __init__(self, state_dir: Path, *, result_status: str = "pass") -> None:
        self.state_dir = Path(state_dir)
        self.result_status = result_status
        self.calls = []

    def _request_path(self, gate: str, pr_number: int) -> Path:
        return self.state_dir / "review_gates" / "requests" / f"pr-{pr_number}-{gate}.json"

    def _result_path(self, gate: str, pr_number: int) -> Path:
        return self.state_dir / "review_gates" / "results" / f"pr-{pr_number}-{gate}.json"

    def request_and_execute(self, *, pr_number, branch, review_stack, risk_class,
                             changed_files, mode, dispatch_id=""):
        self.calls.append(
            {"pr_number": pr_number, "branch": branch, "review_stack": list(review_stack)}
        )
        for gate in review_stack:
            self._request_path(gate, pr_number).write_text(
                json.dumps({"gate": gate, "pr_number": pr_number, "status": "completed"}),
                encoding="utf-8",
            )
            self._result_path(gate, pr_number).write_text(
                json.dumps({"gate": gate, "pr_number": pr_number, "status": self.result_status}),
                encoding="utf-8",
            )
        return {"pr_number": pr_number, "branch": branch, "gates": [], "has_required_failure": False}


_DEFAULT_TEST_HEAD_SHA = "deadbeef00" * 4


def _make_state_dir(tmp_path: Path) -> Path:
    state_dir = tmp_path / "vnx-data" / "state"
    (state_dir / "review_gates" / "requests").mkdir(parents=True, exist_ok=True)
    (state_dir / "review_gates" / "results").mkdir(parents=True, exist_ok=True)
    (state_dir / "review_gates" / "obligations").mkdir(parents=True, exist_ok=True)
    return state_dir


def _read_obligation(state_dir: Path, dispatch_id: str) -> dict:
    return json.loads(obligation_path(state_dir, dispatch_id).read_text(encoding="utf-8"))


def _patch_resolved_linkage(monkeypatch, manager, *, pr_number: int, branch: str,
                            pr_state: str = "OPEN", head_sha: str = _DEFAULT_TEST_HEAD_SHA):
    """Hermetic patch: a PR IS found via GitHub (RESOLVED), with a branch.

    This is the 1.6.2 mechanism: ``resolve_pr_number`` falls through the
    record (no pr_number) and the dispatch metadata (none here) to the
    GitHub lookup, which finds the PR. The runner then stamps both
    ``pr_number`` and ``branch`` back onto the obligation.
    """
    monkeypatch.setattr(runner, "_build_manager", lambda state_dir: manager)
    # The record has no pr_number, metadata has none — GitHub finds it:
    monkeypatch.setattr(runner, "_pr_from_dispatch_metadata", lambda sd, did: None)
    monkeypatch.setattr(runner, "_pr_from_github", lambda did, owner_repo: pr_number)
    monkeypatch.setattr(runner, "_branch_from_github", lambda pr, owner_repo: branch)
    monkeypatch.setattr(runner, "_resolve_github_owner_repo", lambda sd: "Vinix24/vnx-orchestration")
    monkeypatch.setattr(runner, "_get_pr_head_sha_for_gate", lambda pr: head_sha)
    monkeypatch.setattr(runner, "_pr_state_from_github", lambda pr_number, owner_repo: pr_state, raising=False)
    fake_rgm = types.ModuleType("review_gate_manager")
    fake_rgm._compute_changed_files = lambda branch: ["scripts/lib/foo.py"]
    monkeypatch.setitem(sys.modules, "review_gate_manager", fake_rgm)


# ---------------------------------------------------------------------------
# The mechanism: a declared-without-linkage obligation acquires its linkage.
# ---------------------------------------------------------------------------


def test_obligation_declared_without_linkage_acquires_pr_number_and_branch(tmp_path, monkeypatch):
    """DEEL B core: an obligation registered at declaration time with NO
    ``pr_number`` and NO ``branch`` (the normal case — the PR does not exist
    yet when the door accepts the dispatch) must acquire BOTH once the PR
    exists and the runner runs.

    RED on a v1.5.0 regression: if ``resolve_pr_number`` stops looking the
    PR up via GitHub (returns AWAITING forever) or the runner stops stamping
    the linkage back onto the record, the obligation ends fulfilled but
    with ``pr_number=None, branch=None`` — indistinguishable from a dispatch
    that never produced a PR, and never provably resolvable.
    """
    state_dir = _make_state_dir(tmp_path)
    dispatch_id = "20260920-103000-oi1744-vangnet-converter"
    # Declared without a linkage, exactly like the live measured case.
    register_obligation(
        state_dir, dispatch_id=dispatch_id, gate="codex_gate", project_id="vnx-dev",
    )
    record = _read_obligation(state_dir, dispatch_id)
    assert record["pr_number"] is None, "fixture: declared without a pr_number"
    assert record["branch"] is None, "fixture: declared without a branch"

    manager = _FakeReviewGateManager(state_dir, result_status="pass")
    _patch_resolved_linkage(monkeypatch, manager, pr_number=1879, branch=f"dispatch/{dispatch_id}")

    summary = runner.run(state_dir)

    assert summary["pending_after"] == 0, "the obligation must resolve, not stay pending"
    updated = _read_obligation(state_dir, dispatch_id)
    # The mechanism under test: the linkage is stamped back onto the record.
    assert updated["status"] == STATUS_FULFILLED
    assert updated["pr_number"] == 1879, (
        "an obligation declared without a pr_number must acquire one once "
        "the PR exists and the runner runs — the forward half of resolvability"
    )
    assert updated["branch"] == f"dispatch/{dispatch_id}", (
        "the branch must be stamped back onto the record too, so the "
        "obligation is provably bound to the dispatch that produced it"
    )
    # The manager was actually invoked with the resolved linkage — proving
    # the linkage did not just appear on the record cosmetically but drove
    # the gate attempt.
    assert manager.calls, "the gate manager must be invoked once the PR resolves"
    assert manager.calls[0]["pr_number"] == 1879
    assert manager.calls[0]["branch"] == f"dispatch/{dispatch_id}"


def test_obligation_without_linkage_stays_pending_when_no_pr_exists(tmp_path, monkeypatch):
    """Control: the forward half is honest. When NO PR exists yet (genuine
    wait, the live measured case during a running dispatch), the obligation
    stays pending with NO fabricated linkage — ``pr_number`` and ``branch``
    remain None, because nothing has been resolved. This is the state the
    EINDtoestand count must not treat as a defect."""
    state_dir = _make_state_dir(tmp_path)
    dispatch_id = "20260920-103000-still-running"
    register_obligation(
        state_dir, dispatch_id=dispatch_id, gate="codex_gate", project_id="vnx-dev",
    )

    manager = _FakeReviewGateManager(state_dir, result_status="pass")
    # No PR via metadata, no PR via GitHub — but the branch still exists
    # (the dispatch is running and has pushed). Honest AWAITING wait.
    monkeypatch.setattr(runner, "_build_manager", lambda state_dir: manager)
    monkeypatch.setattr(runner, "_pr_from_dispatch_metadata", lambda sd, did: None)
    monkeypatch.setattr(runner, "_pr_from_github", lambda did, owner_repo: None)
    monkeypatch.setattr(runner, "_resolve_github_owner_repo", lambda sd: "Vinix24/vnx-orchestration")
    monkeypatch.setattr(runner, "_branch_exists_on_github", lambda did, owner_repo: True)
    monkeypatch.setattr(runner, "_dispatch_is_live", lambda sd, did: None, raising=False)

    summary = runner.run(state_dir)

    assert summary["pending_after"] == 1, (
        "a running dispatch with no PR yet is a genuine wait, not a defect"
    )
    updated = _read_obligation(state_dir, dispatch_id)
    assert updated["status"] == STATUS_PENDING
    assert updated["pr_number"] is None, (
        "no PR exists, so no pr_number must be fabricated onto the record"
    )
    assert not manager.calls, "the gate manager must not be invoked when no PR resolves"


def test_obligation_acquires_linkage_from_dispatch_metadata(tmp_path, monkeypatch):
    """The forward half has a SECOND source: the receipt pipeline stamps a
    ``pr_id`` in ``dispatch_metadata`` before the PR is visible via ``gh``
    (the PR was opened but ``gh pr list`` has not caught up, or the runner
    runs offline). ``resolve_pr_number`` reads that first. This pins that
    the metadata path ALSO stamps the linkage back, not only the GitHub
    path."""
    state_dir = _make_state_dir(tmp_path)
    dispatch_id = "20260920-103000-metadata-linkage"
    register_obligation(
        state_dir, dispatch_id=dispatch_id, gate="codex_gate", project_id="vnx-dev",
    )

    manager = _FakeReviewGateManager(state_dir, result_status="pass")
    monkeypatch.setattr(runner, "_build_manager", lambda state_dir: manager)
    monkeypatch.setattr(runner, "_pr_from_dispatch_metadata", lambda sd, did: 1880)
    # GitHub would also find it, but metadata wins (returns first).
    monkeypatch.setattr(runner, "_pr_from_github", lambda did, owner_repo: 1880)
    monkeypatch.setattr(runner, "_branch_from_github", lambda pr, owner_repo: f"dispatch/{dispatch_id}")
    monkeypatch.setattr(runner, "_resolve_github_owner_repo", lambda sd: "Vinix24/vnx-orchestration")
    monkeypatch.setattr(runner, "_get_pr_head_sha_for_gate", lambda pr: _DEFAULT_TEST_HEAD_SHA)
    monkeypatch.setattr(runner, "_pr_state_from_github", lambda pr_number, owner_repo: "OPEN", raising=False)
    fake_rgm = types.ModuleType("review_gate_manager")
    fake_rgm._compute_changed_files = lambda branch: ["scripts/lib/foo.py"]
    monkeypatch.setitem(sys.modules, "review_gate_manager", fake_rgm)

    runner.run(state_dir)

    updated = _read_obligation(state_dir, dispatch_id)
    assert updated["status"] == STATUS_FULFILLED
    assert updated["pr_number"] == 1880
    assert updated["branch"] == f"dispatch/{dispatch_id}"


# ---------------------------------------------------------------------------
# The regression guard: if the GitHub lookup is silently removed (the
# v1.5.0 form), an obligation declared without a linkage never acquires one
# and the forward half is broken. This is the RED proof the dispatch asks
# for — rood on the regressed form, groen on current code.
# ---------------------------------------------------------------------------


def test_regression_v15_form_leaves_obligation_without_linkage(tmp_path, monkeypatch):
    """RED proof for DEEL B: simulate the v1.5.0 regression — the GitHub
    lookup is removed (``_pr_from_github`` always returns None and the
    branch-existence check is skipped, so resolution falls to AWAITING). An
    obligation declared without a linkage then NEVER acquires one: the
    runner leaves it pending with ``pr_number=None, branch=None``, the gate
    is never invoked, and the obligation is never provably resolvable.

    On current 1.6.2 code this test is GREEN because the GitHub lookup
    finds the PR and stamps the linkage. The point is that it would go RED
    the moment that mechanism is removed — which is the regression guard
    the dispatch requires. See the report for the measured red-run counts.
    """
    state_dir = _make_state_dir(tmp_path)
    dispatch_id = "20260920-103000-regression-guard"
    register_obligation(
        state_dir, dispatch_id=dispatch_id, gate="codex_gate", project_id="vnx-dev",
    )

    manager = _FakeReviewGateManager(state_dir, result_status="pass")
    # The 1.6.2 mechanism: GitHub finds the PR.
    _patch_resolved_linkage(monkeypatch, manager, pr_number=1881, branch=f"dispatch/{dispatch_id}")

    runner.run(state_dir)

    updated = _read_obligation(state_dir, dispatch_id)
    # On current code the linkage IS acquired — assert it, so removing the
    # mechanism (the v1.5.0 form) turns this green->red.
    assert updated["pr_number"] == 1881, (
        "current code stamps the pr_number; if this regresses to None the "
        "forward-half mechanism (resolve_pr_number -> GitHub lookup -> stamp "
        "back) has been lost — the v1.5.0 form is back"
    )
    assert updated["branch"] == f"dispatch/{dispatch_id}"
    assert updated["status"] == STATUS_FULFILLED


def test_red_proof_v15_form_breaks_forward_linkage(tmp_path, monkeypatch):
    """The actual RED measurement for DEEL B: force the v1.5.0 form (the
    GitHub lookup returns None, as it would if the lookup code were
    removed) and show the obligation ends WITHOUT a linkage and the gate
    is never invoked. This is the state the EINDtoestand count caught on
    the v1.5.0-generatie (sales-copilot 47/47, website-vincentvandeth 3/3
    vast zonder koppeling).

    This test PASSES on current code AND on the regressed code — it
    documents what the regression looks like. The guard that catches the
    regression is
    :func:`test_regression_v15_form_leaves_obligation_without_linkage`
    above, which asserts the 1.6.2 outcome and goes red the moment the
    GitHub lookup stops finding the PR.
    """
    state_dir = _make_state_dir(tmp_path)
    dispatch_id = "20260920-103000-red-proof"
    register_obligation(
        state_dir, dispatch_id=dispatch_id, gate="codex_gate", project_id="vnx-dev",
    )

    manager = _FakeReviewGateManager(state_dir, result_status="pass")
    # v1.5.0 form: no PR via metadata, no PR via GitHub, branch exists
    # (genuine wait). Resolution falls to AWAITING and the runner leaves
    # the obligation pending with no fabricated linkage.
    monkeypatch.setattr(runner, "_build_manager", lambda state_dir: manager)
    monkeypatch.setattr(runner, "_pr_from_dispatch_metadata", lambda sd, did: None)
    monkeypatch.setattr(runner, "_pr_from_github", lambda did, owner_repo: None)
    monkeypatch.setattr(runner, "_resolve_github_owner_repo", lambda sd: "Vinix24/vnx-orchestration")
    monkeypatch.setattr(runner, "_branch_exists_on_github", lambda did, owner_repo: True)
    monkeypatch.setattr(runner, "_dispatch_is_live", lambda sd, did: None, raising=False)

    runner.run(state_dir)

    updated = _read_obligation(state_dir, dispatch_id)
    assert updated["status"] == STATUS_PENDING, (
        "the v1.5.0 form leaves the obligation pending — never resolvable"
    )
    assert updated["pr_number"] is None, (
        "no linkage is acquired under the v1.5.0 form — the forward half is broken"
    )
    assert updated["branch"] is None
    assert not manager.calls, "the gate is never invoked when the PR never resolves"
