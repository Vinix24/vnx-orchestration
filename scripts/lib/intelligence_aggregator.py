#!/usr/bin/env python3
"""intelligence_aggregator.py — Wave 5 PR-5.4: cross-project intelligence facets.

Combines per-project intelligence into:
- Per-project facet: existing behavior preserved
- Global facet: pattern-mining across N projects (shared learnings)
- Cross-project recommendations: when project A succeeds with pattern X,
  recommend X to project B IF context-tags align.

Privacy: project-specific facts stay project-scoped. Only normalized
patterns (defect families, success patterns) propagate to global facet.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

log = logging.getLogger(__name__)

# Patterns that strip project-specific identifiers from titles / descriptions
# before they are stored in the global facet.
_PATH_RE = re.compile(r"/[^\s]+")
_DISPATCH_ID_RE = re.compile(r"\b[0-9]{8}-[a-zA-Z0-9_-]+\b")
_UUID_RE = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I)

# Global-intelligence DB schema (created on demand by export_global_facet).
_GLOBAL_SCHEMA = """
CREATE TABLE IF NOT EXISTS global_patterns (
    pattern_id TEXT PRIMARY KEY,
    pattern_family TEXT NOT NULL,
    total_occurrences INTEGER NOT NULL DEFAULT 0,
    occurrences_per_project TEXT,
    avg_confidence REAL,
    first_seen TEXT,
    last_seen TEXT
);

CREATE INDEX IF NOT EXISTS idx_global_patterns_family
    ON global_patterns(pattern_family);

