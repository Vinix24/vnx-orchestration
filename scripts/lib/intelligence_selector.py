#!/usr/bin/env python3
"""
VNX Intelligence Selector — Bounded injection for dispatch-create and resume paths.

Implements the FP-C Intelligence Contract (docs/core/31_FPC_INTELLIGENCE_CONTRACT.md):
  - Selection algorithm (Section 2.3): one item per class, highest confidence wins
  - Payload bounds (Section 2.2): max 3 items, max 500 chars/item, max 2000 chars total
  - Evidence thresholds: proven_pattern >= 0.6, failure_prevention >= 0.5, recent_comparable >= 0.4
  - Task-class-aware filtering via scope_tags and task_class_filter
  - Injection and suppression events emitted to coordination_events

Governance:
  G-R5: Injection bounded to max 3 items, only at dispatch-create or resume
  G-R6: Every item carries confidence, evidence_count, last_seen, scope_tags
  G-R7: Recommendations are advisory-only (measured but not auto-applied)
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

# ---------------------------------------------------------------------------
# Constants from FP-C Intelligence Contract
# ---------------------------------------------------------------------------

MAX_ITEMS_PER_INJECTION = 3
MAX_CONTENT_CHARS_PER_ITEM = 500
MAX_PAYLOAD_CHARS = 2000
MIN_EVIDENCE_COUNT = 1

# Per-class confidence thresholds (Section 1.2 / 2.3)
CONFIDENCE_THRESHOLDS = {
    "proven_pattern": 0.6,
    "failure_prevention": 0.5,
    "recent_comparable": 0.4,
}

# Per-class minimum evidence counts
EVIDENCE_THRESHOLDS = {
    "proven_pattern": 2,
    "failure_prevention": 1,
    "recent_comparable": 1,
}

# Selection priority order (highest first) — used for payload overflow trimming
ITEM_CLASS_PRIORITY = ["proven_pattern", "failure_prevention", "recent_comparable"]

# Recent comparable window
RECENT_COMPARABLE_DAYS = 14

# Valid injection points
VALID_INJECTION_POINTS = frozenset({"dispatch_create", "dispatch_resume"})

# Valid task classes (from FP-C Execution Contracts)
VALID_TASK_CLASSES = frozenset({
    "coding_interactive",
    "research_structured",
    "docs_synthesis",
    "ops_watchdog",
    "channel_response",
})

# Skill-to-task-class mapping (from 30_FPC_EXECUTION_CONTRACTS.md Section 1.1)
SKILL_TO_TASK_CLASS = {
    "backend-developer": "coding_interactive",
    "frontend-developer": "coding_interactive",
    "api-developer": "coding_interactive",
    "python-optimizer": "coding_interactive",
    "supabase-expert": "coding_interactive",
    "monitoring-specialist": "coding_interactive",
    "vnx-manager": "coding_interactive",
    "debugger": "coding_interactive",
    "test-engineer": "coding_interactive",
    "quality-engineer": "coding_interactive",
    "architect": "research_structured",
    "reviewer": "research_structured",
    "planner": "research_structured",
    "data-analyst": "research_structured",
    "performance-profiler": "research_structured",
    "security-engineer": "research_structured",
    "t0-orchestrator": "research_structured",
    "excel-reporter": "docs_synthesis",
    "technical-writer": "docs_synthesis",
}


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class IntelligenceItem:
    """A single intelligence item conforming to the FP-C schema."""
    item_id: str
    item_class: str
    title: str
    content: str
    confidence: float
    evidence_count: int
    last_seen: str
    scope_tags: List[str]
    source_refs: List[str] = field(default_factory=list)
    task_class_filter: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "item_id": self.item_id,
            "item_class": self.item_class,
            "title": self.title,
            "content": self.content[:MAX_CONTENT_CHARS_PER_ITEM],
            "confidence": self.confidence,
            "evidence_count": self.evidence_count,
            "last_seen": self.last_seen,
            "scope_tags": self.scope_tags,
            "source_refs": self.source_refs,
            "task_class_filter": self.task_class_filter,
        }


@dataclass
class SuppressionRecord:
    """Records why an item class slot was not filled."""
    item_class: str
    reason: str

    def to_dict(self) -> Dict[str, Any]:
        return {"item_class": self.item_class, "reason": self.reason}


@dataclass
class InjectionResult:
    """Complete result of an intelligence selection run."""
    injection_point: str
    injected_at: str
    items: List[IntelligenceItem]
    suppressed: List[SuppressionRecord]
    task_class: str
    dispatch_id: str

    @property
    def items_injected(self) -> int:
        return len(self.items)

    @property
    def items_suppressed(self) -> int:
        return len(self.suppressed)

    @property
    def payload_chars(self) -> int:
        return len(json.dumps(self.to_payload_dict()))

    def to_payload_dict(self) -> Dict[str, Any]:
        """Return the intelligence_payload for bundle.json."""
        return {
            "injection_point": self.injection_point,
            "injected_at": self.injected_at,
            "items": [item.to_dict() for item in self.items],
            "suppressed": [s.to_dict() for s in self.suppressed],
        }

    def to_event_metadata(self) -> Dict[str, Any]:
        """Return metadata for the coordination event."""
        return {
            "injection_point": self.injection_point,
            "task_class": self.task_class,
            "items_injected": self.items_injected,
            "items_suppressed": self.items_suppressed,
            "suppression_reasons": [s.reason for s in self.suppressed],
            "payload_chars": self.payload_chars,
            "item_ids": [item.item_id for item in self.items],
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


def _new_id() -> str:
    return str(uuid.uuid4())


def _stable_item_id(prefix: str, source_key: str) -> str:
    """Build a deterministic, content-derived item_id.

    Patterns offered to multiple dispatches must share the SAME item_id so that
    pattern_usage rows aggregate (one row per underlying pattern) instead of
    fragmenting into a fresh row per offering.  Random UUIDs broke that
    invariant — see codex regate finding for PR #311.

    The id encodes the originating table via *prefix* (e.g. ``sp`` for
    success_patterns, ``ap`` for antipatterns, ``pr`` for prevention_rules,
    ``dm`` for dispatch_metadata) and a stable per-row key (the row PK or
    dispatch_id).
    """
    safe_key = str(source_key).strip().lower().replace(" ", "_")
    return f"intel_{prefix}_{safe_key}"


def _item_hash(item_id: str) -> str:
    """SHA1 of item_id, matching learning_loop.hash_pattern() convention."""
    import hashlib
    return hashlib.sha1(item_id.encode("utf-8")).hexdigest()


def resolve_task_class(
    task_class: Optional[str] = None,
    skill_name: Optional[str] = None,
) -> str:
    """Resolve task class from explicit value or skill name. Defaults to coding_interactive."""
    if task_class and task_class in VALID_TASK_CLASSES:
        return task_class
    if skill_name:
        return SKILL_TO_TASK_CLASS.get(skill_name, "coding_interactive")
    return "coding_interactive"


def _scope_matches(item_scope_tags: List[str], query_scope_tags: List[str]) -> bool:
    """Check if an item's scope tags overlap with the query scope tags.

    Empty item scope = matches everything. Empty query scope = matches everything.
    """
    if not item_scope_tags or not query_scope_tags:
        return True
    return bool(set(item_scope_tags) & set(query_scope_tags))


def _task_class_matches(item_filter: List[str], task_class: str) -> bool:
    """Check if an item's task_class_filter allows the given task class.

    Empty filter = matches all task classes.
    """
    if not item_filter:
        return True
    return task_class in item_filter


# ---------------------------------------------------------------------------
# Intelligence Selector
# ---------------------------------------------------------------------------

class IntelligenceSelector:
    """Selects bounded, evidence-backed intelligence items for dispatch injection.

    Sources data from quality_intelligence.db tables:
      - success_patterns   → proven_pattern items
      - antipatterns        → failure_prevention items
      - prevention_rules   → failure_prevention items
      - dispatch_metadata  → recent_comparable items
    """

    def __init__(
        self,
        quality_db_path: Optional[Path] = None,
        coord_db_state_dir: Optional[Path] = None,
    ) -> None:
        self._quality_db_path = quality_db_path
        self._coord_state_dir = coord_db_state_dir
        self._quality_db: Optional[sqlite3.Connection] = None

    def _get_quality_db(self) -> Optional[sqlite3.Connection]:
        """Lazy-connect to quality_intelligence.db."""
        if self._quality_db is not None:
            return self._quality_db
        if self._quality_db_path is None or not self._quality_db_path.exists():
            return None
        try:
            self._quality_db = sqlite3.connect(str(self._quality_db_path))
            self._quality_db.row_factory = sqlite3.Row
        except Exception:
            self._quality_db = None
        return self._quality_db

    def close(self) -> None:
        if self._quality_db:
            self._quality_db.close()
            self._quality_db = None

    def _maybe_reconcile_confidence(self) -> None:
        """Run reconcile if the cached timestamp is older than the TTL.

        Best-effort safety net: failures are swallowed so a broken reconcile
        never blocks dispatch creation.  The reconcile opens its own SQLite
        connection and commits before returning, so subsequent SELECT
        statements on ``self._quality_db`` observe the new values without
        needing to re-open the cached reader connection.
        """
        if self._quality_db_path is None:
            return
        try:
            from confidence_reconcile import maybe_reconcile
        except ImportError:
            return
        try:
            maybe_reconcile(self._quality_db_path)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def select(
        self,
        dispatch_id: str,
        injection_point: str,
        *,
        task_class: Optional[str] = None,
        skill_name: Optional[str] = None,
        scope_tags: Optional[List[str]] = None,
        track: Optional[str] = None,
        gate: Optional[str] = None,
    ) -> InjectionResult:
        """Run the bounded selection algorithm and return an InjectionResult.

        Args:
            dispatch_id:     Dispatch being created or resumed.
            injection_point: Must be 'dispatch_create' or 'dispatch_resume'.
            task_class:      Explicit task class (overrides skill_name derivation).
            skill_name:      Skill name for task class derivation.
            scope_tags:      Scope tags for filtering (skill, track, gate, etc.).
            track:           Track label (added to scope_tags automatically).
            gate:            Gate identifier (added to scope_tags automatically).

        Returns:
            InjectionResult with 0-3 items and suppression records.
        """
        if injection_point not in VALID_INJECTION_POINTS:
            raise ValueError(
                f"Invalid injection_point: {injection_point!r}. "
                f"Must be one of {sorted(VALID_INJECTION_POINTS)}"
            )

        resolved_class = resolve_task_class(task_class, skill_name)

        # Build scope tags from all available context
        effective_scope: List[str] = list(scope_tags or [])
        if skill_name and skill_name not in effective_scope:
            effective_scope.append(skill_name)
        if track:
            tag = f"Track-{track}" if not track.startswith("Track-") else track
            if tag not in effective_scope:
                effective_scope.append(tag)
        if gate and gate not in effective_scope:
            effective_scope.append(gate)
        if resolved_class not in effective_scope:
            effective_scope.append(resolved_class)

        # Query candidates per class
        candidates = self._query_candidates(resolved_class, effective_scope)

        # Select best item per class
        selected: List[IntelligenceItem] = []
        suppressed: List[SuppressionRecord] = []

        for item_class in ITEM_CLASS_PRIORITY:
            class_candidates = candidates.get(item_class, [])
            if not class_candidates:
                suppressed.append(SuppressionRecord(
                    item_class=item_class,
                    reason="no candidates available",
                ))
                continue

            threshold = CONFIDENCE_THRESHOLDS[item_class]
            evidence_min = EVIDENCE_THRESHOLDS[item_class]

            # Filter by thresholds
            eligible = [
                c for c in class_candidates
                if c.confidence >= threshold and c.evidence_count >= evidence_min
            ]

            if not eligible:
                best_conf = max(c.confidence for c in class_candidates)
                suppressed.append(SuppressionRecord(
                    item_class=item_class,
                    reason=f"confidence {best_conf:.2f} below threshold {threshold}",
                ))
                continue

            # Select highest confidence
            best = max(eligible, key=lambda c: c.confidence)
            selected.append(best)

        # Enforce payload size limit (Section 2.3 step 6)
        selected = self._enforce_payload_limit(selected, suppressed)

        now = _now_utc()
        result = InjectionResult(
            injection_point=injection_point,
            injected_at=now,
            items=selected,
            suppressed=suppressed,
            task_class=resolved_class,
            dispatch_id=dispatch_id,
        )

        return result

    def emit_event(
        self,
        result: InjectionResult,
        coord_state_dir: Optional[Path] = None,
    ) -> Optional[str]:
        """Emit an injection or suppression coordination event.

        Returns the event_id or None if no coord DB available.
        """
        state_dir = coord_state_dir or self._coord_state_dir
        if state_dir is None:
            return None

        try:
            from runtime_coordination import get_connection, _append_event
        except ImportError:
            return None

        if result.items_injected > 0:
            event_type = "intelligence_injection"
            reason = f"injected {result.items_injected} items at {result.injection_point}"
        else:
            event_type = "intelligence_suppression"
            reason = "no items met minimum thresholds"

        try:
            with get_connection(state_dir) as conn:
                event_id = _append_event(
                    conn,
                    event_type=event_type,
                    entity_type="dispatch",
                    entity_id=result.dispatch_id,
                    actor="intelligence_selector",
                    reason=reason,
                    metadata=result.to_event_metadata(),
                )
                conn.commit()
            return event_id
        except Exception:
            return None

    def record_injection(
        self,
        result: InjectionResult,
        coord_state_dir: Optional[Path] = None,
    ) -> None:
        """Record injection decision in the intelligence_injections audit table.

        Also writes per-item rows to pattern_usage in quality_intelligence.db so
        the feedback loop can look up which patterns were offered for a dispatch.
        """
        state_dir = coord_state_dir or self._coord_state_dir
        if state_dir is None:
            return

        try:
            from runtime_coordination import get_connection
        except ImportError:
            return

        injection_id = _new_id()
        items_json = json.dumps([item.to_dict() for item in result.items])
        suppressed_json = json.dumps([s.to_dict() for s in result.suppressed])

        try:
            with get_connection(state_dir) as conn:
                conn.execute(
                    """
                    INSERT INTO intelligence_injections
                        (injection_id, dispatch_id, injection_point, task_class,
                         items_injected, items_suppressed, payload_chars,
                         items_json, suppressed_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        injection_id,
                        result.dispatch_id,
                        result.injection_point,
                        result.task_class,
                        result.items_injected,
                        result.items_suppressed,
                        result.payload_chars,
                        items_json,
                        suppressed_json,
                    ),
                )
                conn.commit()
        except Exception:
            pass

        # Write per-item pattern_usage rows so feedback loop can query by dispatch_id
        if result.items and self._quality_db_path is not None and self._quality_db_path.exists():
            self._record_pattern_usage(result)

    def _record_pattern_usage(self, result: InjectionResult) -> None:
        """Write one pattern_usage row per injected item so feedback can find them later.

        Identity model:
          - ``pattern_id`` is the *stable* per-pattern id derived from the
            originating row (see :func:`_stable_item_id`).  The same pattern
            offered to multiple dispatches collapses onto the same row via
            ON CONFLICT(pattern_id) DO UPDATE.  This restores deduplication —
            random ids fragmented one underlying pattern across many rows.
          - ``pattern_hash`` is SHA1(item_id), following the convention used by
            ``learning_loop.hash_pattern``.  Hash and pattern_id must NOT be
            the same string; consumers may rely on the hash to detect
            content-level identity independently of the id.

        Per-dispatch attribution is recorded in the ``dispatch_pattern_offered``
        junction table (one row per dispatch+pattern pair) so that concurrent
        dispatches offering the same pattern do not overwrite each other.
        """
        db = self._get_quality_db()
        if db is None:
            return
        now = _now_utc()
        try:
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS dispatch_pattern_offered (
                    dispatch_id   TEXT NOT NULL,
                    pattern_id    TEXT NOT NULL,
                    pattern_title TEXT NOT NULL,
                    offered_at    TEXT NOT NULL,
                    PRIMARY KEY (dispatch_id, pattern_id)
                )
                """
            )
            for item in result.items:
                pattern_hash = _item_hash(item.item_id)
                db.execute(
                    """
                    INSERT INTO pattern_usage
                        (pattern_id, pattern_title, pattern_hash, used_count,
                         ignored_count, success_count, failure_count,
                         last_offered, confidence, created_at, updated_at)
                    VALUES (?, ?, ?, 0, 0, 0, 0, ?, ?, ?, ?)
                    ON CONFLICT(pattern_id) DO UPDATE SET
                        pattern_title = excluded.pattern_title,
                        pattern_hash  = excluded.pattern_hash,
                        last_offered  = excluded.last_offered,
                        updated_at    = excluded.updated_at
                    """,
                    (
                        item.item_id,
                        item.title[:255],
                        pattern_hash,
                        now,
                        item.confidence,
                        now,
                        now,
                    ),
                )
                db.execute(
                    """
                    INSERT INTO dispatch_pattern_offered
                        (dispatch_id, pattern_id, pattern_title, offered_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(dispatch_id, pattern_id) DO UPDATE SET
                        offered_at = excluded.offered_at
                    """,
                    (result.dispatch_id, item.item_id, item.title[:255], now),
                )
            db.commit()
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Candidate query methods
    # ------------------------------------------------------------------

    def _query_candidates(
        self,
        task_class: str,
        scope_tags: List[str],
    ) -> Dict[str, List[IntelligenceItem]]:
        """Query all candidate items from quality_intelligence.db, grouped by class."""
        db = self._get_quality_db()
        result: Dict[str, List[IntelligenceItem]] = {
            "proven_pattern": [],
            "failure_prevention": [],
            "recent_comparable": [],
        }
        if db is None:
            return result

        result["proven_pattern"] = self._query_proven_patterns(db, task_class, scope_tags)
        result["failure_prevention"] = self._query_failure_prevention(db, task_class, scope_tags)
        result["recent_comparable"] = self._query_recent_comparable(db, task_class, scope_tags)

        return result

    def _query_proven_patterns(
        self,
        db: sqlite3.Connection,
        task_class: str,
        scope_tags: List[str],
    ) -> List[IntelligenceItem]:
        """Query success_patterns for proven_pattern candidates."""
        # Safety net: if the daily learning_loop reconcile has not run
        # recently, sync pattern_usage learning state into
        # success_patterns.confidence_score before reading it.
        self._maybe_reconcile_confidence()

        items: List[IntelligenceItem] = []
        try:
            rows = db.execute(
                """
                SELECT id, title, description, category, confidence_score,
                       usage_count, source_dispatch_ids, first_seen, last_used
                FROM success_patterns
                WHERE (valid_until IS NULL OR valid_until > datetime('now'))
                ORDER BY confidence_score DESC
                LIMIT 20
                """,
            ).fetchall()
        except Exception:
            return items

        for row in rows:
            row_d = dict(row)
            category = row_d.get("category", "")
            pattern_scope = [category] if category else []

            if not _scope_matches(pattern_scope, scope_tags):
                continue

            source_refs = []
            if row_d.get("source_dispatch_ids"):
                try:
                    source_refs = json.loads(row_d["source_dispatch_ids"])
                except (json.JSONDecodeError, TypeError):
                    pass

            content = (row_d.get("description") or "")[:MAX_CONTENT_CHARS_PER_ITEM]
            last_seen = row_d.get("last_used") or row_d.get("first_seen") or _now_utc()

            items.append(IntelligenceItem(
                item_id=_stable_item_id("sp", str(row_d.get("id", ""))),
                item_class="proven_pattern",
                title=(row_d.get("title") or "Proven pattern")[:120],
                content=content,
                confidence=float(row_d.get("confidence_score", 0.0)),
                evidence_count=int(row_d.get("usage_count", 0)),
                last_seen=last_seen,
                scope_tags=pattern_scope,
                source_refs=source_refs[:5],
                task_class_filter=[],
            ))

        return items

    def _query_failure_prevention(
        self,
        db: sqlite3.Connection,
        task_class: str,
        scope_tags: List[str],
    ) -> List[IntelligenceItem]:
        """Query antipatterns and prevention_rules for failure_prevention candidates."""
        items: List[IntelligenceItem] = []

        # Query antipatterns
        try:
            rows = db.execute(
                """
                SELECT id, title, description, category, severity,
                       why_problematic, better_alternative,
                       occurrence_count, first_seen, last_seen
                FROM antipatterns
                WHERE occurrence_count >= 1
                  AND (valid_until IS NULL OR valid_until > datetime('now'))
                ORDER BY
                    CASE severity
                        WHEN 'critical' THEN 4
                        WHEN 'high' THEN 3
                        WHEN 'medium' THEN 2
                        WHEN 'low' THEN 1
                        ELSE 0
                    END DESC,
                    occurrence_count DESC
                LIMIT 5
                """,
            ).fetchall()
        except Exception:
            rows = []

        severity_confidence = {"critical": 0.9, "high": 0.75, "medium": 0.6, "low": 0.5}

        for row in rows:
            row_d = dict(row)
            category = row_d.get("category", "")
            pattern_scope = [category] if category else []

            if not _scope_matches(pattern_scope, scope_tags):
                continue

            content_parts = []
            if row_d.get("why_problematic"):
                content_parts.append(row_d["why_problematic"])
            if row_d.get("better_alternative"):
                content_parts.append(f"Instead: {row_d['better_alternative']}")
            content = " ".join(content_parts)[:MAX_CONTENT_CHARS_PER_ITEM]

            severity = row_d.get("severity", "medium")
            confidence = severity_confidence.get(severity, 0.5)

            items.append(IntelligenceItem(
                item_id=_stable_item_id("ap", str(row_d.get("id", ""))),
                item_class="failure_prevention",
                title=(row_d.get("title") or "Failure prevention")[:120],
                content=content,
                confidence=confidence,
                evidence_count=int(row_d.get("occurrence_count", 1)),
                last_seen=row_d.get("last_seen") or row_d.get("first_seen") or _now_utc(),
                scope_tags=pattern_scope,
                source_refs=[f"antipattern_{row_d['id']}"],
                task_class_filter=[],
            ))

        # Query prevention_rules
        try:
            rule_rows = db.execute(
                """
                SELECT id, tag_combination, rule_type, description,
                       recommendation, confidence, triggered_count, last_triggered
                FROM prevention_rules
                WHERE (valid_until IS NULL OR valid_until > datetime('now'))
                ORDER BY confidence DESC
                LIMIT 10
                """,
            ).fetchall()
        except Exception:
            rule_rows = []

        for row in rule_rows:
            row_d = dict(row)
            tag_combo = row_d.get("tag_combination", "")
            rule_scope = tag_combo.split(",") if tag_combo else []

            if not _scope_matches(rule_scope, scope_tags):
                continue

            content = (row_d.get("recommendation") or row_d.get("description") or "")[:MAX_CONTENT_CHARS_PER_ITEM]

            items.append(IntelligenceItem(
                item_id=_stable_item_id("pr", str(row_d.get("id", ""))),
                item_class="failure_prevention",
                title=(row_d.get("description") or "Prevention rule")[:120],
                content=content,
                confidence=float(row_d.get("confidence", 0.5)),
                evidence_count=max(1, int(row_d.get("triggered_count", 1))),
                last_seen=row_d.get("last_triggered") or _now_utc(),
                scope_tags=rule_scope,
                source_refs=[f"prevention_rule_{row_d['id']}"],
                task_class_filter=[],
            ))

        return items

    def _query_recent_comparable(
        self,
        db: sqlite3.Connection,
        task_class: str,
        scope_tags: List[str],
    ) -> List[IntelligenceItem]:
        """Query dispatch_metadata for recent_comparable candidates."""
        items: List[IntelligenceItem] = []
        cutoff = (datetime.now(timezone.utc) - timedelta(days=RECENT_COMPARABLE_DAYS)).isoformat()

        try:
            rows = db.execute(
                """
                SELECT dispatch_id, terminal, track, role, skill_name, gate,
                       outcome_status, dispatched_at, pattern_count,
                       prevention_rule_count
                FROM dispatch_metadata
                WHERE dispatched_at >= ?
                  AND outcome_status IS NOT NULL
                ORDER BY dispatched_at DESC
                LIMIT 20
                """,
                (cutoff,),
            ).fetchall()
        except Exception:
            return items

        for row in rows:
            row_d = dict(row)
            dispatch_scope = []
            if row_d.get("skill_name"):
                dispatch_scope.append(row_d["skill_name"])
            if row_d.get("gate"):
                dispatch_scope.append(row_d["gate"])
            if row_d.get("track"):
                dispatch_scope.append(f"Track-{row_d['track']}")

            if not _scope_matches(dispatch_scope, scope_tags):
                continue

            outcome = row_d.get("outcome_status", "unknown")
            skill = row_d.get("skill_name") or row_d.get("role") or "unknown"
            gate = row_d.get("gate") or ""

            content = (
                f"Dispatch {row_d['dispatch_id']} ({skill}, {gate}) "
                f"completed with status: {outcome}. "
                f"Patterns used: {row_d.get('pattern_count', 0)}, "
                f"Prevention rules: {row_d.get('prevention_rule_count', 0)}."
            )[:MAX_CONTENT_CHARS_PER_ITEM]

            confidence = 0.7 if outcome == "success" else 0.45

            items.append(IntelligenceItem(
                item_id=_stable_item_id("dm", str(row_d.get("dispatch_id", ""))),
                item_class="recent_comparable",
                title=f"Recent: {skill} dispatch ({outcome})"[:120],
                content=content,
                confidence=confidence,
                evidence_count=1,
                last_seen=row_d.get("dispatched_at") or _now_utc(),
                scope_tags=dispatch_scope,
                source_refs=[row_d["dispatch_id"]],
                task_class_filter=[],
            ))

        return items

    # ------------------------------------------------------------------
    # Payload enforcement
    # ------------------------------------------------------------------

    def _enforce_payload_limit(
        self,
        selected: List[IntelligenceItem],
        suppressed: List[SuppressionRecord],
    ) -> List[IntelligenceItem]:
        """Enforce MAX_PAYLOAD_CHARS by dropping lowest-priority items first.

        Drop order (per contract Section 2.3 step 6):
          1. recent_comparable
          2. failure_prevention
          3. proven_pattern (last resort)
        """
        if not selected:
            return selected

        payload_size = len(json.dumps({
            "injection_point": "dispatch_create",
            "injected_at": _now_utc(),
            "items": [item.to_dict() for item in selected],
            "suppressed": [s.to_dict() for s in suppressed],
        }))

        if payload_size <= MAX_PAYLOAD_CHARS:
            return selected

        # Drop in reverse priority order
        drop_order = list(reversed(ITEM_CLASS_PRIORITY))
        for drop_class in drop_order:
            to_drop = [i for i in selected if i.item_class == drop_class]
            if not to_drop:
                continue

            selected = [i for i in selected if i.item_class != drop_class]
            suppressed.append(SuppressionRecord(
                item_class=drop_class,
                reason=f"dropped to enforce payload limit ({payload_size} > {MAX_PAYLOAD_CHARS} chars)",
            ))

            payload_size = len(json.dumps({
                "injection_point": "dispatch_create",
                "injected_at": _now_utc(),
                "items": [item.to_dict() for item in selected],
                "suppressed": [s.to_dict() for s in suppressed],
            }))

            if payload_size <= MAX_PAYLOAD_CHARS:
                break

        return selected


# ---------------------------------------------------------------------------
# Module-level convenience
# ---------------------------------------------------------------------------

def select_intelligence(
    dispatch_id: str,
    injection_point: str,
    *,
    quality_db_path: Optional[Path] = None,
    coord_state_dir: Optional[Path] = None,
    task_class: Optional[str] = None,
    skill_name: Optional[str] = None,
    scope_tags: Optional[List[str]] = None,
    track: Optional[str] = None,
    gate: Optional[str] = None,
) -> InjectionResult:
    """Convenience function: select, emit event, record injection, return result."""
    selector = IntelligenceSelector(
        quality_db_path=quality_db_path,
        coord_db_state_dir=coord_state_dir,
    )
    try:
        result = selector.select(
            dispatch_id=dispatch_id,
            injection_point=injection_point,
            task_class=task_class,
            skill_name=skill_name,
            scope_tags=scope_tags,
            track=track,
            gate=gate,
        )
        selector.emit_event(result)
        selector.record_injection(result)
        return result
    finally:
        selector.close()
