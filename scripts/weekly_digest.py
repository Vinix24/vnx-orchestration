#!/usr/bin/env python3
"""Weekly Digest — Aggregate last 7 days of system intelligence.

Reads from quality_intelligence.db, t0_receipts.ndjson, and pending_edits.json.
Generates a narrative summary and writes weekly_digest.json under VNX_STATE_DIR.

CLI: python3 scripts/weekly_digest.py [--days 7] [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

_UTC = timezone.utc

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR / "lib"))
try:
    from vnx_paths import ensure_env
except Exception as exc:
    raise SystemExit(f"Failed to load vnx_paths: {exc}")

PATHS = ensure_env()
STATE_DIR = Path(PATHS["VNX_STATE_DIR"])
DB_PATH = STATE_DIR / "quality_intelligence.db"
RECEIPTS_PATH = STATE_DIR / "t0_receipts.ndjson"
PENDING_PATH = STATE_DIR / "pending_edits.json"
DIGEST_PATH = STATE_DIR / "weekly_digest.json"

_SEVERITY_SCORE = {"critical": 1.0, "high": 0.75, "medium": 0.5, "low": 0.25}


# ---------------------------------------------------------------------------
# Metrics collection
# ---------------------------------------------------------------------------

def collect_metrics(days: int = 7) -> dict:
    """Aggregate intelligence data for the last N days."""
    since = (datetime.now(tz=_UTC) - timedelta(days=days)).strftime("%Y-%m-%d")
    metrics: dict = {
        "patterns_learned": 0,
        "top_patterns": [],
        "antipatterns_active": 0,
        "top_antipatterns": [],
        "avg_success_confidence": None,
        "dispatch_outcomes": {"total": 0, "success": 0, "failure": 0, "unknown": 0},
        "pending_suggestions": 0,
        "accepted_suggestions": 0,
    }

    # --- DB metrics ---
    if DB_PATH.exists():
        try:
            con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
            con.row_factory = sqlite3.Row

            try:
                rows = con.execute(
                    """
                    SELECT title, confidence_score
                    FROM success_patterns
                    WHERE last_used >= ?
                    ORDER BY confidence_score DESC
                    LIMIT 5
                    """,
                    (since,),
                ).fetchall()
                metrics["patterns_learned"] = len(rows)
                metrics["top_patterns"] = [
                    {"title": r["title"], "confidence": float(r["confidence_score"] or 0)}
                    for r in rows
                ]
                if rows:
                    scores = [float(r["confidence_score"] or 0) for r in rows]
                    metrics["avg_success_confidence"] = round(sum(scores) / len(scores), 3)
            except sqlite3.OperationalError:
                pass

            try:
                rows = con.execute(
                    """
                    SELECT title, severity, occurrence_count
                    FROM antipatterns
                    WHERE last_seen >= ?
                    ORDER BY occurrence_count DESC
                    LIMIT 5
                    """,
                    (since,),
                ).fetchall()
                metrics["antipatterns_active"] = len(rows)
                metrics["top_antipatterns"] = [
                    {"title": r["title"], "severity": r["severity"]}
                    for r in rows
                ]
            except sqlite3.OperationalError:
                pass

            con.close()
        except Exception:
            pass

    # --- Receipts outcomes ---
    if RECEIPTS_PATH.exists():
        try:
            lines = RECEIPTS_PATH.read_text(encoding="utf-8", errors="replace").splitlines()
            for line in lines:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                ts = rec.get("timestamp", "")
                if ts and ts[:10] < since:
                    continue
                metrics["dispatch_outcomes"]["total"] += 1
                status = (rec.get("status") or rec.get("event_type") or "").lower()
                if "success" in status or "complete" in status or "done" in status:
                    metrics["dispatch_outcomes"]["success"] += 1
                elif "fail" in status or "error" in status:
                    metrics["dispatch_outcomes"]["failure"] += 1
                else:
                    metrics["dispatch_outcomes"]["unknown"] += 1
        except OSError:
            pass

    # --- Pending suggestions ---
    if PENDING_PATH.exists():
        try:
            data = json.loads(PENDING_PATH.read_text(encoding="utf-8"))
            edits = data.get("edits", [])
            metrics["pending_suggestions"] = sum(1 for e in edits if e.get("status") == "pending")
            metrics["accepted_suggestions"] = sum(1 for e in edits if e.get("status") == "accepted")
        except (json.JSONDecodeError, OSError):
            pass

    return metrics


# ---------------------------------------------------------------------------
# Narrative generation
# ---------------------------------------------------------------------------

def _template_narrative(metrics: dict, days: int) -> str:
    """Generate a template-based narrative (dry-run fallback)."""
    outcomes = metrics["dispatch_outcomes"]
    total = outcomes["total"]
    success = outcomes["success"]
    rate = f"{round(success / total * 100)}%" if total else "n/a"

    parts = [
        f"{days}d digest:",
        f"{metrics['patterns_learned']} patterns learned",
        f"avg confidence {metrics['avg_success_confidence'] or 'n/a'}",
        f"{total} dispatches ({rate} success)",
        f"{metrics['pending_suggestions']} suggestions pending",
    ]
    if metrics["antipatterns_active"]:
        parts.append(f"{metrics['antipatterns_active']} antipatterns active")

    return " | ".join(parts)


def _cli_narrative(metrics: dict, days: int, timeout: int = 20) -> str | None:
    """Try to generate a richer narrative via claude CLI subprocess."""
    prompt = (
        f"Summarize this VNX system weekly digest in ≤500 chars. "
        f"Be concise and actionable. Data: {json.dumps(metrics)}"
    )
    try:
        proc = subprocess.run(
            ["claude", "-p", "--model", "haiku", "--output-format", "json", prompt],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if proc.returncode != 0:
            return None
        raw = proc.stdout.strip()
        try:
            wrapper = json.loads(raw)
            text = wrapper.get("result") or raw
        except json.JSONDecodeError:
            text = raw
        if isinstance(text, str) and text.strip():
            return text.strip()[:500]
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        pass
    return None


def generate_narrative(metrics: dict, days: int = 7, dry_run: bool = False) -> str:
    """Generate digest narrative. Falls back to template if LLM unavailable."""
    if not dry_run:
        cli_result = _cli_narrative(metrics, days)
        if cli_result:
            return cli_result
    return _template_narrative(metrics, days)


# ---------------------------------------------------------------------------
# Build + write digest
# ---------------------------------------------------------------------------

def build_digest(metrics: dict, narrative: str, days: int) -> dict:
    now = datetime.now(tz=_UTC)
    period_start = (now - timedelta(days=days)).strftime("%Y-%m-%d")
    period_end = now.strftime("%Y-%m-%d")
    return {
        "generated_at": now.isoformat().replace("+00:00", "Z"),
        "period": {"start": period_start, "end": period_end, "days": days},
        "metrics": metrics,
        "narrative": narrative,
    }


def write_digest(digest: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    DIGEST_PATH.write_text(json.dumps(digest, indent=2, ensure_ascii=False), encoding="utf-8")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Generate VNX weekly intelligence digest"
    )
    parser.add_argument("--days", type=int, default=7, help="Lookback window in days")
    parser.add_argument("--dry-run", action="store_true",
                        help="Use template narrative (no LLM call)")
    args = parser.parse_args()

    metrics = collect_metrics(days=args.days)
    narrative = generate_narrative(metrics, days=args.days, dry_run=args.dry_run)
    digest = build_digest(metrics, narrative, days=args.days)

    write_digest(digest)
    print(f"Weekly digest written to: {DIGEST_PATH}")
    print(f"  Period: {digest['period']['start']} → {digest['period']['end']}")
    print(f"  Patterns: {metrics['patterns_learned']} | Dispatches: {metrics['dispatch_outcomes']['total']}")
    print(f"  Narrative: {narrative}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
