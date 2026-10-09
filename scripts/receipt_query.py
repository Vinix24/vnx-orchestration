#!/usr/bin/env python3
"""receipt_query.py — the receipt-v2 query interface (ADR-035 §5).

  open-outcomes      — the dispatches of this project whose outcome is reject
                        or investigate and that no T0 has decided on yet
                        (fabric-state-herstel D4b, ``open_outcomes``). Replaces
                        the byte-cursor ``pull``: nothing is consumed, so two
                        T0 sessions see the same open points, and a point
                        leaves the list only through a recorded decision.
  decide             — record a T0 decision (accept/reject) about one dispatch
                        in ``t0_decision_log.jsonl`` through
                        ``t0_decision_log.write_decision`` (append under flock).
  by-dispatch        — thin, project-scoped wrapper over
                        receipt_provenance.find_receipts_by_dispatch. No
                        reimplementation.
  by-pr, since       — new, linear-scan-with-predicate over ``pr_id``/``timestamp``
                        (§5.2 — no new index or SQLite projection, §8 non-goal).
  by-track           — NOT a linear scan: the receipt carries no track_id field
                        (§4). A two-step join instead, reusing existing code:
                        (1) the same ``dispatch_id FROM dispatches WHERE track = ?
                        AND project_id = ?`` query ``tracks.get_recent_receipts``
                        already runs; (2) ``find_receipts_by_dispatch`` per
                        resolved dispatch_id. No new index, no receipt-shape change.

  Every subcommand is project-scoped (ADR-007): ``--project-id`` (default:
  derived from ``--state-dir``, else ``$VNX_PROJECT_ID``, else the command
  refuses; there is no default project) leaves out a receipt stamped with
  another project's ``project_id``, while a line without one counts as this
  project's own (``receipt_outcome.is_foreign_project``). Dispatch ids and PR
  numbers collide across projects, so an unscoped lookup could show a T0 the
  wrong status for its own dispatch or PR.
  digest             — per-dispatch outcome counts (accept/investigate/reject/
                        superseded, via receipt_outcome.summarize) over a window,
                        bookkeeping and test noise as separate line counts,
                        the top ``warnings[]`` codes at destination:"counted",
                        a tally — "N warnings met oi_pending zonder
                        resolutie" (§6.4), computed as a dedup_key join against
                        the CURRENT open-items store, never a rewrite of the
                        (immutable) receipt line, and a further
                        ``oi_pending_escalated_count`` of those unresolved
                        entries already past ``--max-age-days`` — the same
                        read-only age check reconcile-oi-pending applies,
                        computed here without attempting any write, so an
                        entry that keeps failing shows up every time digest
                        runs, not just when someone happens to invoke
                        reconcile-oi-pending directly (§9 PR-8 fold-in).
  reconcile-oi-pending — scans the ledger for still-unresolved oi_pending
                        warnings and retries add_item_programmatic per entry
                        using the preserved dedup_key (§6.4). Project-scoped:
                        a warning on another project's receipt is never filed
                        in this project's store, nor counted here. A real
                        add_item_programmatic failure is counted in ``failed``
                        (distinct from ``still_pending``'s missing-code skips)
                        and logged to stderr — never a bare swallow (codex
                        finding folded in from PR-7, §9 PR-8) — while staying
                        best-effort (one bad entry never crashes the scan). An
                        entry whose originating receipt is older than
                        --max-age-days and still fails is reported as
                        escalated — surfaced via digest's
                        oi_pending_escalated_count, not a new alerting
                        channel.

Every subcommand tolerates a mixed v1/v2 ledger: a line missing
``schema_version`` is a v1 line, read like any other JSON object — never a
reason to crash. A ``schema_version``-absent or ``verdict``-absent line buckets
under an explicit ``"unknown"`` verdict in ``digest``'s per-line tally
(``line_verdict_counts``), never crashing or being silently miscounted as a
real verdict (§5.2).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR / "lib") not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR / "lib"))
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from receipt_provenance import find_receipts_by_dispatch as _find_receipts_by_dispatch
from receipt_outcome import OUTCOME_READER_EPOCH, is_foreign_project, summarize as summarize_outcomes
from open_outcomes import (
    DECISION_LOG_NAME,
    OUTCOME_DECISIONS,
    build_open_outcomes,
    iter_complete_lines,
    record_outcome_decision,
)
from open_outcomes_recount import add_recount
from vnx_paths import project_id_from_state_dir

LEDGER_NAME = "t0_receipts.ndjson"
RUNTIME_COORDINATION_DB_NAME = "runtime_coordination.db"
DEFAULT_DIGEST_WINDOW = "24h"
DEFAULT_RECONCILE_MAX_AGE_DAYS = 7.0

_open_items_manager_cache: Optional[Any] = None


def _ledger_path(state_dir: Path) -> Path:
    return state_dir / LEDGER_NAME


def _iter_ledger(ledger_path: Path):
    """Yield each parsed JSON object in ``ledger_path``. Missing file -> no
    iterations. A blank or malformed line is skipped, never raised — the same
    tolerance ``find_receipts_by_dispatch`` already applies (§5.2: every
    subcommand must handle a mixed v1/v2 ledger without crashing)."""
    if not ledger_path.exists():
        return
    with ledger_path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def _parse_iso8601(value: Any) -> Optional[datetime]:
    """Best-effort ISO8601 parse, tolerant of a trailing ``Z``. Returns None
    (never raises) for anything that isn't a parseable timestamp string — a
    receipt missing/mangling ``timestamp`` must not crash a scan."""
    if not isinstance(value, str) or not value.strip():
        return None
    v = value.strip()
    if v.endswith("Z"):
        v = v[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(v)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def find_receipts_by_dispatch(
    ledger_path: Path,
    dispatch_id: str,
    project_id: str,
) -> List[Dict[str, Any]]:
    """`by-dispatch` — the project-scoped wrapper over
    ``receipt_provenance.find_receipts_by_dispatch`` (ADR-007).

    Dispatch ids are not unique across projects, so a bare lookup can surface
    another project's receipt and show a T0 the wrong status. The shared
    ``receipt_outcome.is_foreign_project`` test scopes the result: a line
    stamped with another project is left out, a line without ``project_id``
    belongs to the ledger's project. ``project_id`` is required: no caller
    gets a silent default project."""
    return _find_receipts_by_dispatch(ledger_path, dispatch_id, project_id=project_id)


def find_receipts_by_pr(
    ledger_path: Path,
    pr_id: str,
    project_id: str,
) -> List[Dict[str, Any]]:
    """`by-pr` (ADR-035 §5.2) — linear scan-with-predicate over ``pr_id``, the
    same approach ``find_receipts_by_dispatch`` already uses (no new index —
    §8 non-goal). Mixed v1/v2 tolerant: both shapes carry ``pr_id``.

    Scoped to ``project_id`` (ADR-007): PR numbers are not unique across
    projects, so another project's receipt for the same PR is left out. A line
    without ``project_id`` counts as this project's own."""
    matches: List[Dict[str, Any]] = []
    target = str(pr_id)
    for entry in _iter_ledger(ledger_path):
        if not isinstance(entry, dict) or is_foreign_project(entry, project_id):
            continue
        entry_pr = entry.get("pr_id")
        if entry_pr is not None and str(entry_pr) == target:
            matches.append(entry)
    return matches


