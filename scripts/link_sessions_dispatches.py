#!/usr/bin/env python3
"""Nightly cross-reference: link sessions, dispatches, and receipts.

Run as Phase 1.5 in conversation_analyzer_nightly.sh.
Performs three linkage passes:
  1. session_analytics.dispatch_id -> dispatch_metadata.session_id
  2. Receipts -> dispatch_metadata.outcome_status
  3. report_findings.dispatch_id from report metadata
"""

import fcntl
import json
import logging
import os
import sqlite3
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR / "lib"))
try:
    from vnx_paths import ensure_env
    from report_findings_migration import ensure_report_findings_table
    from token_harvest import CLAUDE_HARNESS_PROVIDERS
except Exception as exc:
    raise SystemExit(f"Failed to load vnx_paths, report_findings_migration, or token_harvest: {exc}")

from completion_reading import completion_status, is_v3_end_line  # noqa: E402
from append_receipt_internals.idempotency import _flock_until, _lock_file_for  # noqa: E402

logger = logging.getLogger(__name__)

# Upper bound on waiting for the receipt append lock before the nightly backfill gives up.
LEDGER_LOCK_TIMEOUT_SEC = 30.0

# ADR-045 K-2c: the completion types this pass links, both shapes — a start line
# and a report line never end a dispatch.
LINKED_COMPLETION_EVENTS = frozenset({"task_complete", "task_failed", "task_timeout",
                                      "dispatch_finished"})

PATHS = ensure_env()
STATE_DIR = Path(PATHS["VNX_STATE_DIR"])
DB_PATH = STATE_DIR / "quality_intelligence.db"
RECEIPTS_FILE = STATE_DIR / "t0_receipts.ndjson"


def link_sessions_to_dispatches(conn: sqlite3.Connection) -> int:
    """Bidirectional link: set dispatch_metadata.session_id from session_analytics."""
    cur = conn.cursor()
    cur.execute("""
        UPDATE dispatch_metadata
        SET session_id = (
            SELECT sa.session_id FROM session_analytics sa
            WHERE sa.dispatch_id = dispatch_metadata.dispatch_id
            LIMIT 1
        )
        WHERE session_id IS NULL
        AND EXISTS (
            SELECT 1 FROM session_analytics sa
            WHERE sa.dispatch_id = dispatch_metadata.dispatch_id
        )
    """)
    updated = cur.rowcount
    conn.commit()
    return updated


