"""Plan-gate-pass evidence — the durable, tamper-evident record that a track's
plan-first gate passed, plus its verification (ADR-030's merge-gate primitive).

When a track's ``OI-PLAN`` plan-first gate is resolved (``plan-gate run`` PASS or
``plan-gate attest``), emit a hash-chained ``plan_gate_pass`` record keyed by
``track_id`` into ``.vnx-attest/plan-gates.ndjson``. Unlike the evidence-bound
gate's diff-bound receipts, a plan-gate pass PREDATES the diff, so the record is
TRACK-keyed, not content-keyed — the correct front link of the requirements-
traceability chain (PRD → track → plan-gate-pass → dispatch → receipt → PR).

The merge gate (``verify_pr``) then checks, for a PR's linked track, that a valid
``plan_gate_pass`` record exists — advisory-first via ``VNX_PLAN_GATE_ENFORCE``
(the same flag the dispatch door uses). Door gates the dispatch; merge gates the PR.

Reuses the D1 attestation primitives (``sign_manifest`` / ``verify_attestation`` /
``append_chained_entry``): a record signed when a key is available is tamper-PROOF;
an unsigned record is still hash-chained (tamper-EVIDENT). Emission is best-effort
and never raises — it must not break the plan-gate resolution it hangs off.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional, Tuple

_LEDGER = ".vnx-attest/plan-gates.ndjson"
RECORD_TYPE = "plan_gate_pass"

# verification states
VERIFIED = "verified"          # a signed record exists AND its signature verifies
PRESENT_UNSIGNED = "present"   # a hash-chained record exists but is unsigned/unverifiable
ABSENT = "absent"              # no plan-gate-pass record for this track


def _ledger_path(repo_root: "str | Path") -> Path:
    return Path(repo_root) / _LEDGER


def emit_plan_gate_pass(
    *,
    repo_root: "str | Path",
    track_id: str,
    project_id: str,
    resolver: str,                 # "run" | "attest"
    timestamp: str,
    approval_id: Optional[str] = None,
    reason: Optional[str] = None,
    signer_identity: Optional[str] = None,
    key_path: "str | Path | None" = None,
) -> Optional[Dict[str, Any]]:
    """Append a ``plan_gate_pass`` record for a track to the repo-committed ledger.

    Signed when ``key_path`` + ``signer_identity`` are provided (tamper-proof);
    otherwise appended unsigned but hash-chained (tamper-evident). Best-effort:
    returns the appended record on success, ``None`` on any failure (never raises).
    """
    try:
        from ndjson_hash_chain import append_chained_entry  # noqa: PLC0415

        record: Dict[str, Any] = {
            "type": RECORD_TYPE,
            "track_id": track_id,
            "project_id": project_id,
            "resolver": resolver,
            "resolved_at": timestamp,
        }
        if approval_id:
            record["approval_id"] = approval_id
        if reason:
            record["reason"] = reason
        if key_path and signer_identity:
            from attestation import sign_manifest  # noqa: PLC0415
            record["signer_identity"] = signer_identity
            record = sign_manifest(record, key_path)

        ledger = _ledger_path(repo_root)
        ledger.parent.mkdir(parents=True, exist_ok=True)
        append_chained_entry(ledger, record)
        return record
    except Exception:  # noqa: BLE001 — emission must never break the gate resolution
        return None


def verify_plan_gate_pass(
    repo_root: "str | Path",
    track_id: str,
    project_id: str,
    allowed_signers: "str | Path | None" = None,
) -> Tuple[str, Optional[Dict[str, Any]]]:
    """Return ``(state, record)`` for a track's plan-gate pass.

    Walks the hash-chained ledger for the LATEST ``plan_gate_pass`` matching
    ``(track_id, project_id)``. If ``allowed_signers`` is given and the record's
    signature verifies → ``VERIFIED``; a present-but-unverifiable record →
    ``PRESENT_UNSIGNED``; nothing → ``ABSENT``. Read-only; never raises.
    """
    ledger = _ledger_path(repo_root)
    if not ledger.exists():
        return (ABSENT, None)

    latest: Optional[Dict[str, Any]] = None
    try:
        from ndjson_hash_chain import walk_chain  # noqa: PLC0415
        for _i, entry, _h in walk_chain(ledger):
            if not isinstance(entry, dict):
                continue
            if entry.get("type") != RECORD_TYPE:
                continue
            if entry.get("track_id") != track_id:
                continue
            if entry.get("project_id") not in (None, project_id):
                continue
            latest = entry  # last match wins — the most recent pass
    except Exception:  # noqa: BLE001
        return (ABSENT, None)

    if latest is None:
        return (ABSENT, None)

    if allowed_signers is not None:
        try:
            from attestation import verify_attestation  # noqa: PLC0415
            manifest = {k: v for k, v in latest.items() if k != "prev_hash"}
            if verify_attestation(manifest, allowed_signers):
                return (VERIFIED, latest)
        except Exception:  # noqa: BLE001
            pass
    return (PRESENT_UNSIGNED, latest)
