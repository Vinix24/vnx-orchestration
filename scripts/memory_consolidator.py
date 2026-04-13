#!/usr/bin/env python3
"""
VNX Memory Consolidation Pipeline
Extracts, deduplicates, and scores factual patterns from dispatch history.
Inspired by Mem0's extract-consolidate-retrieve pattern.

Usage:
    python3 scripts/memory_consolidator.py --days 7 --dry-run   # preview patterns
    python3 scripts/memory_consolidator.py --days 7              # run consolidation
    python3 scripts/memory_consolidator.py --days 30 --full      # full history
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

script_dir = Path(__file__).resolve().parent
sys.path.insert(0, str(script_dir / "lib"))
try:
    from vnx_paths import ensure_env
except Exception as exc:
    raise SystemExit(f"Failed to load vnx_paths: {exc}")

CATEGORY = "memory_consolidation"


# ── Data structures ────────────────────────────────────────────────────────────

@dataclass
class ExtractedPattern:
    title: str
    description: str
    pattern_type: str          # 'success' | 'antipattern'
    pattern_subtype: str       # 'role_rate', 'terminal_rate', 'duration', etc.
    evidence_count: int = 1
    base_confidence: float = 0.1
    severity: str = "medium"   # antipatterns only
    source_dispatch_ids: List[str] = field(default_factory=list)

    @property
    def confidence(self) -> float:
        return min(self.evidence_count * 0.1 + self.base_confidence, 1.0)


@dataclass
class ConsolidationResult:
    patterns_extracted: int = 0
    patterns_inserted: int = 0
    patterns_updated: int = 0
    patterns_merged: int = 0
    dry_run: bool = False
    patterns: List[ExtractedPattern] = field(default_factory=list)


# ── Similarity helpers ─────────────────────────────────────────────────────────

def _title_overlap(a: str, b: str) -> float:
    """Character-level Jaccard similarity for short titles."""
    if not a or not b:
        return 0.0
    words_a = set(a.lower().split())
    words_b = set(b.lower().split())
    if not words_a or not words_b:
        return 0.0
    return len(words_a & words_b) / len(words_a | words_b)


# ── MemoryConsolidator ─────────────────────────────────────────────────────────

class MemoryConsolidator:
    """Extract, deduplicate, and score patterns from dispatch history."""

    def __init__(self) -> None:
        paths = ensure_env()
        state_dir = Path(paths["VNX_STATE_DIR"]).expanduser().resolve()
        self.db_path = state_dir / "quality_intelligence.db"
        self.receipts_path = state_dir / "t0_receipts.ndjson"
        self.audit_path = state_dir / "dispatch_audit.jsonl"

        # Also look in .vnx-intelligence for receipts
        intel_receipts = (
            Path(paths["VNX_INTELLIGENCE_DIR"]) / "receipts" / "t0_receipts.ndjson"
        )
        if intel_receipts.exists():
            self.receipts_path = intel_receipts

        self.conn: Optional[sqlite3.Connection] = None

    def _open_db(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        return conn

    # ── Extract phase ──────────────────────────────────────────────────────────

    def _read_receipts(self, since: datetime) -> List[Dict]:
        """Read task_complete events from t0_receipts.ndjson since a cutoff."""
        results: List[Dict] = []
        if not self.receipts_path.exists():
            return results
        with open(self.receipts_path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if record.get("event_type") != "task_complete":
                    continue
                ts_raw = record.get("timestamp", "")
                if ts_raw:
                    try:
                        ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
                        if ts.tzinfo is None:
                            ts = ts.replace(tzinfo=timezone.utc)
                        since_aware = since.replace(tzinfo=timezone.utc) if since.tzinfo is None else since
                        if ts < since_aware:
                            continue
                    except ValueError:
                        pass
                results.append(record)
        return results

    def _read_dispatch_metadata(self, conn: sqlite3.Connection, since: datetime) -> List[sqlite3.Row]:
        try:
            return conn.execute(
                "SELECT dispatch_id, terminal, role, outcome_status, cqs, "
                "pattern_count, dispatched_at, completed_at "
                "FROM dispatch_metadata WHERE dispatched_at >= ? OR dispatched_at IS NULL",
                (since.isoformat(),),
            ).fetchall()
        except Exception:
            return []

    def _read_session_analytics(self, conn: sqlite3.Connection, since: datetime) -> List[sqlite3.Row]:
        try:
            return conn.execute(
                "SELECT session_id, terminal, duration_minutes, dispatch_id, "
                "primary_activity FROM session_analytics WHERE session_date >= ?",
                (since.date().isoformat(),),
            ).fetchall()
        except Exception:
            return []

    def _read_audit_log(self, since: datetime) -> List[Dict]:
        results: List[Dict] = []
        if not self.audit_path.exists():
            return results
        since_aware = since.replace(tzinfo=timezone.utc) if since.tzinfo is None else since
        with open(self.audit_path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                ts_raw = record.get("timestamp", "")
                if ts_raw:
                    try:
                        ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
                        if ts.tzinfo is None:
                            ts = ts.replace(tzinfo=timezone.utc)
                        if ts < since_aware:
                            continue
                    except ValueError:
                        pass
                results.append(record)
        return results

    # ── Pattern extraction ─────────────────────────────────────────────────────

    def _extract_role_success_rates(self, meta_rows: List[sqlite3.Row]) -> List[ExtractedPattern]:
        """Success rate per role across dispatch_metadata."""
        role_stats: Dict[str, Dict] = defaultdict(lambda: {"success": 0, "total": 0, "ids": []})
        for row in meta_rows:
            role = row["role"]
            if not role:
                continue
            status = row["outcome_status"] or ""
            role_stats[role]["total"] += 1
            if status == "success":
                role_stats[role]["success"] += 1
            if row["dispatch_id"]:
                role_stats[role]["ids"].append(row["dispatch_id"])

        patterns: List[ExtractedPattern] = []
        for role, stats in role_stats.items():
            total = stats["total"]
            if total < 2:
                continue
            rate = stats["success"] / total
            pct = int(rate * 100)
            title = f"{role} dispatches: {pct}% success rate"
            description = (
                f"Role '{role}' completed {stats['success']}/{total} dispatches successfully "
                f"({pct}%) in the analysis window."
            )
            p = ExtractedPattern(
                title=title,
                description=description,
                pattern_type="success" if rate >= 0.7 else "antipattern",
                pattern_subtype="role_rate",
                evidence_count=total,
                base_confidence=0.2,
                severity="low" if rate >= 0.7 else ("medium" if rate >= 0.4 else "high"),
                source_dispatch_ids=stats["ids"][:20],
            )
            patterns.append(p)
        return patterns

    def _extract_terminal_success_rates(self, meta_rows: List[sqlite3.Row]) -> List[ExtractedPattern]:
        """Success rate per terminal."""
        terminal_stats: Dict[str, Dict] = defaultdict(lambda: {"success": 0, "total": 0, "ids": []})
        for row in meta_rows:
            terminal = row["terminal"]
            if not terminal:
                continue
            status = row["outcome_status"] or ""
            terminal_stats[terminal]["total"] += 1
            if status == "success":
                terminal_stats[terminal]["success"] += 1
            if row["dispatch_id"]:
                terminal_stats[terminal]["ids"].append(row["dispatch_id"])

        patterns: List[ExtractedPattern] = []
        for terminal, stats in terminal_stats.items():
            total = stats["total"]
            if total < 2:
                continue
            rate = stats["success"] / total
            pct = int(rate * 100)
            title = f"{terminal} dispatches: {pct}% success rate"
            description = (
                f"Terminal {terminal} completed {stats['success']}/{total} dispatches "
                f"successfully ({pct}%) in the analysis window."
            )
            p = ExtractedPattern(
                title=title,
                description=description,
                pattern_type="success" if rate >= 0.7 else "antipattern",
                pattern_subtype="terminal_rate",
                evidence_count=total,
                base_confidence=0.2,
                severity="low" if rate >= 0.7 else ("medium" if rate >= 0.4 else "high"),
                source_dispatch_ids=stats["ids"][:20],
            )
            patterns.append(p)
        return patterns

    def _extract_duration_patterns(self, session_rows: List[sqlite3.Row]) -> List[ExtractedPattern]:
        """Average dispatch completion time by primary_activity."""
        activity_durations: Dict[str, List[float]] = defaultdict(list)
        for row in session_rows:
            dur = row["duration_minutes"]
            activity = row["primary_activity"] or "unknown"
            if dur and dur > 0:
                activity_durations[activity].append(float(dur))

        patterns: List[ExtractedPattern] = []
        for activity, durations in activity_durations.items():
            if len(durations) < 2:
                continue
            avg = sum(durations) / len(durations)
            title = f"avg session duration: {avg:.1f} min for {activity}"
            description = (
                f"Sessions with primary activity '{activity}' average {avg:.1f} minutes "
                f"based on {len(durations)} sessions."
            )
            p = ExtractedPattern(
                title=title,
                description=description,
                pattern_type="success",
                pattern_subtype="duration",
                evidence_count=len(durations),
                base_confidence=0.15,
            )
            patterns.append(p)
        return patterns

    def _extract_failure_patterns(self, receipts: List[Dict]) -> List[ExtractedPattern]:
        """Failure patterns from receipt events."""
        failure_groups: Dict[str, List[str]] = defaultdict(list)
        for receipt in receipts:
            status = receipt.get("status", "")
            if status not in ("error", "failure", "failed"):
                continue
            dispatch_id = receipt.get("dispatch_id", receipt.get("cmd_id", ""))
            # Classify failure by gate or terminal
            gate = receipt.get("gate", "unknown")
            terminal = receipt.get("terminal", "unknown")
            key = f"{terminal}:{gate}"
            failure_groups[key].append(dispatch_id)

        patterns: List[ExtractedPattern] = []
        for key, ids in failure_groups.items():
            if len(ids) < 2:
                continue
            terminal, gate = (key.split(":", 1) + ["unknown"])[:2]
            n = len(ids)
            title = f"{n} dispatches failed at {gate} on {terminal} this window"
            description = (
                f"{n} dispatches on terminal {terminal} failed at gate '{gate}' "
                f"in the analysis window. Recurring failure warrants investigation."
            )
            p = ExtractedPattern(
                title=title,
                description=description,
                pattern_type="antipattern",
                pattern_subtype="failure_cluster",
                evidence_count=n,
                base_confidence=0.2,
                severity="high" if n >= 5 else "medium",
                source_dispatch_ids=ids[:20],
            )
            patterns.append(p)
        return patterns

    def _extract_context_correlation(self, meta_rows: List[sqlite3.Row]) -> List[ExtractedPattern]:
        """Correlation between intelligence context count and CQS."""
        with_context: List[float] = []
        without_context: List[float] = []
        for row in meta_rows:
            cqs = row["cqs"]
            pattern_count = row["pattern_count"] or 0
            if cqs is None:
                continue
            if pattern_count >= 2:
                with_context.append(float(cqs))
            else:
                without_context.append(float(cqs))

        patterns: List[ExtractedPattern] = []
        if len(with_context) >= 3 and len(without_context) >= 3:
            avg_with = sum(with_context) / len(with_context)
            avg_without = sum(without_context) / len(without_context)
            diff_pct = ((avg_with - avg_without) / max(avg_without, 1)) * 100
            title = (
                f"dispatches with 2+ intelligence items: "
                f"{'+' if diff_pct >= 0 else ''}{diff_pct:.0f}% CQS vs without"
            )
            description = (
                f"Dispatches that received 2+ intelligence context items averaged "
                f"CQS {avg_with:.1f} vs {avg_without:.1f} without context "
                f"({len(with_context)} vs {len(without_context)} samples)."
            )
            p = ExtractedPattern(
                title=title,
                description=description,
                pattern_type="success" if diff_pct >= 0 else "antipattern",
                pattern_subtype="context_correlation",
                evidence_count=len(with_context) + len(without_context),
                base_confidence=0.25,
            )
            patterns.append(p)
        return patterns

    def _extract_governance_patterns(self, audit_records: List[Dict]) -> List[ExtractedPattern]:
        """Gate override and governance anomaly patterns from audit log."""
        overrides: Dict[str, int] = defaultdict(int)
        for record in audit_records:
            action = record.get("action", "")
            if "override" in action.lower() or "force" in str(record.get("forced", "")).lower():
                gate = record.get("gate", record.get("action", "unknown"))
                if record.get("forced") is True:
                    overrides[gate] += 1

        patterns: List[ExtractedPattern] = []
        total_overrides = sum(overrides.values())
        if total_overrides >= 1:
            gates_str = ", ".join(f"{g}({n})" for g, n in sorted(overrides.items()))
            title = f"{total_overrides} gate override(s) this window: {gates_str}"
            description = (
                f"{total_overrides} forced dispatch promotion(s) occurred in the analysis window. "
                f"Gates bypassed: {gates_str}. Review to ensure quality was not sacrificed."
            )
            p = ExtractedPattern(
                title=title,
                description=description,
                pattern_type="antipattern",
                pattern_subtype="governance",
                evidence_count=total_overrides,
                base_confidence=0.3,
                severity="high" if total_overrides >= 3 else "medium",
            )
            patterns.append(p)
        return patterns

    def extract_patterns(self, days: int) -> List[ExtractedPattern]:
        """Run all extraction phases and return combined pattern list."""
        since = datetime.now(timezone.utc) - timedelta(days=days)
        conn = self._open_db()
        try:
            meta_rows = self._read_dispatch_metadata(conn, since)
            session_rows = self._read_session_analytics(conn, since)
        finally:
            conn.close()

        receipts = self._read_receipts(since)
        audit_records = self._read_audit_log(since)

        all_patterns: List[ExtractedPattern] = []
        all_patterns.extend(self._extract_role_success_rates(meta_rows))
        all_patterns.extend(self._extract_terminal_success_rates(meta_rows))
        all_patterns.extend(self._extract_duration_patterns(session_rows))
        all_patterns.extend(self._extract_failure_patterns(receipts))
        all_patterns.extend(self._extract_context_correlation(meta_rows))
        all_patterns.extend(self._extract_governance_patterns(audit_records))
        return all_patterns

    # ── Deduplication ──────────────────────────────────────────────────────────

    def _find_existing(
        self, conn: sqlite3.Connection, table: str, title: str
    ) -> Optional[sqlite3.Row]:
        """Return existing row from table that matches title (exact or similar)."""
        exact = conn.execute(
            f"SELECT * FROM {table} WHERE title = ? AND category = ?",
            (title, CATEGORY),
        ).fetchone()
        if exact:
            return exact

        # Check similarity against all memory_consolidation rows
        candidates = conn.execute(
            f"SELECT * FROM {table} WHERE category = ?",
            (CATEGORY,),
        ).fetchall()
        for row in candidates:
            if _title_overlap(title, row["title"]) > 0.8:
                return row
        return None

    def _append_ids(self, existing_json: Optional[str], new_ids: List[str]) -> str:
        items: list = []
        if existing_json:
            try:
                items = json.loads(existing_json)
            except (json.JSONDecodeError, TypeError):
                items = []
        for nid in new_ids:
            if nid and nid not in items:
                items.append(nid)
        return json.dumps(items[-20:])

    # ── Persistence ────────────────────────────────────────────────────────────

    def _upsert_success_pattern(
        self, conn: sqlite3.Connection, p: ExtractedPattern, now: str
    ) -> Tuple[str, int]:
        """Returns ('inserted'|'updated'|'merged', 1)."""
        existing = self._find_existing(conn, "success_patterns", p.title)
        source_ids = self._append_ids(
            existing["source_dispatch_ids"] if existing else None,
            p.source_dispatch_ids,
        )
        if existing:
            # Merge evidence counts and recompute confidence
            existing_usage = (existing["usage_count"] or 0)
            new_usage = existing_usage + p.evidence_count
            new_confidence = min(new_usage * 0.1 + p.base_confidence, 1.0)
            action = "merged" if existing["title"] != p.title else "updated"
            conn.execute(
                "UPDATE success_patterns SET usage_count = ?, confidence_score = ?, "
                "last_used = ?, source_dispatch_ids = ?, description = ? WHERE id = ?",
                (new_usage, new_confidence, now, source_ids, p.description, existing["id"]),
            )
            return action, 1

        conn.execute(
            "INSERT INTO success_patterns "
            "(pattern_type, category, title, description, pattern_data, "
            " confidence_score, usage_count, source_dispatch_ids, first_seen, last_used) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "approach", CATEGORY, p.title[:120], p.description[:500],
                json.dumps({"source": "memory_consolidation", "subtype": p.pattern_subtype}),
                p.confidence, p.evidence_count, source_ids, now, now,
            ),
        )
        return "inserted", 1

    def _upsert_antipattern(
        self, conn: sqlite3.Connection, p: ExtractedPattern, now: str
    ) -> Tuple[str, int]:
        existing = self._find_existing(conn, "antipatterns", p.title)
        source_ids = self._append_ids(
            existing["source_dispatch_ids"] if existing else None,
            p.source_dispatch_ids,
        )
        if existing:
            new_count = (existing["occurrence_count"] or 0) + p.evidence_count
            action = "merged" if existing["title"] != p.title else "updated"
            conn.execute(
                "UPDATE antipatterns SET occurrence_count = ?, last_seen = ?, "
                "source_dispatch_ids = ?, description = ? WHERE id = ?",
                (new_count, now, source_ids, p.description, existing["id"]),
            )
            return action, 1

        conn.execute(
            "INSERT INTO antipatterns "
            "(pattern_type, category, title, description, pattern_data, "
            " why_problematic, severity, occurrence_count, source_dispatch_ids, "
            " first_seen, last_seen) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "approach", CATEGORY, p.title[:120], p.description[:500],
                json.dumps({"source": "memory_consolidation", "subtype": p.pattern_subtype}),
                p.description[:500], p.severity, p.evidence_count,
                source_ids, now, now,
            ),
        )
        return "inserted", 1

    # ── Main pipeline ──────────────────────────────────────────────────────────

    def consolidate(self, days: int = 7, dry_run: bool = False) -> ConsolidationResult:
        """Run full consolidation pipeline."""
        result = ConsolidationResult(dry_run=dry_run)
        print(f"[memory_consolidator] Extracting patterns from last {days} day(s)...")

        patterns = self.extract_patterns(days)
        result.patterns_extracted = len(patterns)
        result.patterns = patterns

        if dry_run:
            print(f"[dry-run] {len(patterns)} patterns extracted — no writes")
            for p in patterns:
                tag = "[SUCCESS]" if p.pattern_type == "success" else "[ANTIPAT]"
                print(f"  {tag} conf={p.confidence:.2f} ev={p.evidence_count:3d}  {p.title}")
            return result

        if not self.db_path.exists():
            print(f"[memory_consolidator] DB not found: {self.db_path}", file=sys.stderr)
            return result

        now = datetime.now(timezone.utc).isoformat()
        conn = self._open_db()
        try:
            for p in patterns:
                if p.pattern_type == "success":
                    action, _ = self._upsert_success_pattern(conn, p, now)
                else:
                    action, _ = self._upsert_antipattern(conn, p, now)

                if action == "inserted":
                    result.patterns_inserted += 1
                elif action == "updated":
                    result.patterns_updated += 1
                else:
                    result.patterns_merged += 1

            conn.commit()
        except Exception as exc:
            conn.rollback()
            print(f"[memory_consolidator] DB error: {exc}", file=sys.stderr)
        finally:
            conn.close()

        print(
            f"[memory_consolidator] Done — "
            f"extracted={result.patterns_extracted}, "
            f"inserted={result.patterns_inserted}, "
            f"updated={result.patterns_updated}, "
            f"merged={result.patterns_merged}"
        )
        return result


# ── CLI ────────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="VNX Memory Consolidation Pipeline",
    )
    parser.add_argument(
        "--days", type=int, default=7,
        help="Look back N days of dispatch history (default: 7)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Show patterns that would be extracted without writing to DB",
    )
    parser.add_argument(
        "--full", action="store_true",
        help="Full history consolidation (sets --days 365 unless --days specified)",
    )
    args = parser.parse_args()

    days = args.days
    if args.full and days == 7:
        days = 365

    consolidator = MemoryConsolidator()
    result = consolidator.consolidate(days=days, dry_run=args.dry_run)

    if not args.dry_run:
        summary = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "days": days,
            "patterns_extracted": result.patterns_extracted,
            "patterns_inserted": result.patterns_inserted,
            "patterns_updated": result.patterns_updated,
            "patterns_merged": result.patterns_merged,
        }
        print(json.dumps(summary))


if __name__ == "__main__":
    main()
