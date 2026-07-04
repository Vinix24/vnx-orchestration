"""Tests for scripts/lib/attestation.py (D1 — signing authority + manifest).

Covers:
  - Governed manifest build and field completeness
  - Ad-hoc manifest build and distinguishability from governed
  - Sign + verify round-trip using an ephemeral ed25519 test key
  - Hash-chain linking (prev_hash integrity across two entries)
  - Bad/corrupted signature fails verify_attestation
  - Missing signature/identity fields fail verify gracefully
  - emit_governed_attestation writes to .vnx-attest/governed.ndjson
  - emit_adhoc_attestation writes to .vnx-attest/adhoc.ndjson (no signature)
  - canonical bytes are stable (excludes signature + prev_hash)
"""

import base64
import json
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "lib"))

from attestation import (
    ATTESTATION_ADHOC,
    ATTESTATION_GOVERNED,
    AttestationRecord,
    build_adhoc_manifest,
    build_governed_manifest,
    emit_adhoc_attestation,
    emit_governed_attestation,
    is_adhoc,
    is_governed,
    manifest_canonical_bytes,
    manifest_content_hash,
    sign_manifest,
    verify_attestation,
)
from ndjson_hash_chain import compute_entry_hash, verify_chain


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def ephemeral_key_dir():
    """Generate a non-interactive ed25519 test key in a temp dir (no keychain)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        key_path = Path(tmpdir) / "testkey"
        subprocess.run(
            ["ssh-keygen", "-t", "ed25519", "-f", str(key_path), "-N", ""],
            check=True, capture_output=True,
        )
        pub = key_path.with_suffix(".pub").read_text().strip()
        # allowed_signers format: <identity> <keytype> <base64-pubkey>
        identity = "vnx-test@local"
        allowed_signers = Path(tmpdir) / "allowed_signers"
        allowed_signers.write_text(f"{identity} {pub}\n")
        yield {
            "key_path": key_path,
            "identity": identity,
            "allowed_signers": allowed_signers,
            "tmpdir": Path(tmpdir),
        }


@pytest.fixture
def governed_manifest():
    return build_governed_manifest(
        dispatch_id="D-test-d1",
        deliverable_id="D1",
        track_id="governance-attribution-enforce",
        plan_gate_ref="gate-pass-ref-abc123",
        signer_identity="vnx-test@local",
        timestamp="2026-07-04T12:00:00Z",
    )


@pytest.fixture
def adhoc_manifest():
    return build_adhoc_manifest(
        dispatch_id="ad-hoc:explore-branch",
        signer_identity="vnx-test@local",
        timestamp="2026-07-04T12:00:00Z",
        note="exploratory run, not for merge",
    )


# ---------------------------------------------------------------------------
# Manifest builders
# ---------------------------------------------------------------------------

class TestBuildGovernedManifest:
    def test_required_fields_present(self, governed_manifest):
        m = governed_manifest
        assert m["schema_version"] == "1"
        assert m["attestation_type"] == ATTESTATION_GOVERNED
        assert m["dispatch_id"] == "D-test-d1"
        assert m["deliverable_id"] == "D1"
        assert m["track_id"] == "governance-attribution-enforce"
        assert m["plan_gate_ref"] == "gate-pass-ref-abc123"
        assert m["signer_identity"] == "vnx-test@local"
        assert m["timestamp"] == "2026-07-04T12:00:00Z"

    def test_no_signature_in_unisigned(self, governed_manifest):
        assert "signature" not in governed_manifest

    def test_is_governed(self, governed_manifest):
        assert is_governed(governed_manifest)
        assert not is_adhoc(governed_manifest)


class TestBuildAdhocManifest:
    def test_required_fields(self, adhoc_manifest):
        m = adhoc_manifest
        assert m["schema_version"] == "1"
        assert m["attestation_type"] == ATTESTATION_ADHOC
        assert m["dispatch_id"] == "ad-hoc:explore-branch"
        assert m["signer_identity"] == "vnx-test@local"
        assert m["note"] == "exploratory run, not for merge"

    def test_no_lineage_fields(self, adhoc_manifest):
        m = adhoc_manifest
        assert "deliverable_id" not in m
        assert "track_id" not in m
        assert "plan_gate_ref" not in m

    def test_no_signature(self, adhoc_manifest):
        assert "signature" not in adhoc_manifest

    def test_is_adhoc(self, adhoc_manifest):
        assert is_adhoc(adhoc_manifest)
        assert not is_governed(adhoc_manifest)

    def test_note_omitted_when_empty(self):
        m = build_adhoc_manifest(
            dispatch_id="ad-hoc:x",
            signer_identity="id@local",
            timestamp="2026-07-04T00:00:00Z",
        )
        assert "note" not in m


# ---------------------------------------------------------------------------
# Canonical bytes stability
# ---------------------------------------------------------------------------

class TestCanonicalBytes:
    def test_excludes_signature_field(self, governed_manifest):
        signed = dict(governed_manifest)
        signed["signature"] = "fakesig=="
        assert manifest_canonical_bytes(governed_manifest) == manifest_canonical_bytes(signed)

    def test_excludes_prev_hash_field(self, governed_manifest):
        chained = dict(governed_manifest)
        chained["prev_hash"] = "0" * 64
        assert manifest_canonical_bytes(governed_manifest) == manifest_canonical_bytes(chained)

    def test_content_hash_stable(self, governed_manifest):
        h1 = manifest_content_hash(governed_manifest)
        h2 = manifest_content_hash(governed_manifest)
        assert h1 == h2
        assert len(h1) == 64

    def test_content_hash_changes_on_field_change(self, governed_manifest):
        modified = dict(governed_manifest)
        modified["deliverable_id"] = "D2"
        assert manifest_content_hash(governed_manifest) != manifest_content_hash(modified)


# ---------------------------------------------------------------------------
# Sign + verify round-trip
# ---------------------------------------------------------------------------

class TestSignAndVerify:
    def test_sign_adds_signature_field(self, governed_manifest, ephemeral_key_dir):
        signed = sign_manifest(governed_manifest, ephemeral_key_dir["key_path"])
        assert "signature" in signed
        # Should be valid base64
        decoded = base64.b64decode(signed["signature"])
        assert len(decoded) > 0

    def test_sign_preserves_other_fields(self, governed_manifest, ephemeral_key_dir):
        signed = sign_manifest(governed_manifest, ephemeral_key_dir["key_path"])
        for key in governed_manifest:
            assert signed[key] == governed_manifest[key]

    def test_verify_round_trip_passes(self, governed_manifest, ephemeral_key_dir):
        signed = sign_manifest(governed_manifest, ephemeral_key_dir["key_path"])
        result = verify_attestation(signed, ephemeral_key_dir["allowed_signers"])
        assert result is True

    def test_verify_bad_signature_fails(self, governed_manifest, ephemeral_key_dir):
        signed = sign_manifest(governed_manifest, ephemeral_key_dir["key_path"])
        # Corrupt the signature
        corrupted = dict(signed)
        corrupted["signature"] = base64.b64encode(b"not-a-real-sig").decode("ascii")
        result = verify_attestation(corrupted, ephemeral_key_dir["allowed_signers"])
        assert result is False

    def test_verify_tampered_manifest_fails(self, governed_manifest, ephemeral_key_dir):
        signed = sign_manifest(governed_manifest, ephemeral_key_dir["key_path"])
        # Tamper with a field after signing — canonical bytes will differ
        tampered = dict(signed)
        tampered["deliverable_id"] = "D99"
        result = verify_attestation(tampered, ephemeral_key_dir["allowed_signers"])
        assert result is False

    def test_verify_missing_signature_field_returns_false(self, governed_manifest, ephemeral_key_dir):
        result = verify_attestation(governed_manifest, ephemeral_key_dir["allowed_signers"])
        assert result is False

    def test_verify_missing_identity_returns_false(self, governed_manifest, ephemeral_key_dir):
        signed = sign_manifest(governed_manifest, ephemeral_key_dir["key_path"])
        no_identity = dict(signed)
        del no_identity["signer_identity"]
        result = verify_attestation(no_identity, ephemeral_key_dir["allowed_signers"])
        assert result is False

    def test_verify_wrong_allowed_signers(self, governed_manifest, ephemeral_key_dir):
        """A signature valid for one key fails against empty/wrong allowed_signers."""
        signed = sign_manifest(governed_manifest, ephemeral_key_dir["key_path"])
        with tempfile.NamedTemporaryFile(mode="w", suffix=".as", delete=False) as f:
            # Empty allowed_signers — no keys trusted
            empty_as = Path(f.name)
        try:
            result = verify_attestation(signed, empty_as)
            assert result is False
        finally:
            empty_as.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Hash-chain linking
# ---------------------------------------------------------------------------

class TestHashChainLinking:
    def test_two_governed_entries_chain_correctly(self, ephemeral_key_dir):
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)

            rec1 = emit_governed_attestation(
                dispatch_id="D-chain-test-1",
                deliverable_id="D1",
                track_id="some-track",
                plan_gate_ref="ref-a",
                signer_identity="vnx-test@local",
                timestamp="2026-07-04T10:00:00Z",
                key_path=ephemeral_key_dir["key_path"],
                repo_root=repo_root,
            )
            rec2 = emit_governed_attestation(
                dispatch_id="D-chain-test-2",
                deliverable_id="D2",
                track_id="some-track",
                plan_gate_ref="ref-b",
                signer_identity="vnx-test@local",
                timestamp="2026-07-04T11:00:00Z",
                key_path=ephemeral_key_dir["key_path"],
                repo_root=repo_root,
            )

            ledger = rec1.ledger_path
            is_valid, violations, status = verify_chain(ledger)
            assert is_valid, f"Chain broken: {violations}"
            assert status == "verified"

            # The second entry's prev_hash = first entry's content hash
            lines = [json.loads(l) for l in ledger.read_text().splitlines() if l.strip()]
            assert len(lines) == 2
            expected_prev = compute_entry_hash(lines[0])
            assert lines[1]["prev_hash"] == expected_prev

    def test_chain_hash_in_record_matches_ledger(self, ephemeral_key_dir):
        with tempfile.TemporaryDirectory() as tmpdir:
            rec = emit_governed_attestation(
                dispatch_id="D-hash-check",
                deliverable_id="D1",
                track_id="t",
                plan_gate_ref="r",
                signer_identity="vnx-test@local",
                timestamp="2026-07-04T00:00:00Z",
                key_path=ephemeral_key_dir["key_path"],
                repo_root=Path(tmpdir),
            )
            ledger_entry = json.loads(rec.ledger_path.read_text().strip())
            assert compute_entry_hash(ledger_entry) == rec.chain_hash


# ---------------------------------------------------------------------------
# emit_governed_attestation / emit_adhoc_attestation
# ---------------------------------------------------------------------------

class TestEmitGovernedAttestation:
    def test_returns_attestation_record(self, ephemeral_key_dir):
        with tempfile.TemporaryDirectory() as tmpdir:
            rec = emit_governed_attestation(
                dispatch_id="D-emit-gov",
                deliverable_id="D1",
                track_id="t",
                plan_gate_ref="r",
                signer_identity="vnx-test@local",
                timestamp="2026-07-04T00:00:00Z",
                key_path=ephemeral_key_dir["key_path"],
                repo_root=Path(tmpdir),
            )
            assert isinstance(rec, AttestationRecord)
            assert rec.ledger_path.name == "governed.ndjson"

    def test_ledger_entry_verifiable(self, ephemeral_key_dir):
        with tempfile.TemporaryDirectory() as tmpdir:
            rec = emit_governed_attestation(
                dispatch_id="D-emit-verif",
                deliverable_id="D1",
                track_id="t",
                plan_gate_ref="r",
                signer_identity="vnx-test@local",
                timestamp="2026-07-04T00:00:00Z",
                key_path=ephemeral_key_dir["key_path"],
                repo_root=Path(tmpdir),
            )
            entry = json.loads(rec.ledger_path.read_text().strip())
            assert verify_attestation(entry, ephemeral_key_dir["allowed_signers"])

    def test_attestation_type_is_governed(self, ephemeral_key_dir):
        with tempfile.TemporaryDirectory() as tmpdir:
            rec = emit_governed_attestation(
                dispatch_id="D-type-check",
                deliverable_id="D1",
                track_id="t",
                plan_gate_ref="r",
                signer_identity="vnx-test@local",
                timestamp="2026-07-04T00:00:00Z",
                key_path=ephemeral_key_dir["key_path"],
                repo_root=Path(tmpdir),
            )
            assert rec.manifest["attestation_type"] == ATTESTATION_GOVERNED


class TestEmitAdhocAttestation:
    def test_returns_attestation_record(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            rec = emit_adhoc_attestation(
                dispatch_id="ad-hoc:explore",
                signer_identity="vnx-test@local",
                timestamp="2026-07-04T00:00:00Z",
                repo_root=Path(tmpdir),
            )
            assert isinstance(rec, AttestationRecord)
            assert rec.ledger_path.name == "adhoc.ndjson"

    def test_no_signature_in_adhoc(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            rec = emit_adhoc_attestation(
                dispatch_id="ad-hoc:explore",
                signer_identity="vnx-test@local",
                timestamp="2026-07-04T00:00:00Z",
                repo_root=Path(tmpdir),
            )
            assert "signature" not in rec.manifest

    def test_attestation_type_is_adhoc(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            rec = emit_adhoc_attestation(
                dispatch_id="ad-hoc:explore",
                signer_identity="vnx-test@local",
                timestamp="2026-07-04T00:00:00Z",
                repo_root=Path(tmpdir),
            )
            assert rec.manifest["attestation_type"] == ATTESTATION_ADHOC

    def test_adhoc_not_verifiable_as_governed(self, ephemeral_key_dir):
        """Ad-hoc manifests have no signature, so verify_attestation returns False."""
        with tempfile.TemporaryDirectory() as tmpdir:
            rec = emit_adhoc_attestation(
                dispatch_id="ad-hoc:explore",
                signer_identity="vnx-test@local",
                timestamp="2026-07-04T00:00:00Z",
                repo_root=Path(tmpdir),
            )
            result = verify_attestation(rec.manifest, ephemeral_key_dir["allowed_signers"])
            assert result is False

    def test_adhoc_is_distinguishable_from_governed(self, ephemeral_key_dir):
        with tempfile.TemporaryDirectory() as tmpdir:
            adhoc = emit_adhoc_attestation(
                dispatch_id="ad-hoc:x",
                signer_identity="vnx-test@local",
                timestamp="2026-07-04T00:00:00Z",
                repo_root=Path(tmpdir),
            )
            gov = emit_governed_attestation(
                dispatch_id="D-gov-x",
                deliverable_id="D1",
                track_id="t",
                plan_gate_ref="r",
                signer_identity="vnx-test@local",
                timestamp="2026-07-04T00:00:00Z",
                key_path=ephemeral_key_dir["key_path"],
                repo_root=Path(tmpdir),
            )
            assert is_adhoc(adhoc.manifest)
            assert not is_governed(adhoc.manifest)
            assert is_governed(gov.manifest)
            assert not is_adhoc(gov.manifest)
