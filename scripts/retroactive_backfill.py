#!/usr/bin/env python3
"""retroactive_backfill.py — Retroactive intelligence backfill from historical data.

Populates the Karpathy loop, behavioral analysis, memory consolidation, and
feedback loop with real data from 816 dispatches + 74 event archives.

Usage:
    python3 scripts/retroactive_backfill.py                    # full backfill
    python3 scripts/retroactive_backfill.py --dry-run           # show what would happen
    python3 scripts/retroactive_backfill.py --step experiments  # only dispatch_experiments
    python3 scripts/retroactive_backfill.py --step feedback     # only feedback loop

BILLING SAFETY: No Anthropic SDK. SQLite + subprocess (claude CLI) only.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import subprocess
import sys
from collections import defaultdict
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Path bootstrap
# ---------------------------------------------------------------------------

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR / "lib"))

try:
    from vnx_paths import ensure_env
    _PATHS = ensure_env()
    STATE_DIR = Path(_PATHS["VNX_STATE_DIR"])
except Exception:
    STATE_DIR = SCRIPT_DIR.parent / ".vnx-data" / "state"

REPO_ROOT = SCRIPT_DIR.parent
QI_DB = STATE_DIR / "quality_intelligence.db"
TRACKER_DB = STATE_DIR / "dispatch_tracker.db"
RECEIPTS_PATH = STATE_DIR / "t0_receipts.ndjson"
ARCHIVE_DIR = REPO_ROOT / ".vnx-data" / "events" / "archive"
BEHAVIORS_OUTPUT = STATE_DIR / "dispatch_behaviors.json"
UNIFIED_REPORTS_DIR = REPO_ROOT / ".vnx-data" / "unified_reports"


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def _open_qi(timeout: int = 30) -> sqlite3.Connection:
    conn = sqlite3.connect(str(QI_DB), timeout=timeout)
    conn.row_factory = sqlite3.Row
    return conn


def _open_tracker(timeout: int = 30) -> sqlite3.Connection:
    """Open quality_intelligence.db ensuring dispatch_experiments table exists.

    dispatch_experiments was previously in dispatch_tracker.db; unified here.
    """
    QI_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(QI_DB), timeout=timeout)
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS dispatch_experiments (
            id INTEGER PRIMARY KEY,
            dispatch_id TEXT UNIQUE,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
            instruction_chars INTEGER,
            context_items INTEGER,
            repo_map_symbols INTEGER,
            role TEXT,
            cognition TEXT,
            model TEXT,
            terminal TEXT,
            file_count INTEGER,
            success BOOLEAN,
            cqs REAL,
            completion_minutes REAL,
            test_count INTEGER,
            committed BOOLEAN,
            lines_changed INTEGER
        );
        CREATE INDEX IF NOT EXISTS idx_de_dispatch_id ON dispatch_experiments (dispatch_id);
        CREATE INDEX IF NOT EXISTS idx_de_role ON dispatch_experiments (role);
        CREATE INDEX IF NOT EXISTS idx_de_timestamp ON dispatch_experiments (timestamp DESC);
    """)
    conn.commit()
    return conn


# ---------------------------------------------------------------------------
# Receipt loading
# ---------------------------------------------------------------------------

