"""Auto-dream memory consolidator (ADR-019).

Reads success_patterns + antipatterns + intelligence_injections for a project,
dispatches kimi K2.6 cheap-lane for consolidation, emits NDJSON event, and writes
a pending-review.json for T0 approval.

Per ADR-005: NDJSON emit BEFORE any DB write.
Per ADR-007: project_id required on all DB operations.
Per ADR-003: kimi via CLI subprocess only, no Anthropic SDK.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

# Minimum number of core patterns (success_patterns + antipatterns) required to
# proceed with consolidation. Below this threshold the cycle is skipped cleanly.
_MIN_PATTERN_THRESHOLD: int = 1

# Default kimi subprocess timeout in seconds; override via VNX_DREAM_KIMI_TIMEOUT.
_DEFAULT_KIMI_TIMEOUT: int = 180

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
from project_root import resolve_project_root

# Sort column per table — each table uses a different timestamp column name.
_TABLE_SORT_COL: dict[str, str] = {
    "success_patterns": "first_seen",
    "antipatterns": "first_seen",
    "intelligence_injections": "injected_at",
}


def _check_receipt_completeness(
    data_root: Path, max_age_hours: int = 48
) -> tuple[bool, str]:
    """GAP-7 preflight: verify recent processed receipts exist and are not stale.

    Checks {data_root}/receipts/processed/ for any file modified within max_age_hours.
    Returns (True, "ok") when fresh receipts are found, (False, reason) otherwise.
    """
    processed_dir = data_root / "receipts" / "processed"
    if not processed_dir.exists():
        return False, f"receipts/processed directory absent: {processed_dir}"

    files = list(processed_dir.iterdir())
    if not files:
        return False, "receipts/processed is empty — no dispatch receipts found"

    cutoff = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)
    fresh = [
        f for f in files
        if datetime.fromtimestamp(f.stat().st_mtime, tz=timezone.utc) >= cutoff
    ]
    if not fresh:
        most_recent_ts = max(
            datetime.fromtimestamp(f.stat().st_mtime, tz=timezone.utc) for f in files
        )
        age_h = (datetime.now(timezone.utc) - most_recent_ts).total_seconds() / 3600
        return (
            False,
            f"all receipts stale (newest {age_h:.1f}h ago, threshold {max_age_hours}h)",
        )

    return True, "ok"


def _emit_dream_event(event: dict[str, Any]) -> None:
    """Emit NDJSON event BEFORE any DB mutation (ADR-005)."""
    root = resolve_project_root(__file__)
    events_dir = root / ".vnx-data" / "events" / "dream"
    events_dir.mkdir(parents=True, exist_ok=True)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    event_path = events_dir / f"{today}.ndjson"
    with event_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(event) + "\n")


def _fetch_patterns(conn: sqlite3.Connection, project_id: str) -> dict[str, list[dict]]:
    """Fetch recent patterns for consolidation input, scoped to project_id (ADR-007)."""
    results: dict[str, list[dict]] = {}
    for table, sort_col in _TABLE_SORT_COL.items():
        try:
            rows = conn.execute(
                f"SELECT * FROM {table} WHERE project_id = ?"
                f" ORDER BY {sort_col} DESC LIMIT 200",
                (project_id,),
            )
            results[table] = [dict(row) for row in rows]
        except sqlite3.OperationalError:
            results[table] = []
    return results


def _extract_kimi_text(stdout: str) -> str:
    """Extract assistant text from kimi stream-json output (ContentPart events)."""
    parts: list[str] = []
    for raw_line in stdout.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        event_type = event.get("event_type") or event.get("type") or ""
        if event_type == "ContentPart":
            content = event.get("content", "")
            if content:
                parts.append(content)
    return "".join(parts)


def _parse_kimi_response(output: str) -> dict:
    """Extract JSON object from kimi text output (skips preamble / postamble)."""
    start = output.find("{")
    end = output.rfind("}")
    if start == -1 or end == -1:
        raise ValueError(f"No JSON found in kimi output: {output[:300]!r}")
    try:
        return json.loads(output[start : end + 1])
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON in kimi output: {exc}") from exc


def _build_consolidation_prompt(patterns: dict[str, list], project_id: str) -> str:
    total = sum(len(v) for v in patterns.values())
    payload = json.dumps(patterns, default=str, indent=2)[:50_000]
    return f"""You are a memory-consolidation agent (auto-dream pattern, ADR-019).

