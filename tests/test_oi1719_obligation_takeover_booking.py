#!/usr/bin/env python3
"""OI-1719: the merge door reads the takeover booking from the obligation itself.

Measured live on the vnx-dev store (PRs #1830, #1832-#1838, 2026-09-11): eight
PRs were CI-green and content-reviewed by glm-5.2 on exactly their head, with
full evidence and zero blocking findings — and could not merge. The obligation
store booked each of them ``status: fulfilled`` with ``reason:
fulfilled_by_takeover_evidence`` and ``result_path`` pointing at the glm result
(chain ``codex_gate -> kimi_gate -> glm_gate``), but those glm gates were
started by hand and therefore carry no ``takeover_path`` field — the only field
the OI-1576 route reads. Four readers contradicted each other over the same
door: the obligation store said fulfilled, the forge check-run said success,
``vnx pr-ready`` said "codex_gate absent", and the merge door said NO-GO.

The fix adds a THIRD evidence source to ``check_review_gate_for_merge``, next
to the successor's self-carried ``takeover_path`` (OI-1576) and the peer
signer for a confirmed absence (OI-1624/OI-1642): the obligation's own
booking. It opens only when ALL of these hold:

  1. the obligation joins this PR and this gate (the same join
     ``declared_gates_for_pr`` uses);
  2. ``status == fulfilled`` AND ``reason == fulfilled_by_takeover_evidence``;
  3. the pointed-at ``result_path`` (or ``evidence_result_path``) is readable;
  4. the pointed-at record clears the unchanged invariant chain
     (``_merge_door_record_verdict``: terminal, complete evidence, report on
     disk, pass, no self-contradiction) and is not a ``test_run``;
  5. the sha-binding holds via ``_record_matches_scope``: the record's
     ``commit_sha`` IS the current head — unknown or divergent is a refusal,
     never a suspended judgement.

Hard boundaries pinned here: a real rejection is never weakened by a booking
(neither the declared gate's own, nor a peer's, nor the pointed-at record's),
and an obligation already terminal ``failed`` never opens the route.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

VNX_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = VNX_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(SCRIPTS_DIR / "lib"))

import closure_verifier
from pr_readiness import collect_gate_evidence

# Mirrored from the live vnx-dev store for PR #1836.
PR_ID = "1836"
BRANCH = "dispatch/20260911-oi1702-1703-uitslag-schrijft-receipt"
HEAD_SHA = "88bc8f2f561f9f9260a1a2a95d75bafbffa9ea1d"
STALE_SHA = "e9c9e3d579473569d5204c10d5bf4d346bef1ba2"
DISPATCH_ID = "20260911-oi1702-1703-uitslag-schrijft-receipt"

CLEAN_REPORT = "# Gate report\n\nAll findings reviewed, nothing blocking.\n"


def _report_file(tmp_path: Path, content: str = CLEAN_REPORT) -> Path:
    report = tmp_path / "report.md"
    report.write_text(content, encoding="utf-8")
    return report


def _write_result(results_dir: Path, gate: str, data: dict) -> Path:
    """Write a result record under the writer's real naming (pr-<n>-<gate>.json)."""
    results_dir.mkdir(parents=True, exist_ok=True)
    path = results_dir / f"pr-{PR_ID}-{gate}.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def _write_obligation(root: Path, dispatch_id: str = DISPATCH_ID, **overrides) -> Path:
    """An obligation record the way gate_obligation_runner's D2e branch books it."""
    obligations_root = root / "obligations"
    obligations_root.mkdir(parents=True, exist_ok=True)
    record = {
        "schema_version": 1,
        "kind": "review_gate_obligation",
        "dispatch_id": dispatch_id,
        "gate": "codex_gate",
        "project_id": "vnx-dev",
        "pr_number": int(PR_ID),
        "pr_id": None,
        "branch": BRANCH,
        "status": "fulfilled",
        "reason": "fulfilled_by_takeover_evidence",
        "resolved_by_gate": "glm_gate",
    }
    record.update(overrides)
    path = obligations_root / f"{dispatch_id}.json"
    path.write_text(json.dumps(record), encoding="utf-8")
    return path