def _load_receipts() -> dict[str, dict]:
    """Load all task_complete receipts keyed by dispatch_id."""
    receipts: dict[str, dict] = {}
    if not RECEIPTS_PATH.exists():
        return receipts
    with open(RECEIPTS_PATH, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("event_type") == "task_complete":
                did = rec.get("dispatch_id") or rec.get("cmd_id", "")
                if did:
                    receipts[did] = rec
    return receipts


# ---------------------------------------------------------------------------
# Archive index
# ---------------------------------------------------------------------------

def _build_archive_index() -> dict[str, Path]:
    """Map dispatch_id → archive file path."""
    index: dict[str, Path] = {}
    if not ARCHIVE_DIR.exists():
        return index
    for path in ARCHIVE_DIR.rglob("*.ndjson"):
        index[path.stem] = path
    return index


# ---------------------------------------------------------------------------
# Parse event archive for backfill data
# ---------------------------------------------------------------------------

@dataclass
class ArchiveSummary:
    committed: bool = False
    pushed: bool = False
    test_count: int = 0
    lines_changed: int = 0
    unique_files: int = 0
    first_timestamp: str = ""
    last_timestamp: str = ""


def _parse_archive(path: Path) -> ArchiveSummary:
    """Extract key metrics from a dispatch NDJSON archive."""
    summary = ArchiveSummary()
    files_seen: set[str] = set()
    timestamps: list[str] = []

    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue

                ts = ev.get("timestamp", "")
                if ts:
                    timestamps.append(ts)

                ev_type = ev.get("type", "")

                if ev_type == "tool_use":
                    data = ev.get("data", {})
                    name = data.get("name", "")
                    inp = data.get("input", {})

                    if name in ("Read", "Write", "Edit", "MultiEdit"):
                        fp = inp.get("file_path", "")
                        if fp:
                            files_seen.add(fp)

                    elif name == "Bash":
                        cmd = inp.get("command", "") if isinstance(inp, dict) else str(inp)
                        if "git commit" in cmd:
                            summary.committed = True
                        if "git push" in cmd and "--force" not in cmd and "-f " not in cmd:
                            summary.pushed = True

                elif ev_type == "tool_result":
                    # Look for pytest results and git diff stats
                    data = ev.get("data", {})
                    content = data.get("content", "")
                    if isinstance(content, list):
                        content = " ".join(
                            (c.get("text", "") if isinstance(c, dict) else str(c))
                            for c in content
                        )
                    if not isinstance(content, str):
                        content = str(content)

                    # Pytest test count
                    m = re.search(r"(\d+)\s+passed", content)
                    if m:
                        summary.test_count = max(summary.test_count, int(m.group(1)))

                    # Lines changed from git diff --stat or numstat
                    for pat in [
                        r"(\d+)\s+insertions?\(\+\).*?(\d+)\s+deletions?",
                        r"(\d+)\s+insertions?\(\+\)",
                    ]:
                        dm = re.search(pat, content)
                        if dm:
                            ins = int(dm.group(1))
                            dels = int(dm.group(2)) if dm.lastindex and dm.lastindex >= 2 else 0
                            summary.lines_changed = max(summary.lines_changed, ins + dels)
                            break

    except (OSError, ValueError):
        pass

    summary.unique_files = len(files_seen)
    if timestamps:
        summary.first_timestamp = timestamps[0]
        summary.last_timestamp = timestamps[-1]

    return summary


# ---------------------------------------------------------------------------
# Parameter extraction helpers
# ---------------------------------------------------------------------------

_CONTEXT_MARKERS = re.compile(
    r"^#{1,3}\s*(context|intelligence|patterns|insights|recent dispatches)|"
    r"^---\s*context|\[CONTEXT\]|dispatch_insights|success_patterns",
    re.IGNORECASE | re.MULTILINE,
)

_FILE_PATTERN = re.compile(
    r"\b[\w./\-]+\.(?:py|ts|tsx|js|sh|yaml|yml|json|md|sql|toml)\b"
)


def _count_context_items(instruction: str) -> int:
    return len(_CONTEXT_MARKERS.findall(instruction))


def _count_file_mentions(instruction: str) -> int:
    return len(set(_FILE_PATTERN.findall(instruction)))


def _infer_cognition(raw_cognition: Optional[str], instruction: Optional[str]) -> str:
    """Map DB cognition field or instruction complexity to standard level."""
    if raw_cognition:
        c = raw_cognition.lower()
        if c in ("high", "deep", "complex"):
            return "high"
        if c in ("medium", "normal"):
            return "medium"
        if c in ("low", "simple"):
            return "low"
    if not instruction:
        return "medium"
    length = len(instruction)
    signals = sum(
        1 for s in ("architecture", "design", "analyze", "investigate",
                    "complex", "refactor", "optimize")
        if s in instruction.lower()
    )
    if length > 3000 or signals >= 3:
        return "high"
    if length > 1500 or signals >= 1:
        return "medium"
    return "low"


def _compute_completion_minutes(
    dispatched_at: Optional[str],
    completed_at: Optional[str],
    archive_summary: Optional[ArchiveSummary],
) -> float:
    """Return completion time in minutes from available timestamps."""
    # Try archive timestamps first (most accurate)
    if archive_summary and archive_summary.first_timestamp and archive_summary.last_timestamp:
        try:
            t0 = datetime.fromisoformat(archive_summary.first_timestamp.replace("Z", "+00:00"))
            t1 = datetime.fromisoformat(archive_summary.last_timestamp.replace("Z", "+00:00"))
            return round((t1 - t0).total_seconds() / 60, 2)
        except ValueError:
            pass

    # Fall back to dispatch metadata timestamps
    if dispatched_at and completed_at:
        try:
            t0 = datetime.fromisoformat(dispatched_at.replace("Z", "+00:00"))
            t1 = datetime.fromisoformat(completed_at.replace("Z", "+00:00"))
            if t0.tzinfo is None:
                t0 = t0.replace(tzinfo=timezone.utc)
            if t1.tzinfo is None:
                t1 = t1.replace(tzinfo=timezone.utc)
            return round((t1 - t0).total_seconds() / 60, 2)
        except ValueError:
            pass

    return 0.0


# ---------------------------------------------------------------------------
# Step 1: Backfill dispatch_experiments
# ---------------------------------------------------------------------------

def backfill_dispatch_experiments(dry_run: bool = False) -> int:
    """Populate dispatch_experiments from dispatch_metadata + receipts + archives.

    Returns count of records inserted.
    """
    print("[step1] Loading dispatch_metadata...")
    qi_conn = _open_qi()
    rows = qi_conn.execute(
        """
        SELECT dispatch_id, terminal, role, cognition, outcome_status, cqs,
               instruction_char_count, pattern_count, intelligence_json,
               dispatched_at, completed_at
        FROM dispatch_metadata
        ORDER BY dispatched_at
        """
    ).fetchall()
    qi_conn.close()

    print(f"[step1] Found {len(rows)} dispatches. Loading receipts and archives...")
    receipts = _load_receipts()
    archive_index = _build_archive_index()

    if dry_run:
        print(f"[dry-run] Would process {len(rows)} dispatch rows")
        print(f"[dry-run]   receipts available: {len(receipts)}")
        print(f"[dry-run]   archives available: {len(archive_index)}")
        return 0

    tracker_conn = _open_tracker()
    # Fetch already-existing dispatch_ids to skip duplicates
    existing_ids: set[str] = {
        r[0] for r in tracker_conn.execute(
            "SELECT dispatch_id FROM dispatch_experiments"
        ).fetchall()
    }
    print(f"[step1] Already in dispatch_experiments: {len(existing_ids)}")

    inserted = 0
    skipped_existing = 0
    skipped_no_data = 0

    now_iso = datetime.now(timezone.utc).isoformat()

    for row in rows:
        dispatch_id = row["dispatch_id"]
        if not dispatch_id:
            continue

        if dispatch_id in existing_ids:
            skipped_existing += 1
            continue

        instruction_chars = row["instruction_char_count"] or 0
        pattern_count = row["pattern_count"] or 0
        role = row["role"] or "unknown"
        terminal = row["terminal"] or "unknown"
        cognition = _infer_cognition(row["cognition"], None)
        # Model is not stored in dispatch_metadata, default to sonnet
        model = "sonnet"

        # Instruction text is not in dispatch_metadata directly, so we use
        # instruction_char_count for context item estimation
        context_items = pattern_count  # pattern_count = intelligence items injected
        file_count = 0  # no instruction text to parse

        # Outcome from dispatch_metadata
        outcome_status = row["outcome_status"] or ""
        success_statuses = {"success", "done", "completed"}
        failure_statuses = {"failure", "fail", "failed", "blocked", "error"}
        success: Optional[bool] = None
        if outcome_status.lower() in success_statuses:
            success = True
        elif outcome_status.lower() in failure_statuses:
            success = False
        # For ambiguous statuses, leave success as None if no_confirmation or null

        # No_confirmation = dispatch was acknowledged but no clear pass/fail
        # We count these as None (unknown outcome)
        if outcome_status == "no_confirmation":
            success = None

        cqs = row["cqs"]

        # Archive summary for timing + file/test/commit data
        arch_summary: Optional[ArchiveSummary] = None
        if dispatch_id in archive_index:
            arch_summary = _parse_archive(archive_index[dispatch_id])

        # Also check receipt for additional outcome data
        receipt = receipts.get(dispatch_id)
        if receipt and success is None:
            rec_status = receipt.get("status", "")
            if rec_status in success_statuses:
                success = True
            elif rec_status in failure_statuses:
                success = False

        if receipt and cqs is None:
            rec_cqs = receipt.get("cqs")
            if isinstance(rec_cqs, dict):
                cqs = rec_cqs.get("cqs")
            elif isinstance(rec_cqs, (int, float)):
                cqs = float(rec_cqs)

        completion_minutes = _compute_completion_minutes(
            row["dispatched_at"], row["completed_at"], arch_summary
        )

        test_count = arch_summary.test_count if arch_summary else 0
        committed = arch_summary.committed if arch_summary else False
        lines_changed = arch_summary.lines_changed if arch_summary else 0
        if arch_summary:
            file_count = arch_summary.unique_files

        # Dispatch timestamp from DB
        ts = row["dispatched_at"] or now_iso

        tracker_conn.execute(
            """
            INSERT INTO dispatch_experiments (
                dispatch_id, timestamp,
                instruction_chars, context_items, repo_map_symbols,
                role, cognition, model, terminal, file_count,
                success, cqs, completion_minutes, test_count, committed, lines_changed
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(dispatch_id) DO NOTHING
            """,
            (
                dispatch_id, ts,
                instruction_chars, context_items, 0,
                role, cognition, model, terminal, file_count,
                (1 if success else (0 if success is False else None)),
                cqs,
                completion_minutes,
                test_count,
                (1 if committed else 0),
                lines_changed,
            ),
        )
        inserted += 1

    tracker_conn.commit()
    tracker_conn.close()

    print(f"[step1] Inserted {inserted} dispatch experiments "
          f"(skipped existing: {skipped_existing})")
    return inserted


# ---------------------------------------------------------------------------
# Step 2: Event analyzer on all archives
# ---------------------------------------------------------------------------

def run_event_analyzer(dry_run: bool = False) -> int:
    """Run event_analyzer.py --all --output on all archives.

    Returns count of behaviors analyzed.
    """
    print("[step2] Running event_analyzer on all archives...")
    if dry_run:
        archives = list(ARCHIVE_DIR.rglob("*.ndjson")) if ARCHIVE_DIR.exists() else []
        print(f"[dry-run] Would analyze {len(archives)} archives → {BEHAVIORS_OUTPUT}")
        return len(archives)

    script = SCRIPT_DIR / "lib" / "event_analyzer.py"
    if not script.exists():
        print(f"[step2] ERROR: {script} not found", file=sys.stderr)
        return 0

    result = subprocess.run(
        [
            "python3", str(script),
            "--all",
            "--archive-dir", str(ARCHIVE_DIR),
            "--output", str(BEHAVIORS_OUTPUT),
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )

    if result.returncode != 0:
        print(f"[step2] event_analyzer stderr: {result.stderr[-500:]}", file=sys.stderr)

    # Count behaviors from output
    m = re.search(r"Analyzed (\d+) dispatch archives", result.stdout + result.stderr)
    count = int(m.group(1)) if m else 0
    print(f"[step2] Analyzed {count} archives → {BEHAVIORS_OUTPUT}")

    # Enrich behaviors with role data from dispatch_metadata
    # (event_analyzer doesn't extract role from archive init events)
    if BEHAVIORS_OUTPUT.exists() and QI_DB.exists():
        _enrich_behaviors_with_roles()

    return count


def _enrich_behaviors_with_roles() -> None:
    """Cross-reference behaviors with dispatch_metadata to fill in role and terminal.

    Strategy 1: exact dispatch_id match.
    Strategy 2: infer role from terminal (most T1→backend-developer, T2→test-engineer, T3→reviewer).
    This improves duration baselines in pattern_extractor from 1 to many.
    """
    if not BEHAVIORS_OUTPUT.exists():
        return
    try:
        behaviors = json.loads(BEHAVIORS_OUTPUT.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return

    # Build dispatch_id → (role, terminal) map from DB
    qi_conn = _open_qi()
    rows = qi_conn.execute(
        "SELECT dispatch_id, role, terminal FROM dispatch_metadata "
        "WHERE dispatch_id IS NOT NULL"
    ).fetchall()
    qi_conn.close()

    dispatch_info: dict[str, tuple] = {
        r["dispatch_id"]: (r["role"] or "unknown", r["terminal"] or "unknown")
        for r in rows
    }

    # Terminal → most common role in dispatch_metadata
    qi_conn2 = _open_qi()
    terminal_roles: dict[str, str] = {}
    for terminal_id in ("T1", "T2", "T3"):
        row = qi_conn2.execute(
            "SELECT role, COUNT(*) as cnt FROM dispatch_metadata "
            "WHERE terminal = ? AND role IS NOT NULL AND role != '' "
            "GROUP BY role ORDER BY cnt DESC LIMIT 1",
            (terminal_id,),
        ).fetchone()
        if row:
            terminal_roles[terminal_id] = row["role"]
    qi_conn2.close()
    # Fallback defaults if no data
    terminal_roles.setdefault("T1", "backend-developer")
    terminal_roles.setdefault("T2", "test-engineer")
    terminal_roles.setdefault("T3", "reviewer")

    enriched_exact = 0
    enriched_inferred = 0
    for b in behaviors:
        did = b.get("dispatch_id", "")
        terminal = b.get("terminal", "unknown")

        # Strategy 1: exact ID match
        if did in dispatch_info:
            db_role, db_terminal = dispatch_info[did]
            if b.get("role") in ("unknown", "", None) and db_role not in ("unknown", ""):
                b["role"] = db_role
                enriched_exact += 1
            if b.get("terminal") in ("unknown", "", None) and db_terminal not in ("unknown", ""):
                b["terminal"] = db_terminal
                terminal = db_terminal
            continue

        # Strategy 2: infer role from terminal
        if b.get("role") in ("unknown", "", None) and terminal in terminal_roles:
            b["role"] = terminal_roles[terminal]
            enriched_inferred += 1

    BEHAVIORS_OUTPUT.write_text(json.dumps(behaviors, indent=2), encoding="utf-8")
    print(f"[step2] Enriched behaviors: {enriched_exact} exact ID match, "
          f"{enriched_inferred} inferred from terminal")


# ---------------------------------------------------------------------------
# Step 3: Pattern extractor on behaviors
# ---------------------------------------------------------------------------

def run_pattern_extractor(dry_run: bool = False) -> dict:
    """Run pattern_extractor.py on behaviors JSON.

    Returns insertion count dict.
    """
    print("[step3] Running pattern_extractor...")
    if not BEHAVIORS_OUTPUT.exists():
        print(f"[step3] behaviors file not found: {BEHAVIORS_OUTPUT} — run step2 first",
              file=sys.stderr)
        return {}

    if dry_run:
        try:
            behaviors = json.loads(BEHAVIORS_OUTPUT.read_text())
            print(f"[dry-run] Would extract patterns from {len(behaviors)} behaviors")
        except Exception:
            print("[dry-run] Would extract patterns from behaviors file")
        return {}

    script = SCRIPT_DIR / "lib" / "pattern_extractor.py"
    if not script.exists():
        print(f"[step3] ERROR: {script} not found", file=sys.stderr)
        return {}

    result = subprocess.run(
        [
            "python3", str(script),
            "--input", str(BEHAVIORS_OUTPUT),
            "--db", str(QI_DB),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )

    if result.returncode != 0:
        print(f"[step3] pattern_extractor stderr: {result.stderr[-500:]}", file=sys.stderr)

    # Parse JSON result from stdout
    output_data = {}
    try:
        output_data = json.loads(result.stdout.strip())
    except (json.JSONDecodeError, ValueError):
        pass

    print(f"[step3] Pattern extractor result: {output_data}")
    return output_data


# ---------------------------------------------------------------------------
# Step 4: Memory consolidator on full history
# ---------------------------------------------------------------------------

def run_memory_consolidator(dry_run: bool = False) -> dict:
    """Run memory_consolidator.py --full.

    Returns consolidation result dict.
    """
    print("[step4] Running memory_consolidator --full...")
    script = SCRIPT_DIR / "memory_consolidator.py"
    if not script.exists():
        print(f"[step4] ERROR: {script} not found", file=sys.stderr)
        return {}

    cmd = ["python3", str(script), "--days", "365", "--full"]
    if dry_run:
        cmd.append("--dry-run")

    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=120,
    )

    if result.returncode != 0:
        print(f"[step4] memory_consolidator stderr: {result.stderr[-500:]}", file=sys.stderr)

    # Print output
    if result.stdout:
        for line in result.stdout.splitlines():
            print(f"[step4] {line}")

    # Parse JSON summary if present
    output_data = {}
    for line in result.stdout.splitlines():
        if line.strip().startswith("{"):
            try:
                output_data = json.loads(line.strip())
            except json.JSONDecodeError:
                pass

    return output_data


# ---------------------------------------------------------------------------
# Step 5: Retroactive feedback loop simulation
# ---------------------------------------------------------------------------

def simulate_feedback_loop(dry_run: bool = False) -> dict:
    """Retroactively apply confidence updates from historical dispatch outcomes.

    For each dispatch in dispatch_metadata that has intelligence injected AND
    a known outcome, find pattern_usage rows offered in that dispatch's time
    window and apply confidence boost (success) or decay (failure).

    Since offered_pattern_hashes is empty in historical data, we use
    time-window matching: patterns whose last_offered timestamp falls within
    the dispatch window are considered "offered" to that dispatch.

    Returns dict with counts: updated, boosted, decayed.
    """
    print("[step5] Simulating retroactive feedback loop...")

    qi_conn = _open_qi()

    # Load dispatches with intelligence and known outcome
    dispatches = qi_conn.execute(
        """
        SELECT dispatch_id, terminal, outcome_status, cqs,
               dispatched_at, completed_at
        FROM dispatch_metadata
        WHERE intelligence_json IS NOT NULL
          AND outcome_status IS NOT NULL
          AND outcome_status IN ('success', 'failure', 'fail', 'failed', 'blocked')
        ORDER BY dispatched_at
        """
    ).fetchall()

    # Load pattern_usage rows with last_offered timestamps
    patterns = qi_conn.execute(
        """
        SELECT rowid, pattern_id, pattern_title, used_count, success_count,
               failure_count, confidence, last_offered, created_at
        FROM pattern_usage
        WHERE last_offered IS NOT NULL
        """
    ).fetchall()

    print(f"[step5] Dispatches with intel + outcome: {len(dispatches)}")
    print(f"[step5] Pattern usage rows with last_offered: {len(patterns)}")

    if dry_run:
        print(f"[dry-run] Would process {len(dispatches)} dispatches against "
              f"{len(patterns)} patterns")
        qi_conn.close()
        return {"dispatches_processed": len(dispatches), "patterns_available": len(patterns)}

    # Build a time-sorted list of (last_offered, rowid, current_used, s_count, f_count, conf)
    # We'll match each pattern to dispatches by checking if last_offered falls in dispatch window
    # If no window match, we use the overall dispatch success rate to apply proportional updates

    success_statuses = {"success"}
    failure_statuses = {"failure", "fail", "failed", "blocked"}

    # Bucket patterns by last_offered date (approximate match)
    # For each dispatch: find patterns last_offered within ±2 hours of dispatched_at
    # If no match, skip (don't do global updates — too noisy)

    boosted = 0
    decayed = 0
    updated = 0

    # Build pattern list with parsed timestamps
    parsed_patterns = []
    for p in patterns:
        offered_ts = p["last_offered"]
        if not offered_ts:
            continue
        try:
            ts = datetime.fromisoformat(offered_ts.replace("Z", "+00:00"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            parsed_patterns.append({
                "rowid": p["rowid"],
                "pattern_id": p["pattern_id"],
                "used_count": p["used_count"] or 0,
                "success_count": p["success_count"] or 0,
                "failure_count": p["failure_count"] or 0,
                "confidence": p["confidence"] or 0.5,
                "last_offered": ts,
            })
        except ValueError:
            continue

    # Sort by timestamp for efficient matching
    parsed_patterns.sort(key=lambda x: x["last_offered"])

    # For each dispatch, find patterns offered during that dispatch's window
    now_iso = datetime.now(timezone.utc).isoformat()
    matched_patterns: dict[int, dict] = {}  # rowid → cumulative updates

    for dispatch in dispatches:
        outcome = dispatch["outcome_status"].lower()
        is_success = outcome in success_statuses

        # Parse dispatch time window
        dispatched_at = dispatch["dispatched_at"]
        completed_at = dispatch["completed_at"]
        if not dispatched_at:
            continue

        try:
            t_start = datetime.fromisoformat(dispatched_at.replace("Z", "+00:00"))
            if t_start.tzinfo is None:
                t_start = t_start.replace(tzinfo=timezone.utc)
        except ValueError:
            continue

        if completed_at:
            try:
                t_end = datetime.fromisoformat(completed_at.replace("Z", "+00:00"))
                if t_end.tzinfo is None:
                    t_end = t_end.replace(tzinfo=timezone.utc)
            except ValueError:
                t_end = t_start
        else:
            from datetime import timedelta
            t_end = t_start  # Use just start time for window matching

        # Window: dispatched_at to completed_at (or +2h buffer for patterns offered just before)
        from datetime import timedelta
        window_start = t_start - timedelta(minutes=30)
        window_end = t_end + timedelta(minutes=30)

        for p in parsed_patterns:
            if window_start <= p["last_offered"] <= window_end:
                rowid = p["rowid"]
                if rowid not in matched_patterns:
                    matched_patterns[rowid] = {
                        "rowid": rowid,
                        "used_delta": 0,
                        "success_delta": 0,
                        "failure_delta": 0,
                        "confidence": p["confidence"],
                        "dispatch_count": 0,
                    }
                m = matched_patterns[rowid]
                m["used_delta"] += 1
                m["dispatch_count"] += 1
                if is_success:
                    m["success_delta"] += 1
                    m["confidence"] = min(1.0, m["confidence"] + 0.05)
                else:
                    m["failure_delta"] += 1
                    m["confidence"] = max(0.0, m["confidence"] - 0.10)

    # Apply batch updates
    for rowid, upd in matched_patterns.items():
        qi_conn.execute(
            """
            UPDATE pattern_usage
            SET used_count = used_count + ?,
                success_count = success_count + ?,
                failure_count = failure_count + ?,
                confidence = ?,
                updated_at = ?
            WHERE rowid = ?
            """,
            (
                upd["used_delta"],
                upd["success_delta"],
                upd["failure_delta"],
                round(upd["confidence"], 4),
                now_iso,
                rowid,
            ),
        )
        updated += 1
        if upd["success_delta"] > 0:
            boosted += 1
        if upd["failure_delta"] > 0:
            decayed += 1

    # Also apply dispatch-level confidence updates to success_patterns
    # via intelligence_persist.update_confidence_from_outcome for dispatches
    # that have archive evidence (committed = True)
    sys.path.insert(0, str(SCRIPT_DIR / "lib"))
    try:
        from intelligence_persist import update_confidence_from_outcome as _upcf
        dispatch_updates = 0
        for dispatch in dispatches:
            did = dispatch["dispatch_id"]
            terminal = dispatch["terminal"] or "unknown"
            status = dispatch["outcome_status"].lower()
            if status in success_statuses:
                out = "success"
            elif status in failure_statuses:
                out = "failure"
            else:
                continue
            result = _upcf(QI_DB, did, terminal, out)
            if result.get("boosted", 0) + result.get("decayed", 0) > 0:
                dispatch_updates += 1
        print(f"[step5] Applied confidence updates to {dispatch_updates} dispatches' patterns")
    except ImportError as e:
        print(f"[step5] Could not import intelligence_persist: {e}", file=sys.stderr)

    qi_conn.commit()
    qi_conn.close()

    print(f"[step5] Feedback loop: {updated} pattern_usage rows updated "
          f"({boosted} boosted, {decayed} decayed)")
    return {"updated": updated, "boosted": boosted, "decayed": decayed}


# ---------------------------------------------------------------------------
# Step 6: Karpathy analysis
# ---------------------------------------------------------------------------

def run_karpathy_analysis() -> list[str]:
    """Run DispatchParameterTracker analysis. Returns list of insight strings."""
    print("[step6] Running Karpathy analysis...")
    sys.path.insert(0, str(SCRIPT_DIR / "lib"))
    try:
        from dispatch_parameter_tracker import DispatchParameterTracker
        tracker = DispatchParameterTracker(STATE_DIR)
        insights = tracker.analyze(min_experiments=20)
        stats = tracker.stats()
        print(f"[step6] Experiments: {stats['total_experiments']} total, "
              f"{stats['completed']} completed, "
              f"avg CQS: {stats.get('avg_cqs')}")
        if insights:
            print(f"[step6] Top {len(insights)} insights:")
            for ins in insights:
                print(f"  - {ins.summary()}")
        else:
            print(f"[step6] Insufficient data for insights "
                  f"(need 20 completed, have {stats['completed']})")
        return [i.summary() for i in insights]
    except ImportError as e:
        print(f"[step6] Could not import dispatch_parameter_tracker: {e}", file=sys.stderr)
        return []


# ---------------------------------------------------------------------------
# Step 7: Verify and report
# ---------------------------------------------------------------------------

def _count_table(conn: sqlite3.Connection, table: str, where: str = "") -> int:
    try:
        sql = f"SELECT COUNT(*) FROM {table}"
        if where:
            sql += f" WHERE {where}"
        return conn.execute(sql).fetchone()[0]
    except Exception:
        return -1


def backfill_retroactive_feedback(dry_run: bool = False) -> dict:
    """Apply retroactive confidence updates for 181 dispatches with intelligence_json.

    For each dispatch in dispatch_metadata that has intelligence_json and a known
    outcome, calls update_confidence_from_outcome() to boost or decay success_patterns
    confidence scores.  This is the actionable path since historical intelligence_json
    uses an older format with empty offered_pattern_hashes.

    Returns dict with counts.
    """
    print("[step-feedback] Applying retroactive confidence updates (181 intel dispatches)...")

    qi_conn = _open_qi()

    dispatches = qi_conn.execute(
        """
        SELECT dispatch_id, terminal, outcome_status
        FROM dispatch_metadata
        WHERE intelligence_json IS NOT NULL
          AND outcome_status IS NOT NULL
          AND outcome_status IN ('success', 'done', 'completed',
                                  'failure', 'fail', 'failed', 'blocked')
        """
    ).fetchall()
    qi_conn.close()

    print(f"[step-feedback] Dispatches with intelligence + known outcome: {len(dispatches)}")

    if dry_run:
        return {"dispatches_eligible": len(dispatches), "dry_run": True}

    success_statuses = {"success", "done", "completed"}
    failure_statuses = {"failure", "fail", "failed", "blocked"}

    sys.path.insert(0, str(SCRIPT_DIR / "lib"))
    try:
        from intelligence_persist import update_confidence_from_outcome as _upcf
    except ImportError as e:
        print(f"[step-feedback] Could not import intelligence_persist: {e}", file=sys.stderr)
        return {"dispatches_eligible": len(dispatches), "error": str(e)}

    boosted_total = 0
    decayed_total = 0
    processed = 0

    for row in dispatches:
        dispatch_id = row["dispatch_id"]
        terminal = row["terminal"] or "unknown"
        status_raw = (row["outcome_status"] or "").lower()

        if status_raw in success_statuses:
            outcome = "success"
        elif status_raw in failure_statuses:
            outcome = "failure"
        else:
            continue

        result = _upcf(QI_DB, dispatch_id, terminal, outcome)
        boosted_total += result.get("boosted", 0)
        decayed_total += result.get("decayed", 0)
        processed += 1

    print(f"[step-feedback] Processed {processed} dispatches: "
          f"boosted {boosted_total} patterns, decayed {decayed_total} patterns")
    return {
        "dispatches_processed": processed,
        "patterns_boosted": boosted_total,
        "patterns_decayed": decayed_total,
    }


def build_verification_report(karpathy_insights: list[str]) -> dict:
    """Query DB tables and return verification summary."""
    report: dict = {}

    # dispatch_experiments now in quality_intelligence.db (unified)
    if QI_DB.exists():
        qc_check = sqlite3.connect(str(QI_DB))
        report["dispatch_experiments"] = _count_table(qc_check, "dispatch_experiments")
        qc_check.close()
    else:
        report["dispatch_experiments"] = 0

    # quality_intelligence.db tables
    if QI_DB.exists():
        qc = _open_qi()
        report["success_patterns_behavior_analysis"] = _count_table(
            qc, "success_patterns", "category='behavior_analysis'"
        )
        report["success_patterns_memory_consolidation"] = _count_table(
            qc, "success_patterns", "category='memory_consolidation'"
        )
        report["antipatterns_behavior_analysis"] = _count_table(
            qc, "antipatterns", "category='behavior_analysis'"
        )
        report["antipatterns_memory_consolidation"] = _count_table(
            qc, "antipatterns", "category='memory_consolidation'"
        )
        report["prevention_rules"] = _count_table(qc, "prevention_rules")
        report["pattern_usage_with_used_count"] = _count_table(
            qc, "pattern_usage", "used_count > 0"
        )
        qc.close()

    report["karpathy_insights"] = len(karpathy_insights)

    return report


def print_verification(report: dict, karpathy_insights: list[str]) -> None:
    print()
    print("=== BACKFILL RESULTS ===")
    print(f"dispatch_experiments:                  {report.get('dispatch_experiments', 0)}")
    print(f"success_patterns (behavior_analysis):  {report.get('success_patterns_behavior_analysis', 0)}")
    print(f"success_patterns (memory_consolidation):{report.get('success_patterns_memory_consolidation', 0)}")
    print(f"antipatterns (behavior_analysis):      {report.get('antipatterns_behavior_analysis', 0)}")
    print(f"antipatterns (memory_consolidation):   {report.get('antipatterns_memory_consolidation', 0)}")
    print(f"prevention_rules:                      {report.get('prevention_rules', 0)}")
    print(f"pattern_usage with used_count > 0:     {report.get('pattern_usage_with_used_count', 0)}")
    print(f"Karpathy insights:                     {report.get('karpathy_insights', 0)}")
    if karpathy_insights:
        for insight in karpathy_insights:
            print(f"  - {insight}")
    print()


def write_report(report: dict, karpathy_insights: list[str], step_results: dict) -> Path:
    """Write full report to unified_reports."""
    UNIFIED_REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    report_path = UNIFIED_REPORTS_DIR / "retroactive-backfill-report.md"

    now = datetime.now(timezone.utc).isoformat()
    lines = [
        "# Retroactive Intelligence Backfill Report",
        "",
        f"**Generated:** {now}",
        f"**Dispatch-ID:** 20260415-080000-f60-pr2-retroactive-backfill-A",
        "",
        "## Summary",
        "",
        "| Metric | Count |",
        "|--------|-------|",
        f"| dispatch_experiments | {report.get('dispatch_experiments', 0)} |",
        f"| success_patterns (behavior_analysis) | {report.get('success_patterns_behavior_analysis', 0)} |",
        f"| success_patterns (memory_consolidation) | {report.get('success_patterns_memory_consolidation', 0)} |",
        f"| antipatterns (behavior_analysis) | {report.get('antipatterns_behavior_analysis', 0)} |",
        f"| antipatterns (memory_consolidation) | {report.get('antipatterns_memory_consolidation', 0)} |",
        f"| prevention_rules | {report.get('prevention_rules', 0)} |",
        f"| pattern_usage with used_count > 0 | {report.get('pattern_usage_with_used_count', 0)} |",
        f"| Karpathy insights | {report.get('karpathy_insights', 0)} |",
        "",
        "## Step Results",
        "",
    ]

    for step_name, result in step_results.items():
        lines.append(f"### {step_name}")
        if isinstance(result, dict):
            for k, v in result.items():
                lines.append(f"- {k}: {v}")
        else:
            lines.append(f"- result: {result}")
        lines.append("")

    if karpathy_insights:
        lines.append("## Karpathy Insights")
        lines.append("")
        for insight in karpathy_insights:
            lines.append(f"- {insight}")
        lines.append("")

    lines.append("## Open Items")
    lines.append("")
    lines.append("_None — backfill complete._")
    lines.append("")

    report_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"[report] Written to {report_path}")
    return report_path


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

STEPS = {
    "experiments": backfill_dispatch_experiments,
    "events": run_event_analyzer,
    "patterns": run_pattern_extractor,
    "memory": run_memory_consolidator,
    "feedback": simulate_feedback_loop,
    "retro-feedback": backfill_retroactive_feedback,
    "karpathy": run_karpathy_analysis,
}


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Retroactive intelligence backfill from historical dispatch data."
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Show what would happen without writing to DB",
    )
    parser.add_argument(
        "--step", choices=list(STEPS.keys()),
        help="Run only a specific step (experiments|events|patterns|memory|feedback|retro-feedback|karpathy)",
    )
    args = parser.parse_args()

    if not QI_DB.exists():
        print(f"[error] quality_intelligence.db not found: {QI_DB}", file=sys.stderr)
        sys.exit(1)

    step_results: dict = {}

    if args.step:
        fn = STEPS[args.step]
        result = fn() if args.step == "karpathy" else fn(dry_run=args.dry_run)
        step_results[args.step] = result
        if not args.dry_run and args.step == "karpathy":
            insights = result if isinstance(result, list) else []
            report = build_verification_report(insights)
            print_verification(report, insights)
        return

    # Full backfill
    print("=" * 60)
    print("VNX Retroactive Intelligence Backfill")
    print("=" * 60)

    # Step 1: dispatch_experiments
    n_experiments = backfill_dispatch_experiments(dry_run=args.dry_run)
    step_results["Step 1: dispatch_experiments"] = {"inserted": n_experiments}

    # Step 2: event analyzer
    n_behaviors = run_event_analyzer(dry_run=args.dry_run)
    step_results["Step 2: event_analyzer"] = {"behaviors_analyzed": n_behaviors}

    # Step 3: pattern extractor
    pattern_result = run_pattern_extractor(dry_run=args.dry_run)
    step_results["Step 3: pattern_extractor"] = pattern_result

    # Step 4: memory consolidator
    memory_result = run_memory_consolidator(dry_run=args.dry_run)
    step_results["Step 4: memory_consolidator"] = memory_result

    # Step 5: feedback loop simulation
    feedback_result = simulate_feedback_loop(dry_run=args.dry_run)
    step_results["Step 5: feedback_loop"] = feedback_result

    # Step 5b: retroactive feedback from intelligence_injections
    retro_result = backfill_retroactive_feedback(dry_run=args.dry_run)
    step_results["Step 5b: retro_feedback"] = retro_result

    # Step 6: Karpathy analysis
    karpathy_insights = run_karpathy_analysis()
    step_results["Step 6: karpathy_analysis"] = {"insights": len(karpathy_insights)}

    # Step 7: Verify and report
    report = build_verification_report(karpathy_insights)
    print_verification(report, karpathy_insights)

    if not args.dry_run:
        write_report(report, karpathy_insights, step_results)


if __name__ == "__main__":
    main()
