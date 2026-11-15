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


# --- OI-2849 (preceding part): extra_fields, verify_with, expect_latest_hash under the ledger lock ---
import shutil  # noqa: E402
import subprocess  # noqa: E402

import ndjson_hash_chain  # noqa: E402
import pytest  # noqa: E402

_ID = "operator@example.invalid"


def _ledger(tmp):
    return tmp / ".vnx-attest" / "plan-gates.ndjson"


def _latest_hash(tmp):
    return list(ndjson_hash_chain.walk_chain(_ledger(tmp)))[-1][2]


def _key(tmp, name):
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(tmp / name)], check=True)
    signers = tmp / f"{name}_signers"
    signers.write_text(f'{_ID} namespaces="vnx-attestation" {(tmp / (name + ".pub")).read_text()}')
    return tmp / name, signers


class TestSignHelpers:
    def test_extra_fields_copied_reserved_ignored(self, tmp_path):
        rec = _emit(tmp_path, extra_fields={"signs_record_hash": "h", "type": "x", "prev_hash": "p",
                                            "signature": "s", "signer_identity": "i"})
        assert rec["signs_record_hash"] == "h"
        assert rec["type"] == "plan_gate_pass"
        assert "signature" not in rec and "signer_identity" not in rec
        assert list(ndjson_hash_chain.walk_chain(_ledger(tmp_path)))[0][1]["prev_hash"] != "p"
        assert len(list(ndjson_hash_chain.walk_chain(_ledger(tmp_path)))) == 1

    @pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="ssh-keygen not installed")
    def test_signed_record_failing_verify_with_is_not_appended(self, tmp_path):
        key, signers = _key(tmp_path, "k")
        _, other_signers = _key(tmp_path, "other")
        assert _emit(tmp_path, signer_identity=_ID, key_path=str(key), verify_with=other_signers) is None
        assert not _ledger(tmp_path).exists()
        assert _emit(tmp_path, signer_identity=_ID, key_path=str(key), verify_with=signers) is not None
        assert len(_ledger(tmp_path).read_text().splitlines()) == 1

    def test_expect_latest_hash_matching_appends(self, tmp_path):
        _emit(tmp_path)
        rec = _emit(tmp_path, ts="2026-07-11T01:00:00Z", expect_latest_hash=_latest_hash(tmp_path))
        assert rec is not None
        assert len(list(ndjson_hash_chain.walk_chain(_ledger(tmp_path)))) == 2

    def test_expect_latest_hash_other_hash_raises_ledger_untouched(self, tmp_path):
        _emit(tmp_path)
        before = _ledger(tmp_path).read_bytes()
        with pytest.raises(pge.LatestPassChanged):
            _emit(tmp_path, expect_latest_hash="0" * 64)
        assert _ledger(tmp_path).read_bytes() == before

    def test_expect_latest_hash_without_any_pass_raises(self, tmp_path):
        with pytest.raises(pge.LatestPassChanged):
            _emit(tmp_path, expect_latest_hash="0" * 64)
        assert not _ledger(tmp_path).exists() or _ledger(tmp_path).read_bytes() == b""

    def test_race_newer_unsigned_pass_refuses_the_signature(self, tmp_path):
        _emit(tmp_path, ts="2026-07-11T00:00:00Z")
        hash_a = _latest_hash(tmp_path)  # the operator read pass A
        _emit(tmp_path, ts="2026-07-11T02:00:00Z")  # pass B lands before the signed append
        after_b = _ledger(tmp_path).read_bytes()
        with pytest.raises(pge.LatestPassChanged):
            _emit(tmp_path, ts="2026-07-11T03:00:00Z", expect_latest_hash=hash_a)
        assert _ledger(tmp_path).read_bytes() == after_b

    def test_default_path_appends_chained_entry_walk_chain_verifies(self, tmp_path):
        _emit(tmp_path)
        _emit(tmp_path, ts="2026-07-11T01:00:00Z")
        entries = list(ndjson_hash_chain.walk_chain(_ledger(tmp_path)))
        assert len(entries) == 2
        assert entries[1][1]["prev_hash"] == entries[0][2]

# OI-2849 r2: extra_fields cannot change the track or project of a pass
_SIGN = [False, pytest.param(True, marks=pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="ssh-keygen not installed"))]


@pytest.mark.parametrize("signed", _SIGN)
@pytest.mark.parametrize("field,other", [("track_id", "B"), ("project_id", "other-project")])
class TestIdentityFieldsReserved:
    def _kw(self, tmp_path, signed):
        if not signed:
            return {}
        key, _ = _key(tmp_path, "k")
        return {"signer_identity": _ID, "key_path": str(key)}

    def test_other_identity_refused_nothing_appended(self, tmp_path, field, other, signed):
        kw = self._kw(tmp_path, signed)
        _emit(tmp_path, track="t")
        _emit(tmp_path, track="B", ts="2026-07-11T00:30:00Z")  # B's latest hash differs from t's
        hash_t = list(ndjson_hash_chain.walk_chain(_ledger(tmp_path)))[0][2]
        before = _ledger(tmp_path).read_bytes()
        assert _emit(tmp_path, track="t", expect_latest_hash=hash_t, extra_fields={field: other}, **kw) is None
        assert _ledger(tmp_path).read_bytes() == before

    def test_same_identity_value_appends(self, tmp_path, field, other, signed):
        kw = self._kw(tmp_path, signed)
        _emit(tmp_path)
        same = {"track_id": "t", "project_id": "vnx-dev"}[field]
        rec = _emit(tmp_path, ts="2026-07-11T01:00:00Z", expect_latest_hash=_latest_hash(tmp_path),
                    extra_fields={field: same}, **kw)
        assert rec is not None and rec[field] == same
        assert len(_ledger(tmp_path).read_text().splitlines()) == 2