def find_receipts_since(
    ledger_path: Path,
    since_iso: str,
    project_id: str,
) -> List[Dict[str, Any]]:
    """`since` (ADR-035 §5.2) — linear scan-with-predicate over ``timestamp``
    (v2 and legacy v1 both carry it — §3.2). Raises ValueError only for an
    unparseable ``since_iso`` argument itself; a receipt line with a missing
    or unparseable ``timestamp`` is skipped, never a reason to crash the scan.

    Scoped to ``project_id`` (ADR-007): another project's receipts are left
    out even when their timestamp is in range; a line without ``project_id``
    counts as this project's own."""
    threshold = _parse_iso8601(since_iso)
    if threshold is None:
        raise ValueError(f"invalid ISO8601 timestamp: {since_iso!r}")

    matches: List[Dict[str, Any]] = []
    for entry in _iter_ledger(ledger_path):
        if not isinstance(entry, dict) or is_foreign_project(entry, project_id):
            continue
        ts = _parse_iso8601(entry.get("timestamp"))
        if ts is not None and ts >= threshold:
            matches.append(entry)
    return matches


def _dispatch_ids_for_track(state_dir: Path, track_id: str, project_id: str) -> List[str]:
    """The `dispatches WHERE track = ? AND project_id = ?` half of `by-track`'s
    join (ADR-035 §5.2) — the identical query ``tracks.get_recent_receipts``
    (scripts/lib/tracks.py) already runs today. Returns `[]`, never raises,
    when the state DB is missing, predates the `track` column, or has no
    `dispatches` table at all (T26) — `PRAGMA table_info` on an absent table
    returns an empty result set rather than erroring, so the missing-table and
    missing-column cases collapse into the same check.
    """
    db_path = Path(state_dir) / RUNTIME_COORDINATION_DB_NAME
    if not db_path.exists():
        return []
    try:
        conn = sqlite3.connect(str(db_path))
    except sqlite3.Error:
        return []
    try:
        has_track_column = any(
            row[1] == "track" for row in conn.execute("PRAGMA table_info(dispatches)")
        )
        if not has_track_column:
            return []
        try:
            rows = conn.execute(
                "SELECT dispatch_id FROM dispatches WHERE track = ? AND project_id = ?",
                (track_id, project_id),
            ).fetchall()
        except sqlite3.Error:
            return []
        return [row[0] for row in rows]
    finally:
        conn.close()


