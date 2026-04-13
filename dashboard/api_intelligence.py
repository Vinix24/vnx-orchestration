"""Intelligence data API handlers.

Covers: patterns, injections, classifications, dispatch outcomes, transcripts,
proposals (accept/reject/apply), confidence trends, weekly digest.
Follows the api_token_stats / api_operator module pattern — handler functions
imported into serve_dashboard.py and wired in DashboardHandler.do_GET/do_POST.
"""

from __future__ import annotations

import json
import re
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

_UTC = timezone.utc


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

# ---------------------------------------------------------------------------
# Shared helpers for proposal / digest endpoints
# ---------------------------------------------------------------------------

def _state_dir() -> Path:
    """Return the VNX state directory (parent of quality_intelligence.db)."""
    return _sd().DB_PATH.parent


def _scripts_dir() -> Path:
    """Return the scripts/ directory relative to this module's location."""
    return Path(__file__).resolve().parent.parent / "scripts"


def _load_pending_edits() -> dict:
    path = _state_dir() / "pending_edits.json"
    if not path.exists():
        return {"generated_at": "", "edits": []}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"generated_at": "", "edits": []}


def _save_pending_edits(data: dict) -> None:
    path = _state_dir() / "pending_edits.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


# ---------------------------------------------------------------------------
# /api/intelligence/proposals  (GET)
# ---------------------------------------------------------------------------

def _intelligence_get_proposals(params: dict) -> dict:
    """Return proposals from pending_edits.json."""
    data = _load_pending_edits()
    proposals = []
    for edit in data.get("edits", []):
        proposals.append({
            "id": edit.get("id"),
            "category": edit.get("category", ""),
            "proposed_change": edit.get("content", ""),
            "evidence": edit.get("evidence", ""),
            "confidence": edit.get("confidence", 0.0),
            "status": edit.get("status", "pending"),
            "suggested_at": edit.get("suggested_at", ""),
        })
    return {"proposals": proposals}


# ---------------------------------------------------------------------------
# /api/intelligence/proposals/<id>/accept  (POST)
# ---------------------------------------------------------------------------

def _intelligence_accept_proposal(proposal_id: str) -> tuple[dict, int]:
    """Mark a proposal as accepted."""
    try:
        pid = int(proposal_id)
    except (ValueError, TypeError):
        return {"error": "invalid proposal id"}, 400

    data = _load_pending_edits()
    edits = data.get("edits", [])
    matched = False
    for edit in edits:
        if edit.get("id") == pid and edit.get("status") == "pending":
            edit["status"] = "accepted"
            edit["accepted_at"] = datetime.now(tz=_UTC).isoformat().replace("+00:00", "Z")
            matched = True
            break

    if not matched:
        return {"error": f"proposal {pid} not found or not pending"}, 404

    _save_pending_edits(data)
    return {"ok": True, "id": pid, "status": "accepted"}, 200


# ---------------------------------------------------------------------------
# /api/intelligence/proposals/<id>/reject  (POST)
# ---------------------------------------------------------------------------

def _intelligence_reject_proposal(proposal_id: str, body: dict) -> tuple[dict, int]:
    """Mark a proposal as rejected."""
    try:
        pid = int(proposal_id)
    except (ValueError, TypeError):
        return {"error": "invalid proposal id"}, 400

    reason = body.get("reason", "")

    data = _load_pending_edits()
    edits = data.get("edits", [])
    matched = False
    for edit in edits:
        if edit.get("id") == pid and edit.get("status") in ("pending", "accepted"):
            edit["status"] = "rejected"
            edit["rejected_at"] = datetime.now(tz=_UTC).isoformat().replace("+00:00", "Z")
            if reason:
                edit["reject_reason"] = reason
            matched = True
            break

    if not matched:
        return {"error": f"proposal {pid} not found or already rejected"}, 404

    _save_pending_edits(data)
    return {"ok": True, "id": pid, "status": "rejected"}, 200


# ---------------------------------------------------------------------------
# /api/intelligence/proposals/apply  (POST)
# ---------------------------------------------------------------------------

