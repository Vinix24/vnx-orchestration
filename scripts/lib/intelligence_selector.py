#!/usr/bin/env python3
"""
VNX Intelligence Selector — orchestrator for bounded dispatch injection.

Implements FP-C Intelligence Contract (docs/core/31_FPC_INTELLIGENCE_CONTRACT.md).
Per-source query logic lives in scripts/lib/intelligence_sources/.
This module coordinates budgets, applies selection, records audit.

Governance: G-R5 (max 3 items), G-R6 (confidence+evidence+scope), G-R7 (advisory-only).
"""

from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path
from typing import Dict, List, Optional

from intelligence_sources import (  # noqa: F401  (re-exported for backward compat)
    CONFIDENCE_THRESHOLDS, EVIDENCE_THRESHOLDS, ITEM_CLASS_PRIORITY,
    MAX_CODE_ANCHOR_CHARS, MAX_CONTENT_CHARS_PER_ITEM, MAX_GOVERNANCE_PER_BATCH,
    MAX_ITEMS_PER_INJECTION, MAX_PAYLOAD_CHARS, PATTERN_CATEGORY_GOVERNANCE,
    VALID_INJECTION_POINTS,
    IntelligenceItem, SuppressionRecord,
    InjectionResult, IntelligenceContext,
    _new_id, _now_utc, _table_has_column,
    apply_candidate_diversity, resolve_task_class,
    query_proven_patterns, query_failure_prevention, query_recent_comparable,
    build_adr_item, build_schema_section_item,
    build_code_anchor_item, build_operator_memory_item, build_prior_round_item,
    record_injection_audit, record_pattern_usage,
)
from intelligence_sources import stamp_source_dispatch_ids as _stamp

logger = logging.getLogger(__name__)

try:
    from vnx_paths import resolve_central_data_dir as _resolve_central_data_dir
except ImportError:
    _resolve_central_data_dir = None  # type: ignore[assignment]

try:
    from project_scope import current_project_id, project_filter_enabled
except ImportError:
    import sys as _sys
    _sys.path.insert(0, str(Path(__file__).resolve().parent))
    from project_scope import current_project_id, project_filter_enabled

# Direct injection source table: (item_class, cumulative_drop_order)
_DIRECT_SOURCES = [
    ("prior_round_finding", ["prior_round_finding"]),
    ("adr_relevant",        ["prior_round_finding", "adr_relevant"]),
    ("code_anchor",         ["prior_round_finding", "adr_relevant", "code_anchor"]),
    ("operator_memory",     ["prior_round_finding", "adr_relevant", "code_anchor", "operator_memory"]),
    ("schema_section",      ["prior_round_finding", "adr_relevant", "code_anchor", "operator_memory", "schema_section"]),
]


