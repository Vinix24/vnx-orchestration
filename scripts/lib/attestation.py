"""VNX signing authority and attestation manifest (D1).

Emits a signed attestation for a governed action: lineage (Dispatch-ID,
deliverable_id, track_id, plan-gate-pass ref) hash-chained via
ndjson_hash_chain, signed with a caller-supplied SSH key.

The signing key is ALWAYS passed in — never provisioned here.  Keychain
provisioning is an operator step; see docs/governance/KEY_PROVISIONING.md.

SSH signing namespace: "vnx-attestation"
Attestation ledger: .vnx-attest/governed.ndjson  (governed)
                    .vnx-attest/adhoc.ndjson      (ad-hoc)

References:
  - ndjson_hash_chain.py: hash-chain primitive
  - trace_token_validator.py: Dispatch-ID format
  - docs/governance/2026-07-04-governance-attribution-enforce-PLAN.md
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from ndjson_hash_chain import append_chained_entry, compute_entry_hash

# SSH signing namespace — must match verify calls.
_SSH_NAMESPACE = "vnx-attestation"

SCHEMA_VERSION = "1"

ATTESTATION_GOVERNED = "governed"
ATTESTATION_ADHOC = "ad-hoc"


# ---------------------------------------------------------------------------
# Manifest helpers
# ---------------------------------------------------------------------------

def manifest_canonical_bytes(manifest: dict) -> bytes:
    """Return canonical UTF-8 bytes for signing/verification.

    Excludes 'signature' and 'prev_hash': prev_hash is a chain-pointer, not
    semantic content; signature is what this function feeds INTO the sign step.
    """
    excluded = {"signature", "prev_hash"}
    filtered = {k: v for k, v in manifest.items() if k not in excluded}
    return json.dumps(filtered, sort_keys=True, separators=(",", ":")).encode("utf-8")


def manifest_content_hash(manifest: dict) -> str:
    """SHA-256 of canonical bytes (excludes signature and prev_hash)."""
    return hashlib.sha256(manifest_canonical_bytes(manifest)).hexdigest()


# ---------------------------------------------------------------------------
# Manifest builders
# ---------------------------------------------------------------------------

def build_governed_manifest(
    *,
    dispatch_id: str,
    deliverable_id: str,
    track_id: str,
    plan_gate_ref: str,
    signer_identity: str,
    timestamp: str,
) -> dict:
    """Build a governed attestation manifest dict (without chain/signature fields).

    Args:
        dispatch_id: Dispatch-ID of the governed build.
        deliverable_id: Deliverable being attested (e.g. "D1").
        track_id: Track/objective ID (e.g. "governance-attribution-enforce").
        plan_gate_ref: Reference to the plan-gate pass (e.g. PASS commit or receipt hash).
        signer_identity: The signer identity string matching an allowed_signers entry.
        timestamp: ISO-8601 UTC timestamp (caller supplies — no Date.now in scripts).
    """
    return {
        "schema_version": SCHEMA_VERSION,
        "attestation_type": ATTESTATION_GOVERNED,
        "dispatch_id": dispatch_id,
        "deliverable_id": deliverable_id,
        "track_id": track_id,
        "plan_gate_ref": plan_gate_ref,
        "signer_identity": signer_identity,
        "timestamp": timestamp,
    }


def build_adhoc_manifest(
    *,
    dispatch_id: str,
    signer_identity: str,
    timestamp: str,
    note: str = "",
) -> dict:
    """Build an ad-hoc low-attestation manifest.

    Ad-hoc actions NEVER receive a governed signature.  The attestation_type
    field is "ad-hoc" so they are always distinguishable from governed actions.
    Lineage fields (deliverable_id, track_id, plan_gate_ref) are absent.

    Args:
        dispatch_id: Dispatch-ID of the ad-hoc action (or "ad-hoc:<slug>").
        signer_identity: The signer identity string.
        timestamp: ISO-8601 UTC timestamp.
        note: Optional free-text note describing the ad-hoc action.
    """
    manifest: dict = {
        "schema_version": SCHEMA_VERSION,
        "attestation_type": ATTESTATION_ADHOC,
        "dispatch_id": dispatch_id,
        "signer_identity": signer_identity,
        "timestamp": timestamp,
    }
    if note:
        manifest["note"] = note
    return manifest


# ---------------------------------------------------------------------------
# SSH sign / verify
# ---------------------------------------------------------------------------

def sign_manifest(manifest: dict, key_path: "str | Path") -> dict:
    """Sign a manifest with a caller-supplied SSH key.

    Returns the manifest dict with a "signature" field added (base64-encoded
    raw SSH .sig bytes).  The canonical bytes that are signed exclude the
    "signature" and "prev_hash" fields so the signature is stable across
    chain positions.

    Args:
        manifest: Manifest dict WITHOUT a signature field.
        key_path: Path to the SSH private key file (ed25519 or equivalent).
                  The caller is responsible for key custody; this function
                  never provisions, stores, or looks up keys.

    Raises:
        RuntimeError: If ssh-keygen signing fails.
    """
    key_path = Path(key_path)
    canonical = manifest_canonical_bytes(manifest)

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        data_file = tmp / "manifest.bin"
        data_file.write_bytes(canonical)

        result = subprocess.run(
            ["ssh-keygen", "-Y", "sign", "-f", str(key_path),
             "-n", _SSH_NAMESPACE, str(data_file)],
            capture_output=True,
        )
        if result.returncode != 0:
            stderr = result.stderr.decode("utf-8", errors="replace").strip()
            raise RuntimeError(f"ssh-keygen signing failed: {stderr}")

        sig_file = data_file.with_suffix(".bin.sig")
        if not sig_file.exists():
            raise RuntimeError("ssh-keygen did not produce a .sig file")

        sig_bytes = sig_file.read_bytes()

    signed = dict(manifest)
    signed["signature"] = base64.b64encode(sig_bytes).decode("ascii")
    return signed


def verify_attestation(manifest: dict, allowed_signers: "str | Path") -> bool:
    """Verify a detached SSH signature embedded in a manifest dict.

    Args:
        manifest: Manifest dict containing a "signature" field (base64 SSH .sig)
                  and a "signer_identity" field matching an allowed_signers entry.
        allowed_signers: Path to an SSH allowed_signers file.
                         Format: "<identity> <keytype> <base64-pubkey>"

    Returns:
        True if the signature is valid for the manifest's canonical bytes
        and the signer is in allowed_signers.  False on any verification
        failure (bad sig, unknown signer, missing fields).
    """
    sig_b64 = manifest.get("signature")
    identity = manifest.get("signer_identity")
    if not sig_b64 or not identity:
        return False

    try:
        sig_bytes = base64.b64decode(sig_b64)
    except Exception:
        return False

    canonical = manifest_canonical_bytes(manifest)
    allowed_signers = Path(allowed_signers)

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        sig_file = tmp / "manifest.sig"
        sig_file.write_bytes(sig_bytes)

        result = subprocess.run(
            ["ssh-keygen", "-Y", "verify",
             "-f", str(allowed_signers),
             "-I", identity,
             "-n", _SSH_NAMESPACE,
             "-s", str(sig_file)],
            input=canonical,
            capture_output=True,
        )
        return result.returncode == 0


# ---------------------------------------------------------------------------
# High-level emit
# ---------------------------------------------------------------------------

@dataclass
class AttestationRecord:
    """Fully signed and hash-chained attestation record."""
    manifest: dict
    chain_hash: str
    ledger_path: Path


def emit_governed_attestation(
    *,
    dispatch_id: str,
    deliverable_id: str,
    track_id: str,
    plan_gate_ref: str,
    signer_identity: str,
    timestamp: str,
    key_path: "str | Path",
    repo_root: "str | Path | None" = None,
) -> AttestationRecord:
    """Build, sign, hash-chain, and persist a governed attestation.

    Writes to <repo_root>/.vnx-attest/governed.ndjson.

    Args:
        dispatch_id: Dispatch-ID of the governed build.
        deliverable_id: Deliverable being attested.
        track_id: Track/objective ID.
        plan_gate_ref: Plan-gate pass reference.
        signer_identity: Signer identity (matches allowed_signers).
        timestamp: ISO-8601 UTC timestamp.
        key_path: SSH private key path (caller-supplied; never provisioned here).
        repo_root: Repository root.  Defaults to cwd.

    Returns:
        AttestationRecord with the signed manifest, its chain hash, and ledger path.
    """
    repo_root = Path(repo_root) if repo_root else Path.cwd()
    ledger_path = repo_root / ".vnx-attest" / "governed.ndjson"

    manifest = build_governed_manifest(
        dispatch_id=dispatch_id,
        deliverable_id=deliverable_id,
        track_id=track_id,
        plan_gate_ref=plan_gate_ref,
        signer_identity=signer_identity,
        timestamp=timestamp,
    )
    signed = sign_manifest(manifest, key_path)
    chain_hash = append_chained_entry(ledger_path, signed)

    return AttestationRecord(
        manifest=signed,
        chain_hash=chain_hash,
        ledger_path=ledger_path,
    )


def emit_adhoc_attestation(
    *,
    dispatch_id: str,
    signer_identity: str,
    timestamp: str,
    note: str = "",
    repo_root: "str | Path | None" = None,
) -> AttestationRecord:
    """Build and hash-chain an ad-hoc low-attestation manifest.

    Ad-hoc attestations are NOT signed (no key required).  They are
    distinguishable from governed attestations by attestation_type="ad-hoc"
    and the absence of lineage fields and a signature.

    Args:
        dispatch_id: Dispatch-ID or ad-hoc slug.
        signer_identity: Identity string for audit trail labelling.
        timestamp: ISO-8601 UTC timestamp.
        note: Optional description of the ad-hoc action.
        repo_root: Repository root.  Defaults to cwd.

    Returns:
        AttestationRecord (no signature field; chain_hash reflects the entry).
    """
    repo_root = Path(repo_root) if repo_root else Path.cwd()
    ledger_path = repo_root / ".vnx-attest" / "adhoc.ndjson"

    manifest = build_adhoc_manifest(
        dispatch_id=dispatch_id,
        signer_identity=signer_identity,
        timestamp=timestamp,
        note=note,
    )
    chain_hash = append_chained_entry(ledger_path, manifest)

    return AttestationRecord(
        manifest=manifest,
        chain_hash=chain_hash,
        ledger_path=ledger_path,
    )


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def is_governed(manifest: dict) -> bool:
    """Return True if the manifest is a governed attestation."""
    return manifest.get("attestation_type") == ATTESTATION_GOVERNED


def is_adhoc(manifest: dict) -> bool:
    """Return True if the manifest is an ad-hoc attestation."""
    return manifest.get("attestation_type") == ATTESTATION_ADHOC