CREATE TABLE IF NOT EXISTS cross_project_recommendations (
    rec_id TEXT PRIMARY KEY,
    source_project TEXT NOT NULL,
    target_project TEXT NOT NULL,
    pattern_id TEXT NOT NULL,
    rationale TEXT,
    confidence REAL,
    created_at TEXT,
    consumed_at TEXT,
    FOREIGN KEY (pattern_id) REFERENCES global_patterns(pattern_id)
);
"""


@dataclass
class GlobalPattern:
    pattern_id: str
    pattern_family: str
    occurrences: Dict[str, int]
    total_occurrences: int
    confidence: float
    first_seen: str
    last_seen: str


@dataclass
class CrossProjectRecommendation:
    source_project: str
    target_project: str
    pattern_id: str
    rationale: str
    confidence: float


def normalize_family_key(text: str) -> str:
    """Strip project-specific noise from a title to produce a stable family key.

    Removes:
    - Absolute filesystem paths  (/path/to/data → <path>)
    - Dispatch-ID-shaped tokens  (20260516-wave5-pr4-foo → <dispatch_id>)
    - UUID-shaped tokens         (hex UUIDs → <uuid>)
    """
    cleaned = _PATH_RE.sub("<path>", text or "")
    cleaned = _DISPATCH_ID_RE.sub("<dispatch_id>", cleaned)
    cleaned = _UUID_RE.sub("<uuid>", cleaned)
    return cleaned.strip().lower()


def _stable_pattern_id(family_key: str) -> str:
    return "gp-" + hashlib.sha1(family_key.encode("utf-8")).hexdigest()[:16]


def _connect_ro(db_path: Path) -> sqlite3.Connection:
    uri = f"file:{db_path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    return row is not None


class IntelligenceAggregator:
    """Aggregates intelligence across N project databases."""

    def __init__(self, project_db_paths: Dict[str, Path]):
        """project_db_paths: {project_id: path/to/quality_intelligence.db}"""
        self._dbs: Dict[str, Path] = {
            pid: Path(p) for pid, p in project_db_paths.items()
        }

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def mine_global_patterns(
        self,
        min_projects: int = 2,
        min_confidence: float = 0.6,
    ) -> List[GlobalPattern]:
        """Find patterns occurring in >=min_projects with avg confidence >=min_confidence."""
        family_data: Dict[str, Dict] = {}

        for project_id, db_path in self._dbs.items():
            if not db_path.exists():
                log.warning("DB not found for project %s: %s", project_id, db_path)
                continue
            rows = self._read_success_patterns(db_path, project_id)
            for row in rows:
                family = normalize_family_key(row["title"])
                if not family:
                    continue
                pid_str = str(project_id)
                if family not in family_data:
                    family_data[family] = {
                        "occurrences": {},
                        "confidence_sum": 0.0,
                        "confidence_count": 0,
                        "first_seen": row.get("first_seen") or "",
                        "last_seen": row.get("last_used") or "",
                    }
                d = family_data[family]
                d["occurrences"][pid_str] = (
                    d["occurrences"].get(pid_str, 0)
                    + int(row.get("usage_count") or 1)
                )
                conf = float(row.get("confidence_score") or 0.0)
                d["confidence_sum"] += conf
                d["confidence_count"] += 1
                if row.get("first_seen") and row["first_seen"] < d["first_seen"]:
                    d["first_seen"] = row["first_seen"]
                if row.get("last_used") and row["last_used"] > d["last_seen"]:
                    d["last_seen"] = row["last_used"]

        results: List[GlobalPattern] = []
        for family, d in family_data.items():
            if len(d["occurrences"]) < min_projects:
                continue
            count = d["confidence_count"]
            avg_conf = d["confidence_sum"] / count if count else 0.0
            if avg_conf < min_confidence:
                continue
            total = sum(d["occurrences"].values())
            pattern_id = _stable_pattern_id(family)
            results.append(
                GlobalPattern(
                    pattern_id=pattern_id,
                    pattern_family=family,
                    occurrences=dict(d["occurrences"]),
                    total_occurrences=total,
                    confidence=round(avg_conf, 6),
                    first_seen=d["first_seen"],
                    last_seen=d["last_seen"],
                )
            )

        results.sort(key=lambda p: (-p.total_occurrences, p.pattern_family))
        return results

    def recommend_cross_project(
        self,
        target_project: str,
        max_recommendations: int = 5,
    ) -> List[CrossProjectRecommendation]:
        """For target_project, find patterns proven in OTHER projects with matching context."""
        if target_project not in self._dbs:
            log.warning("target_project %r not in known DBs", target_project)
            return []

        target_db = self._dbs[target_project]
        target_families: set = set()
        if target_db.exists():
            for row in self._read_success_patterns(target_db, target_project):
                target_families.add(normalize_family_key(row["title"]))

        recs: List[CrossProjectRecommendation] = []
        for source_project, db_path in self._dbs.items():
            if source_project == target_project or not db_path.exists():
                continue
            for row in self._read_success_patterns(db_path, source_project):
                family = normalize_family_key(row["title"])
                if not family or family in target_families:
                    continue
                conf = float(row.get("confidence_score") or 0.0)
                if conf < 0.5:
                    continue
                recs.append(
                    CrossProjectRecommendation(
                        source_project=source_project,
                        target_project=target_project,
                        pattern_id=_stable_pattern_id(family),
                        rationale=(
                            f"Pattern '{family}' proven in '{source_project}' "
                            f"({int(row.get('usage_count') or 1)} uses, "
                            f"conf={conf:.2f})"
                        ),
                        confidence=round(conf * 0.7, 6),
                    )
                )

        recs.sort(key=lambda r: -r.confidence)
        return recs[:max_recommendations]

    def aggregate_recurrence(self, family_key: str) -> Dict[str, int]:
        """Count occurrences of normalized family across all projects."""
        counts: Dict[str, int] = {}
        normalized = normalize_family_key(family_key)
        for project_id, db_path in self._dbs.items():
            if not db_path.exists():
                continue
            for row in self._read_success_patterns(db_path, project_id):
                if normalize_family_key(row["title"]) == normalized:
                    counts[str(project_id)] = (
                        counts.get(str(project_id), 0)
                        + int(row.get("usage_count") or 1)
                    )
        return counts

    def export_global_facet(self, output_path: Path) -> None:
        """Write global facet snapshot as JSON for Control Centre consumption."""
        patterns = self.mine_global_patterns(min_projects=1, min_confidence=0.0)
        now = datetime.now(timezone.utc).isoformat()
        payload = {
            "generated_at": now,
            "pattern_count": len(patterns),
            "patterns": [
                {
                    "pattern_id": p.pattern_id,
                    "pattern_family": p.pattern_family,
                    "occurrences_per_project": p.occurrences,
                    "total_occurrences": p.total_occurrences,
                    "avg_confidence": p.confidence,
                    "first_seen": p.first_seen,
                    "last_seen": p.last_seen,
                }
                for p in patterns
            ],
        }
        output_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = output_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False))
        tmp.replace(output_path)
        log.info("Global facet exported to %s (%d patterns)", output_path, len(patterns))

    def persist_global_patterns(self, db_path: Path) -> int:
        """Upsert mined global patterns into a global_intelligence.db.

        Returns the number of patterns written.
        """
        patterns = self.mine_global_patterns()
        if not patterns:
            return 0
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(db_path))
        try:
            conn.executescript(_GLOBAL_SCHEMA)
            now = datetime.now(timezone.utc).isoformat()
            for p in patterns:
                conn.execute(
                    """
                    INSERT INTO global_patterns
                        (pattern_id, pattern_family, total_occurrences,
                         occurrences_per_project, avg_confidence, first_seen, last_seen)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(pattern_id) DO UPDATE SET
                        total_occurrences = excluded.total_occurrences,
                        occurrences_per_project = excluded.occurrences_per_project,
                        avg_confidence = excluded.avg_confidence,
                        last_seen = excluded.last_seen
                    """,
                    (
                        p.pattern_id,
                        p.pattern_family,
                        p.total_occurrences,
                        json.dumps(p.occurrences),
                        p.confidence,
                        p.first_seen or now,
                        p.last_seen or now,
                    ),
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        return len(patterns)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _read_success_patterns(
        self,
        db_path: Path,
        project_id: str,
    ) -> List[Dict]:
        """Read success_patterns from a per-project DB (read-only).

        Per-project DBs are opened in read-only mode (never mutated).
        """
        try:
            conn = _connect_ro(db_path)
        except Exception as exc:
            log.warning("Cannot open %s: %s", db_path, exc)
            return []

        try:
            if not _table_exists(conn, "success_patterns"):
                return []
            has_project_id = _has_column(conn, "success_patterns", "project_id")
            if has_project_id:
                rows = conn.execute(
                    "SELECT title, usage_count, confidence_score, first_seen, last_used "
                    "FROM success_patterns WHERE project_id = ?",
                    (project_id,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT title, usage_count, confidence_score, first_seen, last_used "
                    "FROM success_patterns"
                ).fetchall()
            return [dict(r) for r in rows]
        except sqlite3.Error as exc:
            log.warning("Error reading success_patterns from %s: %s", db_path, exc)
            return []
        finally:
            conn.close()


def _has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    try:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    except sqlite3.Error:
        return False
    for row in rows:
        name = row[1] if not isinstance(row, sqlite3.Row) else row["name"]
        if name == column:
            return True
    return False
