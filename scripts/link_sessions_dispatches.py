#!/usr/bin/env python3
"""Nightly cross-reference: link sessions, dispatches, and receipts.

Run as Phase 1.5 in conversation_analyzer_nightly.sh.
Performs three linkage passes:
  1. session_analytics.dispatch_id -> dispatch_metadata.session_id
  2. Receipts -> dispatch_metadata.outcome_status
  3. report_findings.dispatch_id from report metadata
"""

import json
import os
import sqlite3
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR / "lib"))
try:
    from vnx_paths import ensure_env
    from report_findings_migration import ensure_report_findings_table
    from token_harvest import CLAUDE_HARNESS_PROVIDERS
except Exception as exc:
    raise SystemExit(f"Failed to load vnx_paths, report_findings_migration, or token_harvest: {exc}")

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
            if event_type not in ("task_complete", "task_failed", "task_timeout"):
                continue

            dispatch_id = receipt.get("dispatch_id", "")
            if not dispatch_id:
                continue

            status = receipt.get("status", "unknown")
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


def backfill_receipt_token_usage(conn: sqlite3.Connection) -> int:
    """Backfill ``token_usage`` in claude-harness receipts from session_analytics.

    Walks ``t0_receipts.ndjson`` line by line.  For each receipt whose
    provider is in ``token_harvest.CLAUDE_HARNESS_PROVIDERS`` (claude,
    deepseek-harness, glm-harness — every lane that runs through the Claude
    Code harness) and whose ``token_usage`` is null, empty, or marked
    unavailable, looks up the matching row in ``session_analytics`` by
    ``dispatch_id``. When token data is found, the receipt's ``token_usage``
    is populated.

    Lines that need no enrichment pass through verbatim.  The rewritten
    file is staged in a ``.tmp`` sibling and atomically renamed on
    success, so a mid-write crash leaves the original file intact.

    Returns the count of enriched receipts.
    """
    if not RECEIPTS_FILE.exists():
        return 0

    # Build a lookup: dispatch_id -> token_usage dict from session_analytics.
    cur = conn.cursor()
    cur.execute(
        "SELECT dispatch_id, total_input_tokens, total_output_tokens, "
        "       cache_creation_tokens, cache_read_tokens "
        "FROM session_analytics "
        "WHERE dispatch_id IS NOT NULL"
    )
    session_tokens: dict[str, dict[str, int]] = {}
    for row in cur.fetchall():
        did, inp, out, cc, cr = row
        if any(v is not None and v > 0 for v in (inp, out, cc, cr)):
            session_tokens[did] = {
                "input": int(inp or 0),
                "output": int(out or 0),
                "cache_creation_5m": int(cc or 0),
                "cache_creation_1h": 0,
                "cache_read": int(cr or 0),
            }

    if not session_tokens:
        return 0

    tmp_path = RECEIPTS_FILE.with_suffix(".ndjson.tmp")
    enriched = 0

    try:
        with open(RECEIPTS_FILE, "r", encoding="utf-8", errors="replace") as fin, \
             open(tmp_path, "w", encoding="utf-8") as fout:
            for line in fin:
                line_stripped = line.strip()
                if not line_stripped:
                    continue
                try:
                    receipt = json.loads(line_stripped)
                except json.JSONDecodeError:
                    fout.write(line_stripped + "\n")
                    continue

                provider = receipt.get("provider", "")
                dispatch_id = receipt.get("dispatch_id", "")
                token_usage = receipt.get("token_usage")

                needs_backfill = (
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

                if needs_backfill:
                    receipt["token_usage"] = session_tokens[dispatch_id]
                    enriched += 1

                fout.write(json.dumps(receipt, sort_keys=False, ensure_ascii=False) + "\n")

        # Atomic rename.
        import os as _os
        _os.replace(tmp_path, RECEIPTS_FILE)
    except Exception:
        # Clean up the temp file on failure — never leave a partial.
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise

    return enriched


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
