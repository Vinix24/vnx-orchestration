#!/usr/bin/env python3
"""gate_reanchor_cli.py — re-anchor a gate verdict instead of buying it again.

OI-1471. A rebase moves a PR onto a new commit; the fabric's rule is "gate
AFTER a rebase, never before", so the verdict has to sit on the commit that
merges. On a busy day main moved four times, and each shift forced a fresh
model call on PRs whose diff had not changed at all — PR #1691 was gated three
times against the identical ``contract_hash`` ``dd5ac45f7e84535e``.

This command moves the existing verdict onto the new commit when, and only
when, both halves of the condition hold. ``scripts/lib/gate_reanchor`` decides;
this file resolves the inputs, writes through the guarded recorder, and stamps
provenance so nobody can later mistake a re-anchored verdict for a fresh one.

Read-only by default. ``--apply`` writes.

Exit codes:
  0  - re-anchored (with --apply), or allowed (without)
  1  - refused: a condition does not hold, so the gate must actually run
  10 - bad arguments, or the existing record cannot be read
  20 - the write itself failed

Usage:
  python3 scripts/gate_reanchor_cli.py --pr 1691 --gate glm_gate
  python3 scripts/gate_reanchor_cli.py --pr 1691 --gate glm_gate --apply
  python3 scripts/gate_reanchor_cli.py --pr 1691 --gate glm_gate --depth full
"""

from __future__ import annotations

import argparse
import fcntl
import json
import subprocess
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