def find_receipts_by_track(
    state_dir: Path,
    ledger_path: Path,
    track_id: str,
    project_id: str,
) -> List[Dict[str, Any]]:
    """`by-track` (ADR-035 §5.2) — NOT a linear scan (the receipt carries no
    `track_id` field, §4/§8). A two-step join reusing existing code: resolve
    `dispatch_id`s for (track_id, project_id) via the same query
    `tracks.get_recent_receipts` already runs, then wrap
    `find_receipts_by_dispatch` per dispatch_id — the same project-scoped
    lookup `by-dispatch` already wraps. The project filter applies to BOTH
    halves: the dispatch-id lookup (SQLite) and the receipts returned for
    those ids (the ledger can hold a colliding dispatch id for another
    project). No new index, no receipt-shape change. Returns `[]`, never
    raises, when the track has no dispatches or the state DB predates the
    `track` column (T26)."""
    dispatch_ids = _dispatch_ids_for_track(state_dir, track_id, project_id)
    receipts: List[Dict[str, Any]] = []
    for dispatch_id in dispatch_ids:
        receipts.extend(find_receipts_by_dispatch(ledger_path, dispatch_id, project_id))
    return receipts


def _parse_window(window_str: str) -> timedelta:
    """Parse a `--window` value like `24h`/`30m`/`7d`/`3600s` into a timedelta."""
    match = re.fullmatch(r"(\d+)([smhd])", (window_str or "").strip())
    if not match:
        raise ValueError(
            f"invalid --window value: {window_str!r} (expected e.g. '24h', '30m', '7d', '3600s')"
        )
    amount, unit = int(match.group(1)), match.group(2)
    unit_seconds = {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]
    return timedelta(seconds=amount * unit_seconds)


def _load_open_items_manager() -> Any:
    """Lazy, cached import of the real `open_items_manager` module — mirrors
    `append_receipt_internals.common._get_open_items_manager`'s pattern.
    Callers that need test isolation (a per-test STATE_DIR) pass their own
    `open_items_manager_module` instead of relying on this cache."""
    global _open_items_manager_cache
    if _open_items_manager_cache is None:
        import open_items_manager as _oim  # noqa: PLC0415

        _open_items_manager_cache = _oim
    return _open_items_manager_cache


def _unresolved_oi_pending(
    entries: List[Dict[str, Any]],
    open_items_manager_module: Optional[Any],
) -> List[Dict[str, Any]]:
    """ADR-035 §6.4: an `oi_pending` warning is "resolved" the moment a
    matching open item exists in the CURRENT open-items store, joined by
    `dedup_key` (the warning's `code` — `warning_destination.dedup_key_for`)
    — never by re-reading the (immutable) receipt line, which never changes.
    `entries` is a list of `{dispatch_id, code}` dicts pulled from `warnings[]`
    entries with `destination == "oi_pending"`."""
    if not entries:
        return []
    oim = open_items_manager_module or _load_open_items_manager()
    data = oim.load_items()
    unresolved = []
    for entry in entries:
        dedup_key = entry.get("code") or ""
        if not dedup_key or oim._find_by_dedup_key(data, dedup_key) is None:
            unresolved.append(entry)
    return unresolved


def _escalated_oi_pending(
    unresolved: List[Dict[str, Any]],
    now: datetime,
    max_age_days: float,
) -> List[Dict[str, Any]]:
    """§6.4's escalation rule, applied read-only: an unresolved `oi_pending`
    entry (already computed by `_unresolved_oi_pending` — still no matching
    open item in the store) whose ORIGINATING receipt is older than
    `max_age_days` has been failing to reconcile long enough to escalate.
    No new retry-count store — age is measured from the receipt already on
    disk (the same rule `reconcile_oi_pending` applies), so this never needs
    to actually attempt `add_item_programmatic` to know an entry is stuck."""
    escalated: List[Dict[str, Any]] = []
    for entry in unresolved:
        age_days = _age_in_days(entry.get("timestamp"), now)
        if age_days is not None and age_days >= max_age_days:
            escalated.append({**entry, "age_days": round(age_days, 2)})
    return escalated