def link_receipts_to_dispatches(conn: sqlite3.Connection) -> int:
    """Update dispatch outcomes from receipt file."""
    if not RECEIPTS_FILE.exists():
        return 0

    cur = conn.cursor()
    linked = 0

    with open(RECEIPTS_FILE, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                receipt = json.loads(line)
            except json.JSONDecodeError:
                continue

            event_type = receipt.get("event_type") or receipt.get("event", "")
            if event_type not in LINKED_COMPLETION_EVENTS:
                continue

            dispatch_id = receipt.get("dispatch_id", "")
            if not dispatch_id:
                continue

            # A v3 end line's status is its end_state's projection, not the
            # line's own field (ADR-045 §4); a legacy receipt keeps its value.
            status = receipt.get("status", "unknown")
            if is_v3_end_line(receipt):
                status = completion_status(receipt) or "unknown"
            report_path = receipt.get("report_path", "")
            timestamp = receipt.get("timestamp", "")

            cur.execute("""
                UPDATE dispatch_metadata
                SET outcome_status = ?, outcome_report_path = ?, completed_at = ?
                WHERE dispatch_id = ? AND outcome_status IS NULL
            """, (status, report_path or None, timestamp or None, dispatch_id))
            if cur.rowcount > 0:
                linked += 1

    conn.commit()
    return linked


def link_reports_to_dispatches(conn: sqlite3.Connection) -> int:
    """Extract dispatch_id from report files and update report_findings."""
    from conversation_analyzer.parser import SessionParser  # noqa: PLC0415

    cur = conn.cursor()
    cur.execute("SELECT id, report_path FROM report_findings WHERE dispatch_id IS NULL")
    rows = cur.fetchall()

    updated = 0
    for row_id, report_path in rows:
        if not report_path:
            continue
        rp = Path(report_path)
        if not rp.exists():
            continue

        try:
            content = rp.read_text(encoding="utf-8", errors="replace")[:5000]
        except OSError:
            continue

        # Same OI-872 extraction as session parsing: first VALID dispatch ID in
        # document order (skips template placeholders, strips trailing prose
        # punctuation) instead of the old first-match-only regex.
        dispatch_id = SessionParser._extract_dispatch_id(content)
        if not dispatch_id:
            continue

        cur.execute(
            "UPDATE report_findings SET dispatch_id = ? WHERE id = ?",
            (dispatch_id, row_id)
        )
        updated += 1

    conn.commit()
    return updated


def clean_polluted_dispatch_ids(conn: sqlite3.Connection) -> int:
    """NULL out session_analytics.dispatch_id where the literal placeholder
    ``<dispatch_id>`` was stored instead of a real ID (OI-872).

    Idempotent: only matches the exact literal; real dispatch IDs
    (which always start with a date-prefix ``YYYYMMDD-``) are never
    touched.  A second run is a no-op because the matching rows already
    carry NULL.
    """
    cur = conn.cursor()
    cur.execute(
        "UPDATE session_analytics SET dispatch_id = NULL "
        "WHERE dispatch_id = '<dispatch_id>'"
    )
    cleaned = cur.rowcount
    if cleaned:
        conn.commit()
    return cleaned


def backfill_session_dispatch_ids(conn: sqlite3.Connection) -> int:
    """Re-parse transcripts of NULL-dispatch sessions and fill dispatch_id (OI-872).

    The parser fix (conversation_analyzer/parser.py) recovers dispatch IDs from
    sessions the old first-match-only extraction missed — the worker-context
    template carries ``Dispatch-ID: <dispatch_id>`` before the Dispatch Metadata
    footer with the real ID, so the old parser stopped at the rejected
    placeholder and never reached the real ID. Rows already analyzed are NOT
    re-processed by the nightly analyzer (it only imports new session_ids), so
    this phase re-parses the existing NULL rows once using the fixed
    ``SessionParser`` and fills ``dispatch_id``.

    Only ever tightens data: a row that yields no valid ID (no dispatch text,
    placeholder-only, T0 orchestration, bench-*) keeps NULL. Idempotent — rows
    already linked are skipped.

    Returns the count of sessions newly linked.
    """
    from conversation_analyzer.parser import SessionParser  # noqa: PLC0415

    cur = conn.cursor()
    cur.execute(
        "SELECT session_id, dispatch_id FROM session_analytics "
        "WHERE dispatch_id IS NULL OR dispatch_id = ''"
    )
    pending_ids = {row[0] for row in cur.fetchall()}

    # Also re-parse rows whose stored ID is polluted (trailing prose punctuation
    # like "20260613-x." that the old regex captured, e.g. 2 benchmark-review
    # sessions). The parser now strips such punctuation on extraction.
    cur.execute(
        "SELECT session_id, dispatch_id FROM session_analytics "
        "WHERE dispatch_id IS NOT NULL AND dispatch_id != ''"
    )
    for session_id, stored in cur.fetchall():
        if stored and SessionParser._validate_dispatch_id(stored) != stored:
            pending_ids.add(session_id)

    if not pending_ids:
        return 0

    # Index transcripts once: session_id -> jsonl path across ~/.claude/projects/*.
    projects_dir = Path(
        os.environ.get(
            "CLAUDE_PROJECTS_DIR", str(Path.home() / ".claude" / "projects")
        )
    )
    transcript_by_session: dict[str, Path] = {}
    if projects_dir.is_dir():
        for jsonl in projects_dir.glob("*/*.jsonl"):
            sid = jsonl.stem
            if sid in pending_ids and sid not in transcript_by_session:
                transcript_by_session[sid] = jsonl

    if not transcript_by_session:
        return 0

    parser = SessionParser()
    linked = 0
    for session_id, jsonl_path in transcript_by_session.items():
        try:
            metrics, _ = parser.parse_file(jsonl_path)
        except Exception as exc:  # noqa: BLE001 — a bad transcript must not abort the phase
            print(f"  backfill_session_dispatch_ids: parse failed {session_id[:8]}...: {exc}")
            continue
        if metrics.dispatch_id:
            cur.execute(
                "UPDATE session_analytics SET dispatch_id = ? WHERE session_id = ?",
                (metrics.dispatch_id, session_id),
            )
            linked += 1
    if linked:
        conn.commit()
    return linked


def _needs_token_backfill(receipt: dict, session_tokens: dict) -> bool:
    """True when a claude-harness receipt has no usable ``token_usage`` and a session row exists."""
    provider = receipt.get("provider", "")
    dispatch_id = receipt.get("dispatch_id", "")
    token_usage = receipt.get("token_usage")
    return bool(
        provider in CLAUDE_HARNESS_PROVIDERS
        and dispatch_id
        and (
            token_usage is None
            or (isinstance(token_usage, dict) and (
                token_usage.get("unavailable")
                or (token_usage.get("input", 0) == 0
                    and token_usage.get("output", 0) == 0)
            ))
        )
        and dispatch_id in session_tokens
    )


def _enrich_ledger_line(raw: bytes, session_tokens: dict) -> tuple[bytes | None, bool]:
    """Return ``(bytes to write, enriched)`` for one ledger line; ``None`` drops a blank line.

    A line that is not enriched comes back exactly as read, minus its line
    ending; only an enriched line is re-serialised.
    """
    body = raw.rstrip(b"\r\n")
    if not body.strip():
        return None, False
    try:
        receipt = json.loads(body)
    except ValueError:  # JSONDecodeError and UnicodeDecodeError; the line stays as it is
        return body, False
    if not isinstance(receipt, dict) or not _needs_token_backfill(receipt, session_tokens):
        return body, False
    receipt["token_usage"] = session_tokens[receipt["dispatch_id"]]
    return json.dumps(receipt, sort_keys=False, ensure_ascii=False).encode("utf-8"), True


def _rewrite_ledger_lines(session_tokens: dict) -> tuple[list[bytes], int]:
    """Read the ledger once and return its output lines plus the count of enriched ones."""
    out: list[bytes] = []
    enriched = 0
    with open(RECEIPTS_FILE, "rb") as fin:
        for raw in fin:
            data, was_enriched = _enrich_ledger_line(raw, session_tokens)
            if data is not None:
                out.append(data)
            enriched += was_enriched
    return out, enriched


def _replace_ledger(lines: list[bytes]) -> None:
    """Stage ``lines`` in a ``.tmp`` sibling and atomically rename it over the ledger."""
    tmp_path = RECEIPTS_FILE.with_suffix(".ndjson.tmp")
    try:
        with open(tmp_path, "wb") as fout:
            fout.write(b"".join(line + b"\n" for line in lines))
        os.replace(tmp_path, RECEIPTS_FILE)
    except Exception:
        # Clean up the temp file on failure — never leave a partial.
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _session_token_lookup(conn: sqlite3.Connection) -> dict[str, dict[str, int]]:
    """Map dispatch_id -> token_usage dict from session_analytics rows with any tokens."""
    cur = conn.cursor()
    cur.execute(
        "SELECT dispatch_id, total_input_tokens, total_output_tokens, "
        "       cache_creation_tokens, cache_read_tokens "
        "FROM session_analytics "
        "WHERE dispatch_id IS NOT NULL"
    )
    session_tokens: dict[str, dict[str, int]] = {}
    for did, inp, out, cc, cr in cur.fetchall():
        if any(v is not None and v > 0 for v in (inp, out, cc, cr)):
            session_tokens[did] = {
                "input": int(inp or 0),
                "output": int(out or 0),
                "cache_creation_5m": int(cc or 0),
                "cache_creation_1h": 0,
                "cache_read": int(cr or 0),
            }
    return session_tokens


def backfill_receipt_token_usage(conn: sqlite3.Connection,
                                 lock_timeout_sec: float = LEDGER_LOCK_TIMEOUT_SEC) -> int:
    """Backfill ``token_usage`` in claude-harness receipts from session_analytics (OI-1940).

    No line to enrich means no rewrite: a read-only first pass finds out, and
    nothing is written when it finds none.  The rewrite runs under the same
    ``append_receipt.lock`` every receipt writer takes, bounded by
    ``lock_timeout_sec`` (a lock not obtained logs a WARNING and returns 0),
    and re-reads the ledger under it so a receipt appended meanwhile survives.
    A line that is not enriched is written back byte for byte.

    Returns the count of enriched receipts.
    """
    if not RECEIPTS_FILE.exists():
        return 0
    session_tokens = _session_token_lookup(conn)
    if not session_tokens:
        return 0
    if _rewrite_ledger_lines(session_tokens)[1] == 0:
        return 0

    lock_path = _lock_file_for(RECEIPTS_FILE)
    with open(lock_path, "a+") as lock_handle:
        try:
            deadline = time.monotonic() + lock_timeout_sec
            if not _flock_until(lock_handle.fileno(), deadline):
                logger.warning("backfill_receipt_token_usage: %s stayed taken for %ss; "
                               "the receipt ledger was not touched", lock_path, lock_timeout_sec)
                return 0
            lines, enriched = _rewrite_ledger_lines(session_tokens)
            if enriched:
                _replace_ledger(lines)
            return enriched
        finally:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)


