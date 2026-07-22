#!/usr/bin/env python3
"""receipt_query.py — the receipt-v2 pull interface (ADR-035 §5).

PR-6 (ADR-035 §9) shipped ``pull``/``by-dispatch``. This PR (§9 PR-7) adds the
rest of the interface plus the §6.4 oi_pending follow-up loop:

  pull               — the tick primitive. Absorbs receipt_pull.py's cursor
                        algorithm (parked branch feat/receipt-mailbox-delivery,
                        commit 54089155), reimplemented against current main
                        rather than resurrecting the branch: byte cursor in
                        receipt_pull_cursor.json, read-then-advance, advances
                        only past complete (newline-terminated) lines so a
                        concurrent append's partial trailing line is never
                        consumed early, resets to 0 on a truncated/rotated
                        ledger, --seed-now sets the cursor to EOF (skip the
                        backlog without deleting it), --peek reads without
                        advancing.
  by-dispatch        — thin wrapper over receipt_provenance.find_receipts_by_dispatch.
                        No reimplementation.
  by-pr, since       — new, linear-scan-with-predicate over ``pr_id``/``timestamp``
                        (§5.2 — no new index or SQLite projection, §8 non-goal).
  by-track           — NOT a linear scan: the receipt carries no track_id field
                        (§4). A two-step join instead, reusing existing code:
                        (1) the same ``dispatch_id FROM dispatches WHERE track = ?
                        AND project_id = ?`` query ``tracks.get_recent_receipts``
                        already runs; (2) ``find_receipts_by_dispatch`` per
                        resolved dispatch_id. No new index, no receipt-shape change.
  digest             — verdict counts (accept/investigate/reject) over a window,
                        the top ``warnings[]`` codes at destination:"counted",
                        and a second tally — "N warnings met oi_pending zonder
                        resolutie" (§6.4), computed as a dedup_key join against
                        the CURRENT open-items store, never a rewrite of the
                        (immutable) receipt line.
  reconcile-oi-pending — scans the ledger for still-unresolved oi_pending
                        warnings and retries add_item_programmatic per entry
                        using the preserved dedup_key (§6.4). An entry whose
                        originating receipt is older than --max-age-days and
                        still fails is reported as escalated — surfaced via
                        digest, not a new alerting channel.

Every subcommand tolerates a mixed v1/v2 ledger: a line missing
``schema_version`` is a v1 line, read like any other JSON object — never a
reason to crash. A ``schema_version``-absent or ``verdict``-absent line buckets
under an explicit ``"unknown"`` verdict in ``digest``, never crashing or being
silently miscounted as a real verdict (§5.2).
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

from receipt_provenance import find_receipts_by_dispatch  # noqa: E402

LEDGER_NAME = "t0_receipts.ndjson"
CURSOR_NAME = "receipt_pull_cursor.json"
RUNTIME_COORDINATION_DB_NAME = "runtime_coordination.db"
DEFAULT_PROJECT_ID = "vnx-dev"
DEFAULT_DIGEST_WINDOW = "24h"
DEFAULT_RECONCILE_MAX_AGE_DAYS = 7.0

_open_items_manager_cache: Optional[Any] = None


def _ledger_path(state_dir: Path) -> Path:
    return state_dir / LEDGER_NAME


def _default_cursor_path(state_dir: Path) -> Path:
    return state_dir / CURSOR_NAME


def load_cursor(cursor_path: Path) -> int:
    """Read the byte offset from ``cursor_path``. Missing or corrupt -> 0."""
    if not cursor_path.exists():
        return 0
    try:
        return int(json.loads(cursor_path.read_text(encoding="utf-8")).get("offset", 0))
    except (json.JSONDecodeError, ValueError, OSError, TypeError):
        return 0


def save_cursor(cursor_path: Path, offset: int) -> None:
    """Atomically persist ``offset`` to ``cursor_path`` (tmp write + os.replace)."""
    tmp = cursor_path.with_suffix(cursor_path.suffix + ".tmp")
    tmp.write_text(json.dumps({"offset": int(offset)}), encoding="utf-8")
    os.replace(tmp, cursor_path)


def pull_new_receipts(
    ledger_path: Path,
    cursor_offset: int = 0,
) -> Tuple[List[Dict[str, Any]], int]:
    """Read receipts appended after ``cursor_offset``. Returns ``(receipts, new_offset)``.

    Read-then-advance: ``new_offset`` only ever moves past COMPLETE
    (newline-terminated) lines, so a concurrent append's partial trailing line is
    left untouched for the next pull. A truncated/rotated ledger (smaller than the
    cursor) resets the cursor to 0. A malformed complete line is skipped, but the
    cursor still advances past it — it will never parse on a later pull either.
    Mixed v1/v2 lines are both plain JSON objects; no schema_version branching is
    needed to read them.
    """
    receipts: List[Dict[str, Any]] = []
    new_offset = cursor_offset
    if not ledger_path.exists():
        return receipts, new_offset
    if ledger_path.stat().st_size < cursor_offset:
        new_offset = 0
        cursor_offset = 0
    with open(ledger_path, "rb") as f:
        f.seek(cursor_offset)
        while True:
            raw = f.readline()
            if not raw:
                break
            if not raw.endswith(b"\n"):
                break  # incomplete trailing line (mid-append) — do not advance past it
            new_offset = f.tell()
            try:
                receipts.append(json.loads(raw.decode("utf-8")))
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue  # skip a malformed complete line; cursor already advanced past it
    return receipts, new_offset


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


def find_receipts_by_pr(ledger_path: Path, pr_id: str) -> List[Dict[str, Any]]:
    """`by-pr` (ADR-035 §5.2) — linear scan-with-predicate over ``pr_id``, the
    same approach ``find_receipts_by_dispatch`` already uses (no new index —
    §8 non-goal). Mixed v1/v2 tolerant: both shapes carry ``pr_id``."""
    matches: List[Dict[str, Any]] = []
    target = str(pr_id)
    for entry in _iter_ledger(ledger_path):
        entry_pr = entry.get("pr_id")
        if entry_pr is not None and str(entry_pr) == target:
            matches.append(entry)
    return matches


def find_receipts_since(ledger_path: Path, since_iso: str) -> List[Dict[str, Any]]:
    """`since` (ADR-035 §5.2) — linear scan-with-predicate over ``timestamp``
    (v2 and legacy v1 both carry it — §3.2). Raises ValueError only for an
    unparseable ``since_iso`` argument itself; a receipt line with a missing
    or unparseable ``timestamp`` is skipped, never a reason to crash the scan.
    """
    threshold = _parse_iso8601(since_iso)
    if threshold is None:
        raise ValueError(f"invalid ISO8601 timestamp: {since_iso!r}")

    matches: List[Dict[str, Any]] = []
    for entry in _iter_ledger(ledger_path):
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
    `find_receipts_by_dispatch` per dispatch_id — the same lookup `by-dispatch`
    already wraps. No new index, no receipt-shape change. Returns `[]`, never
    raises, when the track has no dispatches or the state DB predates the
    `track` column (T26)."""
    dispatch_ids = _dispatch_ids_for_track(state_dir, track_id, project_id)
    receipts: List[Dict[str, Any]] = []
    for dispatch_id in dispatch_ids:
        receipts.extend(find_receipts_by_dispatch(ledger_path, dispatch_id))
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