def _intelligence_apply_proposals() -> tuple[dict, int]:
    """Trigger apply_suggested_edits.py apply for accepted proposals."""
    script = _scripts_dir() / "apply_suggested_edits.py"
    if not script.exists():
        return {"error": f"apply_suggested_edits.py not found at {script}"}, 500

    try:
        proc = subprocess.run(
            [sys.executable, str(script), "apply"],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except subprocess.TimeoutExpired:
        return {"error": "apply timed out"}, 500
    except OSError as exc:
        return {"error": f"subprocess error: {exc}"}, 500

    stdout = proc.stdout or ""
    stderr = proc.stderr or ""

    # Parse "Applied: N | Failed: M" from stdout
    applied = 0
    errors: list[str] = []
    m_applied = re.search(r"Applied:\s*(\d+)", stdout)
    m_failed = re.search(r"Failed:\s*(\d+)", stdout)
    if m_applied:
        applied = int(m_applied.group(1))
    failed_count = int(m_failed.group(1)) if m_failed else 0

    if proc.returncode != 0 or failed_count > 0:
        if stderr.strip():
            errors.append(stderr.strip()[:500])
        if failed_count > 0:
            errors.append(f"{failed_count} edit(s) failed to apply")

    return {"applied": applied, "errors": errors}, 200


# ---------------------------------------------------------------------------
# /api/intelligence/confidence-trends  (GET)
# ---------------------------------------------------------------------------

_SEVERITY_SCORE = {"critical": 1.0, "high": 0.75, "medium": 0.5, "low": 0.25}


def _intelligence_get_confidence_trends(params: dict) -> dict:
    """Return time-series confidence data grouped by date."""
    sd = _sd()
    db_path: Path = sd.DB_PATH
    trends: list[dict] = []

    if not db_path.exists():
        return {"trends": trends}

    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row

        # Collect success pattern confidence by date
        success_by_date: dict[str, list[float]] = {}
        try:
            rows = con.execute(
                """
                SELECT SUBSTR(last_used, 1, 10) AS day, confidence_score
                FROM success_patterns
                WHERE last_used IS NOT NULL AND last_used != ''
                ORDER BY day
                """
            ).fetchall()
            for row in rows:
                day = row["day"]
                if day:
                    success_by_date.setdefault(day, []).append(float(row["confidence_score"] or 0.0))
        except sqlite3.OperationalError:
            pass

        # Collect antipattern severity by date
        anti_by_date: dict[str, list[float]] = {}
        try:
            rows = con.execute(
                """
                SELECT SUBSTR(last_seen, 1, 10) AS day, severity
                FROM antipatterns
                WHERE last_seen IS NOT NULL AND last_seen != ''
                ORDER BY day
                """
            ).fetchall()
            for row in rows:
                day = row["day"]
                if day:
                    score = _SEVERITY_SCORE.get((row["severity"] or "medium").lower(), 0.5)
                    anti_by_date.setdefault(day, []).append(score)
        except sqlite3.OperationalError:
            pass

        con.close()
    except Exception:
        return {"trends": trends}

    all_days = sorted(set(list(success_by_date.keys()) + list(anti_by_date.keys())))
    for day in all_days:
        s_vals = success_by_date.get(day, [])
        a_vals = anti_by_date.get(day, [])
        trends.append({
            "date": day,
            "avg_success_confidence": round(sum(s_vals) / len(s_vals), 4) if s_vals else None,
            "avg_antipattern_severity": round(sum(a_vals) / len(a_vals), 4) if a_vals else None,
            "pattern_count": len(s_vals) + len(a_vals),
        })

    return {"trends": trends}


# ---------------------------------------------------------------------------
# /api/intelligence/weekly-digest  (GET)
# ---------------------------------------------------------------------------

def _intelligence_get_weekly_digest() -> tuple[dict, int]:
    """Return the latest weekly_digest.json from state dir."""
    digest_path = _state_dir() / "weekly_digest.json"
    if not digest_path.exists():
        return {"error": "weekly_digest.json not found — run scripts/weekly_digest.py to generate"}, 404

    try:
        data = json.loads(digest_path.read_text(encoding="utf-8"))
        return data, 200
    except (json.JSONDecodeError, OSError) as exc:
        return {"error": f"failed to read weekly digest: {exc}"}, 500


# ---------------------------------------------------------------------------
# /api/intelligence/weekly-digest/generate  (POST)
# ---------------------------------------------------------------------------

def _intelligence_generate_weekly_digest() -> tuple[dict, int]:
    """Run scripts/weekly_digest.py to regenerate the weekly digest."""
    script = _scripts_dir() / "weekly_digest.py"
    if not script.exists():
        return {"error": f"weekly_digest.py not found at {script}"}, 500

    try:
        proc = subprocess.run(
            [sys.executable, str(script), "--dry-run"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except subprocess.TimeoutExpired:
        return {"error": "generate timed out"}, 500
    except OSError as exc:
        return {"error": f"subprocess error: {exc}"}, 500

    if proc.returncode != 0:
        stderr = (proc.stderr or "").strip()[:500]
        return {"error": stderr or "weekly_digest.py exited non-zero"}, 500

    digest_path = _state_dir() / "weekly_digest.json"
    if not digest_path.exists():
        return {"error": "digest file not written after generate"}, 500

    try:
        data = json.loads(digest_path.read_text(encoding="utf-8"))
        return data, 200
    except (json.JSONDecodeError, OSError) as exc:
        return {"error": f"failed to read generated digest: {exc}"}, 500


# ---------------------------------------------------------------------------
# /api/intelligence/learning-summary  (GET)
# ---------------------------------------------------------------------------

def _intelligence_get_learning_summary() -> tuple[dict, int]:
    """Return learning feedback loop metrics for the last 7 days.

    Queries confidence_events to produce:
      boosts               — confidence-boost event count
      decays               — confidence-decay event count
      net_confidence_drift — sum of confidence_change over the window
      prevention_suggestions — antipatterns with occurrence_count >= 3
    """
    sd = _sd()
    db_path: Path = sd.DB_PATH

    if not db_path.exists():
        return {"boosts": 0, "decays": 0, "net_confidence_drift": 0.0, "prevention_suggestions": 0}, 200

    from datetime import timedelta
    since = (datetime.now(_UTC) - timedelta(days=7)).isoformat()

    boosts = 0
    decays = 0
    net_drift = 0.0
    prevention_suggestions = 0

    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row

        try:
            rows = con.execute(
                "SELECT outcome, confidence_change FROM confidence_events WHERE occurred_at >= ?",
                (since,),
            ).fetchall()
            for row in rows:
                change = float(row["confidence_change"] or 0.0)
                if row["outcome"] == "success":
                    boosts += 1
                else:
                    decays += 1
                net_drift += change
        except sqlite3.OperationalError:
            pass

        try:
            result = con.execute(
                "SELECT COUNT(*) FROM antipatterns WHERE occurrence_count >= 3"
            ).fetchone()
            if result:
                prevention_suggestions = int(result[0] or 0)
        except sqlite3.OperationalError:
            pass

        con.close()
    except Exception:
        pass

    return {
        "boosts": boosts,
        "decays": decays,
        "net_confidence_drift": round(net_drift, 4),
        "prevention_suggestions": prevention_suggestions,
    }, 200


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