def compute_digest(
    ledger_path: Path,
    *,
    window: str = DEFAULT_DIGEST_WINDOW,
    now: Optional[datetime] = None,
    max_age_days: float = DEFAULT_RECONCILE_MAX_AGE_DAYS,
    open_items_manager_module: Optional[Any] = None,
    project_id: str,
) -> Dict[str, Any]:
    """`digest` (ADR-035 §5.2/§6.4): verdict counts PER DISPATCH
    (accept/investigate/reject/superseded, plus `unknown` for a dispatch with
    only evidence) over `window`, read by `receipt_outcome.summarize` — one
    outcome per dispatch, bookkeeping and test noise counted apart
    (fabric-state-herstel D3). The old per-line stamp tally stays visible as
    `line_verdict_counts` so a reader can see the delta, and
    `outcome_reader_epoch` dates the switch (ADR-005: the ledger is
    untouched). Then the top `warnings[]` codes at `destination: "counted"`, and
    the "N warnings met oi_pending zonder resolutie" tally — plus, per §6.4's
    follow-up obligation ("surfaced via T0 digest, not a new alerting
    channel"), an `oi_pending_escalated_count` tally of unresolved entries
    already past `max_age_days` (`reconcile-oi-pending`'s own escalation
    threshold, §9 PR-8 — the failure isn't a one-off logged-and-forgotten
    event, it stays visible here every time digest runs until it resolves).

    `window` scopes the verdict counts, the counted-warning codes, and the
    `oi_pending_unresolved` tally — but escalation is computed over EVERY
    `oi_pending` warning in the ledger, regardless of `window`. An entry
    older than `window` still needs to escalate once it passes `max_age_days`;
    scoping escalation to `window` would mean an entry old enough to fall out
    of the window could never be escalated, defeating the age-based rule.

    A line missing `schema_version`/`verdict` entirely (legacy v1, or any
    line the writer never stamped a verdict onto) buckets under an explicit
    `"unknown"` bucket of `line_verdict_counts` — never crashes, never
    silently miscounted as a real verdict (T17). `open_items_manager_module`
    is a test-injection seam (mirrors `warning_destination.assign_destination`'s own seam);
    production callers rely on the default (the real on-disk OI store).
    """
    delta = _parse_window(window)
    now = now or datetime.now(timezone.utc)
    cutoff = now - delta

    verdict_counts: Dict[str, int] = {"accept": 0, "investigate": 0, "reject": 0, "unknown": 0}
    entries: List[Dict[str, Any]] = []
    counted_codes: Dict[str, int] = {}
    oi_pending_window_candidates: List[Dict[str, Any]] = []
    oi_pending_all_candidates: List[Dict[str, Any]] = []

    for entry in _iter_ledger(ledger_path):
        if not isinstance(entry, dict):
            continue  # a JSON line that is not an object is no receipt
        entries.append(entry)  # summarize_outcomes counts a foreign line as noise itself
        if is_foreign_project(entry, project_id):
            continue
        ts = _parse_iso8601(entry.get("timestamp"))
        in_window = ts is not None and ts >= cutoff

        for warning in entry.get("warnings") or []:
            if not isinstance(warning, dict):
                continue
            destination = warning.get("destination")
            code = str(warning.get("code") or "")
            if destination == "oi_pending":
                candidate = {
                    "dispatch_id": entry.get("dispatch_id"),
                    "code": code,
                    "timestamp": entry.get("timestamp"),
                }
                oi_pending_all_candidates.append(candidate)
                if in_window:
                    oi_pending_window_candidates.append(candidate)
            elif destination == "counted" and code and in_window:
                counted_codes[code] = counted_codes.get(code, 0) + 1

        if not in_window:
            continue

        verdict = entry.get("verdict")
        decision = verdict.get("decision") if isinstance(verdict, dict) else None
        if decision not in ("accept", "investigate", "reject"):
            decision = "unknown"
        verdict_counts[decision] += 1

    # escalation reads the full (window-independent) set; the tally below
    # filters that same result down to the window-scoped candidates so the
    # OI store is only ever loaded once per digest call.
    all_unresolved = _unresolved_oi_pending(oi_pending_all_candidates, open_items_manager_module)
    unresolved = [entry for entry in all_unresolved if entry in oi_pending_window_candidates]
    escalated = _escalated_oi_pending(all_unresolved, now, max_age_days)
    top_counted = sorted(counted_codes.items(), key=lambda kv: (-kv[1], kv[0]))
    outcomes = summarize_outcomes(entries, project_id=project_id, cutoff=cutoff)

    return {
        "window": window,
        "project_id": project_id,
        "outcome_reader_epoch": OUTCOME_READER_EPOCH,
        "verdict_counts": outcomes["verdict_counts"],
        "line_verdict_counts": verdict_counts,
        "bookkeeping_counts": outcomes["bookkeeping_counts"],
        "noise_counts": outcomes["noise_counts"],
        "unlinked_gate_evidence": outcomes["unlinked_gate_evidence"],
        "outcomes": outcomes["outcomes"],
        "counted_warnings": [{"code": code, "count": count} for code, count in top_counted],
        "oi_pending_unresolved_count": len(unresolved),
        "oi_pending_unresolved": unresolved,
        "oi_pending_escalated_count": len(escalated),
        "oi_pending_escalated": escalated,
    }


def _age_in_days(timestamp: Any, now: datetime) -> Optional[float]:
    ts = _parse_iso8601(timestamp)
    if ts is None:
        return None
    return (now - ts).total_seconds() / 86400.0