def _glm_pass(report: Path, **overrides) -> dict:
    """pr-1836-glm_gate.json as measured: a fully evidenced pass on the head,
    started by hand — so no ``takeover_path`` of its own (the OI-1576 route
    can never fire for it)."""
    data = {
        "gate": "glm_gate",
        "pr_id": PR_ID,
        "status": "pass",
        "blocking_count": 0,
        "blocking_findings": [],
        "contract_hash": "36c2673c3e21480a",
        "report_path": str(report),
        "branch": BRANCH,
        "commit_sha": HEAD_SHA,
    }
    data.update(overrides)
    return data


def _booked_setup(tmp_path: Path, **result_overrides):
    """The live #1836 shape: fulfilled takeover booking + on-head glm pass."""
    results_dir = tmp_path / "results"
    report = _report_file(tmp_path)
    record_path = _write_result(results_dir, "glm_gate", _glm_pass(report, **result_overrides))
    _write_obligation(tmp_path, result_path=str(record_path), evidence_result_path=str(record_path))
    return results_dir


def _check(root: Path, gate: str = "codex_gate", head_sha=HEAD_SHA) -> dict:
    return closure_verifier.check_review_gate_for_merge(
        PR_ID, gate, root / "results", branch=BRANCH, head_sha=head_sha
    )


class TestBookingRouteOpens:
    def test_fulfilled_booking_with_on_head_evidence_is_go(self, tmp_path):
        """The live #1836 shape: the booking stands in for the declared gate."""
        _booked_setup(tmp_path)

        verdict = _check(tmp_path)

        assert verdict["verdict"] == "GO"
        assert verdict["gate"] == "codex_gate"
        assert verdict["evidence_gate"] == "glm_gate"
        assert verdict["evidence_via"] == "takeover_boeking"
        assert "takeover-boeking" in verdict["message"]
        assert DISPATCH_ID in verdict["message"]

    def test_evidence_result_path_is_read_when_result_path_is_absent(self, tmp_path):
        """The booking names its evidence in either field."""
        results_dir = tmp_path / "results"
        report = _report_file(tmp_path)
        record_path = _write_result(results_dir, "glm_gate", _glm_pass(report))
        _write_obligation(tmp_path, result_path=None, evidence_result_path=str(record_path))

        verdict = _check(tmp_path)

        assert verdict["verdict"] == "GO"
        assert verdict["evidence_gate"] == "glm_gate"

    def test_booking_joins_on_normalised_pr_id_when_pr_number_is_absent(self, tmp_path):
        """The same join declared_gates_for_pr uses: records the runner never
        stamped a pr_number on still join via the normalised spec pr_id."""
        results_dir = tmp_path / "results"
        report = _report_file(tmp_path)
        record_path = _write_result(results_dir, "glm_gate", _glm_pass(report))
        _write_obligation(
            tmp_path, pr_number=None, pr_id=f"PR-{PR_ID}", result_path=str(record_path)
        )

        verdict = _check(tmp_path)

        assert verdict["verdict"] == "GO"


