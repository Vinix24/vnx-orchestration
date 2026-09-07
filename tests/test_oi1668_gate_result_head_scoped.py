"""Gate evidence hangs on the HEAD it was produced against (OI-1668).

Live measurement, PR #1808 on 2026-09-07 (T0, reproduced read-only in this
dispatch from ``state/review_gates/results/pr-1808-codex_gate.json``):

  18:33  codex_gate ran against head ``5fd261fd`` and recorded
         ``status=completed``, ``contract_hash=b37403cb1d40fc3f``.
  18:41  a fix-forward pushed head ``1f8c985f``.
  18:48 / 18:54 / 18:57  three attempts to make codex sign for the NEW head
         produced no new record. The write path reported
         ``write_refused: true``, ``attempted_status: unavailable``,
         ``attempted_commit_sha: 1f8c985f...`` -- the guard compared
         decidedness and evidence, and never looked at which commit either
         record was about.
  Result, verbatim from the merge door: ``NO-GO: geen review-gate resultaat
  gevonden voor codex_gate op 1808: merge niet toetsbaar``. A legitimate PR
  with green CI could not merge, because a decided verdict about code that no
  longer exists occupied the slot the current head needed.

Two mechanisms, one property:

  1. ``gate_recorder._check_overwrite_guard`` -- a terminal, evidenced record
     for a DIFFERENT head is not evidence for the head being written now, so
     it may be replaced (even by something less decided). Same head: the
     OI-1469/OI-1470 protection is unchanged.
  2. ``gate_request_handler._dispatch_review_seat`` -- the takeover walk reads
     each candidate's last result to decide whether to route AROUND that seat.
     A record about another head says nothing about this head, so the seat is
     dispatched again instead of being routed around.

Both halves only engage when the head being written/reviewed is actually
KNOWN. A payload with no ``commit_sha``, or a PR whose head sha cannot be
resolved, leaves today's behaviour exactly as it was -- the fix must not
weaken the guard on the (many) paths that never carried a sha.
"""
from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

import pytest

VNX_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = VNX_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(SCRIPTS_DIR / "lib"))

import gate_request_handler
from gate_recorder import record_failure, write_result_guarded
from gate_request_handler import GateRequestHandlerMixin

# The two real heads of PR #1808 (see module docstring).
SHA_A = "5fd261fd9af761ff94911df5eea14c5a6e8caa74"
SHA_B = "1f8c985f73d3e5900034b9eb93ff36479a3bfc82"

# OI-1569 recency gate: a lane_exhausted fixture must carry a FRESH
# recorded_at, or the handler tests below would exercise the TTL branch
# instead of the head-scoping branch they are about.
_FRESH_RECORDED_AT = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _decided_pass(commit_sha: str, **overrides: Any) -> Dict[str, Any]:
    """A terminal, fully-evidenced codex verdict -- the shape the overwrite
    guard exists to protect."""
    payload = {
        "gate": "codex_gate",
        "pr_id": "1808",
        "pr_number": 1808,
        "status": "completed",
        "contract_hash": "b37403cb1d40fc3f",
        "report_path": "/tmp/codex-report-1808.md",
        "blocking_findings": [],
        "dispatch_id": "20260907-golfb-b2a-forge-client",
        "branch": "dispatch/20260907-golfb-b2a-forge-client",
        "commit_sha": commit_sha,
    }
    payload.update(overrides)
    return payload


def _outage(commit_sha: str) -> Dict[str, Any]:
    """The live attempted write: an execution failure booked as
    ``unavailable`` with no evidence, carrying the NEW head's sha."""
    return {
        "gate": "codex_gate",
        "pr_id": "1808",
        "pr_number": 1808,
        "status": "unavailable",
        "reason": "exit_nonzero",
        "reason_detail": "Subprocess exited with code 1",
        "contract_hash": "",
        "report_path": "",
        "commit_sha": commit_sha,
    }