def reconcile_oi_pending(
    ledger_path: Path,
    *,
    max_age_days: float = DEFAULT_RECONCILE_MAX_AGE_DAYS,
    now: Optional[datetime] = None,
    open_items_manager_module: Optional[Any] = None,
    project_id: str,
) -> Dict[str, Any]:
    """`reconcile-oi-pending` (ADR-035 §6.4): scans the ledger for every
    `warnings[]` entry with `destination == "oi_pending"` and retries
    `add_item_programmatic` per entry, using the preserved `dedup_key` (the
    warning's `code`). `add_item_programmatic` is itself dedup-safe — an
    already-resolved entry (a matching item created by an earlier reconcile,
    or by a concurrent writer) just returns the existing item id — so this
    never double-creates. Never rewrites the (immutable) receipt line; the
    only observable effect is the open-items store gaining a matching item,
    which `digest`'s oi_pending tally reads at query time (T34).

    ``project_id`` scopes the scan (ADR-007): a warning on a receipt stamped
    with another project is not this project's to reconcile — it is skipped
    before it can be counted, so it never creates an open item in this
    project's store and never counts as scanned/reconciled/failed here. A
    line without ``project_id`` counts as this project's own.

    An entry whose ORIGINATING receipt's own `timestamp` is older than
    `max_age_days` and still fails to promote is reported as escalated in the
    return value — surfaced via `digest`'s `oi_pending_escalated_count`
    (computed independently, read-only, by `_escalated_oi_pending`), not a
    new alerting channel (§6.4). No new retry-count store: age is measured
    from the receipt already on disk, not a separately persisted attempt
    counter.

    A real `add_item_programmatic` failure (store unreachable/locked, raises)
    is never a bare log-and-forget: it is counted in `failed` (distinct from
    `still_pending`'s missing-`code` skips, so a caller can tell "nothing to
    do" from "this genuinely failed"), and a one-line diagnostic is written to
    stderr — this scan stays best-effort (it must never crash on one bad
    entry), but a failure must be visible, not silently swallowed (codex
    finding folded in from PR-7, §9 PR-8).
    """
    now = now or datetime.now(timezone.utc)
    oim = open_items_manager_module or _load_open_items_manager()

    pending_entries: List[Tuple[Dict[str, Any], Dict[str, Any]]] = []
    for receipt in _iter_ledger(ledger_path):
        if not isinstance(receipt, dict) or is_foreign_project(receipt, project_id):
            continue
        for warning in receipt.get("warnings") or []:
            if isinstance(warning, dict) and warning.get("destination") == "oi_pending":
                pending_entries.append((receipt, warning))

    reconciled = 0
    still_pending = 0
    failed = 0
    escalated: List[Dict[str, Any]] = []

    for receipt, warning in pending_entries:
        code = str(warning.get("code") or "")
        if not code:
            still_pending += 1
            continue

        dispatch_id = str(receipt.get("dispatch_id") or "")
        message = warning.get("message") or ""
        try:
            oim.add_item_programmatic(
                title=message or code,
                severity=warning.get("severity"),
                dispatch_id=dispatch_id,
                report_path=str(receipt.get("report_path") or ""),
                pr_id=str(receipt.get("pr_id") or ""),
                details=message,
                dedup_key=code,
                source="reconcile-oi-pending",
            )
            reconciled += 1
        except Exception as exc:  # noqa: BLE001 — best-effort scan must never crash on one
            # bad entry, but the failure must be counted and logged, never a
            # silent swallow (codex finding folded in from PR-7, §9 PR-8).
            still_pending += 1
            failed += 1
            print(
                f"reconcile-oi-pending: FAILED to promote dedup_key={code!r} "
                f"dispatch_id={dispatch_id!r}: {exc}",
                file=sys.stderr,
            )
            age_days = _age_in_days(receipt.get("timestamp"), now)
            if age_days is not None and age_days >= max_age_days:
                escalated.append({
                    "dispatch_id": dispatch_id,
                    "code": code,
                    "age_days": round(age_days, 2),
                    "error": str(exc),
                })

    return {
        "project_id": project_id,
        "scanned": len(pending_entries),
        "reconciled": reconciled,
        "still_pending": still_pending,
        "failed": failed,
        "escalated": escalated,
    }


def _format_receipt(r: Dict[str, Any]) -> str:
    term = r.get("terminal_id", "?")
    did = r.get("dispatch_id", "?")
    status = r.get("status", "?")
    schema_version = r.get("schema_version", 1)
    pr = r.get("pr_id") or "-"
    return f"  {term} {did} [{status}] schema_version={schema_version} pr={pr}"


