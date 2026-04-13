"""Intelligence data API handlers.

Covers: patterns, injections, classifications, dispatch outcomes, transcripts.
Follows the api_token_stats / api_operator module pattern — handler functions
imported into serve_dashboard.py and wired in DashboardHandler.do_GET.
"""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path


def _sd():
    """Lazy accessor for serve_dashboard constants (avoids circular import)."""
    import serve_dashboard
    return serve_dashboard


# ---------------------------------------------------------------------------
# /api/intelligence/patterns
# ---------------------------------------------------------------------------

def _intelligence_get_patterns(params: dict) -> dict:
    """Return success_patterns and antipatterns from quality_intelligence.db."""
    try:
        raw_limit = (params.get("limit") or [None])[0]
        limit = max(1, min(int(raw_limit), 500)) if raw_limit else 50
    except (ValueError, TypeError):
        limit = 50

    sd = _sd()
    db_path: Path = sd.DB_PATH

    success_patterns: list[dict] = []
    antipatterns: list[dict] = []

    if not db_path.exists():
        return {"success_patterns": success_patterns, "antipatterns": antipatterns}

    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        try:
            rows = con.execute(
                """
                SELECT title, confidence_score, category, usage_count, last_used
                FROM success_patterns
                ORDER BY confidence_score DESC, usage_count DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            for row in rows:
                success_patterns.append({
                    "title": row["title"] or "",
                    "confidence": float(row["confidence_score"] or 0.0),
                    "category": row["category"] or "",
                    "used_count": int(row["usage_count"] or 0),
                    "last_seen": row["last_used"] or "",
                })
        except sqlite3.OperationalError:
            pass

        try:
            rows = con.execute(
                """
                SELECT title, severity, occurrence_count, last_seen
                FROM antipatterns
                ORDER BY
                    CASE severity
                        WHEN 'critical' THEN 4
                        WHEN 'high' THEN 3
                        WHEN 'medium' THEN 2
                        WHEN 'low' THEN 1
                        ELSE 0
                    END DESC,
                    occurrence_count DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            for row in rows:
                antipatterns.append({
                    "title": row["title"] or "",
                    "severity": row["severity"] or "medium",
                    "occurrence_count": int(row["occurrence_count"] or 0),
                    "last_seen": row["last_seen"] or "",
                })
        except sqlite3.OperationalError:
            pass

        con.close()
    except Exception:
        pass

    return {"success_patterns": success_patterns, "antipatterns": antipatterns}


# ---------------------------------------------------------------------------
# /api/intelligence/injections
# ---------------------------------------------------------------------------

def _intelligence_get_injections(params: dict) -> dict:
    """Return injection events from coordination_events table."""
    try:
        raw_limit = (params.get("limit") or [None])[0]
        limit = max(1, min(int(raw_limit), 500)) if raw_limit else 50
    except (ValueError, TypeError):
        limit = 50

    sd = _sd()
    db_path: Path = sd.DB_PATH
    injections: list[dict] = []

    if not db_path.exists():
        return {"injections": injections}

    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        try:
            rows = con.execute(
                """
                SELECT timestamp, dispatch_id, items_injected, items_suppressed
                FROM coordination_events
                WHERE event_type LIKE '%injection%'
                ORDER BY timestamp DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            for row in rows:
                injections.append({
                    "timestamp": row["timestamp"] or "",
                    "dispatch_id": row["dispatch_id"] or "",
                    "items_injected": int(row["items_injected"] or 0),
                    "items_suppressed": int(row["items_suppressed"] or 0),
                })
        except sqlite3.OperationalError:
            # Table may not exist yet
            pass
        con.close()
    except Exception:
        pass

    return {"injections": injections}


# ---------------------------------------------------------------------------
# /api/intelligence/classifications
# ---------------------------------------------------------------------------

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---", re.DOTALL)
_FIELD_RE = re.compile(r"^([a-z_]+)\s*:\s*(.+)$", re.MULTILINE | re.IGNORECASE)

_BOLD_FIELDS = {
    "quality_score": re.compile(r"\*\*quality[_\s]score\*\*\s*[:\-]\s*([^\n]+)", re.IGNORECASE),
    "content_type": re.compile(r"\*\*content[_\s]type\*\*\s*[:\-]\s*([^\n]+)", re.IGNORECASE),
    "complexity": re.compile(r"\*\*complexity\*\*\s*[:\-]\s*([^\n]+)", re.IGNORECASE),
    "summary": re.compile(r"\*\*summary\*\*\s*[:\-]\s*([^\n]+)", re.IGNORECASE),
}


def _parse_report_fields(text: str) -> dict[str, str]:
    """Extract classification fields from a markdown report."""
    result: dict[str, str] = {}

    fm_match = _FRONTMATTER_RE.match(text)
    if fm_match:
        for m in _FIELD_RE.finditer(fm_match.group(1)):
            key = m.group(1).lower()
            if key in ("quality_score", "content_type", "complexity", "summary"):
                result[key] = m.group(2).strip()

    for field, pattern in _BOLD_FIELDS.items():
        if field not in result:
            m = pattern.search(text)
            if m:
                result[field] = m.group(1).strip()

    return result


def _intelligence_get_classifications(params: dict) -> dict:
    """Scan unified_reports/*.md for haiku classification metadata."""
    try:
        raw_limit = (params.get("limit") or [None])[0]
        limit = max(1, min(int(raw_limit), 500)) if raw_limit else 50
    except (ValueError, TypeError):
        limit = 50

    sd = _sd()
    reports_dir: Path = sd.REPORTS_DIR
    classifications: list[dict] = []

    if not reports_dir.exists():
        return {"classifications": classifications}

    md_files = sorted(reports_dir.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
    for path in md_files[:limit]:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

        fields = _parse_report_fields(text)
        classifications.append({
            "report_file": path.name,
            "quality_score": fields.get("quality_score", ""),
            "content_type": fields.get("content_type", ""),
            "complexity": fields.get("complexity", ""),
            "summary": fields.get("summary", ""),
        })

    return {"classifications": classifications}


# ---------------------------------------------------------------------------
# /api/intelligence/dispatch-outcomes
# ---------------------------------------------------------------------------

def _intelligence_get_dispatch_outcomes(params: dict) -> dict:
    """Parse t0_receipts.ndjson for dispatch completion status."""
    try:
        raw_limit = (params.get("limit") or [None])[0]
        limit = max(1, min(int(raw_limit), 500)) if raw_limit else 50
    except (ValueError, TypeError):
        limit = 50

    sd = _sd()
    receipts_path: Path = sd.RECEIPTS_PATH
    outcomes: list[dict] = []

    if not receipts_path.exists():
        return {"outcomes": outcomes}

    try:
        lines = receipts_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return {"outcomes": outcomes}

    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue

        outcomes.append({
            "dispatch_id": record.get("dispatch_id") or "",
            "terminal": record.get("terminal") or "",
            "track": record.get("track") or "",
            "status": record.get("status") or record.get("event_type") or "",
            "timestamp": record.get("timestamp") or "",
        })

        if len(outcomes) >= limit:
            break

    return {"outcomes": outcomes}


# ---------------------------------------------------------------------------
# /api/conversations/<session_id>/transcript
# ---------------------------------------------------------------------------

_CONV_DB_PATH = Path.home() / ".claude" / "conversation-index.db"


def _intelligence_get_transcript(session_id: str) -> tuple[dict, int]:
    """Return messages for a session from conversation-index.db.

    Returns (payload, http_status_int).
    """
    if not _CONV_DB_PATH.exists():
        return {"error": "conversation-index.db not found"}, 404

    if not session_id or "/" in session_id or "\\" in session_id:
        return {"error": "invalid session_id"}, 400

    try:
        con = sqlite3.connect(f"file:{_CONV_DB_PATH}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row

        session_row = con.execute(
            "SELECT session_id FROM conversations WHERE session_id = ?",
            (session_id,),
        ).fetchone()

        if session_row is None:
            con.close()
            return {"error": "session not found", "session_id": session_id}, 404

        rows = con.execute(
            """
            SELECT role, content, timestamp
            FROM messages
            WHERE session_id = ?
            ORDER BY id ASC
            """,
            (session_id,),
        ).fetchall()
        con.close()

        messages = [
            {
                "role": row["role"] or "",
                "content": row["content"] or "",
                "timestamp": row["timestamp"] or "",
            }
            for row in rows
        ]
        return {"messages": messages}, 200

    except sqlite3.OperationalError as exc:
        return {"error": f"db error: {exc}"}, 500
    except Exception as exc:
        return {"error": str(exc)}, 500
