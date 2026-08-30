#!/usr/bin/env python3
"""gate_obligation_reopen_stale_evidence.py — audited reopen of an obligation
that was booked fulfilled/failed off evidence about a DIFFERENT commit
(OI-1571 tak 3).

The gate obligation runner used to book an obligation fulfilled off any
complete-evidence decided verdict for the same dispatch_id/PR+gate,
regardless of which commit that verdict actually reviewed
(``gate_obligation_runner._has_decided_evidence`` gained a sha-binding check
in this same dispatch — 20260830-153000-oi1569-quota-heeft-geen-tijddimensie
— but the fix is not retroactive). Any obligation booked BEFORE that fix
landed can be sitting on a record that is, in fact, unreviewed against its
PR's current head.

This is a hand-triggered CORRECTION tool, not part of the runner's own
retry loop: :func:`gate_obligations.update_obligation` is the ONLY place
state gets mutated, and this script is the ONLY place that state mutation
happens for THIS specific defect class — a worker or operator hand-editing
the JSON file directly is exactly what this exists to make unnecessary.

Live measured case (30-08, PR #1719): obligation
``20260830-133000-oi1453-noemer-is-pass`` (gate=codex_gate) was booked
``fulfilled``/``resolved_by_gate=glm_gate`` off ``pr-1719-glm_gate.json``, a
PASS recorded 2026-08-29T11:35:55Z against commit 8101fdf2 — the PR head at
the time this script was written is 64df9933f6b3fed46070d597965f4415acca83e.
``vnx pr-ready 1719`` independently confirmed the same fact
("glm_gate NOT on head").

Safety: this script VERIFIES the mismatch itself before writing anything —
it never trusts an operator's claim on faith. It refuses (no write, exit 2)
when:
  - the obligation is not in a terminal fulfilled/failed state (nothing to
    reopen),
  - the obligation's own ``evidence_result_path``/``result_path`` cannot be
    read,
  - the PR's current head sha cannot be resolved (``gh`` unavailable) — the
    mismatch cannot be PROVEN, so nothing is reopened on an unverified claim
    (the same "third branch, never guess" discipline the runner's own sha
    check applies),
  - the evidence's own commit_sha turns out to MATCH the current head after
    all (there is genuinely nothing to correct).

Usage:
    python3 scripts/gate_obligation_reopen_stale_evidence.py \\
        --dispatch-id 20260830-133000-oi1453-noemer-is-pass        # dry run
    python3 scripts/gate_obligation_reopen_stale_evidence.py \\
        --dispatch-id 20260830-133000-oi1453-noemer-is-pass --write

Dry run is the default on purpose.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, Optional

SCRIPT_DIR = Path(__file__).resolve().parent
for _p in (SCRIPT_DIR / "lib", SCRIPT_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from gate_obligations import (  # noqa: E402
    STATUS_FAILED,
    STATUS_FULFILLED,
    STATUS_PENDING,
    obligation_path,
    update_obligation,
)
from gate_executor import _classify_sha_binding  # noqa: E402
from gate_obligation_runner import _get_pr_head_sha_for_gate  # noqa: E402


def _utc_now_iso() -> str:
    from datetime import datetime, timezone  # noqa: PLC0415

    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class ReopenRefused(RuntimeError):
    """The obligation cannot be reopened — see the message for why."""


def verify_stale_evidence(state_dir: Path, dispatch_id: str) -> Dict[str, Any]:
    """Read the obligation and PROVE its evidence is about a different
    commit than the PR's current head. Never writes. Raises
    :class:`ReopenRefused` (never returns a half-verified result) when the
    mismatch cannot be established.
    """
    path = obligation_path(state_dir, dispatch_id)
    if not path.exists():
        raise ReopenRefused(f"no obligation record for dispatch_id={dispatch_id!r} at {path}")
    record = json.loads(path.read_text(encoding="utf-8"))

    status = record.get("status")
    if status not in (STATUS_FULFILLED, STATUS_FAILED):
        raise ReopenRefused(
            f"obligation status is {status!r}, not fulfilled/failed — nothing to reopen"
        )

    evidence_path_str = record.get("evidence_result_path") or record.get("result_path")
    if not evidence_path_str:
        raise ReopenRefused("obligation carries no evidence_result_path/result_path to verify")
    evidence_path = Path(evidence_path_str)
    if not evidence_path.exists():
        raise ReopenRefused(f"evidence file does not exist on disk: {evidence_path}")
    try:
        evidence_record = json.loads(evidence_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReopenRefused(f"evidence file unreadable: {evidence_path} ({exc})") from exc

    pr_number = record.get("pr_number") or evidence_record.get("pr_number")
    if not isinstance(pr_number, int) or pr_number <= 0:
        raise ReopenRefused("obligation has no resolvable pr_number to check the PR head against")

    head_sha = _get_pr_head_sha_for_gate(pr_number)
    evidence_sha = str(evidence_record.get("commit_sha") or "")
    binding = _classify_sha_binding(head_sha, evidence_sha)

    if binding == "unknown":
        raise ReopenRefused(
            f"sha binding unverifiable (head_sha={head_sha!r}, evidence_sha={evidence_sha!r}) "
            "— refusing to reopen on an unproven claim"
        )
    if binding == "match":
        raise ReopenRefused(
            f"evidence commit_sha {evidence_sha!r} MATCHES the current PR #{pr_number} head "
            f"{head_sha!r} — there is nothing stale to correct"
        )

    return {
        "path": path,
        "record": record,
        "pr_number": pr_number,
        "head_sha": head_sha,
        "evidence_sha": evidence_sha,
        "evidence_path": str(evidence_path),
        "resolved_by_gate": record.get("resolved_by_gate") or record.get("fulfilled_by") or record.get("gate"),
    }


def reopen_obligation(
    state_dir: Path, dispatch_id: str, *, operator_reason: str, write: bool,
) -> Dict[str, Any]:
    """Verify the mismatch, then (if ``write``) reopen the obligation to
    ``STATUS_PENDING`` via the audited :func:`update_obligation` API —
    never a hand-edit of the JSON file.
    """
    proof = verify_stale_evidence(state_dir, dispatch_id)
    record = proof["record"]
    reason_detail = (
        f"reopened by gate_obligation_reopen_stale_evidence.py (OI-1571 tak 3): "
        f"the obligation was booked {record.get('status')!r} via "
        f"{proof['resolved_by_gate']!r} off evidence at {proof['evidence_path']} "
        f"(commit_sha={proof['evidence_sha'][:12]!r}), which is a DIFFERENT "
        f"commit than PR #{proof['pr_number']}'s current head "
        f"({proof['head_sha'][:12]!r}) — the gate obligation runner's own "
        "sha-binding check (this same dispatch) would now refuse this "
        f"evidence outright. Operator reason: {operator_reason}"
    )
    outcome: Dict[str, Any] = {
        "dispatch_id": dispatch_id,
        "path": str(proof["path"]),
        "previous_status": record.get("status"),
        "pr_number": proof["pr_number"],
        "head_sha": proof["head_sha"],
        "evidence_sha": proof["evidence_sha"],
        "reason_detail": reason_detail,
        "write": write,
    }
    if not write:
        outcome["action"] = "would_reopen"
        return outcome

    from review_gate_manager import emit_governance_receipt  # noqa: PLC0415

    # ADR-005: the ledger is canonical, written before the state mutation —
    # the same order every other durable state-mutation in this fleet uses
    # (see gate_request_handler._check_ci_gate_requirement_mismatch).
    emit_governance_receipt(
        "gate_obligation_reopened_stale_evidence",
        receipt_kind="review_gate",
        status="reopened",
        dispatch_id=dispatch_id,
        gate=str(record.get("gate") or ""),
        pr_number=proof["pr_number"],
        previous_status=record.get("status"),
        previous_resolved_by_gate=proof["resolved_by_gate"],
        evidence_commit_sha=proof["evidence_sha"],
        pr_head_sha=proof["head_sha"],
        reason_detail=reason_detail,
    )
    update_obligation(
        proof["path"],
        status=STATUS_PENDING,
        attempts=0,
        last_attempt_at=None,
        resolved_at=None,
        request_path=None,
        result_path=None,
        resolved_by_gate=None,
        takeover_hops=None,
        fulfilled_by=None,
        takeover_gate=None,
        evidence_result_path=None,
        reason="reopened_stale_takeover_evidence",
        reason_detail=reason_detail,
    )
    outcome["action"] = "reopened"
    return outcome


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--state-dir", type=Path, default=None)
    parser.add_argument("--dispatch-id", required=True)
    parser.add_argument(
        "--reason", default="OI-1571 tak 3 remediation — see script docstring for the measured case",
        help="Operator-supplied audit reason, embedded in the obligation's reason_detail",
    )
    parser.add_argument("--write", action="store_true", help="Persist the reopen (default: dry run)")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    if args.state_dir is not None:
        state_dir = args.state_dir
    else:
        import vnx_paths  # noqa: PLC0415

        state_dir = Path(vnx_paths.ensure_env()["VNX_STATE_DIR"])
    if not state_dir.is_dir():
        print(f"ERROR: state dir not found: {state_dir}", file=sys.stderr)
        return 20

    try:
        outcome = reopen_obligation(
            state_dir, args.dispatch_id, operator_reason=args.reason, write=args.write,
        )
    except ReopenRefused as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(outcome, indent=2))
    else:
        print(f"action={outcome['action']} dispatch_id={outcome['dispatch_id']}")
        print(f"  previous_status={outcome['previous_status']}")
        print(f"  pr_number={outcome['pr_number']} head_sha={outcome['head_sha'][:12]}")
        print(f"  evidence_sha={outcome['evidence_sha'][:12]}")
        if not args.write:
            print("  DRY RUN — pass --write to persist")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
