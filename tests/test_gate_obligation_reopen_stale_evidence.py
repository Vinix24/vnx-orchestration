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
