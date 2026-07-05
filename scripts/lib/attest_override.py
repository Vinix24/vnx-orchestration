"""D4: Signed, budgeted, audited gate override.

An operator can bypass the server-gate for a specific PR/diff WITH a signed
override manifest — reason (required, non-empty), content-key (diff-bound),
operator signature (reusing D1 sign_manifest), and a budget decrement.

This is NOT a blanket skip.  An override is itself a signed attestation of
type "override".  The ISO/ISAE principle: zero UNRECORDED deviations, but a
RECORDED+SIGNED override is allowed.

Override budget: N overrides per rolling 30-day window, configurable via
VNX_ATTEST_OVERRIDE_BUDGET (default: 5).  The count is derived from the
append-only NDJSON trail, never a mutable counter.

Override record:  .vnx-attest/override-<content-key>.json
Override trail:   .vnx-attest/override-trail.ndjson  (hash-chained, append-only)

Security invariants:
  - The override is diff-bound: content_key ties it to exactly one diff.
  - Signer-pinned: verified against base-branch allowed_signers, not PR-tree.
  - A PR cannot forge its own override: the signer must be in allowed_signers
    at the base branch before the PR was opened.
  - Budget is append-only: the trail cannot be truncated to recover budget
    without operator-visible forensic evidence.

References:
  - attestation.py: sign_manifest, verify_attestation, SCHEMA_VERSION
  - attest_record.py: ATTEST_DIR
  - content_key.py: compute_diff_hash
  - ndjson_hash_chain.py: append_chained_entry
  - docs/governance/2026-07-04-governance-attribution-enforce-PLAN.md (D4)
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from attestation import (
    SCHEMA_VERSION,
    sign_manifest,
    verify_attestation,
)
from attest_record import ATTEST_DIR
from content_key import compute_diff_hash
from ndjson_hash_chain import append_chained_entry, verify_chain, walk_chain

ATTESTATION_OVERRIDE = "override"

OVERRIDE_TRAIL_FILE = "override-trail.ndjson"
OVERRIDE_RECORD_PREFIX = "override-"

DEFAULT_OVERRIDE_BUDGET = 5
OVERRIDE_WINDOW_DAYS = 30


@dataclass
class OverrideRecord:
    """Written override record for a gate deviation."""
    content_key: str
    diff_hash: str
    record_path: Path
    trail_path: Path
    manifest: dict


def get_override_budget() -> int:
    """Return the per-window override budget from VNX_ATTEST_OVERRIDE_BUDGET (default: 5)."""
    raw = os.environ.get("VNX_ATTEST_OVERRIDE_BUDGET", "").strip()
    if raw.isdigit():
        return int(raw)
    return DEFAULT_OVERRIDE_BUDGET


def build_override_manifest(
    *,
    content_key: str,
    reason: str,
    dispatch_id: str,
    signer_identity: str,
    timestamp: str,
) -> dict:
    """Build an override manifest dict (without signature field).

    Args:
        content_key: Diff hash (content-key) this override applies to.
        reason: Non-empty human-readable justification.
        dispatch_id: Dispatch-ID or override slug for traceability.
        signer_identity: Signer identity matching an allowed_signers entry.
        timestamp: ISO-8601 UTC timestamp (caller-supplied).

    Raises:
        ValueError: If reason is empty or whitespace-only.
    """
    if not reason or not reason.strip():
        raise ValueError("override reason must be non-empty")
    return {
        "schema_version": SCHEMA_VERSION,
        "attestation_type": ATTESTATION_OVERRIDE,
        "content_key": content_key,
        "diff_hash": content_key,
        "reason": reason.strip(),
        "dispatch_id": dispatch_id,
        "signer_identity": signer_identity,
        "timestamp": timestamp,
    }


def count_overrides_in_window(
    trail_path: "str | Path",
    window_days: int = OVERRIDE_WINDOW_DAYS,
    allowed_signers: "str | Path | None" = None,
    _now_ts: "str | None" = None,
) -> int:
    """Count VALID override entries in the NDJSON trail within the rolling window.

    Derived from the append-only trail — not a mutable counter. An entry only
    counts toward the budget when it is (a) part of an intact hash-chain and,
    when ``allowed_signers`` is given, (b) carries a valid detached signature.
    A forged, unsigned, or chain-tampered entry MUST NOT count toward — nor be
    able to deflate — the budget.

    Args:
        trail_path: Path to override-trail.ndjson.
        window_days: Rolling window in days (default: 30).
        allowed_signers: allowed_signers file used to verify each entry's
            signature. When None the signature is NOT checked — callers that
            enforce a budget (the gate, write_override_record) MUST pass a
            base-branch-pinned allowed_signers so a rogue key cannot self-authorize.
        _now_ts: ISO-8601 UTC 'now' — injected by tests; production omits.

    Returns:
        Count of valid override entries within the window.

    Raises:
        ValueError: If the trail's hash-chain integrity check fails (tamper) —
            a tampered budget ledger must never be silently trusted.
    """
    trail_path = Path(trail_path)
    if not trail_path.exists():
        return 0

    # Chain integrity first: a tampered/spliced trail must never be trusted for
    # budgeting (an attacker could delete their own override rows to free budget).
    chain_ok, _violations, chain_err = verify_chain(trail_path)
    if not chain_ok:
        raise ValueError(
            f"override trail chain integrity failed ({trail_path}): {chain_err}"
        )
    # A non-empty "unchained" trail = the hash-chain was stripped (production
    # always writes via append_chained_entry). Refuse to trust it — otherwise an
    # attacker replaces the chained ledger with raw rows to dodge the chain check.
    if chain_err == "unchained" and trail_path.read_text(encoding="utf-8").strip():
        raise ValueError(
            f"override trail is unchained (hash-chain stripped): {trail_path}"
        )

    if _now_ts is not None:
        now = datetime.fromisoformat(_now_ts.replace("Z", "+00:00"))
    else:
        now = datetime.now(timezone.utc)

    window_seconds = window_days * 86400
    count = 0
    for _idx, entry, _entry_hash in walk_chain(trail_path):
        if not isinstance(entry, dict):
            continue
        if entry.get("attestation_type") != ATTESTATION_OVERRIDE:
            continue
        # Signature must be valid. Strip the chain pointer (prev_hash) added by
        # append_chained_entry to reconstruct the exact signed manifest.
        if allowed_signers is not None:
            manifest = {k: v for k, v in entry.items() if k != "prev_hash"}
            if not verify_attestation(manifest, allowed_signers):
                continue
        ts_str = entry.get("timestamp", "")
        if not ts_str:
            continue
        try:
            ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        except ValueError:
            continue
        if (now - ts).total_seconds() <= window_seconds:
            count += 1

    return count


def write_override_record(
    *,
    content_key: str,
    reason: str,
    dispatch_id: str,
    signer_identity: str,
    timestamp: str,
    key_path: "str | Path",
    repo_root: "str | Path | None" = None,
    allowed_signers: "str | Path | None" = None,
    _now_ts: "str | None" = None,
) -> OverrideRecord:
    """Build, sign, and persist an override record + trail entry.

    Writes:
      .vnx-attest/override-<content_key>.json  — signed override record
      .vnx-attest/override-trail.ndjson         — append-only audit trail

    Enforces the rolling budget itself (defense in depth): it re-derives the
    used count from the (chain-verified, signature-checked) trail and REFUSES to
    write when the budget is exhausted, even if a caller skipped its own check.
    Pass ``allowed_signers`` (base-branch-pinned) so the budget count verifies
    signatures; without it the count cannot reject a forged trail entry.

    Args:
        content_key: The diff hash this override applies to.
        reason: Non-empty justification (validated in build_override_manifest).
        dispatch_id: Dispatch-ID or override slug.
        signer_identity: Signer identity matching allowed_signers.
        timestamp: ISO-8601 UTC timestamp.
        key_path: SSH private key path for signing.
        repo_root: Repository root.  Defaults to cwd.

    Raises:
        ValueError: If reason is empty.
        RuntimeError: If ssh-keygen signing fails.
    """
    repo_root = Path(repo_root) if repo_root else Path.cwd()
    attest_dir = repo_root / ATTEST_DIR
    attest_dir.mkdir(parents=True, exist_ok=True)

    # Defense in depth: enforce the rolling budget here too, re-derived from the
    # chain-verified, signature-checked trail. A caller that forgot to check (or
    # a race between check and write) must still not exceed the budget.
    trail_path = attest_dir / OVERRIDE_TRAIL_FILE
    budget = get_override_budget()
    used = count_overrides_in_window(
        trail_path, allowed_signers=allowed_signers, _now_ts=_now_ts
    )
    if used >= budget:
        raise RuntimeError(
            f"override budget exhausted ({used}/{budget} used in the last "
            f"{OVERRIDE_WINDOW_DAYS} days); refusing to write override record"
        )

    manifest = build_override_manifest(
        content_key=content_key,
        reason=reason,
        dispatch_id=dispatch_id,
        signer_identity=signer_identity,
        timestamp=timestamp,
    )
    signed = sign_manifest(manifest, key_path)

    # Write the record file (content-key-addressed)
    record_path = attest_dir / f"{OVERRIDE_RECORD_PREFIX}{content_key}.json"
    record_path.write_text(
        json.dumps(signed, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )

    # Append to the hash-chained audit trail (append-only)
    trail_path = attest_dir / OVERRIDE_TRAIL_FILE
    append_chained_entry(trail_path, signed)

    return OverrideRecord(
        content_key=content_key,
        diff_hash=content_key,
        record_path=record_path,
        trail_path=trail_path,
        manifest=signed,
    )


def verify_override_record(
    *,
    allowed_signers: "str | Path",
    repo_root: "str | Path | None" = None,
    base_ref: str = "origin/main",
    head_ref: str = "HEAD",
) -> "tuple[bool, str, dict | None]":
    """Verify the override record for the current branch.

    Checks in order:
    1. Compute current diff's content-key.
    2. .vnx-attest/override-<content-key>.json exists.
    3. manifest.content_key matches current content-key (diff-binding).
    4. attestation_type is "override".
    5. manifest.reason is non-empty.
    6. Signature is valid against base-branch allowed_signers.

    Args:
        allowed_signers: Path to SSH allowed_signers file (base-branch copy).
        repo_root: Repository root.  Defaults to cwd.
        base_ref: Base branch for merge-base (default: origin/main).
        head_ref: Branch tip to verify (default: HEAD).

    Returns:
        (ok: bool, reason_str: str, manifest: dict | None)
        manifest is the parsed override manifest on success, None on failure.
    """
    repo_root = Path(repo_root) if repo_root else Path.cwd()

    try:
        current_content_key = compute_diff_hash(
            repo_root=repo_root, base_ref=base_ref, head_ref=head_ref
        )
    except RuntimeError as e:
        return (False, f"diff computation failed: {e}", None)

    record_path = repo_root / ATTEST_DIR / f"{OVERRIDE_RECORD_PREFIX}{current_content_key}.json"
    if not record_path.exists():
        return (
            False,
            f"no override record for content-key {current_content_key[:12]}…",
            None,
        )

    try:
        manifest = json.loads(record_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        return (False, f"override record unreadable: {e}", None)

    # Diff-binding: the override must be for THIS exact diff
    record_key = manifest.get("content_key") or manifest.get("diff_hash")
    if record_key != current_content_key:
        return (
            False,
            f"diff-binding mismatch: override.content_key={record_key!r} "
            f"!= current={current_content_key!r}",
            None,
        )

    # attestation_type guard
    if manifest.get("attestation_type") != ATTESTATION_OVERRIDE:
        return (
            False,
            f"record is not an override "
            f"(attestation_type={manifest.get('attestation_type')!r})",
            None,
        )

    # Reason must be non-empty
    reason_val = manifest.get("reason", "")
    if not reason_val or not reason_val.strip():
        return (False, "override manifest has empty reason", None)

    # Signature must be valid against base-branch allowed_signers
    # (signer-pinned: PR-tree cannot supply its own trust anchor)
    if not verify_attestation(manifest, allowed_signers):
        return (False, "override signature verification failed", None)

    return (True, "ok", manifest)