class IntelligenceSelector:
    """Selects bounded, evidence-backed intelligence items for dispatch injection."""

    def __init__(self, quality_db_path=None, coord_db_state_dir=None) -> None:
        self._quality_db_path = quality_db_path
        self._coord_state_dir = coord_db_state_dir
        self._quality_db: Optional[sqlite3.Connection] = None

    def _get_quality_db(self) -> Optional[sqlite3.Connection]:
        if self._quality_db is not None:
            return self._quality_db
        if self._quality_db_path is None or not self._quality_db_path.exists():
            return None
        try:
            self._quality_db = sqlite3.connect(str(self._quality_db_path))
            self._quality_db.row_factory = sqlite3.Row
        except Exception as exc:
            logger.warning("_get_quality_db: failed to open quality_intelligence DB at %s: %s", self._quality_db_path, exc)
            self._quality_db = None
        return self._quality_db

    def close(self) -> None:
        if self._quality_db:
            self._quality_db.close()
            self._quality_db = None

    def _has_column(self, table: str, column: str) -> bool:
        db = self._get_quality_db()
        return _table_has_column(db, table, column) if db is not None else False

    def _maybe_reconcile_confidence(self) -> None:
        if self._quality_db_path is None:
            return
        try:
            from confidence_reconcile import maybe_reconcile
            maybe_reconcile(self._quality_db_path)
        except (ImportError, sqlite3.Error, OSError) as e:
            logger.debug("Reconciliation step skipped: %s", e)

    def _get_central_qi_conn(self) -> Optional[sqlite3.Connection]:
        if _resolve_central_data_dir is None:
            return None
        try:
            project_id = current_project_id()
            if not project_id:
                return None
            db_path = _resolve_central_data_dir(project_id) / "state" / "quality_intelligence.db"
            if not db_path.exists():
                return None
            conn = sqlite3.connect(str(db_path))
            conn.row_factory = sqlite3.Row
            return conn
        except Exception as exc:
            logger.debug("_get_central_qi_conn: failed to connect to central DB: %s", exc)
            return None

    def _query_candidates(self, task_class: str, scope_tags: List[str]) -> Dict[str, List[IntelligenceItem]]:
        """Query all standard-class candidates from quality_intelligence.db."""
        db = self._get_quality_db()
        result: Dict[str, List[IntelligenceItem]] = {"proven_pattern": [], "failure_prevention": [], "recent_comparable": []}
        if db is None:
            return result
        kw = dict(has_column_fn=self._has_column, central_conn_fn=self._get_central_qi_conn, project_id_fn=current_project_id)
        result["proven_pattern"] = query_proven_patterns(db, task_class, scope_tags, reconcile_fn=self._maybe_reconcile_confidence, **kw)
        result["failure_prevention"] = query_failure_prevention(db, task_class, scope_tags, **kw)
        result["recent_comparable"] = query_recent_comparable(db, task_class, scope_tags, **kw)
        return result

    def select(
        self,
        dispatch_id: str,
        injection_point: str,
        *,
        task_class=None, skill_name=None, scope_tags=None,
        track=None, gate=None, pr_id=None,
        dispatch_paths=None, instruction_text=None,
    ) -> InjectionResult:
        """Run the bounded selection algorithm and return an InjectionResult."""
        if injection_point not in VALID_INJECTION_POINTS:
            raise ValueError(f"Invalid injection_point: {injection_point!r}. Must be one of {sorted(VALID_INJECTION_POINTS)}")
        resolved_class = resolve_task_class(task_class, skill_name, dispatch_paths, instruction_text)
        effective_scope: List[str] = list(scope_tags or [])
        for tag in [skill_name, (f"Track-{track}" if track and not track.startswith("Track-") else track), gate, resolved_class]:
            if tag and tag not in effective_scope:
                effective_scope.append(tag)

        candidates = apply_candidate_diversity(self._query_candidates(resolved_class, effective_scope), resolved_class)
        selected, suppressed = self._select_standard_classes(candidates)
        selected = self._enforce_payload_limit(selected, suppressed, list(reversed(ITEM_CLASS_PRIORITY)))

        now_ts = _now_utc()
        paths = dispatch_paths or []
        direct = {
            "prior_round_finding": build_prior_round_item(pr_id or "", paths, now_ts) if pr_id else None,
            "adr_relevant": build_adr_item(dispatch_id, paths, now_ts) if paths else None,
            "code_anchor": build_code_anchor_item(dispatch_id, paths, instruction_text or "", now_ts) if (paths and instruction_text) else None,
            "operator_memory": build_operator_memory_item(dispatch_id, skill_name, paths, instruction_text or "", now_ts) if (skill_name or paths or instruction_text) else None,
            "schema_section": build_schema_section_item(dispatch_id, paths, instruction_text or "", now_ts) if (paths or instruction_text) else None,
        }
        for class_name, extra_drop in _DIRECT_SOURCES:
            item = direct.get(class_name)
            if item is not None:
                selected.append(item)
                selected = self._enforce_payload_limit(selected, suppressed, list(reversed(ITEM_CLASS_PRIORITY)) + extra_drop)

        return InjectionResult(injection_point=injection_point, injected_at=now_ts, items=selected, suppressed=suppressed, task_class=resolved_class, dispatch_id=dispatch_id)

    def _select_standard_classes(self, candidates):
        selected: List[IntelligenceItem] = []
        suppressed: List[SuppressionRecord] = []
        seen_hashes: set = set()
        governance_used = 0
        for item_class in ITEM_CLASS_PRIORITY:
            class_candidates = candidates.get(item_class, [])
            if not class_candidates:
                suppressed.append(SuppressionRecord(item_class=item_class, reason="no candidates available"))
                continue
            threshold = CONFIDENCE_THRESHOLDS[item_class]
            evidence_min = EVIDENCE_THRESHOLDS[item_class]
            eligible = [c for c in class_candidates if c.confidence >= threshold and c.evidence_count >= evidence_min]
            if not eligible:
                best_conf = max(c.confidence for c in class_candidates)
                suppressed.append(SuppressionRecord(item_class=item_class, reason=f"confidence {best_conf:.2f} below threshold {threshold}"))
                continue
            diverse, dropped_dup, dropped_gov = [], 0, 0
            for cand in eligible:
                if cand.content_hash and cand.content_hash in seen_hashes:
                    dropped_dup += 1
                elif cand.pattern_category == PATTERN_CATEGORY_GOVERNANCE and governance_used >= MAX_GOVERNANCE_PER_BATCH:
                    dropped_gov += 1
                else:
                    diverse.append(cand)
            if not diverse:
                parts = ([f"{dropped_dup} duplicates removed by content hash"] if dropped_dup else []) + ([f"{dropped_gov} governance items past per-batch cap"] if dropped_gov else [])
                suppressed.append(SuppressionRecord(item_class=item_class, reason=f"diversity filter dropped all eligible items ({'; '.join(parts) or 'no diverse candidates remain'})"))
                continue
            best = max(diverse, key=lambda c: c.confidence)
            selected.append(best)
            if best.content_hash:
                seen_hashes.add(best.content_hash)
            if best.pattern_category == PATTERN_CATEGORY_GOVERNANCE:
                governance_used += 1
        return selected, suppressed


    def _enforce_payload_limit(self, selected, suppressed, drop_order=None) -> List[IntelligenceItem]:
        """Drop lowest-priority classes until payload fits within MAX_PAYLOAD_CHARS."""
        if drop_order is None:
            drop_order = list(reversed(ITEM_CLASS_PRIORITY))
        if not selected:
            return selected
        def _size():
            return len(json.dumps({"injection_point": "x", "injected_at": "x", "items": [i.to_dict() for i in selected], "suppressed": [s.to_dict() for s in suppressed]}))
        if _size() <= MAX_PAYLOAD_CHARS:
            return selected
        for drop_class in drop_order:
            if not any(i.item_class == drop_class for i in selected):
                continue
            sz = _size()
            selected = [i for i in selected if i.item_class != drop_class]
            suppressed.append(SuppressionRecord(item_class=drop_class, reason=f"dropped to enforce payload limit ({sz} > {MAX_PAYLOAD_CHARS} chars)"))
            if _size() <= MAX_PAYLOAD_CHARS:
                break
        return selected

    def emit_event(self, result: InjectionResult, coord_state_dir=None) -> Optional[str]:
        """Emit an injection or suppression coordination event."""
        if not result.dispatch_id or not str(result.dispatch_id).strip():
            raise ValueError("emit_event: dispatch_id required for audit attribution")
        state_dir = coord_state_dir or self._coord_state_dir
        if state_dir is None:
            return None
        try:
            from runtime_coordination import get_connection, _append_event
        except ImportError:
            return None
        event_type = "intelligence_injection" if result.items_injected > 0 else "intelligence_suppression"
        reason = f"injected {result.items_injected} items at {result.injection_point}" if result.items_injected > 0 else "no items met minimum thresholds"
        try:
            with get_connection(state_dir) as conn:
                event_id = _append_event(conn, event_type=event_type, entity_type="dispatch", entity_id=result.dispatch_id, actor="intelligence_selector", reason=reason, metadata=result.to_event_metadata())
                conn.commit()
            return event_id
        except Exception as exc:
            logger.warning("emit_event: failed to append coordination event for dispatch %s: %s", result.dispatch_id, exc)
            return None

    def record_injection(self, result: InjectionResult, coord_state_dir=None) -> None:
        """Record injection decision in intelligence_injections audit table."""
        if not result.dispatch_id or not str(result.dispatch_id).strip():
            raise ValueError("record_injection: dispatch_id required for audit attribution")
        state_dir = coord_state_dir or self._coord_state_dir
        if state_dir is not None:
            record_injection_audit(result, state_dir, current_project_id())
        if result.items and self._quality_db_path is not None and self._quality_db_path.exists():
            record_pattern_usage(result, self._get_quality_db(), self._has_column)
        try:
            _stamp(result, self._get_quality_db())
        except (sqlite3.Error, OSError) as e:
            logger.debug("Failed to stamp source_dispatch_ids: %s", e)

    def stamp_source_dispatch_ids(self, result: InjectionResult) -> int:
        """Public injection-time stamping helper (Phase 1.5 PR-2 / OI-1315)."""
        return _stamp(result, self._get_quality_db())