def _project_id_for(args: argparse.Namespace, state_dir: Path) -> str:
    """The project a state dir belongs to: ``--project-id``, else derived from
    the state dir, else ``VNX_PROJECT_ID``. Empty when none is known — never a
    default project, so a decision cannot land under the wrong one (ADR-007)."""
    return (
        (args.project_id or "").strip()
        or project_id_from_state_dir(state_dir)
        or os.environ.get("VNX_PROJECT_ID", "").strip()
    )


def _resolve_project(args: argparse.Namespace, state_dir: Path) -> Optional[str]:
    """The project of a subcommand, or None after printing the refusal."""
    project_id = _project_id_for(args, state_dir)
    if not project_id:
        print("error: no project id (pass --project-id or set VNX_PROJECT_ID)", file=sys.stderr)
        return None
    return project_id


def _recounted(result: Dict[str, Any], state_dir: Path, project_id: str,
               limit: Optional[int]) -> Dict[str, Any]:
    """``result`` (built without a limit) with the fresh reading per item and the
    summary over all of them; ``limit`` then trims what is shown. Read-only."""
    receipts = list(iter_complete_lines(state_dir / LEDGER_NAME))
    counted = add_recount(result, receipts, project_id=project_id, data_dir=state_dir.parent)
    shown = counted["items"] if limit is None else counted["items"][:max(0, limit)]
    return dict(counted, items=shown, more=len(counted["items"]) - len(shown))


def _print_open_outcomes(result: Dict[str, Any], project_id: str) -> None:
    bo = result["by_outcome"]
    print(f"open outcomes (project={project_id}, since {result['since']}): "
          f"{result['total']} (reject={bo['reject']} investigate={bo['investigate']})")
    for item in result["items"]:
        fresh = item.get("recount", {}).get("fresh_decision")
        column = f"  fresh={fresh}" if fresh else ""
        print(f"  {item['outcome']:<11} {item['dispatch_id']}  "
              f"[{item['kind']}] {item.get('reason') or ''}{column}")
    if result["more"]:
        print(f"  ... and {result['more']} more")
    if result.get("ignored"):
        print(f"  ({result['ignored']} .md in dispatches/active/ without a [[TARGET:...]] "
              "marker ignored: no dispatch; check_active_drain.py names them)")
    for pair in result.get("recount_summary", []):
        print(f"  recount: stored={pair['stored_decision']} fresh={pair['fresh_decision']} "
              f"count={pair['count']}")


def _cmd_open_outcomes(args: argparse.Namespace) -> int:
    state_dir = Path(args.state_dir)
    project_id = _resolve_project(args, state_dir)
    if not project_id:
        return 2
    result = build_open_outcomes(
        state_dir, project_id=project_id, limit=None if args.recount else args.limit)
    if not result["available"]:
        print(f"error: {result['reason']}", file=sys.stderr)
        return 2
    if args.recount:
        result = _recounted(result, state_dir, project_id, args.limit)

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        _print_open_outcomes(result, project_id)
    return 0


def _cmd_decide(args: argparse.Namespace) -> int:
    state_dir = Path(args.state_dir)
    project_id = _resolve_project(args, state_dir)
    if not project_id:
        return 2
    if not (args.reason or "").strip():
        print("error: --reason must not be blank", file=sys.stderr)
        return 2
    record = record_outcome_decision(
        state_dir / DECISION_LOG_NAME,
        dispatch_id=args.dispatch_id,
        project_id=project_id,
        decision=args.decision,
        reason=args.reason.strip(),
    )
    if args.json:
        print(json.dumps(record, indent=2))
    else:
        print(f"recorded {record['decision']} for {record['dispatch_id']} "
              f"(project {project_id}) at {record['timestamp']}")
    return 0


def _cmd_by_dispatch(args: argparse.Namespace) -> int:
    state_dir = Path(args.state_dir)
    project_id = _resolve_project(args, state_dir)
    if not project_id:
        return 2
    ledger = _ledger_path(state_dir)
    receipts = find_receipts_by_dispatch(ledger, args.dispatch_id, project_id)

    if args.json:
        print(json.dumps(
            {
                "dispatch_id": args.dispatch_id,
                "project_id": project_id,
                "count": len(receipts),
                "receipts": receipts,
            },
            indent=2,
        ))
    else:
        print(f"{len(receipts)} receipt(s) for dispatch {args.dispatch_id} "
              f"(project {project_id}):")
        for r in receipts:
            print(_format_receipt(r))
    return 0


def _cmd_by_pr(args: argparse.Namespace) -> int:
    state_dir = Path(args.state_dir)
    project_id = _resolve_project(args, state_dir)
    if not project_id:
        return 2
    ledger = _ledger_path(state_dir)
    receipts = find_receipts_by_pr(ledger, args.pr_id, project_id)

    if args.json:
        print(json.dumps(
            {
                "pr_id": args.pr_id,
                "project_id": project_id,
                "count": len(receipts),
                "receipts": receipts,
            },
            indent=2,
        ))
    else:
        print(f"{len(receipts)} receipt(s) for pr {args.pr_id} (project {project_id}):")
        for r in receipts:
            print(_format_receipt(r))
    return 0


