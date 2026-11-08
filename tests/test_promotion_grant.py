"""Tests for the promotion grant verifier (ADR-056 D6, zelfst PR-5a)."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_LIB = Path(__file__).resolve().parent.parent / "scripts" / "lib"
if str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))

import attestation  # noqa: E402
import plan_gate_enforcement as pge_enf  # noqa: E402
import plan_gate_evidence as evidence  # noqa: E402


def _verify(tmp, track="t", project="vnx-dev", signers="/fake/signers"):
    assert getattr(pge_enf, "verify_promotion_grant", None) is not None
    return pge_enf.verify_promotion_grant(track, project, tmp, signers)


def _emit(tmp, project="vnx-dev", signed=False, ts="2026-11-08T00:00:00Z"):
    kw = {"signer_identity": "vincent", "key_path": "/fake/key"} if signed else {}
    return evidence.emit_plan_gate_pass(
        repo_root=tmp, track_id="t", project_id=project, resolver="attest", timestamp=ts, **kw
    )


@pytest.fixture
def fake_signing(monkeypatch):
    """Seam from tests/test_plan_gate_evidence.py: patch attestation, never a real key."""
    monkeypatch.setattr(attestation, "sign_manifest", lambda m, k: {**m, "signature": "sig"})

    def _set(valid: bool):
        monkeypatch.setattr(attestation, "verify_attestation", lambda m, s: valid)

    return _set


class TestVerdicts:
    def test_unsigned_is_not_verified(self, tmp_path, fake_signing):
        # The old verifier returns PRESENT_UNSIGNED for an invalid signature.
        fake_signing(False)
        _emit(tmp_path, signed=True)
        assert _verify(tmp_path) is pge_enf.PromotionGrant.SIGNATURE_INVALID
        assert _verify(tmp_path) is not pge_enf.PromotionGrant.VERIFIED

    def test_valid_signature_is_verified(self, tmp_path, fake_signing):
        fake_signing(True)
        _emit(tmp_path, signed=True)
        assert _verify(tmp_path) is pge_enf.PromotionGrant.VERIFIED

    def test_unsigned_record_is_present_unsigned(self, tmp_path):
        _emit(tmp_path)
        assert _verify(tmp_path) is pge_enf.PromotionGrant.PRESENT_UNSIGNED

    def test_signed_record_without_signers_is_not_verified(self, tmp_path, fake_signing):
        fake_signing(True)
        _emit(tmp_path, signed=True)
        assert _verify(tmp_path, signers=None) is pge_enf.PromotionGrant.PRESENT_UNSIGNED

    def test_no_ledger_is_absent(self, tmp_path):
        assert _verify(tmp_path) is pge_enf.PromotionGrant.ABSENT

    def test_other_track_is_absent(self, tmp_path):
        _emit(tmp_path)
        assert _verify(tmp_path, track="other") is pge_enf.PromotionGrant.ABSENT


class TestProjectId:
    def test_other_project_is_absent(self, tmp_path, fake_signing):
        fake_signing(True)
        _emit(tmp_path, signed=True)
        assert _verify(tmp_path, project="other") is pge_enf.PromotionGrant.ABSENT

    def test_record_without_project_is_absent_even_if_signed_valid(self, tmp_path, fake_signing):
        fake_signing(True)
        _emit(tmp_path, project=None, signed=True)
        # The old verifier accepts a None project and returns VERIFIED here.
        assert evidence.verify_plan_gate_pass(tmp_path, "t", "vnx-dev", "/fake/signers")[0] == evidence.VERIFIED
        assert _verify(tmp_path) is pge_enf.PromotionGrant.ABSENT

    @pytest.mark.parametrize("project", [None, "", 7, ["vnx-dev"]])
    def test_bad_project_argument_is_absent(self, tmp_path, fake_signing, project):
        fake_signing(True)
        _emit(tmp_path, signed=True)
        assert _verify(tmp_path, project=project) is pge_enf.PromotionGrant.ABSENT

    @pytest.mark.parametrize("track", [None, "", 7])
    def test_bad_track_argument_is_absent(self, tmp_path, track):
        _emit(tmp_path)
        assert _verify(tmp_path, track=track) is pge_enf.PromotionGrant.ABSENT


class TestFailClosed:
    def test_unreadable_ledger_is_absent(self, tmp_path):
        ledger = tmp_path / ".vnx-attest" / "plan-gates.ndjson"
        ledger.parent.mkdir(parents=True)
        ledger.write_text("not json at all\n")
        assert _verify(tmp_path) is pge_enf.PromotionGrant.ABSENT

    def test_exception_in_verifier_is_absent(self, tmp_path, monkeypatch):
        def boom(*a, **k):
            raise RuntimeError("boom")

        monkeypatch.setattr(evidence, "verify_plan_gate_pass", boom)
        assert _verify(tmp_path) is pge_enf.PromotionGrant.ABSENT

    def test_verifier_exception_is_logged_as_warning(self, tmp_path, monkeypatch, caplog):
        def boom(*a, **k):
            raise RuntimeError("ledger exploded")

        monkeypatch.setattr(evidence, "verify_plan_gate_pass", boom)
        with caplog.at_level("WARNING", logger=pge_enf.__name__):
            assert _verify(tmp_path) is pge_enf.PromotionGrant.ABSENT
        assert any(
            r.levelname == "WARNING" and "RuntimeError" in r.getMessage() and "ledger exploded" in r.getMessage()
            for r in caplog.records
        )

    @pytest.mark.parametrize("state", [None, "", "verified ", "VERIFIED", "bogus"])
    def test_unknown_state_is_absent(self, tmp_path, monkeypatch, state):
        rec = {"project_id": "vnx-dev", "signature": "sig"}
        monkeypatch.setattr(evidence, "verify_plan_gate_pass", lambda *a, **k: (state, rec))
        assert _verify(tmp_path) is pge_enf.PromotionGrant.ABSENT

    @pytest.mark.parametrize("record", [None, "x", [], {}])
    def test_malformed_record_is_absent(self, tmp_path, monkeypatch, record):
        monkeypatch.setattr(evidence, "verify_plan_gate_pass", lambda *a, **k: (evidence.VERIFIED, record))
        assert _verify(tmp_path) is pge_enf.PromotionGrant.ABSENT

    def test_verified_state_with_missing_project_key_is_absent(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            evidence, "verify_plan_gate_pass", lambda *a, **k: (evidence.VERIFIED, {"signature": "s"})
        )
        assert _verify(tmp_path) is pge_enf.PromotionGrant.ABSENT
