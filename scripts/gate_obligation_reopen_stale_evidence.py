#!/usr/bin/env python3
"""gate_obligation_reopen_stale_evidence.py — audited reopen of an obligation
that was booked fulfilled/failed off evidence that can no longer stand: either
about a DIFFERENT commit (OI-1571 tak 3), or off a file that no longer exists
at all (OI-1726).

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

Safety: this script VERIFIES the misstand itself before writing anything —
it never trusts an operator's claim on faith. It refuses (no write, exit 2)
when:
  - the obligation is not in a terminal fulfilled/failed state (nothing to
    reopen),
  - the obligation carries no ``evidence_result_path``/``result_path`` at
    all,
  - the evidence file EXISTS but cannot be parsed (corrupt JSON) — a
    present-but-unreadable file cannot be PROVEN stale, so it is refused
    rather than reopened on an unverified claim,
  - the PR's current head sha cannot be resolved (``gh`` unavailable) — the
    mismatch cannot be PROVEN, so nothing is reopened on an unverified claim
    (the same "third branch, never guess" discipline the runner's own sha
    check applies),
  - the evidence's own commit_sha turns out to MATCH the current head after
    all (there is genuinely nothing to correct).

OI-1726 (2026-09-12): the ONE ground that flips from refusal to reason is an
evidence file that does NOT exist on disk. An obligation that claims
fulfilment/failure off a file that provably does not exist is false by
construction — the absence IS the proof, so no PR-head resolution or sha
comparison is needed (or possible). That is the strongest reopen case there
is, and this script used to refuse exactly it (measured: ``REFUSED: evidence
file does not exist on disk: .../pr-1840-kimi_gate.json``).

OI-1721 (2026-09-12): the same "absence IS the proof" branch now also admits
a terminally-parked obligation — ``status=not_executable``,
``reason=gate_parked_timeout``, evidence file missing. The gate never ran (it
was parked and escalated to terminal), so its terminal state is a temporary
refusal that exhausted its retry bound, not a verdict; the missing evidence
file proves exactly that. Every OTHER not_executable reason stays refused.

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
    REASON_GATE_PARKED_TIMEOUT,
    STATUS_FAILED,
    STATUS_FULFILLED,
    STATUS_NOT_EXECUTABLE,
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
    """Read the obligation and PROVE its evidence can no longer stand. Never
    writes. Raises :class:`ReopenRefused` (never returns a half-verified
    result) when the misstand cannot be established.

    Three provable misstands are recognized:

      - OI-1721: the obligation is terminally ``not_executable`` with
        ``reason=REASON_GATE_PARKED_TIMEOUT`` (the gate was parked and
        escalated to terminal without ever running) AND its evidence file
        does NOT exist on disk. Same branch and same returned shape as
        OI-1726 — the absence IS the proof that no verdict exists.
      - OI-1726: the evidence file does NOT exist on disk. The obligation
        claims fulfilment/failure off a file that provably does not exist, so
        the claim is false by construction. Returned with
        ``evidence_missing=True`` and empty shas — there is no PR-head
        resolution or sha comparison because the absence IS the proof.
      - OI-1571 tak 3: the evidence file EXISTS and is readable, but its own
        ``commit_sha`` binds to a DIFFERENT commit than the PR's current
        head. Returned with ``evidence_missing=False`` and both shas.
    """
    path = obligation_path(state_dir, dispatch_id)
    if not path.exists():
        raise ReopenRefused(f"no obligation record for dispatch_id={dispatch_id!r} at {path}")
    record = json.loads(path.read_text(encoding="utf-8"))

    status = record.get("status")
    # OI-1721: a terminally-parked obligation is the ONE not_executable shape
    # that is reopenable — its gate never ran, so "not_executable" here is a
    # temporary refusal that exhausted its retry bound, not a verdict about
    # the code. Every other not_executable reason (required_failure,
    # stay_pending_timeout, ...) stays refused: the terminal state IS the
    # honest end of a real decision, and this script must not reopen it.
    parked_timeout = (
        status == STATUS_NOT_EXECUTABLE
        and record.get("reason") == REASON_GATE_PARKED_TIMEOUT
    )
    if status not in (STATUS_FULFILLED, STATUS_FAILED) and not parked_timeout:
        raise ReopenRefused(
            f"obligation status is {status!r}, not fulfilled/failed — nothing to reopen"
        )

    evidence_path_str = record.get("evidence_result_path") or record.get("result_path")
    if not evidence_path_str:
        raise ReopenRefused("obligation carries no evidence_result_path/result_path to verify")
    evidence_path = Path(evidence_path_str)
    if not evidence_path.exists():
        # OI-1726 + OI-1721: a MISSING evidence file is the strongest possible
        # reopen reason — the obligation claims fulfilment/failure (or, for
        # OI-1721, a terminal verdict) off a file that provably does not
        # exist, so the claim is false by construction. The absence IS the
        # proof; there is no PR head to resolve and no sha to compare (the
        # stale-evidence path below only fires when the file EXISTS). A
        # present-but-unreadable file (corrupt) is deliberately NOT this case
        # and still refuses below.
        return {
            "path": path,
            "record": record,
            "pr_number": record.get("pr_number"),
            "head_sha": "",
            "evidence_sha": "",
            "evidence_path": str(evidence_path),
            "evidence_missing": True,
            "parked_timeout": parked_timeout,
            "resolved_by_gate": record.get("resolved_by_gate") or record.get("fulfilled_by") or record.get("gate"),
        }
    if parked_timeout:
        # OI-1721: the parked-timeout reopen ground is DEFINED by the absence
        # of the evidence file — the gate never ran, so there is nothing to
        # bind against. An existing file means the absence is not provable,
        # and a parked gate's result is not a code verdict: refuse rather
        # than fall through to the sha-binding check, which would compare
        # shas of a verdict that never existed.
        raise ReopenRefused(
            "obligation is parked-timeout but its evidence file still exists "
            f"on disk ({evidence_path}) — the absence is not provable, so "
            "there is nothing to reopen"
        )
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
        "evidence_missing": False,
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
    if proof.get("parked_timeout"):
        reopen_reason = "reopened_parked_timeout"
        reason_detail = (
            f"reopened by gate_obligation_reopen_stale_evidence.py (OI-1721): "
            f"the obligation was booked {record.get('status')!r} with "
            f"reason={record.get('reason')!r} — the gate was parked (provider "
            "missing / config flag disabled) and escalated to terminal without "
            f"ever running, and its evidence at {proof['evidence_path']} does "
            "not exist on disk — there is no verdict to bind against, so the "
            "runner must re-resolve the obligation now that the gate may be "
            f"unparked/installed. Operator reason: {operator_reason}"
        )
    elif proof.get("evidence_missing"):
        reopen_reason = "reopened_stale_takeover_evidence"
        reason_detail = (
            f"reopened by gate_obligation_reopen_stale_evidence.py (OI-1726): "
            f"the obligation was booked {record.get('status')!r} via "
            f"{proof['resolved_by_gate']!r} off evidence at {proof['evidence_path']} "
            "which no longer exists on disk — the claimed evidence is provably "
            "gone, so the fulfilment/failure cannot stand and the runner must "
            f"re-resolve the obligation against whatever real evidence exists. "
            f"Operator reason: {operator_reason}"
        )
    else:
        reopen_reason = "reopened_stale_takeover_evidence"
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
        evidence_missing=bool(proof.get("evidence_missing")),
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
        reason=reopen_reason,
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