def _cmd_since(args: argparse.Namespace) -> int:
    state_dir = Path(args.state_dir)
    project_id = _resolve_project(args, state_dir)
    if not project_id:
        return 2
    ledger = _ledger_path(state_dir)
    try:
        receipts = find_receipts_since(ledger, args.timestamp, project_id)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(
            {
                "since": args.timestamp,
                "project_id": project_id,
                "count": len(receipts),
                "receipts": receipts,
            },
            indent=2,
        ))
    else:
        print(f"{len(receipts)} receipt(s) since {args.timestamp} (project {project_id}):")
        for r in receipts:
            print(_format_receipt(r))
    return 0


def _cmd_by_track(args: argparse.Namespace) -> int:
    state_dir = Path(args.state_dir)
    project_id = _resolve_project(args, state_dir)
    if not project_id:
        return 2
    ledger = _ledger_path(state_dir)
    receipts = find_receipts_by_track(state_dir, ledger, args.track_id, project_id)

    if args.json:
        print(json.dumps(
            {
                "track_id": args.track_id,
                "project_id": project_id,
                "count": len(receipts),
                "receipts": receipts,
            },
            indent=2,
        ))
    else:
        print(f"{len(receipts)} receipt(s) for track {args.track_id} (project {project_id}):")
        for r in receipts:
            print(_format_receipt(r))
    return 0


def _cmd_digest(args: argparse.Namespace) -> int:
    state_dir = Path(args.state_dir)
    project_id = _resolve_project(args, state_dir)
    if not project_id:
        return 2
    ledger = _ledger_path(state_dir)
    try:
        result = compute_digest(
            ledger, window=args.window, max_age_days=args.max_age_days, project_id=project_id,
        )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        vc = result["verdict_counts"]
        print(f"digest (window={result['window']}, project={result['project_id']}, "
              f"per-dispatch reader since {result['outcome_reader_epoch']}):")
        print(
            f"  per dispatch: accept={vc['accept']} investigate={vc['investigate']} "
            f"reject={vc['reject']} superseded={vc['superseded']} zonder uitkomst={vc['unknown']}"
        )
        print(f"  boekhouding (regels): {result['bookkeeping_counts']}")
        print(f"  ruis (regels): {result['noise_counts']}")
        print(f"  gate-bewijs zonder werk-dispatch: {result['unlinked_gate_evidence']}")
        print(f"  counted warnings (top codes): {result['counted_warnings']}")
        print(f"  oi_pending zonder resolutie: {result['oi_pending_unresolved_count']}")
        print(f"  oi_pending escalated (>{args.max_age_days}d, still failing): "
              f"{result['oi_pending_escalated_count']}")
    return 0


def _cmd_reconcile_oi_pending(args: argparse.Namespace) -> int:
    state_dir = Path(args.state_dir)
    project_id = _resolve_project(args, state_dir)
    if not project_id:
        return 2
    ledger = _ledger_path(state_dir)
    result = reconcile_oi_pending(
        ledger, max_age_days=args.max_age_days, project_id=project_id,
    )

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(
            f"reconcile-oi-pending (project={result['project_id']}): "
            f"scanned={result['scanned']} reconciled={result['reconciled']} "
            f"still_pending={result['still_pending']} failed={result['failed']} "
            f"escalated={len(result['escalated'])}"
        )
    return 0


def _add_outcome_parsers(sub: Any) -> None:
    """``open-outcomes`` and ``decide``: the T0's open points (fabric-state-herstel D4b)."""
    p_open = sub.add_parser(
        "open-outcomes",
        help="dispatches of this project without an outcome a T0 decided on",
    )
    p_open.add_argument("--state-dir", required=True)
    p_open.add_argument(
        "--project-id", default=None,
        help="default: derived from --state-dir, else $VNX_PROJECT_ID (ADR-007)",
    )
    p_open.add_argument(
        "--limit", type=int, default=None,
        help="list at most N items; the counts always cover all (default: all)",
    )
    p_open.add_argument(
        "--recount", action="store_true",
        help="next to the stored reading: the reading of the report with the reader on "
             "this checkout and the decision it would give (read-only)",
    )
    p_open.add_argument("--json", action="store_true")
    p_open.set_defaults(func=_cmd_open_outcomes)

    p_decide = sub.add_parser(
        "decide",
        help="record a T0 decision about a dispatch's outcome (append-only decision log)",
    )
    p_decide.add_argument("dispatch_id")
    p_decide.add_argument("decision", choices=sorted(OUTCOME_DECISIONS))
    p_decide.add_argument("--reason", required=True, help="why (must not be blank)")
    p_decide.add_argument("--state-dir", required=True)
    p_decide.add_argument(
        "--project-id", default=None,
        help="default: derived from --state-dir, else $VNX_PROJECT_ID (ADR-007)",
    )
    p_decide.add_argument("--json", action="store_true")
    p_decide.set_defaults(func=_cmd_decide)


