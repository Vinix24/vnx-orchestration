"""tests/test_gate_obligation_reopen_stale_evidence.py — OI-1571 tak 3: the
audited, one-off correction tool for an obligation booked fulfilled/failed
off evidence about a DIFFERENT commit, before the runner's own sha-binding
check existed.

Live measured shape (PR #1719, 30-08): obligation
``20260830-133000-oi1453-noemer-is-pass`` booked fulfilled via
``resolved_by_gate=glm_gate`` off a glm_gate PASS recorded against an OLDER
commit than the PR's current head. This script is the ONLY audited place
that state mutation happens — never a hand-edit of the JSON.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parent.parent
for p in (ROOT / "scripts" / "lib", ROOT / "scripts", ROOT):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import gate_obligation_reopen_stale_evidence as reopener  # noqa: E402
from gate_obligations import (  # noqa: E402
    STATUS_FULFILLED,
    STATUS_PENDING,
    obligation_path,
    register_obligation,
    update_obligation,
)

_HEAD_SHA = "64df9933f6b3fed46070d597965f4415acca83e"
_STALE_SHA = "8101fdf2dabcc29190710f9f62aed6bb451859d1"


def _make_state_dir(tmp_path: Path) -> Path:
    state_dir = tmp_path / "vnx-data" / "state"
    (state_dir / "review_gates" / "results").mkdir(parents=True, exist_ok=True)
    return state_dir


def _write_evidence(state_dir: Path, *, commit_sha: str, gate: str = "glm_gate", pr_number: int = 1719) -> Path:
    path = state_dir / "review_gates" / "results" / f"pr-{pr_number}-{gate}.json"
    path.write_text(
        json.dumps({
            "gate": gate, "pr_number": pr_number, "status": "pass",
            "contract_hash": "sha256:deadbeef", "report_path": "/tmp/does-not-need-to-exist.md",
            "commit_sha": commit_sha, "recorded_at": "2026-08-29T11:35:55Z",
        }),
        encoding="utf-8",
    )
    return path


def _seed_takeover_fulfilled_obligation(state_dir: Path, dispatch_id: str, evidence_path: Path) -> Path:
    path = register_obligation(
        state_dir, dispatch_id=dispatch_id, gate="codex_gate",
        project_id="vnx-dev", pr_number=1719,
    )
    update_obligation(
        path,
        status=STATUS_FULFILLED,
        resolved_at="2026-08-30T13:10:19Z",
        result_path=str(evidence_path),
        evidence_result_path=str(evidence_path),
        resolved_by_gate="glm_gate",
        fulfilled_by="glm_gate",
        takeover_gate="glm_gate",
        reason="fulfilled_by_takeover_evidence",
    )
    return path


class TestVerifyStaleEvidence:
    def test_refuses_when_sha_actually_matches(self, tmp_path, monkeypatch):
        state_dir = _make_state_dir(tmp_path)
        evidence_path = _write_evidence(state_dir, commit_sha=_HEAD_SHA)
        _seed_takeover_fulfilled_obligation(state_dir, "d-matching", evidence_path)
        monkeypatch.setattr(reopener, "_get_pr_head_sha_for_gate", lambda pr_number: _HEAD_SHA)

        with pytest.raises(reopener.ReopenRefused, match="MATCHES"):
            reopener.verify_stale_evidence(state_dir, "d-matching")

    def test_refuses_when_head_sha_unresolvable(self, tmp_path, monkeypatch):
        state_dir = _make_state_dir(tmp_path)
        evidence_path = _write_evidence(state_dir, commit_sha=_STALE_SHA)
        _seed_takeover_fulfilled_obligation(state_dir, "d-unresolvable", evidence_path)
        monkeypatch.setattr(reopener, "_get_pr_head_sha_for_gate", lambda pr_number: "")

        with pytest.raises(reopener.ReopenRefused, match="unverifiable"):
            reopener.verify_stale_evidence(state_dir, "d-unresolvable")

    def test_refuses_when_obligation_not_terminal(self, tmp_path, monkeypatch):
        state_dir = _make_state_dir(tmp_path)
        register_obligation(
            state_dir, dispatch_id="d-still-pending", gate="codex_gate",
            project_id="vnx-dev", pr_number=1719,
        )
        with pytest.raises(reopener.ReopenRefused, match="not fulfilled/failed"):
            reopener.verify_stale_evidence(state_dir, "d-still-pending")

    def test_refuses_when_dispatch_id_unknown(self, tmp_path):
        state_dir = _make_state_dir(tmp_path)
        with pytest.raises(reopener.ReopenRefused, match="no obligation record"):
            reopener.verify_stale_evidence(state_dir, "d-does-not-exist")

    def test_proves_mismatch_and_returns_both_shas(self, tmp_path, monkeypatch):
        state_dir = _make_state_dir(tmp_path)
        evidence_path = _write_evidence(state_dir, commit_sha=_STALE_SHA)
        _seed_takeover_fulfilled_obligation(state_dir, "d-1719-shape", evidence_path)
        monkeypatch.setattr(reopener, "_get_pr_head_sha_for_gate", lambda pr_number: _HEAD_SHA)

        proof = reopener.verify_stale_evidence(state_dir, "d-1719-shape")
        assert proof["head_sha"] == _HEAD_SHA
        assert proof["evidence_sha"] == _STALE_SHA
        assert proof["resolved_by_gate"] == "glm_gate"


class TestMissingEvidence:
    """OI-1726 (2026-09-12): an obligation booked fulfilled/failed off an
    evidence file that no longer exists on disk is the STRONGEST possible
    reopen case — the claim is false by construction, not merely stale. This
    script used to refuse exactly that (measured live: ``REFUSED: evidence
    file does not exist on disk: .../pr-1840-kimi_gate.json``), which left the
    poisoned-purge victim terminal with no way forward.

    The absence IS the proof, so the missing-evidence path must NOT resolve a
    PR head or compare shas — there is nothing to compare against."""

    def _seed_missing_evidence_obligation(self, state_dir, dispatch_id, *, gate="kimi_gate", pr_number=1840):
        path = register_obligation(
            state_dir, dispatch_id=dispatch_id, gate=gate,
            project_id="vnx-dev", pr_number=pr_number,
        )
        # The vanished record: referenced by the obligation, never written —
        # exactly the shape a purge victim leaves behind.
        vanished = state_dir / "review_gates" / "results" / f"pr-{pr_number}-{gate}.json"
        update_obligation(
            path,
            status=STATUS_FULFILLED,
            resolved_at="2026-09-11T10:00:00Z",
            result_path=str(vanished),
            evidence_result_path=str(vanished),
            resolved_by_gate=gate,
            fulfilled_by=gate,
            takeover_gate=gate,
            reason="fulfilled_by_takeover_evidence",
        )
        return path, vanished

    def test_missing_evidence_is_a_valid_reopen_reason(self, tmp_path, monkeypatch):
        state_dir = _make_state_dir(tmp_path)
        _path, vanished = self._seed_missing_evidence_obligation(state_dir, "d-missing-evidence")
        # The absence is itself the proof — a PR-head resolution attempt here
        # would be a bug (there is no evidence sha to bind against).
        monkeypatch.setattr(
            reopener, "_get_pr_head_sha_for_gate",
            lambda pr_number: pytest.fail("missing-evidence reopen must not resolve a PR head"),
        )

        proof = reopener.verify_stale_evidence(state_dir, "d-missing-evidence")

        assert proof.get("evidence_missing") is True
        assert proof["evidence_path"] == str(vanished)
        assert proof["head_sha"] == ""
        assert proof["evidence_sha"] == ""

    def test_write_reopens_a_missing_evidence_obligation(self, tmp_path, monkeypatch):
        state_dir = _make_state_dir(tmp_path)
        path, _vanished = self._seed_missing_evidence_obligation(state_dir, "d-missing-write")

        with patch("review_gate_manager.emit_governance_receipt") as mock_emit:
            outcome = reopener.reopen_obligation(
                state_dir, "d-missing-write", operator_reason="OI-1726 purged record", write=True,
            )

        assert outcome["action"] == "reopened"
        assert mock_emit.called, "the ledger event must be emitted before the mutation (ADR-005)"
        record = json.loads(path.read_text(encoding="utf-8"))
        assert record["status"] == STATUS_PENDING
        assert record["attempts"] == 0
        assert record["result_path"] is None
        assert record["evidence_result_path"] is None
        assert record["resolved_by_gate"] is None
        assert record["fulfilled_by"] is None
        assert "OI-1726 purged record" in record["reason_detail"]
        assert "no longer exists on disk" in record["reason_detail"]

    def test_corrupt_evidence_is_still_refused(self, tmp_path):
        """A present-but-unparseable file is NOT the missing-evidence case.
        Its stale-ness cannot be PROVEN (no commit_sha is readable), so the
        script must refuse (no write) and leave the obligation untouched —
        the same verify-before-write discipline, applied to the ambiguous
        branch. Chosen deliberately over reopening: reopening an obligation
        whose evidence might be current would risk losing a valid review."""
        state_dir = _make_state_dir(tmp_path)
        evidence = state_dir / "review_gates" / "results" / "pr-1719-codex_gate.json"
        evidence.write_text('{"gate": "codex_gate", "commit_sha": "8101fd', encoding="utf-8")  # torn JSON
        path = register_obligation(
            state_dir, dispatch_id="d-corrupt", gate="codex_gate",
            project_id="vnx-dev", pr_number=1719,
        )
        update_obligation(
            path,
            status=STATUS_FULFILLED,
            resolved_at="2026-09-11T10:00:00Z",
            result_path=str(evidence),
            evidence_result_path=str(evidence),
            resolved_by_gate="codex_gate",
            reason="fulfilled_by_takeover_evidence",
        )

        with pytest.raises(reopener.ReopenRefused, match="unreadable"):
            reopener.verify_stale_evidence(state_dir, "d-corrupt")

        record = json.loads(path.read_text(encoding="utf-8"))
        assert record["status"] == STATUS_FULFILLED, "a corrupt-evidence refusal must never mutate"


class TestRunnerRebooksReopenedObligation:
    """MOET (OI-1726): once a vanished-evidence obligation is reopened to
    pending, the obligation-runner re-books it on the REAL evidence that
    exists on disk — never the purged record. The reopen is the missing half;
    the runner's own fulfilment-on-existing-evidence is pre-existing."""

    def test_runner_rebooks_reopened_obligation_on_real_evidence(self, tmp_path, monkeypatch):
        import gate_obligation_runner as runner

        state_dir = _make_state_dir(tmp_path)
        results = state_dir / "review_gates" / "results"

        # The real, still-current evidence for the declared gate.
        report = state_dir / "unified_reports" / "glm-pr1840.md"
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text("# glm_gate verdict for PR 1840\n", encoding="utf-8")
        real = results / "pr-1840-glm_gate.json"
        real.write_text(
            json.dumps({
                "gate": "glm_gate", "pr_number": 1840, "status": "pass",
                "verdict": "pass", "contract_hash": "sha256:real",
                "report_path": str(report), "commit_sha": _HEAD_SHA,
                "recorded_at": "2026-09-11T11:00:00Z",
            }),
            encoding="utf-8",
        )

        # The obligation: declared glm_gate, booked fulfilled off a record that
        # no longer exists (the purged poison), then reopened to pending.
        vanished = results / "pr-1840-kimi_gate.json"  # never written — gone
        path = register_obligation(
            state_dir, dispatch_id="d-rebook", gate="glm_gate",
            project_id="vnx-dev", pr_number=1840,
        )
        update_obligation(
            path,
            status=STATUS_FULFILLED,
            resolved_at="2026-09-11T10:00:00Z",
            result_path=str(vanished),
            evidence_result_path=str(vanished),
            resolved_by_gate="glm_gate",
            fulfilled_by="glm_gate",
            reason="fulfilled_by_takeover_evidence",
        )

        monkeypatch.setattr(reopener, "_get_pr_head_sha_for_gate", lambda pr_number: _HEAD_SHA)
        with patch("review_gate_manager.emit_governance_receipt"):
            reopened = reopener.reopen_obligation(
                state_dir, "d-rebook", operator_reason="OI-1726", write=True,
            )
        assert reopened["action"] == "reopened"

        record = json.loads(path.read_text(encoding="utf-8"))
        assert record["status"] == STATUS_PENDING

        # The runner now picks the reopened obligation back up.
        monkeypatch.setattr(runner, "_get_pr_head_sha_for_gate", lambda pr_number: _HEAD_SHA)
        outcome = runner.fulfill_obligation(state_dir, path, record)

        assert outcome["action"] == STATUS_FULFILLED
        final = json.loads(path.read_text(encoding="utf-8"))
        assert final["status"] == STATUS_FULFILLED
        assert final["result_path"] == str(real), "re-booked on the real evidence, not the vanished record"