# ---------------------------------------------------------------------------
# 1. The overwrite guard is head-scoped
# ---------------------------------------------------------------------------


class TestOverwriteGuardIsHeadScoped:

    def test_outage_for_a_new_head_may_replace_a_pass_from_an_old_head(self, tmp_path):
        """RED on main: refused, so the slot stayed pinned to ``5fd261fd``
        and the merge door found no result for ``1f8c985f``."""
        out = tmp_path / "pr-1808-codex_gate.json"
        out.write_text(json.dumps(_decided_pass(SHA_A)), encoding="utf-8")

        payload, written = write_result_guarded(
            out, _outage(SHA_B), gate="codex_gate", pr_ref="1808",
        )

        assert written is True, (
            "a decided verdict about head 5fd261fd is not evidence about head "
            "1f8c985f -- it must not block a write for the new head (OI-1668)"
        )
        on_disk = json.loads(out.read_text(encoding="utf-8"))
        assert on_disk["commit_sha"] == SHA_B
        assert on_disk["status"] == "unavailable"
        assert payload["commit_sha"] == SHA_B

    def test_live_record_failure_path_for_a_new_head_lands(self, tmp_path):
        """The same claim through the writer that actually produced the live
        refusal: ``record_failure`` (gate_runner's exit_nonzero booking),
        whose request payload carries the new head."""
        results_dir = tmp_path / "results"
        requests_dir = tmp_path / "requests"
        results_dir.mkdir(parents=True, exist_ok=True)
        requests_dir.mkdir(parents=True, exist_ok=True)
        rf = results_dir / "pr-1808-codex_gate.json"
        rf.write_text(json.dumps(_decided_pass(SHA_A)), encoding="utf-8")

        result = record_failure(
            # pr_id="" so result_file_path resolves the pr-<N>-<gate>.json
            # slot the live record occupies, not the pr_id contract slug.
            gate="codex_gate", pr_number=1808, pr_id="",
            result={
                "reason": "exit_nonzero",
                "reason_detail": "Subprocess exited with code 1",
                "duration_seconds": 12.0, "partial_output_lines": 4, "runner_pid": 1,
            },
            request_payload={
                "gate": "codex_gate", "pr_number": 1808,
                "branch": "dispatch/20260907-golfb-b2a-forge-client",
                "commit_sha": SHA_B,
                "dispatch_id": "20260907-golfb-shafix-2",
            },
            requests_dir=requests_dir, results_dir=results_dir,
        )

        assert result.get("write_refused") is not True, (
            "the live shape: a re-gate for the new head must not come back "
            "annotated as a refused write (OI-1668)"
        )
        on_disk = json.loads(rf.read_text(encoding="utf-8"))
        assert on_disk["commit_sha"] == SHA_B
        assert on_disk["status"] == "unavailable"

    def test_outage_for_the_same_head_is_still_refused(self, tmp_path):
        """The OI-1469/OI-1470 protection must not weaken: on the SAME head a
        provider outage still may not erase a decided, evidenced verdict."""
        out = tmp_path / "pr-1808-codex_gate.json"
        existing = _decided_pass(SHA_A)
        out.write_text(json.dumps(existing), encoding="utf-8")

        payload, written = write_result_guarded(
            out, _outage(SHA_A), gate="codex_gate", pr_ref="1808",
        )

        assert written is False
        assert payload == existing
        assert json.loads(out.read_text(encoding="utf-8")) == existing

    def test_pass_for_a_new_head_replaces_a_pass_on_an_old_head(self, tmp_path):
        """Control (already green on main via terminal-over-terminal): a real
        re-gate on the new head lands. Kept so a head-scoping regression that
        froze the slot the other way round would be caught here too."""
        out = tmp_path / "pr-1808-codex_gate.json"
        out.write_text(json.dumps(_decided_pass(SHA_A)), encoding="utf-8")

        fresh = _decided_pass(SHA_B, contract_hash="fresh0000head0b", dispatch_id="rerun-2")
        payload, written = write_result_guarded(
            out, fresh, gate="codex_gate", pr_ref="1808",
        )

        assert written is True
        assert json.loads(out.read_text(encoding="utf-8"))["commit_sha"] == SHA_B

    def test_existing_record_without_a_commit_sha_belongs_to_no_head(self, tmp_path, caplog):
        """A record that never recorded which commit it judged cannot be
        evidence for the head being written now. Overwritable -- and LOUD,
        because an unanchored verdict being displaced is exactly the thing an
        operator must be able to see in the log."""
        out = tmp_path / "pr-1808-codex_gate.json"
        unanchored = _decided_pass(SHA_A)
        unanchored.pop("commit_sha")
        out.write_text(json.dumps(unanchored), encoding="utf-8")

        with caplog.at_level(logging.WARNING, logger="gate_recorder"):
            _payload, written = write_result_guarded(
                out, _outage(SHA_B), gate="codex_gate", pr_ref="1808",
            )

        assert written is True
        assert json.loads(out.read_text(encoding="utf-8"))["commit_sha"] == SHA_B
        assert any(
            "no commit_sha" in record.getMessage() and record.levelno >= logging.WARNING
            for record in caplog.records
        ), (
            "displacing an unanchored terminal record must be logged loudly, "
            f"got: {[r.getMessage() for r in caplog.records]!r}"
        )

    def test_new_payload_without_a_commit_sha_does_not_unlock_the_guard(self, tmp_path):
        """The escape hatch needs a KNOWN head to be about. A write that
        carries no sha of its own says nothing about which head it is for, so
        the guard stays exactly as strict as it is today -- otherwise every
        sha-less writer in the tree would silently lose the protection."""
        out = tmp_path / "pr-1808-codex_gate.json"
        existing = _decided_pass(SHA_A)
        out.write_text(json.dumps(existing), encoding="utf-8")

        sha_less = _outage("")
        sha_less.pop("commit_sha")
        _payload, written = write_result_guarded(
            out, sha_less, gate="codex_gate", pr_ref="1808",
        )

        assert written is False
        assert json.loads(out.read_text(encoding="utf-8")) == existing