def _add_lookup_parsers(sub: Any) -> None:
    """``by-dispatch``, ``by-pr``, ``since`` and ``by-track``: raw ledger lookups (ADR-035 §5.2)."""
    p_by_dispatch = sub.add_parser(
        "by-dispatch",
        help="all receipts for a dispatch_id (wraps receipt_provenance.find_receipts_by_dispatch)",
    )
    p_by_dispatch.add_argument("dispatch_id")
    p_by_dispatch.add_argument("--state-dir", required=True)
    p_by_dispatch.add_argument(
        "--project-id", default=None,
        help="default: derived from --state-dir, else $VNX_PROJECT_ID; receipts stamped "
             "with another project_id are left out (ADR-007)",
    )
    p_by_dispatch.add_argument("--json", action="store_true")
    p_by_dispatch.set_defaults(func=_cmd_by_dispatch)

    p_by_pr = sub.add_parser(
        "by-pr",
        help="all receipts for a pr_id (linear scan, no new index — §8 non-goal)",
    )
    p_by_pr.add_argument("pr_id")
    p_by_pr.add_argument("--state-dir", required=True)
    p_by_pr.add_argument(
        "--project-id", default=None,
        help="default: derived from --state-dir, else $VNX_PROJECT_ID; receipts stamped "
             "with another project_id are left out (ADR-007)",
    )
    p_by_pr.add_argument("--json", action="store_true")
    p_by_pr.set_defaults(func=_cmd_by_pr)

    p_since = sub.add_parser(
        "since",
        help="all receipts with timestamp >= the given ISO8601 timestamp (linear scan)",
    )
    p_since.add_argument("timestamp", help="ISO8601 timestamp, e.g. 2026-07-20T00:00:00Z")
    p_since.add_argument("--state-dir", required=True)
    p_since.add_argument(
        "--project-id", default=None,
        help="default: derived from --state-dir, else $VNX_PROJECT_ID; receipts stamped "
             "with another project_id are left out (ADR-007)",
    )
    p_since.add_argument("--json", action="store_true")
    p_since.set_defaults(func=_cmd_since)

    p_by_track = sub.add_parser(
        "by-track",
        help="all receipts for dispatches belonging to a track (SQLite join, §5.2)",
    )
    p_by_track.add_argument("track_id")
    p_by_track.add_argument("--state-dir", required=True)
    p_by_track.add_argument(
        "--project-id", default=None,
        help="default: derived from --state-dir, else $VNX_PROJECT_ID; receipts stamped "
             "with another project_id are left out (ADR-007)",
    )
    p_by_track.add_argument("--json", action="store_true")
    p_by_track.set_defaults(func=_cmd_by_track)


def _add_digest_parsers(sub: Any) -> None:
    """``digest`` and ``reconcile-oi-pending`` (ADR-035 §5.2/§6.4)."""
    p_digest = sub.add_parser(
        "digest",
        help="verdict counts + counted-warning top codes + oi_pending-unresolved/escalated tallies",
    )
    p_digest.add_argument("--state-dir", required=True)
    p_digest.add_argument("--window", default=DEFAULT_DIGEST_WINDOW)
    p_digest.add_argument(
        "--max-age-days", type=float, default=DEFAULT_RECONCILE_MAX_AGE_DAYS,
        help="same threshold as reconcile-oi-pending's escalation rule (§6.4)",
    )
    p_digest.add_argument(
        "--project-id", default=None,
        help="default: derived from --state-dir, else $VNX_PROJECT_ID; receipts stamped "
             "with another project_id are left out (ADR-007)",
    )
    p_digest.add_argument("--json", action="store_true")
    p_digest.set_defaults(func=_cmd_digest)

    p_reconcile = sub.add_parser(
        "reconcile-oi-pending",
        help="retry add_item_programmatic for unresolved oi_pending warnings (§6.4)",
    )
    p_reconcile.add_argument("--state-dir", required=True)
    p_reconcile.add_argument(
        "--max-age-days", type=float, default=DEFAULT_RECONCILE_MAX_AGE_DAYS,
        help="a still-failing entry older than this (by its receipt's own timestamp) escalates",
    )
    p_reconcile.add_argument(
        "--project-id", default=None,
        help="default: derived from --state-dir, else $VNX_PROJECT_ID; warnings on receipts "
             "stamped with another project_id are left out (ADR-007)",
    )
    p_reconcile.add_argument("--json", action="store_true")
    p_reconcile.set_defaults(func=_cmd_reconcile_oi_pending)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Receipt v2 query interface (ADR-035 §5)",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    _add_outcome_parsers(sub)
    _add_lookup_parsers(sub)
    _add_digest_parsers(sub)
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = _build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
