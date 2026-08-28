#!/usr/bin/env python3
"""gate_obligations.py — review-gate obligations: the durable link between a
``gate=<name>`` declaration in a dispatch spec and actual review-gate evidence.

OI-876 / OI-881 (2026-07-31): the dispatch spec carried ``gate=codex_gate`` all
the way into the staged bundle, but after ``dispatch_cli.load_spec`` read the
field nothing consumed it — nine dispatches and ten merged PRs produced zero
request records and zero result records while every one of them declared a
gate. A dispatch whose declared gate never ran was indistinguishable from one
whose gate did run.

This module is the registry half of the fix:

  1. The dispatch door (``dispatch_cli.run_dispatch``) calls
     :func:`register_obligation` for every accepted dispatch whose spec
     declares a gate. One obligation record per dispatch, written to
     ``<state_dir>/review_gates/obligations/<dispatch_id>.json``.
  2. ``scripts/gate_obligation_runner.py`` fulfils pending obligations: it
     resolves the dispatch's PR and invokes ``review_gate_manager`` so the
     request record AND the result record actually land — or, when the gate
     cannot run, records a loud ``not_executable``/``failed`` outcome via
     ``gate_recorder`` (which also writes both records). Silence is no longer
     a possible end state.
  3. ``producer_freshness.scan_gate_obligations`` groups obligations per gate
     key and flags a key whose oldest pending declaration exceeds cadence —
     declaration without evidence becomes a finding, per sleutel, never per
     directory.

OI-1388 (2026-08-23): an obligation can also become permanently un-gateable —
its PR merged or closed, or its dispatch died before ever opening one — and
nothing closed the record. It sat ``pending`` forever, indistinguishable from
a dispatch that is still running. :data:`STATUS_RETIRED` books that honestly
(never ``fulfilled``): ``scripts/gate_obligation_runner.py`` closes it going
forward when a dispatch dies without a PR, and the one-time
``scripts/gate_obligation_retire_backlog.py`` books the existing backlog.

Design notes:
  - Best-effort at the door: :func:`register_obligation` never raises; the
    door must never be blocked by bookkeeping (same contract as
    ``_persist_dispatch_row``).
  - Idempotent: re-registering an existing obligation (retry / fix-forward
    with the same dispatch_id) leaves the existing record untouched — a
    fulfilled obligation is never reset to pending.
  - Atomic writes: tmp-file + ``os.replace`` (lint pattern B).
  - No central-DB write path: plain JSON files under the state dir, keeping
    ADR-007 out of scope exactly like the review_gates requests/results
    directories themselves.
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, Optional, Tuple

logger = logging.getLogger(__name__)

OBLIGATIONS_SUBDIR = Path("review_gates") / "obligations"

STATUS_PENDING = "pending"
STATUS_FULFILLED = "fulfilled"
STATUS_NOT_EXECUTABLE = "not_executable"
STATUS_FAILED = "failed"
# "unresolvable" is NOT terminal: the runner keeps retrying resolution while
# the environment may be fixed. It is distinct from "pending" (a genuine wait
# for a not-yet-opened PR) so a misconfigured obligation never reads as "not
# yet" forever (OI-1253 fix-forward).
STATUS_UNRESOLVABLE = "unresolvable"
# OI-1388: an obligation whose PR/branch reality means NOTHING can ever gate
# it any more — the PR merged/closed ungated, or the dispatch died without
# ever producing one. Deliberately NOT "fulfilled": fulfilled means "a gate
# actually reviewed this"; retired means "the window to review it is closed
# and nothing did". Every consumer that counts "reviewed" work must key off
# STATUS_FULFILLED specifically, never off TERMINAL_STATUSES membership, or a
# retired obligation would silently inflate that count.
STATUS_RETIRED = "retired"

TERMINAL_STATUSES = frozenset(
    {STATUS_FULFILLED, STATUS_NOT_EXECUTABLE, STATUS_FAILED, STATUS_RETIRED}
)

# The four distinct retirement reasons (OI-1388). The distinction is the
# information this status exists to preserve — collapsing them into one
# generic "retired" reason would throw away exactly what an audit needs.
#
# REASON_NO_PR_BRANCH_EXISTS is part of the vocabulary but is deliberately
# NEVER emitted by this fleet's automation (scripts/gate_obligation_runner.py,
# scripts/gate_obligation_retire_backlog.py): "the dispatch never produced a
# PR, but its branch is still on origin" cannot be told apart from "the
# dispatch is still running" without an age threshold, and OI-1388 forbids
# using age as the discriminator. It stays defined for a human operator who
# has out-of-band evidence the dispatch is dead (e.g. its tmux session is
# gone) to book the same honest end-state by hand.
REASON_PR_MERGED = "pr_merged"
REASON_PR_CLOSED = "pr_closed"
REASON_NO_PR_BRANCH_GONE = "no_pr_branch_gone"
REASON_NO_PR_BRANCH_EXISTS = "no_pr_branch_exists"

# Distinct sentinel gate key for explicit no-gate records (dispatch
# 20260816-gate-never-skippable). Deliberately NOT a member of the Gate enum: a
# no-gate dispatch is not declaring any gate, but the freshness scanner needs a
# stable key to group/count these records under. Using "" here would fall
# through to the per-dispatch filename in scan_gate_obligations and create a
# new key per dispatch instead of one countable bucket.
NO_GATE_KEY = "__no_gate__"

# Whole-string match: bare digits, or the PR-<digits> family. Contract slugs
# like "pr-4d" must NOT resolve to PR 4 — they are not GitHub PRs.
_PR_NUMBER_RE = re.compile(r"^(?:#|PR[- ]?#?)?(\d+)$", re.IGNORECASE)


def obligations_dir(state_dir: Path) -> Path:
    return Path(state_dir) / OBLIGATIONS_SUBDIR


def obligation_path(state_dir: Path, dispatch_id: str) -> Path:
    return obligations_dir(state_dir) / f"{dispatch_id}.json"


def pr_number_from_pr_id(pr_id: Optional[str]) -> Optional[int]:
    """Extract an integer PR number from a pr_id string.

    Accepts the shapes the fabric actually uses: ``"879"``, ``"PR-879"``,
    ``"pr879"``, ``"#879"``. Returns None when no digits are present (e.g.
    contract slugs like ``pr-4d`` — those are not GitHub PRs).
    """
    if not pr_id:
        return None
    match = _PR_NUMBER_RE.match(str(pr_id).strip())
    if match is None:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


def normalise_pr_id(pr_id: str) -> str:
    """Normalise an internal PR id for obligation matching.

    "PR-879", "pr879" and "879" all normalise to "879"; "PR-HYG-1" to "HYG-1".
    The door stores the raw spec pr_id on the obligation; every consumer joins
    that against the GitHub PR number, which may differ in case, hyphen and the
    "PR" prefix.

    Promoted here from ``pr_merge._norm_pr_id`` so the merge gate and the
    readiness report answer "which gate does this PR owe" from ONE
    implementation — a second copy is exactly how the two would start
    disagreeing about a PR's obligations.
    """
    s = (pr_id or "").strip().upper()
    if s.startswith("PR-"):
        return s[3:]
    if len(s) > 2 and s.startswith("PR") and s[2].isdigit():
        return s[2:]
    return s


def declared_gates_for_pr(state_dir: Path, pr_number: int) -> list:
    """Every review gate declared for ``pr_number``, oldest obligation first.

    Joins on the GitHub PR number the runner stamps on the obligation, falling
    back to the normalised spec ``pr_id`` for records the runner never touched.
    The ``__no_gate__`` sentinel and blank gates are excluded: they declare an
    explicit absence, not an obligation.

    Raises ValueError (via :func:`iter_obligations`) when an obligation file is
    unreadable. That is deliberate — an obligation nobody can read is the
    silent-evidence failure this whole mechanism exists to expose, so a caller
    reports it rather than reading it as "this PR owes nothing".
    """
    num = str(pr_number)
    num_forms = {normalise_pr_id(num), normalise_pr_id(f"PR-{num}")}
    gates = []
    for _path, record in iter_obligations(state_dir):
        rec_num = record.get("pr_number")
        matched = rec_num is not None and str(rec_num) == num
        if not matched:
            matched = normalise_pr_id(str(record.get("pr_id") or "")) in num_forms
        if not matched:
            continue
        gate = (record.get("gate") or "").strip()
        if gate and gate != NO_GATE_KEY:
            gates.append(gate)
    return gates


def _utc_now_iso() -> str:
    # Stdlib-only on purpose: this module is imported by the dispatch door,
    # which must never fail on a transitive scripts/-side import chain.
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _atomic_write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=path.name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, indent=2))
            fh.write("\n")
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            logger.debug("gate_obligations: tmp cleanup failed for %s", tmp_name)
        raise


def register_obligation(
    state_dir: Path,
    *,
    dispatch_id: str,
    gate: str,
    project_id: str = "",
    pr_number: Optional[int] = None,
    pr_id: Optional[str] = None,
    branch: Optional[str] = None,
    gate_requirement_resolution: Optional[Dict[str, Any]] = None,
) -> Optional[Path]:
    """Register the review-gate obligation for a door-accepted dispatch.

    Returns the obligation path, or None when the registration was skipped
    (empty gate/dispatch_id, or an OS error — this function NEVER raises;
    obligation bookkeeping must never block the door).

    Idempotent: an existing obligation for the same dispatch_id is left
    untouched, so a retry never resets a fulfilled obligation to pending.

    ``pr_id`` (the internal PR-N/PR-LABEL) is the stable join key the merge
    gate uses to find a PR's declared gate: ``pr_number`` is only derivable
    for numeric pr_ids, so alphanumeric labels (PR-HYG-1) would otherwise be
    unfindable at merge time.

    ``gate_requirement_resolution`` (OI-1462): a snapshot of what THIS
    process — the eiser — resolved for the gate-requirement config flags at
    registration time. The fulfiller process runs later, in a different
    environment, and may resolve the same flags differently;
    :func:`check_gate_requirement_mismatch` compares the two. Shape (when
    the caller attempted a capture):
    ``{"status": "captured", "flags": {"VNX_CI_GATE_REQUIRED": True}, "error": None}``
    on success, or ``{"status": "failed", "flags": None, "error": "<reason>"}``
    when the capture itself raised. THREE distinct states, never collapsed
    to two (OI-1462 residu — a Codex-gate finding on this very obligation
    mechanism): ``None`` (the default) means the caller never attempted a
    capture at all — UNKNOWN, never "no requirement"; ``status="failed"``
    means it tried and the read itself broke — a FAULT, distinguishable from
    both "unknown" and "captured false"; ``status="captured"`` means the
    snapshot is trustworthy. Collapsing "failed" into ``None`` (as an earlier
    version of this fix did) makes a broken flag-read at the eiser
    indistinguishable from "nobody ever asked" — and blinds
    :func:`check_gate_requirement_mismatch` to precisely the OI-1462 failure
    mode: the read that breaks.
    """
    gate = (gate or "").strip()
    dispatch_id = (dispatch_id or "").strip()
    if not gate or not dispatch_id:
        return None
    try:
        path = obligation_path(state_dir, dispatch_id)
        if path.exists():
            return path
        record: Dict[str, Any] = {
            "schema_version": 1,
            "kind": "review_gate_obligation",
            "dispatch_id": dispatch_id,
            "gate": gate,
            "project_id": project_id or "",
            "declared_at": _utc_now_iso(),
            "pr_number": pr_number,
            "pr_id": pr_id,
            "branch": branch,
            "status": STATUS_PENDING,
            "attempts": 0,
            "last_attempt_at": None,
            "resolved_at": None,
            "request_path": None,
            "result_path": None,
            "reason": None,
            "reason_detail": None,
            "gate_requirement_resolution": gate_requirement_resolution,
        }
        _atomic_write_json(path, record)
        return path
    except OSError as exc:
        logger.warning(
            "gate_obligations: registration failed for dispatch=%s gate=%s (non-fatal): %s",
            dispatch_id, gate, exc,
        )
        return None


def check_gate_requirement_mismatch(
    record: Dict[str, Any], *, flag: str, reader_value: bool,
) -> Optional[Dict[str, Any]]:
    """Compare a fulfiller's own resolution of ``flag`` against the value the
    obligation's writer stamped at registration time (OI-1462: the eiser and
    the vervuller run in different processes / environments and can resolve
    the SAME config flag through ``config_runtime.get_bool`` differently).

    Three input states, THREE different answers — never collapsed to two:

      1. No resolution was ever captured (``gate_requirement_resolution`` is
         absent/not a dict, or its ``status`` field is missing/unrecognised):
         returns None. UNKNOWN, never "no requirement" — this never
         manufactures a mismatch (or a match) out of missing data.
      2. The writer's OWN capture attempt failed (``status == "failed"``):
         returns a ``"kind": "writer_capture_failed"`` finding. This is
         DISTINCT from case 1 — the writer tried and its own flag-read broke,
         which is exactly the OI-1462 failure mode this function exists to
         surface, not silence. No value comparison is possible (there is
         nothing trustworthy to compare against), but the finding itself is
         not nothing: it says the obligation's requirement was never
         reliably established.
      3. The writer captured a real value (``status == "captured"``) and it
         either matches the reader's (returns None — genuine agreement) or
         diverges (returns a ``"kind": "value_mismatch"`` finding with both
         values).

    Returns a dict — never a bare bool — so the caller has concrete detail to
    log and persist instead of a silent skip, for either finding kind.
    """
    resolution = record.get("gate_requirement_resolution")
    if not isinstance(resolution, dict):
        return None
    status = resolution.get("status")
    if status == "failed":
        return {
            "flag": flag,
            "kind": "writer_capture_failed",
            "writer_error": resolution.get("error"),
            "detected_at": _utc_now_iso(),
        }
    if status != "captured":
        return None
    flags = resolution.get("flags")
    if not isinstance(flags, dict) or flag not in flags:
        return None
    writer_value = bool(flags[flag])
    reader_value = bool(reader_value)
    if writer_value == reader_value:
        return None
    return {
        "flag": flag,
        "kind": "value_mismatch",
        "writer_value": writer_value,
        "reader_value": reader_value,
        "detected_at": _utc_now_iso(),
    }


def register_no_gate_obligation(
    state_dir: Path,
    *,
    dispatch_id: str,
    project_id: str = "",
    pr_number: Optional[int] = None,
    pr_id: Optional[str] = None,
    branch: Optional[str] = None,
    reason: str = "no_gate_declared",
    reason_detail: str = "",
) -> Optional[Path]:
    """Record an explicit no-gate decision for a door-accepted dispatch.

    The inverse of :func:`register_obligation`: a read-only dispatch
    legitimately runs without a review gate, but "no gate" must still be an
    explicit, countable record rather than a silent absence. This writes a
    terminal no-gate obligation (status ``not_executable``, resolved at write
    time) under the ``NO_GATE_KEY`` sentinel, so the producer-freshness
    scanner groups every no-gate dispatch under one countable key instead of a
    new key per dispatch.

    Never raises; idempotent like :func:`register_obligation`.
    """
    dispatch_id = (dispatch_id or "").strip()
    if not dispatch_id:
        return None
    try:
        path = obligation_path(state_dir, dispatch_id)
        if path.exists():
            return path
        now = _utc_now_iso()
        record: Dict[str, Any] = {
            "schema_version": 1,
            "kind": "review_gate_obligation",
            "dispatch_id": dispatch_id,
            "gate": NO_GATE_KEY,
            "project_id": project_id or "",
            "declared_at": now,
            "pr_number": pr_number,
            "pr_id": pr_id,
            "branch": branch,
            "status": STATUS_NOT_EXECUTABLE,
            "attempts": 0,
            "last_attempt_at": None,
            "resolved_at": now,
            "request_path": None,
            "result_path": None,
            "reason": reason,
            "reason_detail": reason_detail,
            "no_gate": True,
        }
        _atomic_write_json(path, record)
        return path
    except OSError as exc:
        logger.warning(
            "gate_obligations: no-gate registration failed for dispatch=%s (non-fatal): %s",
            dispatch_id, exc,
        )
        return None


def iter_obligations(state_dir: Path) -> Iterator[Tuple[Path, Dict[str, Any]]]:
    """Yield (path, record) for every readable obligation, sorted by name.

    Unreadable files raise ValueError — an obligation that cannot be read is
    exactly the class of silent-evidence failure this mechanism exists to
    expose, so callers (the freshness scanner) surface it as a finding
    instead of skipping it.
    """
    root = obligations_dir(state_dir)
    if not root.is_dir():
        return
    for entry in sorted(root.glob("*.json")):
        if not entry.is_file():
            continue
        try:
            record = json.loads(entry.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"unreadable gate obligation {entry.name}: {exc}") from exc
        if not isinstance(record, dict):
            raise ValueError(f"gate obligation {entry.name} is not a JSON object")
        yield entry, record


def update_obligation(path: Path, **fields: Any) -> Dict[str, Any]:
    """Atomically merge ``fields`` into an existing obligation record.

    Returns the updated record. Raises OSError/ValueError on unreadable or
    malformed existing content — the runner treats that as a loud failure,
    never a skip.
    """
    path = Path(path)
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot update unreadable gate obligation {path.name}: {exc}") from exc
    if not isinstance(record, dict):
        raise ValueError(f"gate obligation {path.name} is not a JSON object")
    record.update(fields)
    _atomic_write_json(path, record)
    return record


__all__ = [
    "OBLIGATIONS_SUBDIR",
    "STATUS_PENDING",
    "STATUS_FULFILLED",
    "STATUS_NOT_EXECUTABLE",
    "STATUS_FAILED",
    "STATUS_UNRESOLVABLE",
    "STATUS_RETIRED",
    "REASON_PR_MERGED",
    "REASON_PR_CLOSED",
    "REASON_NO_PR_BRANCH_GONE",
    "REASON_NO_PR_BRANCH_EXISTS",
    "TERMINAL_STATUSES",
    "NO_GATE_KEY",
    "obligations_dir",
    "obligation_path",
    "pr_number_from_pr_id",
    "register_obligation",
    "register_no_gate_obligation",
    "check_gate_requirement_mismatch",
    "iter_obligations",
    "update_obligation",
]