def main():
    if not DB_PATH.exists():
        print(f"ERROR: DB not found: {DB_PATH}")
        return 1

    conn = sqlite3.connect(DB_PATH)

    # Ensure report_findings exists even if Phase 0 (quality_db_init.py) failed.
    created = ensure_report_findings_table(conn)
    if created:
        print("  Migrated: created report_findings table (was missing)")

    print("=== Nightly Session-Dispatch Linkage ===")

    # Phase 1: Clean up placeholder-polluted dispatch_ids (OI-872).
    cleaned = clean_polluted_dispatch_ids(conn)
    print(f"  Placeholder dispatch_ids cleaned: {cleaned}")

    # Phase 2: Backfill dispatch_id for already-analyzed NULL sessions (OI-872
    # parser fix recovery — the nightly analyzer only imports new sessions).
    backfilled_dispatch_ids = backfill_session_dispatch_ids(conn)
    print(f"  Session dispatch_ids backfilled from transcripts: {backfilled_dispatch_ids}")

    # Phase 3: Link sessions to dispatches (bidirectional join).
    linked_sessions = link_sessions_to_dispatches(conn)
    print(f"  Sessions linked to dispatches: {linked_sessions}")

    # Phase 4: Link receipt outcomes to dispatches.
    linked_receipts = link_receipts_to_dispatches(conn)
    print(f"  Receipt outcomes linked: {linked_receipts}")

    # Phase 5: Link report findings to dispatches.
    linked_reports = link_reports_to_dispatches(conn)
    print(f"  Report findings linked: {linked_reports}")

    # Phase 6: Backfill receipt token_usage from session_analytics (OI-872 chain closure).
    enriched = backfill_receipt_token_usage(conn)
    print(f"  Receipts enriched with token_usage: {enriched}")

    conn.close()
    print("Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