# ---------------------------------------------------------------------------
# 2. The takeover walk is head-scoped
# ---------------------------------------------------------------------------


_CHAIN = {"codex_gate": "kimi_gate"}


class _StubHandler(GateRequestHandlerMixin):
    """Minimal stand-in for ReviewGateManager: the two path helpers plus a
    recording ``_dispatch_one_review``. ``_dispatch_review_seat`` itself is
    real production code."""

    def __init__(self, state_dir: Path) -> None:
        self.requests_dir = state_dir / "review_gates" / "requests"
        self.results_dir = state_dir / "review_gates" / "results"
        self.requests_dir.mkdir(parents=True, exist_ok=True)
        self.results_dir.mkdir(parents=True, exist_ok=True)
        self.dispatched: List[str] = []

    def _request_path(self, gate: str, pr_number: int) -> Path:
        return self.requests_dir / f"pr-{pr_number}-{gate}.json"

    def _result_path(self, gate: str, pr_number: int) -> Path:
        return self.results_dir / f"pr-{pr_number}-{gate}.json"

    def _dispatch_one_review(self, gate, pr_number, branch, risk_class, changed_files, mode, dispatch_id):
        self.dispatched.append(gate)
        return {"gate": gate, "pr_number": pr_number, "status": "requested"}


def _lane_exhausted(commit_sha: str) -> Dict[str, Any]:
    """A codex seat whose own last result is a real lane exhaustion. The
    marker sits in ``residual_risk`` so ``_scan_seat_failure_text`` classifies
    it from the record's own fields, without needing a lane log on disk."""
    return {
        "gate": "codex_gate",
        "pr_id": "1808",
        "pr_number": 1808,
        "status": "not_executable",
        "reason": "provider_quota_exhausted",
        "reason_detail": "You've hit your usage limit.",
        "residual_risk": "Error code: 429 - You've hit your usage limit. Upgrade to Pro",
        "summary": "codex_gate not executable",
        "contract_hash": "",
        "report_path": "",
        "recorded_at": _FRESH_RECORDED_AT,
        "branch": "dispatch/20260907-golfb-b2a-forge-client",
        "commit_sha": commit_sha,
    }