class TestBookingRouteRefusals:
    def test_evidence_on_another_commit_is_no_go(self, tmp_path):
        """Condition 5: the pointed record's commit_sha must BE the head."""
        _booked_setup(tmp_path, commit_sha=STALE_SHA)

        verdict = _check(tmp_path)

        assert verdict["verdict"] == "NO-GO"
        assert "takeover-boeking" in verdict["message"]
        assert "commit_sha" in verdict["message"]

    def test_unknown_head_is_a_refusal_not_a_suspension(self, tmp_path):
        """Onbekend is GEEN vervulling: with no head sha to bind against, the
        route refuses — it does not suspend judgement like OI-1571 tak 3."""
        _booked_setup(tmp_path)

        verdict = _check(tmp_path, head_sha=None)

        assert verdict["verdict"] == "NO-GO"

    def test_pending_obligation_never_opens_the_route(self, tmp_path):
        """Condition 2: only a fulfilled obligation counts."""
        _booked_setup(tmp_path)
        _write_obligation(tmp_path, status="pending", reason=None)

        verdict = _check(tmp_path)

        assert verdict["verdict"] == "NO-GO"
        assert "takeover-boeking" not in verdict["message"]

    def test_another_fulfilment_reason_never_opens_the_route(self, tmp_path):
        """Condition 2: fulfilled by something OTHER than takeover evidence is
        not this route's business."""
        _booked_setup(tmp_path)
        _write_obligation(tmp_path, reason="fulfilled_by_direct_run")

        verdict = _check(tmp_path)

        assert verdict["verdict"] == "NO-GO"
        assert "takeover-boeking" not in verdict["message"]

    def test_terminal_failed_obligation_never_opens_the_route(self, tmp_path):
        """Hard boundary: a booking that already closed failed — even one
        pointing at a passing record — gives this route nothing."""
        _booked_setup(tmp_path)
        _write_obligation(
            tmp_path, status="failed", reason="failed_by_takeover_evidence"
        )

        verdict = _check(tmp_path)

        assert verdict["verdict"] == "NO-GO"
        assert "takeover-boeking" not in verdict["message"]

    def test_unreadable_result_path_is_no_go(self, tmp_path):
        """Condition 3: the pointed-at evidence must be readable."""
        _write_obligation(
            tmp_path, result_path=str(tmp_path / "results" / "does-not-exist.json")
        )

        verdict = _check(tmp_path)

        assert verdict["verdict"] == "NO-GO"
        assert "onleesbaar" in verdict["message"]

    def test_absent_result_path_is_no_go(self, tmp_path):
        """Condition 3: a booking that names no evidence at all proves nothing."""
        _write_obligation(tmp_path, result_path=None, evidence_result_path=None)

        verdict = _check(tmp_path)

        assert verdict["verdict"] == "NO-GO"
        assert "takeover-boeking" in verdict["message"]

    def test_pointed_not_executable_record_is_no_go(self, tmp_path):
        """Condition 4: a not_executable record is a non-verdict, not evidence."""
        _booked_setup(
            tmp_path,
            status="not_executable",
            reason="gate_not_subprocess_routable",
            contract_hash="",
            report_path="",
        )

        verdict = _check(tmp_path)

        assert verdict["verdict"] == "NO-GO"

    def test_pointed_record_with_a_real_rejection_is_no_go(self, tmp_path):
        """Hard boundary: a booking pointing at a real rejection changes nothing."""
        _booked_setup(tmp_path, status="fail", blocking_count=1)

        verdict = _check(tmp_path)

        assert verdict["verdict"] == "NO-GO"

    def test_pointed_test_run_record_is_no_go(self, tmp_path):
        """Condition 4/5: offline test-run records are never real evidence."""
        _booked_setup(tmp_path, test_run=True)

        verdict = _check(tmp_path)

        assert verdict["verdict"] == "NO-GO"

    def test_pointed_non_review_gate_is_no_go(self, tmp_path):
        """A booking that points a review obligation at ci_gate lets CI sign a
        review — the same thing OI-1645 forbids both other routes."""
        _booked_setup(tmp_path, gate="ci_gate")

        verdict = _check(tmp_path)

        assert verdict["verdict"] == "NO-GO"
        assert "review-ondertekenaar" in verdict["message"]

    def test_booking_for_another_pr_never_opens_the_route(self, tmp_path):
        """Condition 1: the join is per PR — someone else's booking is not ours."""
        results_dir = tmp_path / "results"
        report = _report_file(tmp_path)
        record_path = _write_result(results_dir, "glm_gate", _glm_pass(report))
        _write_obligation(tmp_path, pr_number=9999, result_path=str(record_path))

        verdict = _check(tmp_path)

        assert verdict["verdict"] == "NO-GO"
        assert "takeover-boeking" not in verdict["message"]

    def test_booking_for_another_gate_never_opens_the_route(self, tmp_path):
        """Condition 1: the join is per gate — kimi's booking is not codex's."""
        results_dir = tmp_path / "results"
        report = _report_file(tmp_path)
        record_path = _write_result(results_dir, "glm_gate", _glm_pass(report))
        _write_obligation(tmp_path, gate="kimi_gate", result_path=str(record_path))

        verdict = _check(tmp_path)

        assert verdict["verdict"] == "NO-GO"
        assert "takeover-boeking" not in verdict["message"]