class TestReopenObligation:
    def test_dry_run_never_writes(self, tmp_path, monkeypatch):
        state_dir = _make_state_dir(tmp_path)
        evidence_path = _write_evidence(state_dir, commit_sha=_STALE_SHA)
        path = _seed_takeover_fulfilled_obligation(state_dir, "d-dry-run", evidence_path)
        monkeypatch.setattr(reopener, "_get_pr_head_sha_for_gate", lambda pr_number: _HEAD_SHA)

        outcome = reopener.reopen_obligation(
            state_dir, "d-dry-run", operator_reason="test", write=False,
        )
        assert outcome["action"] == "would_reopen"
        record = json.loads(path.read_text(encoding="utf-8"))
        assert record["status"] == STATUS_FULFILLED, "a dry run must never mutate the record"

    def test_write_reopens_via_the_audited_api_and_emits_a_receipt(self, tmp_path, monkeypatch):
        """RED on unfixed main (measured 2026-08-30: this script did not
        exist): the live PR #1719 obligation stayed fulfilled forever with
        no code path to correct it — a worker's only option was a hand-edit,
        which is exactly what this tool exists to make unnecessary."""
        state_dir = _make_state_dir(tmp_path)
        evidence_path = _write_evidence(state_dir, commit_sha=_STALE_SHA)
        path = _seed_takeover_fulfilled_obligation(state_dir, "d-write-reopen", evidence_path)
        monkeypatch.setattr(reopener, "_get_pr_head_sha_for_gate", lambda pr_number: _HEAD_SHA)

        with patch("review_gate_manager.emit_governance_receipt") as mock_emit:
            outcome = reopener.reopen_obligation(
                state_dir, "d-write-reopen", operator_reason="OI-1571 PR #1719 correction", write=True,
            )

        assert outcome["action"] == "reopened"
        assert mock_emit.called, "the ledger event must be emitted before the mutation (ADR-005)"
        emit_kwargs = mock_emit.call_args.kwargs
        assert emit_kwargs["dispatch_id"] == "d-write-reopen"
        assert emit_kwargs["pr_head_sha"] == _HEAD_SHA
        assert emit_kwargs["evidence_commit_sha"] == _STALE_SHA

        record = json.loads(path.read_text(encoding="utf-8"))
        assert record["status"] == STATUS_PENDING
        assert record["attempts"] == 0
        assert record["resolved_at"] is None
        assert record["resolved_by_gate"] is None
        assert record["fulfilled_by"] is None
        assert _STALE_SHA[:12] in record["reason_detail"]
        assert _HEAD_SHA[:12] in record["reason_detail"]
        assert "OI-1571 PR #1719 correction" in record["reason_detail"]

    def test_write_refused_without_writing_when_evidence_still_matches(self, tmp_path, monkeypatch):
        state_dir = _make_state_dir(tmp_path)
        evidence_path = _write_evidence(state_dir, commit_sha=_HEAD_SHA)
        path = _seed_takeover_fulfilled_obligation(state_dir, "d-no-op", evidence_path)
        monkeypatch.setattr(reopener, "_get_pr_head_sha_for_gate", lambda pr_number: _HEAD_SHA)

        with pytest.raises(reopener.ReopenRefused):
            reopener.reopen_obligation(state_dir, "d-no-op", operator_reason="test", write=True)

        record = json.loads(path.read_text(encoding="utf-8"))
        assert record["status"] == STATUS_FULFILLED


class TestCli:
    def test_main_refused_exits_2(self, tmp_path, capsys):
        state_dir = _make_state_dir(tmp_path)
        rc = reopener.main(["--state-dir", str(state_dir), "--dispatch-id", "d-nonexistent"])
        assert rc == 2
        assert "REFUSED" in capsys.readouterr().err

    def test_main_dry_run_reports_would_reopen(self, tmp_path, monkeypatch, capsys):
        state_dir = _make_state_dir(tmp_path)
        evidence_path = _write_evidence(state_dir, commit_sha=_STALE_SHA)
        _seed_takeover_fulfilled_obligation(state_dir, "d-cli-dry-run", evidence_path)
        monkeypatch.setattr(reopener, "_get_pr_head_sha_for_gate", lambda pr_number: _HEAD_SHA)

        rc = reopener.main(["--state-dir", str(state_dir), "--dispatch-id", "d-cli-dry-run"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "action=would_reopen" in out
        assert "DRY RUN" in out
