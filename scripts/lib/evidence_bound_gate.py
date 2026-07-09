"""Evidence-bound merge gate (D3 bootstrap).

Advisory-by-default verification that a PR's attestation is backed by the
required evidence, each receipt signed and diff-bound to the same content-key.

Evidence is stored as signed governed attestations in the existing
``.vnx-attest/governed.ndjson`` hash-chain (the same ledger used by D1
``emit_governed_attestation``).  Each evidence entry is a normal governed
manifest extended with ``content_key`` and ``evidence_type`` so it can be
located, signature-verified with ``verify_attestation``, and bound to the PR
diff via ``content_key.compute_diff_hash``.

Flag: ``VNX_EVIDENCE_BOUND_GATE`` (off | advisory | required).
  - off:      no evidence check, provenance-only gate behaviour unchanged.
  - advisory: verify evidence, log missing/invalid, but never block (default).
  - required: block the merge-gate when required evidence is missing/invalid.

References:
  - attestation.py: build_governed_manifest, sign_manifest, verify_attestation
  - content_key.py: compute_diff_hash
  - ndjson_hash_chain.py: verify_chain, walk_chain, append_chained_entry
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from attestation import build_governed_manifest, sign_manifest, verify_attestation
from content_key import compute_diff_hash
from ndjson_hash_chain import append_chained_entry, verify_chain, walk_chain

# Ledger shared with D1 governed attestations.
_EVIDENCE_LEDGER = ".vnx-attest/governed.ndjson"

# Evidence type discriminators stored in governed manifest entries.
EVIDENCE_TEST_PASS = "test_pass"
EVIDENCE_TIER1_GATE_PASS = "tier1_gate_pass"
EVIDENCE_TIER2_PANEL_PASS = "tier2_panel_pass"

# Required evidence set per task class.
_REQUIRED_EVIDENCE: Dict[str, List[str]] = {
    "implementation": [EVIDENCE_TEST_PASS, EVIDENCE_TIER1_GATE_PASS],
    "closeout": [
        EVIDENCE_TEST_PASS,
        EVIDENCE_TIER1_GATE_PASS,
        EVIDENCE_TIER2_PANEL_PASS,
    ],
}


def gate_state() -> str:
    """Resolve ``VNX_EVIDENCE_BOUND_GATE`` to one of off/advisory/required.

    Defaults to ``advisory`` so the D3 bootstrap never blocks merges.
    Unknown values fall back to ``off`` (fail-safe disable).
    """
    raw = (os.environ.get("VNX_EVIDENCE_BOUND_GATE") or "advisory").lower().strip()
    if raw in {"off", "advisory", "required"}:
        return raw
    return "off"


def _task_class_for_manifest(manifest: Dict[str, Any]) -> str:
    """Determine the task class used for evidence requirements.

    Explicit ``task_class`` field wins, then ``tags`` containing ``closeout``,
    then a ``deliverable_id`` ending in ``-closeout``.  Everything else is
    treated as ``implementation`` so the default requirement set is minimal.
    """
    tc = manifest.get("task_class")
    if tc in _REQUIRED_EVIDENCE:
        return str(tc)

    tags = manifest.get("tags") or []
    if isinstance(tags, (list, tuple, set)) and "closeout" in tags:
        return "closeout"

    deliverable = manifest.get("deliverable_id")
    if isinstance(deliverable, str) and deliverable.endswith("-closeout"):
        return "closeout"

    return "implementation"


def required_evidence_for(manifest: Dict[str, Any]) -> List[str]:
    """Return the ordered list of required evidence types for ``manifest``."""
    return list(_REQUIRED_EVIDENCE.get(_task_class_for_manifest(manifest), []))


def emit_evidence_attestation(
    *,
    evidence_type: str,
    content_key: str,
    dispatch_id: str,
    track_id: str,
    signer_identity: str,
    timestamp: str,
    key_path: "str | Path",
    repo_root: "str | Path | None" = None,
) -> Dict[str, Any]:
    """Sign and append an evidence receipt to the governed hash-chain.

    Reuses ``build_governed_manifest`` + ``sign_manifest`` + ``append_chained_entry``
    so the receipt format is the existing D1 governed attestation with only
    additive ``content_key`` / ``evidence_type`` fields.
    """
    repo_root = Path(repo_root) if repo_root else Path.cwd()
    ledger_path = repo_root / _EVIDENCE_LEDGER

    manifest = build_governed_manifest(
        dispatch_id=dispatch_id,
        deliverable_id=evidence_type,
        track_id=track_id,
        plan_gate_ref=content_key,
        signer_identity=signer_identity,
        timestamp=timestamp,
    )
    manifest["content_key"] = content_key
    manifest["evidence_type"] = evidence_type

    signed = sign_manifest(manifest, key_path)
    append_chained_entry(ledger_path, signed)
    return signed


def verify_evidence_bound(
    *,
    repo_root: "str | Path",
    base_ref: str,
    head_ref: str,
    allowed_signers: "str | Path",
    manifest: Dict[str, Any],
    verbose: bool = False,
) -> Tuple[bool, str, List[str]]:
    """Verify the required evidence set for a PR attestation.

    Returns ``(ok, status, details)``:
      - ok: True if every required evidence type is present, validly signed,
        and bound to the current diff.
      - status: short human-readable summary.
      - details: list of per-item advisory messages (missing/invalid evidence).

    Required evidence is derived from ``manifest`` via ``required_evidence_for``.
    """
    repo_root = Path(repo_root)
    ledger_path = repo_root / _EVIDENCE_LEDGER
    required = required_evidence_for(manifest)

    if not required:
        return (True, "no required evidence for task class", [])

    try:
        current_key = compute_diff_hash(
            repo_root=repo_root, base_ref=base_ref, head_ref=head_ref
        )
    except RuntimeError as exc:
        detail = f"diff computation failed: {exc}"
        return (False, detail, [detail])

    details: List[str] = []

    # Chain-integrity check: a tampered evidence ledger is itself a violation.
    if ledger_path.exists():
        chain_ok, violations, chain_status = verify_chain(ledger_path)
        if not chain_ok:
            detail = (
                f"evidence ledger chain broken: "
                f"{violations[0] if violations else 'unknown'}"
            )
            return (False, detail, [detail])
    else:
        detail = f"evidence ledger missing; required: {', '.join(required)}"
        return (False, detail, [detail])

    if chain_status == "unchained":
        # Chaining was never enabled (VNX_CHAIN_RECEIPTS off).  The ledger may
        # still contain evidence entries, but we cannot assert chain integrity.
        # Continue with per-entry signature verification.
        if verbose:
            details.append("evidence ledger is unchained; verifying signatures only")

    found: Dict[str, List[Dict[str, Any]]] = {et: [] for et in required}
    invalid: List[str] = []

    for line_no, entry, _entry_hash in walk_chain(ledger_path):
        if not isinstance(entry, dict):
            continue
        et = entry.get("evidence_type")
        if et not in required:
            continue
        if entry.get("content_key") != current_key:
            continue

        # Verify the detached SSH signature against base-branch allowed_signers.
        # Drop the chain pointer so verify_attestation computes canonical bytes.
        verification_manifest = {k: v for k, v in entry.items() if k != "prev_hash"}
        if verify_attestation(verification_manifest, allowed_signers):
            found[et].append(entry)
        else:
            invalid.append(
                f"{et} evidence at line {line_no} has an invalid signature"
            )

    missing = [et for et in required if not found[et]]
    if missing:
        details.append(f"missing evidence: {', '.join(missing)}")
    details.extend(invalid)

    ok = not missing and not invalid
    status = "ok" if ok else "; ".join(details)
    return (ok, status, details)
