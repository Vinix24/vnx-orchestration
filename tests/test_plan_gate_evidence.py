"""Tests for plan-gate-pass evidence (ADR-030 merge-gate primitive)."""
from __future__ import annotations

import sys
from pathlib import Path

_LIB = Path(__file__).resolve().parent.parent / "scripts" / "lib"
if str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))

import plan_gate_evidence as pge  # noqa: E402


def _emit(tmp, track="t", project="vnx-dev", resolver="attest", ts="2026-07-11T00:00:00Z", **kw):
    return pge.emit_plan_gate_pass(
        repo_root=tmp, track_id=track, project_id=project, resolver=resolver, timestamp=ts, **kw
    )


class TestEmit:
    def test_emit_unsigned_creates_chained_ledger(self, tmp_path):
        rec = _emit(tmp_path, approval_id="op-1", reason="already done")
        assert rec is not None
        ledger = tmp_path / ".vnx-attest" / "plan-gates.ndjson"
        assert ledger.exists()
        assert rec["type"] == "plan_gate_pass"
        assert rec["track_id"] == "t"
        assert rec["approval_id"] == "op-1"

    def test_emit_is_best_effort_returns_none_on_failure(self, tmp_path, monkeypatch):
        # A ledger path that cannot be created (parent is a file) → None, no raise.
        bad = tmp_path / "afile"
        bad.write_text("x")
        rec = pge.emit_plan_gate_pass(
            repo_root=bad, track_id="t", project_id="vnx-dev",
            resolver="run", timestamp="2026-07-11T00:00:00Z",
        )
        assert rec is None


class TestVerify:
    def test_absent_when_no_ledger(self, tmp_path):
        state, rec = pge.verify_plan_gate_pass(tmp_path, "t", "vnx-dev")
        assert state == pge.ABSENT and rec is None

    def test_present_unsigned_round_trip(self, tmp_path):
        _emit(tmp_path)
        state, rec = pge.verify_plan_gate_pass(tmp_path, "t", "vnx-dev")
        assert state == pge.PRESENT_UNSIGNED
        assert rec is not None and rec["track_id"] == "t"

    def test_absent_for_other_track(self, tmp_path):
        _emit(tmp_path, track="t")
        state, _ = pge.verify_plan_gate_pass(tmp_path, "other", "vnx-dev")
        assert state == pge.ABSENT

    def test_tenant_isolation(self, tmp_path):
        _emit(tmp_path, track="t", project="vnx-dev")
        state, _ = pge.verify_plan_gate_pass(tmp_path, "t", "other-project")
        assert state == pge.ABSENT

    def test_latest_pass_wins(self, tmp_path):
        _emit(tmp_path, ts="2026-07-11T00:00:00Z", reason="first")
        _emit(tmp_path, ts="2026-07-11T09:00:00Z", reason="second")
        state, rec = pge.verify_plan_gate_pass(tmp_path, "t", "vnx-dev")
        assert state == pge.PRESENT_UNSIGNED
        assert rec["reason"] == "second"

    def test_verified_when_signature_checks(self, tmp_path, monkeypatch):
        """With a key + a verifying signer, a record round-trips to VERIFIED."""
        import attestation
        monkeypatch.setattr(attestation, "sign_manifest",
                            lambda m, k: {**m, "signature": "sig"})
        monkeypatch.setattr(attestation, "verify_attestation", lambda m, s: True)
        _emit(tmp_path, signer_identity="vincent", key_path="/fake/key")
        state, rec = pge.verify_plan_gate_pass(tmp_path, "t", "vnx-dev", allowed_signers="/fake/signers")
        assert state == pge.VERIFIED
        assert rec["signer_identity"] == "vincent"

    def test_signed_but_signature_fails_is_present(self, tmp_path, monkeypatch):
        import attestation
        monkeypatch.setattr(attestation, "sign_manifest",
                            lambda m, k: {**m, "signature": "sig"})
        monkeypatch.setattr(attestation, "verify_attestation", lambda m, s: False)
        _emit(tmp_path, signer_identity="vincent", key_path="/fake/key")
        state, _ = pge.verify_plan_gate_pass(tmp_path, "t", "vnx-dev", allowed_signers="/fake/signers")
        assert state == pge.PRESENT_UNSIGNED
