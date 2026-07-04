"""D2: In-repo attestation record, content-keyed and diff-bound.

Each governed build commit writes a .vnx-attest/<content-key>.json record
alongside the code change.  The record is:

  - Content-keyed: keyed by SHA-256 of the merge-base→HEAD diff, so it
    survives squash-merges and rebases that produce the same code delta.
  - Diff-bound: the manifest embeds diff_hash, so a manifest for track A
    cannot be silently reused for track B's code.
  - Detached-signed: the manifest bytes are SSH-signed (D1's sign_manifest),
    not just incidentally covered by git commit -S.

The D3 server gate verifies: (a) a .vnx-attest/<content-key>.json exists for
the PR's content-key; (b) manifest.diff_hash matches the PR's diff; (c) the
signature is valid against allowed_signers.

References:
  - attestation.py: sign_manifest, verify_attestation, build_governed_manifest
  - content_key.py: compute_diff_hash
  - docs/governance/2026-07-04-governance-attribution-enforce-PLAN.md (D2)
"""
from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

from attestation import (
    build_governed_manifest,
    sign_manifest,
    verify_attestation,
)
from content_key import compute_diff_hash

ATTEST_DIR = ".vnx-attest"


@dataclass
class AttestRecord:
    """Written attestation record for a governed build commit."""
    content_key: str
    diff_hash: str
    record_path: Path
    manifest: dict
    git_signed: bool


def build_attest_manifest(
    *,
    dispatch_id: str,
    deliverable_id: str,
    track_id: str,
    plan_gate_ref: str,
    signer_identity: str,
    timestamp: str,
    diff_hash: str,
) -> dict:
    """Governed manifest extended with diff_hash for diff-binding.

    The diff_hash field is covered by the signature, so a manifest cannot
    be repurposed for a different diff without invalidating the signature.
    """
    manifest = build_governed_manifest(
        dispatch_id=dispatch_id,
        deliverable_id=deliverable_id,
        track_id=track_id,
        plan_gate_ref=plan_gate_ref,
        signer_identity=signer_identity,
        timestamp=timestamp,
    )
    manifest["diff_hash"] = diff_hash
    return manifest


def write_attest_record(
    *,
    dispatch_id: str,
    deliverable_id: str,
    track_id: str,
    plan_gate_ref: str,
    signer_identity: str,
    timestamp: str,
    key_path: "str | Path | None" = None,
    repo_root: "str | Path | None" = None,
    base_ref: str = "origin/main",
    head_ref: str = "HEAD",
) -> AttestRecord:
    """Compute content-key, build + sign manifest, write .vnx-attest/<key>.json.

    Does NOT stage or commit the file.  The caller (vnx attest write) stages
    the file and commits with -S if a signing key is available.

    Args:
        dispatch_id: Dispatch-ID of the governed build.
        deliverable_id: Deliverable being attested (e.g. "D2").
        track_id: Track/objective ID.
        plan_gate_ref: Plan-gate pass reference.
        signer_identity: Signer identity matching an allowed_signers entry.
        timestamp: ISO-8601 UTC timestamp (caller-supplied).
        key_path: SSH private key path for detached signature.  None = unsigned.
        repo_root: Repository root.  Defaults to cwd.
        base_ref: Base branch ref for merge-base (default: origin/main).
        head_ref: Branch tip (default: HEAD).

    Returns:
        AttestRecord with content_key, diff_hash, record_path, and manifest.

    Raises:
        RuntimeError: If git diff/merge-base operations fail.
    """
    repo_root = Path(repo_root) if repo_root else Path.cwd()

    diff_hash = compute_diff_hash(
        repo_root=repo_root, base_ref=base_ref, head_ref=head_ref
    )
    content_key = diff_hash  # the content-key IS the diff hash

    manifest = build_attest_manifest(
        dispatch_id=dispatch_id,
        deliverable_id=deliverable_id,
        track_id=track_id,
        plan_gate_ref=plan_gate_ref,
        signer_identity=signer_identity,
        timestamp=timestamp,
        diff_hash=diff_hash,
    )

    if key_path is not None:
        manifest = sign_manifest(manifest, key_path)

    attest_dir = repo_root / ATTEST_DIR
    attest_dir.mkdir(parents=True, exist_ok=True)
    record_path = attest_dir / f"{content_key}.json"
    record_path.write_text(
        json.dumps(manifest, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )

    return AttestRecord(
        content_key=content_key,
        diff_hash=diff_hash,
        record_path=record_path,
        manifest=manifest,
        git_signed=False,
    )


def verify_attest_record(
    *,
    allowed_signers: "str | Path",
    repo_root: "str | Path | None" = None,
    base_ref: str = "origin/main",
    head_ref: str = "HEAD",
) -> "tuple[bool, str]":
    """Verify the attest record for the current branch.

    Checks in order:
    1. .vnx-attest/<computed-content-key>.json exists.
    2. manifest.diff_hash matches the current branch diff (diff-binding).
    3. The detached signature is valid against allowed_signers.

    Args:
        allowed_signers: Path to SSH allowed_signers file.
        repo_root: Repository root.  Defaults to cwd.
        base_ref: Base branch ref (default: origin/main).
        head_ref: Branch tip (default: HEAD).

    Returns:
        (ok: bool, reason: str)
    """
    repo_root = Path(repo_root) if repo_root else Path.cwd()

    try:
        current_diff_hash = compute_diff_hash(
            repo_root=repo_root, base_ref=base_ref, head_ref=head_ref
        )
    except RuntimeError as e:
        return (False, f"diff computation failed: {e}")

    record_path = repo_root / ATTEST_DIR / f"{current_diff_hash}.json"
    if not record_path.exists():
        return (False, f"no attest record for content-key {current_diff_hash[:12]}…")

    try:
        manifest = json.loads(record_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        return (False, f"record unreadable: {e}")

    # Diff-binding: the manifest must have been signed for THIS diff
    record_diff_hash = manifest.get("diff_hash")
    if record_diff_hash != current_diff_hash:
        return (
            False,
            f"diff-binding mismatch: record.diff_hash={record_diff_hash!r} "
            f"!= current={current_diff_hash!r}",
        )

    # Detached signature check
    if not verify_attestation(manifest, allowed_signers):
        return (False, "signature verification failed")

    return (True, "ok")


def read_allowed_signers_from_base(
    repo_root: "str | Path",
    base_ref: str = "origin/main",
) -> "bytes | None":
    """Read allowed_signers bytes from the BASE branch, never the PR working tree.

    A PR-writable allowed_signers file cannot be trusted as a trust anchor — a
    rogue key committed to .vnx-attest/allowed_signers would verify itself.
    Reading from base_ref (e.g. origin/main) ensures only keys present BEFORE
    the PR can be used for verification.

    Tries .vnx-attest/allowed_signers then .vnx/allowed_signers at base_ref.
    Returns raw bytes of the file, or None if not found in either location.

    Note: CODEOWNERS protection of .vnx-attest/allowed_signers on the protected
    branch is the server-side enforcement complement (wired in D3/D5).
    """
    repo_root = Path(repo_root)
    for candidate in (
        f"{ATTEST_DIR}/allowed_signers",
        ".vnx/allowed_signers",
    ):
        result = subprocess.run(
            ["git", "show", f"{base_ref}:{candidate}"],
            cwd=str(repo_root), capture_output=True,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout
    return None