def _seat(handler: _StubHandler) -> Dict[str, Any]:
    return handler._dispatch_review_seat(
        "codex_gate", 1808, "dispatch/20260907-golfb-b2a-forge-client",
        "medium", ["scripts/lib/gate_recorder.py"], "per_pr",
        "20260907-golfb-shafix-poortbewijs-per-head", chain=dict(_CHAIN),
    )


class TestTakeoverWalkIsHeadScoped:

    def test_record_from_another_head_does_not_route_around_the_seat(self, tmp_path, monkeypatch):
        """RED on main: the walk read a record about ``5fd261fd``, concluded
        the codex seat was dead, and handed the seat to the successor -- while
        the head under review had moved on and codex had never been asked
        about it."""
        handler = _StubHandler(tmp_path / "state")
        handler._result_path("codex_gate", 1808).write_text(
            json.dumps(_lane_exhausted(SHA_A)), encoding="utf-8",
        )
        monkeypatch.setattr(gate_request_handler, "get_pr_head_sha", lambda _pr: SHA_B)

        payload = _seat(handler)

        assert handler.dispatched == ["codex_gate"], (
            "an exhaustion recorded against another head is no statement about "
            "the current head -- the seat must be dispatched again (OI-1668)"
        )
        assert payload["gate"] == "codex_gate"
        assert "takeover" not in payload

    def test_record_from_the_current_head_still_routes_around_the_seat(self, tmp_path, monkeypatch):
        """Control: on the SAME head the takeover mechanism is untouched."""
        handler = _StubHandler(tmp_path / "state")
        handler._result_path("codex_gate", 1808).write_text(
            json.dumps(_lane_exhausted(SHA_B)), encoding="utf-8",
        )
        monkeypatch.setattr(gate_request_handler, "get_pr_head_sha", lambda _pr: SHA_B)

        payload = _seat(handler)

        assert handler.dispatched == ["kimi_gate"]
        assert payload["takeover"] is True
        assert payload["takeover_from"] == "codex_gate"

    def test_unresolvable_head_leaves_the_walk_exactly_as_it_was(self, tmp_path, monkeypatch):
        """No head to compare against (gh unavailable/unauthenticated, or a PR
        that does not resolve) means the head question cannot be answered --
        and an unanswerable question must not silently change the takeover
        decision."""
        handler = _StubHandler(tmp_path / "state")
        handler._result_path("codex_gate", 1808).write_text(
            json.dumps(_lane_exhausted(SHA_A)), encoding="utf-8",
        )
        monkeypatch.setattr(gate_request_handler, "get_pr_head_sha", lambda _pr: "")

        payload = _seat(handler)

        assert handler.dispatched == ["kimi_gate"]
        assert payload["takeover"] is True

    def test_head_sha_is_not_resolved_when_there_is_no_prior_result(self, tmp_path, monkeypatch):
        """The common first-round shape must not pay for a GitHub round-trip:
        with no prior record there is nothing to compare a head against."""
        handler = _StubHandler(tmp_path / "state")
        calls: List[Any] = []

        def _counting(pr_number):
            calls.append(pr_number)
            return SHA_B

        monkeypatch.setattr(gate_request_handler, "get_pr_head_sha", _counting)

        _seat(handler)

        assert handler.dispatched == ["codex_gate"]
        assert calls == [], "no prior result means no head lookup is needed"