def compute_digest(
    ledger_path: Path,
    *,
    window: str = DEFAULT_DIGEST_WINDOW,
    now: Optional[datetime] = None,
    open_items_manager_module: Optional[Any] = None,
) -> Dict[str, Any]:
    """`digest` (ADR-035 §5.2/§6.4): verdict counts (accept/investigate/reject)
    over `window`, the top `warnings[]` codes at `destination: "counted"`, and
    the "N warnings met oi_pending zonder resolutie" tally.

    A line missing `schema_version`/`verdict` entirely (legacy v1, or any
    line the writer never stamped a verdict onto) buckets under an explicit
    `"unknown"` verdict count — never crashes, never silently miscounted as
    a real verdict (T17). `open_items_manager_module` is a test-injection
    seam (mirrors `warning_destination.assign_destination`'s own seam);
    production callers rely on the default (the real on-disk OI store).
    """
    delta = _parse_window(window)
    now = now or datetime.now(timezone.utc)
    cutoff = now - delta

    verdict_counts: Dict[str, int] = {"accept": 0, "investigate": 0, "reject": 0, "unknown": 0}
    counted_codes: Dict[str, int] = {}
    oi_pending_candidates: List[Dict[str, Any]] = []

    for entry in _iter_ledger(ledger_path):
        ts = _parse_iso8601(entry.get("timestamp"))
        if ts is None or ts < cutoff:
            continue

        verdict = entry.get("verdict")
        decision = verdict.get("decision") if isinstance(verdict, dict) else None
        if decision not in ("accept", "investigate", "reject"):
            decision = "unknown"
        verdict_counts[decision] += 1

        for warning in entry.get("warnings") or []:
            if not isinstance(warning, dict):
                continue
            destination = warning.get("destination")
            code = str(warning.get("code") or "")
            if destination == "counted" and code:
                counted_codes[code] = counted_codes.get(code, 0) + 1
            elif destination == "oi_pending":
                oi_pending_candidates.append({
                    "dispatch_id": entry.get("dispatch_id"),
                    "code": code,
                })

    unresolved = _unresolved_oi_pending(oi_pending_candidates, open_items_manager_module)
    top_counted = sorted(counted_codes.items(), key=lambda kv: (-kv[1], kv[0]))

    return {
        "window": window,
        "verdict_counts": verdict_counts,
        "counted_warnings": [{"code": code, "count": count} for code, count in top_counted],
        "oi_pending_unresolved_count": len(unresolved),
        "oi_pending_unresolved": unresolved,
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

    An entry whose ORIGINATING receipt's own `timestamp` is older than
    `max_age_days` and still fails to promote is reported as escalated in the
    return value — surfaced via `digest`, not a new alerting channel (§6.4).
    No new retry-count store: age is measured from the receipt already on
    disk, not a separately persisted attempt counter.
    """
    now = now or datetime.now(timezone.utc)
    oim = open_items_manager_module or _load_open_items_manager()

    pending_entries: List[Tuple[Dict[str, Any], Dict[str, Any]]] = []
    for receipt in _iter_ledger(ledger_path):
        for warning in receipt.get("warnings") or []:
            if isinstance(warning, dict) and warning.get("destination") == "oi_pending":
                pending_entries.append((receipt, warning))

    reconciled = 0
    still_pending = 0
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
        except Exception as exc:  # noqa: BLE001 — OI store failure must never crash the scan
            still_pending += 1
            age_days = _age_in_days(receipt.get("timestamp"), now)
            if age_days is not None and age_days >= max_age_days:
                escalated.append({
                    "dispatch_id": dispatch_id,
                    "code": code,
                    "age_days": round(age_days, 2),
                    "error": str(exc),
                })

    return {
        "scanned": len(pending_entries),
        "reconciled": reconciled,
        "still_pending": still_pending,
        "escalated": escalated,
    }


def _format_receipt(r: Dict[str, Any]) -> str:
    term = r.get("terminal_id", "?")
    did = r.get("dispatch_id", "?")
    status = r.get("status", "?")
    schema_version = r.get("schema_version", 1)
    pr = r.get("pr_id") or "-"
    return f"  {term} {did} [{status}] schema_version={schema_version} pr={pr}"


def _cmd_pull(args: argparse.Namespace) -> int:
    state_dir = Path(args.state_dir)
    ledger = _ledger_path(state_dir)
    cursor_path = Path(args.cursor_file) if args.cursor_file else _default_cursor_path(state_dir)

    if args.seed_now:
        eof = ledger.stat().st_size if ledger.exists() else 0
        save_cursor(cursor_path, eof)
        if args.json:
            print(json.dumps({"seeded": True, "cursor": eof}))
        else:
            print(
                f"cursor seeded to EOF ({eof} bytes) — "
                "backlog skipped (still in the ledger, auditable)."
            )
        return 0

    cursor = load_cursor(cursor_path)
    receipts, new_offset = pull_new_receipts(ledger, cursor)

    if args.json:
        print(json.dumps(
            {"count": len(receipts), "cursor": new_offset, "receipts": receipts},
            indent=2,
        ))
    else:
        print(f"{len(receipts)} new receipt(s) since cursor {cursor}:")
        for r in receipts:
            print(_format_receipt(r))

    if not args.peek and new_offset != cursor:
        save_cursor(cursor_path, new_offset)
    return 0


def _cmd_by_dispatch(args: argparse.Namespace) -> int:
    state_dir = Path(args.state_dir)
    ledger = _ledger_path(state_dir)
    receipts = find_receipts_by_dispatch(ledger, args.dispatch_id)

    if args.json:
        print(json.dumps(
            {"dispatch_id": args.dispatch_id, "count": len(receipts), "receipts": receipts},
            indent=2,
        ))
    else:
        print(f"{len(receipts)} receipt(s) for dispatch {args.dispatch_id}:")
        for r in receipts:
            print(_format_receipt(r))
    return 0


def _cmd_by_pr(args: argparse.Namespace) -> int:
    state_dir = Path(args.state_dir)
    ledger = _ledger_path(state_dir)
    receipts = find_receipts_by_pr(ledger, args.pr_id)

    if args.json:
        print(json.dumps(
            {"pr_id": args.pr_id, "count": len(receipts), "receipts": receipts},
            indent=2,
        ))
    else:
        print(f"{len(receipts)} receipt(s) for pr {args.pr_id}:")
        for r in receipts:
            print(_format_receipt(r))
    return 0


def _cmd_since(args: argparse.Namespace) -> int:
    state_dir = Path(args.state_dir)
    ledger = _ledger_path(state_dir)
    try:
        receipts = find_receipts_since(ledger, args.timestamp)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(
            {"since": args.timestamp, "count": len(receipts), "receipts": receipts},
            indent=2,
        ))
    else:
        print(f"{len(receipts)} receipt(s) since {args.timestamp}:")
        for r in receipts:
            print(_format_receipt(r))
    return 0


def _cmd_by_track(args: argparse.Namespace) -> int:
    state_dir = Path(args.state_dir)
    ledger = _ledger_path(state_dir)
    receipts = find_receipts_by_track(state_dir, ledger, args.track_id, args.project_id)

    if args.json:
        print(json.dumps(
            {
                "track_id": args.track_id,
                "project_id": args.project_id,
                "count": len(receipts),
                "receipts": receipts,
            },
            indent=2,
        ))
    else:
        print(f"{len(receipts)} receipt(s) for track {args.track_id} (project {args.project_id}):")
        for r in receipts:
            print(_format_receipt(r))
    return 0


def _cmd_digest(args: argparse.Namespace) -> int:
    state_dir = Path(args.state_dir)
    ledger = _ledger_path(state_dir)
    try:
        result = compute_digest(ledger, window=args.window)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        vc = result["verdict_counts"]
        print(f"digest (window={result['window']}):")
        print(
            f"  verdict: accept={vc['accept']} investigate={vc['investigate']} "
            f"reject={vc['reject']} unknown={vc['unknown']}"
        )
        print(f"  counted warnings (top codes): {result['counted_warnings']}")
        print(f"  oi_pending zonder resolutie: {result['oi_pending_unresolved_count']}")
    return 0


def _cmd_reconcile_oi_pending(args: argparse.Namespace) -> int:
    state_dir = Path(args.state_dir)
    ledger = _ledger_path(state_dir)
    result = reconcile_oi_pending(ledger, max_age_days=args.max_age_days)

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(
            f"scanned={result['scanned']} reconciled={result['reconciled']} "
            f"still_pending={result['still_pending']} escalated={len(result['escalated'])}"
        )
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Receipt v2 pull-model query interface (ADR-035 §5)",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_pull = sub.add_parser(
        "pull", help="tick primitive — read receipts since the cursor, advance it",
    )
    p_pull.add_argument("--state-dir", required=True)
    p_pull.add_argument(
        "--cursor-file", default=None,
        help="override the cursor file path (default: <state-dir>/receipt_pull_cursor.json)",
    )
    p_pull.add_argument(
        "--seed-now", action="store_true",
        help="set the cursor to EOF (skip the backlog; it stays on disk, auditable)",
    )
    p_pull.add_argument(
        "--peek", action="store_true",
        help="read new receipts without advancing the cursor",
    )
    p_pull.add_argument("--json", action="store_true")
    p_pull.set_defaults(func=_cmd_pull)

    p_by_dispatch = sub.add_parser(
        "by-dispatch",
        help="all receipts for a dispatch_id (wraps receipt_provenance.find_receipts_by_dispatch)",
    )
    p_by_dispatch.add_argument("dispatch_id")
    p_by_dispatch.add_argument("--state-dir", required=True)
    p_by_dispatch.add_argument("--json", action="store_true")
    p_by_dispatch.set_defaults(func=_cmd_by_dispatch)

    p_by_pr = sub.add_parser(
        "by-pr",
        help="all receipts for a pr_id (linear scan, no new index — §8 non-goal)",
    )
    p_by_pr.add_argument("pr_id")
    p_by_pr.add_argument("--state-dir", required=True)
    p_by_pr.add_argument("--json", action="store_true")
    p_by_pr.set_defaults(func=_cmd_by_pr)

    p_since = sub.add_parser(
        "since",
        help="all receipts with timestamp >= the given ISO8601 timestamp (linear scan)",
    )
    p_since.add_argument("timestamp", help="ISO8601 timestamp, e.g. 2026-07-20T00:00:00Z")
    p_since.add_argument("--state-dir", required=True)
    p_since.add_argument("--json", action="store_true")
    p_since.set_defaults(func=_cmd_since)

    p_by_track = sub.add_parser(
        "by-track",
        help="all receipts for dispatches belonging to a track (SQLite join, §5.2)",
    )
    p_by_track.add_argument("track_id")
    p_by_track.add_argument("--state-dir", required=True)
    p_by_track.add_argument(
        "--project-id", default=os.environ.get("VNX_PROJECT_ID", DEFAULT_PROJECT_ID),
    )
    p_by_track.add_argument("--json", action="store_true")
    p_by_track.set_defaults(func=_cmd_by_track)

    p_digest = sub.add_parser(
        "digest",
        help="verdict counts + counted-warning top codes + oi_pending-unresolved tally",
    )
    p_digest.add_argument("--state-dir", required=True)
    p_digest.add_argument("--window", default=DEFAULT_DIGEST_WINDOW)
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
    p_reconcile.add_argument("--json", action="store_true")
    p_reconcile.set_defaults(func=_cmd_reconcile_oi_pending)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