SCRIPT_DIR = Path(__file__).resolve().parent
for _p in (str(SCRIPT_DIR / "lib"), str(SCRIPT_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import gate_reanchor  # noqa: E402
from gate_artifacts import _compute_contract_hash  # noqa: E402  canonical hasher, never a second one
from gate_register_emit import register_path  # noqa: E402  one resolver, not a second copy
from gate_status import has_complete_evidence, is_terminal  # noqa: E402
import state_writer  # noqa: E402
from vnx_paths import ensure_env  # noqa: E402

EXIT_OK = 0
EXIT_REFUSED = 1
EXIT_BAD_INPUT = 10
EXIT_WRITE_FAILED = 20

#: Gates whose ``contract_hash`` is ``sha256(prompt)[:16]`` with the full diff
#: inside the prompt — the ONLY gates for which hash equality proves the input
#: was byte-identical.
#:
#: This restriction is load-bearing, not caution. ``_compute_contract_hash``
#: falls back to ``sha256({gate, branch, sorted(changed_files)})`` when the
#: request carries no prompt, and that fallback is stable across content
#: changes: edit a file's contents without adding or removing files and the
#: hash does not move. Re-anchoring on such a hash would carry a verdict onto
#: a diff nobody reviewed — the exact failure this whole mechanism exists to
#: avoid. Every other gate refuses, loudly.
DIFF_DERIVED_HASH_GATES = ("glm_gate", "kimi_gate")


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _gh(args, timeout: int = 30) -> str:
    proc = subprocess.run(["gh", *args], capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        raise RuntimeError(f"gh {' '.join(args)} failed: {(proc.stderr or '').strip()[:200]}")
    return proc.stdout


def load_existing_result(results_dir: Path, gate: str, pr_number: int) -> Dict[str, Any]:
    """The record whose verdict is a candidate for re-anchoring.

    Requires a terminal, fully-evidenced record. A ``not_executable`` or an
    ``unavailable`` outage record carries no verdict to move, and moving one
    would manufacture evidence rather than relocate it.
    """
    path = results_dir / f"pr-{pr_number}-{gate}.json"
    if not path.is_file():
        raise FileNotFoundError(f"no existing {gate} result for PR #{pr_number} at {path}")
    record = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(record, dict):
        raise ValueError(f"{path} is not a JSON object")
    if not is_terminal(record):
        raise ValueError(
            f"{gate} result for #{pr_number} is not terminal (status="
            f"{record.get('status')!r}) — there is no verdict to re-anchor"
        )
    if not has_complete_evidence(record):
        raise ValueError(
            f"{gate} result for #{pr_number} lacks contract_hash and/or report_path — "
            "an unevidenced record cannot be relocated onto a new commit"
        )
    return record


def current_contract_hash(gate: str, pr_number: int) -> str:
    """Recompute the hash the gate WOULD produce for the PR head, offline.

    No model call: ``_build_prompt`` is deterministic and the diff comes from
    ``gh``. Verified against PR #1691, whose recorded hash
    ``dd5ac45f7e84535e`` this reproduces exactly.
    """
    diff = _gh(["pr", "diff", str(pr_number)])
    if gate == "glm_gate":
        import glm_gate as gate_module
    elif gate == "kimi_gate":
        import kimi_gate as gate_module
    else:  # pragma: no cover — guarded by DIFF_DERIVED_HASH_GATES at the call site
        raise ValueError(f"{gate} has no diff-derived contract hash")
    prompt = gate_module._build_prompt(diff, str(pr_number))
    return _compute_contract_hash({"prompt": prompt}, gate)


def build_reanchored_payload(
    record: Dict[str, Any], *, new_sha: str, new_branch: str, decision,
) -> Dict[str, Any]:
    """The old verdict, on the new commit, marked as relocated.

    Everything that makes the verdict evidence is carried over unchanged:
    status, findings, ``contract_hash``, ``report_path`` (the report reviewed a
    byte-identical diff, so it is still the right report) and ``dispatch_id``
    (the producer identity is the run that actually reviewed it — claiming a
    new one would be the lie).

    What is added is provenance. ``evidence_source: "reanchored"`` sits beside
    the existing ``"live"``/``"reprocessed"`` vocabulary, and
    ``reanchored_from_commit_sha`` plus ``reanchor_basis`` record which commit
    the verdict came from and on what grounds. An audit that cannot tell a
    relocated verdict from a freshly bought one is worse than paying twice.
    """
    payload = dict(record)
    payload["commit_sha"] = new_sha
    payload["branch"] = new_branch
    payload["evidence_source"] = "reanchored"
    payload["reanchored_from_commit_sha"] = record.get("commit_sha", "")
    payload["reanchored_at"] = _utc_now_iso()
    payload["reanchor_basis"] = decision.to_dict()
    return payload


@contextmanager
def exclusive_result_lock(result_path: Path):
    """Hold an exclusive lock over one result slot for a whole read-check-write.

    ``gate_recorder`` has no lock anywhere: ``_write_result_atomic`` is atomic
    per WRITE (tmp + replace), but the read in ``_check_overwrite_guard`` and
    the write that follows are two separate operations with a window between
    them. Measured with two threads that both read the record on the old sha
    before either wrote: both cleared the overwrite guard.

    Everything the re-anchor decides — is this record terminal, is its hash
    still equal, has the head moved — is read BEFORE the write. Without this
    lock two concurrent ``--apply`` runs both decide against the same old sha
    and both act on it.
    """
    lock_path = result_path.with_suffix(".reanchor.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def compare_and_swap_guard(result_path: Path, expected_old_sha: str) -> None:
    """Refuse when the record moved between the decision and the write.

    The lock above keeps two of THIS command apart. This check keeps it honest
    against every other writer of the same slot — the live gate, --reprocess,
    the obligation runner — none of which take that lock. Re-reading under the
    lock and confirming the record still sits on the commit the decision was
    made against turns the write into a compare-and-swap instead of a
    last-writer-wins.
    """
    if not result_path.is_file():
        raise ValueError(
            f"{result_path} disappeared between the decision and the write — refusing"
        )
    current = json.loads(result_path.read_text(encoding="utf-8"))
    observed = (current or {}).get("commit_sha", "")
    if observed != expected_old_sha:
        raise ValueError(
            f"{result_path} moved from {expected_old_sha[:12]} to {observed[:12] or '(none)'} "
            "while this re-anchor was deciding — another writer got there first, so the "
            "decision was made against a record that no longer exists"
        )


def emit_reanchor_event(
    *, gate: str, pr_number: int, record: Dict[str, Any], new_sha: str, decision,
) -> Path:
    """Append the re-anchor to dispatch_register.ndjson (ADR-005).

    A re-anchor is a gate outcome — one of the ledger's named classes — and it
    is the one gate outcome nobody paid a model for. Writing the result record
    without a register line would leave a state mutation with no line in the
    ledger, which is the whole defect this cluster is about.

    Reuses ``gate_register_emit.register_path`` and ``state_writer.append_locked``:
    the same resolver and the same locked append every other gate line goes
    through, never a second ledger invented for this one writer.

    Raises on failure rather than swallowing it. The result record is already
    on disk and carries its own provenance, so the state is not corrupt — but
    a missing ledger line has to be said out loud, not logged and forgotten.
    """
    path = register_path()
    state_writer.append_locked(path, {
        "timestamp": _utc_now_iso(),
        "event": "gate_reanchored",
        "gate": gate,
        "pr_number": pr_number,
        "dispatch_id": record.get("dispatch_id", ""),
        "status": record.get("status", ""),
        "contract_hash": record.get("contract_hash", ""),
        "from_commit_sha": record.get("commit_sha", ""),
        "to_commit_sha": new_sha,
        "commits_in_range": decision.commits_in_range,
        "depth": decision.depth,
        "reason": decision.reason,
    })
    return path


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="gate_reanchor_cli.py",
        description="Re-anchor an existing gate verdict onto the PR's current head "
                    "when the reviewed input is provably identical (OI-1471).",
    )
    parser.add_argument("--pr", required=True, type=int, help="PR number")
    parser.add_argument("--gate", required=True, help=f"one of {', '.join(DIFF_DERIVED_HASH_GATES)}")
    parser.add_argument("--apply", action="store_true", help="write the re-anchored record")
    parser.add_argument(
        "--depth", default="direct", choices=("direct", "full"),
        help="import-graph depth for the symbol analysis (default: direct — see "
             "gate_reanchor.DEPTH_DEFAULT for why)",
    )
    parser.add_argument("--project-root", default=None)
    parser.add_argument("--base-ref", default="origin/main")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    if args.gate not in DIFF_DERIVED_HASH_GATES:
        print(
            f"gate-reanchor: {args.gate} has no diff-derived contract hash. Only "
            f"{', '.join(DIFF_DERIVED_HASH_GATES)} hash the full diff; every other gate's "
            "hash is stable across content changes, so equality would prove nothing.",
            file=sys.stderr,
        )
        return EXIT_BAD_INPUT

    project_root = Path(args.project_root) if args.project_root else Path.cwd()
    results_dir = Path(ensure_env()["VNX_STATE_DIR"]) / "review_gates" / "results"

    try:
        record = load_existing_result(results_dir, args.gate, args.pr)
        pr_view = json.loads(_gh(["pr", "view", str(args.pr), "--json", "headRefOid,headRefName,files"]))
        new_sha = pr_view.get("headRefOid") or ""
        new_branch = pr_view.get("headRefName") or ""
        pr_files = [f["path"] for f in (pr_view.get("files") or []) if f.get("path")]
        new_hash = current_contract_hash(args.gate, args.pr)
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
        print(f"gate-reanchor: cannot establish the inputs: {exc}", file=sys.stderr)
        return EXIT_BAD_INPUT

    decision = gate_reanchor.can_reanchor(
        project_root,
        old_sha=record.get("commit_sha", ""),
        new_sha=new_sha,
        pr_files=pr_files,
        old_contract_hash=record.get("contract_hash", ""),
        new_contract_hash=new_hash,
        base_ref=args.base_ref,
        depth=gate_reanchor.DEPTH_FULL if args.depth == "full" else gate_reanchor.DEPTH_DIRECT,
    )

    payload_out: Dict[str, Any] = {
        "pr": args.pr, "gate": args.gate, "applied": False, "decision": decision.to_dict(),
    }

    if not decision.allowed:
        if args.json:
            print(json.dumps(payload_out, indent=2))
        else:
            print(f"REFUSED  #{args.pr} {args.gate}: {decision.reason}")
            print("         the gate has to run — this is not a saving that was available")
        return EXIT_REFUSED

    if not args.apply:
        if args.json:
            print(json.dumps(payload_out, indent=2))
        else:
            print(f"ALLOWED  #{args.pr} {args.gate}: {decision.reason}")
            print(f"         old {decision.old_sha[:12]} -> new {decision.new_sha[:12]}; "
                  f"re-run with --apply to write it")
        return EXIT_OK

    from gate_recorder import ResultOverwriteRefused, record_terminal_result  # noqa: PLC0415

    payload = build_reanchored_payload(
        record, new_sha=new_sha, new_branch=new_branch, decision=decision,
    )
    result_path = results_dir / f"pr-{args.pr}-{args.gate}.json"
    try:
        with exclusive_result_lock(result_path):
            compare_and_swap_guard(result_path, record.get("commit_sha", ""))
            written = record_terminal_result(
                gate=args.gate,
                pr_id=str(args.pr),
                result_path=result_path,
                payload=payload,
            )
    except (OSError, ValueError, ResultOverwriteRefused) as exc:
        print(f"gate-reanchor: the guarded write refused: {exc}", file=sys.stderr)
        return EXIT_WRITE_FAILED

    try:
        register = emit_reanchor_event(
            gate=args.gate, pr_number=args.pr, record=record,
            new_sha=new_sha, decision=decision,
        )
    except (OSError, ValueError) as exc:
        print(
            f"gate-reanchor: the record was written to {written} but the register line "
            f"failed: {exc}. The verdict carries its own provenance, so the state is "
            "sound — the LEDGER is now incomplete and needs the line adding by hand.",
            file=sys.stderr,
        )
        return EXIT_WRITE_FAILED

    payload_out["applied"] = True
    payload_out["result_path"] = str(written)
    payload_out["register_path"] = str(register)
    if args.json:
        print(json.dumps(payload_out, indent=2))
    else:
        print(f"RE-ANCHORED  #{args.pr} {args.gate} onto {new_sha[:12]} "
              f"(from {decision.old_sha[:12]}) -> {written}")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