def select_intelligence(
    dispatch_id,
    injection_point,
    *,
    quality_db_path=None,
    coord_state_dir=None,
    task_class=None,
    skill_name=None,
    scope_tags=None,
    track=None,
    gate=None,
    dispatch_paths=None,
    instruction_text=None,
) -> InjectionResult:
    """Convenience function: select, emit event, record injection, return result."""
    selector = IntelligenceSelector(quality_db_path=quality_db_path, coord_db_state_dir=coord_state_dir)
    try:
        result = selector.select(
            dispatch_id=dispatch_id,
            injection_point=injection_point,
            task_class=task_class,
            skill_name=skill_name,
            scope_tags=scope_tags,
            track=track,
            gate=gate,
            dispatch_paths=dispatch_paths,
            instruction_text=instruction_text,
        )
        selector.emit_event(result)
        selector.record_injection(result)
        return result
    finally:
        selector.close()


def build_intelligence_context(*, dispatch_id="", role="", pr_id=None, dispatch_paths=None, quality_db_path=None, coord_state_dir=None) -> Optional[IntelligenceContext]:
    """Build an IntelligenceContext for adapter prompt assembly.

    Returns None immediately when dispatch_id is empty (no audit rows written).
    """
    if not dispatch_id or not str(dispatch_id).strip():
        logger.debug("build_intelligence_context: empty dispatch_id, skipping injection (no audit write)")
        return None
    selector = IntelligenceSelector(quality_db_path=quality_db_path, coord_db_state_dir=coord_state_dir)
    try:
        result = selector.select(dispatch_id=dispatch_id, injection_point="dispatch_create", skill_name=role or "", dispatch_paths=dispatch_paths or [], pr_id=pr_id)
        try:
            selector.emit_event(result, coord_state_dir=coord_state_dir)
        except Exception as exc:
            logger.debug("build_intelligence_context: emit_event failed for %s: %s", dispatch_id, exc)
        try:
            selector.record_injection(result, coord_state_dir=coord_state_dir)
        except Exception as exc:
            logger.debug("build_intelligence_context: record_injection failed for %s: %s", dispatch_id, exc)
        return IntelligenceContext(result=result, dispatch_id=dispatch_id)
    finally:
        selector.close()