Project: {project_id}
Input: {total} patterns from recent dispatch cycles.

For each pattern, decide:
- KEEP: high-signal, recent, not superseded
- MERGE_INTO: overlaps with another pattern (cite which id)
- DROP: stale (>30d untouched), low-signal, exact duplicate
- ARCHIVE: superseded by a newer pattern (cite which id)
- FLAG_FOR_REVIEW: novel or high-impact, needs operator judgment

Output STRICT JSON (no preamble, no postamble):
{{
  "merged": [{{"keep_id": N, "drop_ids": [...], "merge_note": "..."}}, ...],
  "dropped": [{{"id": N, "table": "...", "reason": "..."}}, ...],
  "archived": [{{"id": N, "table": "...", "reason": "..."}}, ...],
  "flagged": [{{"id": N, "table": "...", "reason": "..."}}, ...],
  "summary": "1-paragraph summary of consolidation decisions"
}}

Patterns to consolidate:
{payload}
"""


def _dispatch_kimi_consolidation(
    patterns_input: dict, project_id: str, timeout: float = _DEFAULT_KIMI_TIMEOUT
) -> dict:
    """Dispatch kimi K2.6 cheap-lane for pattern consolidation and parse the result.

    Raises subprocess.TimeoutExpired if kimi does not respond within ``timeout`` seconds.
    """
    from kimi_wrapper import kimi_exec  # noqa: PLC0415

    prompt = _build_consolidation_prompt(patterns_input, project_id)
    stdout = kimi_exec(
        prompt,
        dispatch_id=f"dream-{project_id}",
        project_id=project_id,
        timeout=timeout,
    )
    text = _extract_kimi_text(stdout) or stdout
    return _parse_kimi_response(text)


def run_dream_cycle(
    project_id: str,
    db_path: Path,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Run a single dream consolidation cycle for a project.

    ADR-005: NDJSON emit before DB write.
    ADR-007: project_id required throughout.
    Returns a dict with cycle_id, counts, and review_path.
    """
    cycle_id = (
        f"dream-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}-{uuid4().hex[:8]}"
    )
    print(f"dream run: cycle={cycle_id} project={project_id}", flush=True)

    # GAP-7: receipt-completeness preflight — skip cycle on empty/stale data
    data_root = resolve_project_root(__file__) / ".vnx-data"
    complete, detail = _check_receipt_completeness(data_root)
    if not complete:
        _emit_dream_event(
            {
                "event_type": "dream_cycle_skipped",
                "cycle_id": cycle_id,
                "project_id": project_id,
                "reason": "incomplete_data",
                "detail": detail,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
        )
        return {
            "status": "skipped",
            "cycle_id": cycle_id,
            "reason": "incomplete_data",
            "detail": detail,
        }

    _emit_dream_event(
        {
            "event_type": "dream_cycle_started",
            "cycle_id": cycle_id,
            "project_id": project_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    )

    kimi_timeout = float(
        os.environ.get("VNX_DREAM_KIMI_TIMEOUT", str(_DEFAULT_KIMI_TIMEOUT))
    )

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        patterns = _fetch_patterns(conn, project_id)
        total_input = sum(len(v) for v in patterns.values())

        # Insufficient-data guard: skip cleanly when there are no core patterns to
        # consolidate (e.g. freshly-bootstrapped DB). Kimi is NOT invoked. (GAP-7)
        core_count = len(patterns.get("success_patterns", [])) + len(
            patterns.get("antipatterns", [])
        )
        if core_count < _MIN_PATTERN_THRESHOLD:
            _emit_dream_event(
                {
                    "event_type": "dream_cycle_skipped",
                    "cycle_id": cycle_id,
                    "project_id": project_id,
                    "reason": "insufficient_data",
                    "detail": (
                        f"core_count={core_count} below threshold={_MIN_PATTERN_THRESHOLD}"
                    ),
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }
            )
            return {
                "status": "skipped",
                "cycle_id": cycle_id,
                "reason": "insufficient_data",
                "detail": f"core_count={core_count}",
            }

        try:
            consolidation = _dispatch_kimi_consolidation(
                patterns, project_id, timeout=kimi_timeout
            )
        except subprocess.TimeoutExpired:
            _emit_dream_event(
                {
                    "event_type": "dream_cycle_timeout",
                    "cycle_id": cycle_id,
                    "project_id": project_id,
                    "timeout_seconds": int(kimi_timeout),
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }
            )
            return {
                "status": "timeout",
                "cycle_id": cycle_id,
                "reason": "kimi_timeout",
                "timeout_seconds": int(kimi_timeout),
            }
        except Exception as exc:
            _emit_dream_event(
                {
                    "event_type": "dream_cycle_failed",
                    "cycle_id": cycle_id,
                    "project_id": project_id,
                    "error": str(exc)[:500],
                }
            )
            raise

        root = resolve_project_root(__file__)
        review_dir = root / ".vnx-data" / "state" / "dream"
        review_dir.mkdir(parents=True, exist_ok=True)
        review_path = review_dir / f"{cycle_id}-pending-review.json"
        review_path.write_text(
            json.dumps(
                {
                    "cycle_id": cycle_id,
                    "project_id": project_id,
                    "input_count": total_input,
                    "consolidation": consolidation,
                    "requires_operator_review": True,
                },
                indent=2,
            ),
            encoding="utf-8",
        )

        merged_count = len(consolidation.get("merged", []))
        dropped_count = len(consolidation.get("dropped", []))
        archived_count = len(consolidation.get("archived", []))
        flagged_count = len(consolidation.get("flagged", []))

        _emit_dream_event(
            {
                "event_type": "dream_cycle_completed",
                "cycle_id": cycle_id,
                "project_id": project_id,
                "input_count": total_input,
                "merged_count": merged_count,
                "dropped_count": dropped_count,
                "archived_count": archived_count,
                "flagged_count": flagged_count,
                "review_path": str(review_path),
            }
        )

        if not dry_run:
            conn.execute(
                """
                INSERT INTO dream_cycles
                    (cycle_id, project_id, completed_at, status, provider,
                     insights_input, merged_count, dropped_count, archived_count,
                     flagged_count, report_path)
                VALUES (?, ?, ?, 'completed', 'kimi', ?, ?, ?, ?, ?, ?)
                """,
                (
                    cycle_id,
                    project_id,
                    datetime.now(timezone.utc).isoformat(),
                    total_input,
                    merged_count,
                    dropped_count,
                    archived_count,
                    flagged_count,
                    str(review_path),
                ),
            )
            conn.commit()

        return {
            "cycle_id": cycle_id,
            "input_count": total_input,
            "merged_count": merged_count,
            "dropped_count": dropped_count,
            "archived_count": archived_count,
            "flagged_count": flagged_count,
            "review_path": str(review_path),
        }
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Auto-dream memory consolidator (ADR-019)")
    parser.add_argument("--project-id", required=True, help="Project to consolidate (ADR-007)")
    parser.add_argument("--db-path", default=None, help="Override quality_intelligence.db path")
    parser.add_argument(
        "--dry-run", action="store_true", help="Emit events but skip DB writes"
    )
    args = parser.parse_args()

    root = resolve_project_root(__file__)
    db_path = (
        Path(args.db_path)
        if args.db_path
        else root / ".vnx-data" / "state" / "quality_intelligence.db"
    )

    result = run_dream_cycle(args.project_id, db_path, args.dry_run)
    print(json.dumps(result, indent=2))
    if result.get("status") == "timeout":
        sys.exit(1)


if __name__ == "__main__":
    main()
