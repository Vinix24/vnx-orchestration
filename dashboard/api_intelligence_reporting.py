"""Intelligence reporting API handlers — confidence trends, digest, learning, transcript.

Extracted from api_intelligence.py (OI-1085 file-size split).
Covers: confidence-trends, weekly-digest (GET/POST), learning-summary,
        conversations transcript, behavioral summary.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

_UTC = timezone.utc


def _sd():
    """Lazy accessor for serve_dashboard constants (avoids circular import)."""
    import serve_dashboard
    return serve_dashboard


def _state_dir() -> Path:
    """Return the VNX state directory (parent of quality_intelligence.db)."""
    return _sd().DB_PATH.parent


def _scripts_dir() -> Path:
    """Return the scripts/ directory relative to this module's location."""
    return Path(__file__).resolve().parent.parent / "scripts"


_CONV_DB_PATH = Path.home() / ".claude" / "conversation-index.db"

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
    """Return learning feedback loop metrics for the last 7 days."""
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


# ---------------------------------------------------------------------------
# /api/conversations/<session_id>/transcript  (GET)
# ---------------------------------------------------------------------------

def _intelligence_get_transcript(session_id: str) -> tuple[dict, int]:
    """Return messages for a session from conversation-index.db."""
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


# ---------------------------------------------------------------------------
# /api/intelligence/behavioral  (GET)
# ---------------------------------------------------------------------------

def _intelligence_get_behavioral_summary() -> tuple[dict, int]:
    """Return behavioral intelligence summary from intelligence_dashboard_data."""
    scripts_lib = str(Path(__file__).resolve().parent.parent / "scripts" / "lib")
    if scripts_lib not in sys.path:
        sys.path.insert(0, scripts_lib)
    try:
        from intelligence_dashboard_data import get_behavioral_summary  # noqa: PLC0415
        return get_behavioral_summary(), 200
    except ImportError as exc:
        return {"error": f"intelligence_dashboard_data not available: {exc}"}, 500
    except Exception as exc:
        return {"error": str(exc)}, 500