class TestRealRejectionsAreNeverWeakened:
    def test_a_peer_rejection_on_the_same_head_blocks_the_booking(self, tmp_path):
        """Hard boundary: a REAL rejection anywhere on this head blocks, even
        beside a fulfilled booking pointing at a pass — the same peer scan the
        OI-1624/OI-1642 absence route applies."""
        results_dir = _booked_setup(tmp_path)
        report = _report_file(tmp_path)
        _write_result(
            results_dir,
            "kimi_gate",
            _glm_pass(report, gate="kimi_gate", status="fail", blocking_count=1),
        )

        verdict = _check(tmp_path)

        assert verdict["verdict"] == "NO-GO"
        assert "afkeuring" in verdict["message"]

    def test_the_declared_gates_own_rejection_stands_above_any_booking(self, tmp_path):
        """A decided verdict at the declared gate always stands on its own;
        the booking route is never even consulted."""
        results_dir = _booked_setup(tmp_path)
        report = _report_file(tmp_path)
        _write_result(
            results_dir,
            "codex_gate",
            _glm_pass(report, gate="codex_gate", status="fail", blocking_count=1),
        )

        verdict = _check(tmp_path)

        assert verdict["verdict"] == "NO-GO"
        assert "codex_gate" in verdict["message"]
        assert "takeover-boeking" not in verdict["message"]

    def test_the_declared_gates_own_pass_needs_no_booking(self, tmp_path):
        """And its own pass never reads as takeover evidence."""
        results_dir = tmp_path / "results"
        report = _report_file(tmp_path)
        _write_result(results_dir, "codex_gate", _glm_pass(report, gate="codex_gate"))
        record_path = _write_result(results_dir, "glm_gate", _glm_pass(report))
        _write_obligation(tmp_path, result_path=str(record_path))

        verdict = _check(tmp_path)

        assert verdict["verdict"] == "GO"
        assert "evidence_via" not in verdict


class TestReadinessReadsTheSameSource:
    """The second reader (OI-1719): `vnx pr-ready` must reach the SAME decision
    from the SAME source as the merge door — including where the evidence came
    from, so a reader never thinks codex itself looked."""

    def test_collect_gate_evidence_carries_the_booking_provenance(self, tmp_path):
        state_dir = tmp_path / "state"
        results_dir = state_dir / "review_gates" / "results"
        report = _report_file(tmp_path)
        record_path = _write_result(results_dir, "glm_gate", _glm_pass(report))
        obligations_root = state_dir / "review_gates" / "obligations"
        obligations_root.mkdir(parents=True, exist_ok=True)
        record = {
            "schema_version": 1,
            "kind": "review_gate_obligation",
            "dispatch_id": DISPATCH_ID,
            "gate": "codex_gate",
            "pr_number": int(PR_ID),
            "status": "fulfilled",
            "reason": "fulfilled_by_takeover_evidence",
            "result_path": str(record_path),
        }
        (obligations_root / f"{DISPATCH_ID}.json").write_text(
            json.dumps(record), encoding="utf-8"
        )

        evidence = collect_gate_evidence(
            int(PR_ID), ["codex_gate"], state_dir, branch=BRANCH, head_sha=HEAD_SHA
        )

        assert len(evidence) == 1
        gate = evidence[0]
        assert gate.satisfied
        assert gate.evidence_gate == "glm_gate"
        assert gate.evidence_via == "takeover_boeking"
        # The sha/report shown are the EVIDENCE record's, not a blank "absent"
        # for the declared gate whose obligation was fulfilled by a booking.
        assert gate.record_sha == HEAD_SHA
        assert gate.report_exists is True
