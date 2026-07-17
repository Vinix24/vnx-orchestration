#!/usr/bin/env python3
"""
VNX Learning Loop & Optimization System
Tracks pattern usage, adjusts confidence scores, and optimizes intelligence delivery.
Runs daily at 18:00 to analyze receipts and update pattern effectiveness.
"""

import hashlib
import json
import logging
import os
import sqlite3
import time
import sys
from pathlib import Path
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from dataclasses import dataclass
from collections import defaultdict
import re

log = logging.getLogger(__name__)

script_dir = Path(__file__).resolve().parent
sys.path.insert(0, str(script_dir / "lib"))
try:
    from vnx_paths import ensure_env
except Exception as exc:
    raise SystemExit(f"Failed to load vnx_paths: {exc}")
from report_contract_scope import contract_invalid_effective_timestamp, is_stale_contract_invalid


# Failure statuses sampled from the governed receipt stream when mining for
# recurring failure patterns. Keep in sync with check_active_drain.FAILURE_STATUSES,
# weekly_digest._FAILURE_STATUSES, receipt_classifier._FAILURE_STATUSES, and
# payload.FAILURE_STATUSES (gate-F2). "timeout" is included here (unlike the
# confidence-scoring set in payload.py): a recurring task_timeout is a legitimate
# failure to learn a prevention rule from — generate_prevention_suggestion has a
# dedicated 'timeout' branch.
_FAILURE_STATUSES = frozenset(
    {"failed", "failure", "error", "blocked", "timeout", "contract_invalid"}
)


def _to_aware_utc(dt: Optional[datetime]) -> Optional[datetime]:
    """Normalize a datetime to timezone-aware UTC, handling naive datetimes gracefully."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _parse_receipt_timestamp(value) -> Optional[datetime]:
    """Parse a receipt timestamp string to tz-aware UTC, or None if unparseable.

    Handles both ISO forms seen in t0_receipts.ndjson:
      "2026-05-07T08:31:55.295343+00:00" and "2026-06-26T12:51:47Z".
    """
    if not value or not isinstance(value, str):
        return None
    raw = value.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        return _to_aware_utc(datetime.fromisoformat(raw))
    except ValueError:
        # Fall back to a date-only prefix (day-granularity window).
        try:
            return _to_aware_utc(datetime.fromisoformat(raw[:10]))
        except ValueError:
            return None


class LearningLoopMisconfigured(RuntimeError):
    """Raised when VNX_LEARNING_LOOP_ENABLED=1 but VNX_INJECTION_FEEDBACK_ENABLED=0.

    The injection-effectiveness probe (PR-6) is the thing that gates the loop;
    arming the loop without the probe that gates it is a hard misconfiguration,
    not a degraded-but-tolerable state (framework-status-audit-and-cockpit PR-17).
    """


def evaluate_activation_gate(state_dir: Optional[Path] = None) -> Dict[str, Any]:
    """The PR-17 activation gate: four explicit, non-overlapping paths.

    1. ``VNX_LEARNING_LOOP_ENABLED=0`` (default)            -> dormant no-op.
    2. ``=1`` and ``VNX_INJECTION_FEEDBACK_ENABLED=0``        -> raises
       ``LearningLoopMisconfigured`` (cannot activate learning without the
       effectiveness probe that gates it).
    3. Both enabled, probe health is anything other than ``"ok"`` (``unknown``,
       ``degraded``, ``produces_crap``)                       -> degraded, no
       pattern updates. ``degraded`` is deliberately gated here alongside
       ``unknown``/``produces_crap`` — it is NOT healthy enough to activate.
    4. Both enabled and probe health ``"ok"``                 -> run.

    The activation gate is the probe health, not a flag: the two env flags are
    only the arm-switch: they decide whether the probe is even consulted.

    Returns ``{"action": "dormant"|"degraded"|"run", "probe_health": str|None,
    "detail": str}``.
    """
    learning_enabled = os.environ.get("VNX_LEARNING_LOOP_ENABLED", "0") == "1"
    feedback_enabled = os.environ.get("VNX_INJECTION_FEEDBACK_ENABLED", "0") == "1"

    if not learning_enabled:
        return {"action": "dormant", "probe_health": None, "detail": "VNX_LEARNING_LOOP_ENABLED=0"}

    if not feedback_enabled:
        raise LearningLoopMisconfigured(
            "VNX_LEARNING_LOOP_ENABLED=1 requires VNX_INJECTION_FEEDBACK_ENABLED=1 — "
            "the injection-effectiveness probe (PR-6) gates the learning loop; "
            "cannot activate learning without it."
        )

    from injection_effectiveness_probe import InjectionEffectivenessProbe

    result = InjectionEffectivenessProbe(state_dir=state_dir).run()

    if result.status != "ok":
        return {"action": "degraded", "probe_health": result.status, "detail": result.signal}

    return {"action": "run", "probe_health": result.status, "detail": result.signal}


@dataclass
class PatternUsageMetric:
    """Track pattern usage statistics"""
    pattern_id: str
    pattern_title: str
    pattern_hash: str  # Hash of pattern content for matching
    used_count: int = 0
    ignored_count: int = 0
    success_count: int = 0
    failure_count: int = 0
    last_used: Optional[datetime] = None
    confidence: float = 1.0
    decay_rate: float = 0.95  # 5% daily decay for unused patterns
    boost_rate: float = 1.10  # 10% boost for used patterns


class LearningLoop:
    """Learning loop for pattern optimization and confidence adjustment"""

    def __init__(self):
        """Initialize learning loop with database connections"""
        paths = ensure_env()
        self.vnx_path = Path(paths["VNX_HOME"])
        state_dir = Path(paths["VNX_STATE_DIR"]).expanduser().resolve()
        self.db_path = state_dir / "quality_intelligence.db"
        # Mine failures from the governed receipt stream (the single NDJSON the
        # receipt processor writes), not the long-gone terminals/file_bus/receipts
        # directory — that path never existed under the central store, which is why
        # the proposal tier never produced a single pending_rules.json.
        self.receipts_path = state_dir / "t0_receipts.ndjson"
        self.archive_path = state_dir / "archive" / "patterns"

        # Create archive directory if it doesn't exist
        self.archive_path.mkdir(parents=True, exist_ok=True)

        # Initialize database connection
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row

        # Initialize pattern tracking
        self.pattern_metrics: Dict[str, PatternUsageMetric] = {}
        self.load_pattern_metrics()

        # Performance metrics
        self.learning_stats = {
            "patterns_tracked": 0,
            "patterns_used": 0,
            "patterns_ignored": 0,
            "patterns_archived": 0,
            "confidence_adjustments": 0,
            "new_patterns_learned": 0
        }

    def load_pattern_metrics(self):
        """Load existing pattern metrics from database"""
        try:
            # Ensure pattern_usage table exists (matches schema definition)
            self.conn.execute('''
                CREATE TABLE IF NOT EXISTS pattern_usage (
                    pattern_id TEXT PRIMARY KEY,
                    pattern_title TEXT NOT NULL,
                    pattern_hash TEXT NOT NULL,
                    used_count INTEGER DEFAULT 0,
                    ignored_count INTEGER DEFAULT 0,
                    success_count INTEGER DEFAULT 0,
                    failure_count INTEGER DEFAULT 0,
                    last_used TIMESTAMP,
                    last_offered TIMESTAMP,
                    confidence REAL DEFAULT 1.0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')

            # Load existing metrics
            cursor = self.conn.execute('SELECT * FROM pattern_usage')
            for row in cursor:
                metric = PatternUsageMetric(
                    pattern_id=row['pattern_id'],
                    pattern_title=row['pattern_title'],
                    pattern_hash=row['pattern_hash'],
                    used_count=row['used_count'],
                    ignored_count=row['ignored_count'],
                    success_count=row['success_count'],
                    failure_count=row['failure_count'],
                    last_used=_to_aware_utc(datetime.fromisoformat(row['last_used'])) if row['last_used'] else None,
                    confidence=row['confidence']
                )
                self.pattern_metrics[metric.pattern_id] = metric

            print(f"📊 Loaded {len(self.pattern_metrics)} pattern metrics")

        except Exception as e:
            print(f"⚠️ Error loading pattern metrics: {e}")

    def extract_used_patterns(self, start_time: datetime = None) -> Dict[str, List[str]]:
        """Extract patterns that were actually used from pattern_usage table.

        Queries patterns where used_count > 0 and updated_at is within the window.
        """
        if not start_time:
            start_time = datetime.now(timezone.utc) - timedelta(hours=24)
        else:
            start_time = _to_aware_utc(start_time)

        used_patterns = defaultdict(list)

        try:
            cursor = self.conn.execute('''
                SELECT pattern_id, used_count, last_used
                FROM pattern_usage
                WHERE used_count > 0
                  AND updated_at >= ?
            ''', (start_time.isoformat(),))

            for row in cursor:
                used_patterns[row['pattern_id']].append(f"db_tracked_{row['used_count']}")

            print(f"  DB query: {len(used_patterns)} used patterns found")

        except Exception as e:
            print(f"  DB query error: {e}")

        return used_patterns

    def extract_ignored_patterns(self, start_time: datetime = None) -> Dict[str, int]:
        """Extract patterns that were offered but never used.

        Queries pattern_usage for patterns with used_count=0 that were recently offered
        (last_offered within the time window).
        """
        if not start_time:
            start_time = datetime.now(timezone.utc) - timedelta(hours=24)
        else:
            start_time = _to_aware_utc(start_time)

        ignored_patterns = defaultdict(int)

        try:
            cursor = self.conn.execute('''
                SELECT pattern_id, ignored_count
                FROM pattern_usage
                WHERE used_count = 0
                  AND last_offered >= ?
            ''', (start_time.isoformat(),))

            for row in cursor:
                ignored_patterns[row['pattern_id']] = max(1, row['ignored_count'])

            if ignored_patterns:
                print(f"  DB query: {len(ignored_patterns)} ignored patterns found")
                return ignored_patterns

        except Exception as e:
            print(f"  DB query fallback: {e}")

        # Fallback: patterns in pattern_usage that have never been used
        try:
            cursor = self.conn.execute('''
                SELECT pattern_id
                FROM pattern_usage
                WHERE used_count = 0
                  AND created_at >= ?
            ''', (start_time.isoformat(),))

            for row in cursor:
                ignored_patterns[row['pattern_id']] = 1

        except sqlite3.OperationalError as e:
            log.debug("DB fallback ignored-patterns query failed: %s", e)

        return ignored_patterns

    def _log_confidence_change(self, pattern_id: str, source: str,
                               old_confidence: float, new_confidence: float) -> None:
        """Append confidence change event to intelligence_usage.ndjson (G-L7)."""
        try:
            paths = ensure_env()
            state_dir = Path(paths["VNX_STATE_DIR"]).expanduser().resolve()
            usage_log = state_dir / "intelligence_usage.ndjson"
            event = {
                "timestamp": datetime.now().isoformat(),
                "event_type": "confidence_change",
                "pattern_id": pattern_id,
                "source": source,
                "old_confidence": round(old_confidence, 6),
                "new_confidence": round(new_confidence, 6),
            }
            with open(usage_log, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(event, separators=(",", ":")) + "\n")
        except OSError:
            pass

    def update_confidence_scores(self, used_patterns: Dict[str, List[str]],
                                ignored_patterns: Dict[str, int]):
        """Update confidence scores based on usage patterns"""

        # Boost confidence for used patterns
        for pattern_id, dispatch_ids in used_patterns.items():
            if pattern_id not in self.pattern_metrics:
                # Create new metric for previously untracked pattern
                self.pattern_metrics[pattern_id] = PatternUsageMetric(
                    pattern_id=pattern_id,
                    pattern_title=f"Pattern_{pattern_id}",
                    pattern_hash=self.hash_pattern(pattern_id)
                )

            metric = self.pattern_metrics[pattern_id]
            metric.used_count += len(dispatch_ids)
            metric.last_used = datetime.now(timezone.utc)

            # Boost confidence (cap at 2.0)
            old_confidence = metric.confidence
            metric.confidence = min(metric.confidence * metric.boost_rate, 2.0)
            self._log_confidence_change(pattern_id, "adoption_boost", old_confidence, metric.confidence)

            self.learning_stats["confidence_adjustments"] += 1
            print(f"📈 Boosted {pattern_id}: {old_confidence:.3f} → {metric.confidence:.3f}")

        # Decay confidence for ignored patterns
        now = datetime.now().isoformat()
        for pattern_id, ignore_count in ignored_patterns.items():
            if pattern_id not in self.pattern_metrics:
                self.pattern_metrics[pattern_id] = PatternUsageMetric(
                    pattern_id=pattern_id,
                    pattern_title=f"Pattern_{pattern_id}",
                    pattern_hash=self.hash_pattern(pattern_id)
                )

            metric = self.pattern_metrics[pattern_id]
            metric.ignored_count += ignore_count

            # Decay confidence (floor at 0.1)
            old_confidence = metric.confidence
            metric.confidence = max(metric.confidence * metric.decay_rate, 0.1)
            self._log_confidence_change(pattern_id, "ignore_decay", old_confidence, metric.confidence)

            # Persist ignored_count increment to DB
            try:
                self.conn.execute('''
                    UPDATE pattern_usage
                    SET ignored_count = ignored_count + ?, updated_at = ?
                    WHERE pattern_id = ?
                ''', (ignore_count, now, pattern_id))
            except sqlite3.OperationalError as e:
                log.debug("Failed to update ignored_count for %s: %s", pattern_id, e)

            self.learning_stats["confidence_adjustments"] += 1
            print(f"📉 Decayed {pattern_id}: {old_confidence:.3f} → {metric.confidence:.3f}")

        # Commit ignored_count updates
        try:
            self.conn.commit()
        except sqlite3.OperationalError as e:
            log.debug("Failed to commit ignored_count updates: %s", e)

    def extract_failure_patterns(self, start_time: datetime = None) -> List[Dict]:
        """Extract recurring failure patterns from the governed receipt stream.

        Reads the single ``t0_receipts.ndjson`` line by line, keeps receipts whose
        ``status`` is a failure status (``_FAILURE_STATUSES``) inside the time
        window, and projects each onto the failure dict consumed downstream by
        ``generate_prevention_rules`` / ``persist_to_intelligence_db``
        (keys: task / terminal / agent / error / timestamp).

        Filter: receipts with a missing/empty provider AND a failure status are
        skipped — these are the 9,052+ unknown:unknown receipts that carry no
        usable provenance and would poison pattern proposals (D3 data-quality
        guard). The post-filter corpus size is logged.
        """
        if not start_time:
            start_time = datetime.now(timezone.utc) - timedelta(hours=24)
        else:
            start_time = _to_aware_utc(start_time)

        failure_patterns: List[Dict] = []

        if not self.receipts_path.exists():
            return failure_patterns

        total_scanned = 0
        no_provider_skipped = 0

        try:
            with open(self.receipts_path, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        receipt = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    total_scanned += 1
                    status = str(receipt.get("status", "")).lower()
                    if status not in _FAILURE_STATUSES:
                        continue

                    # A frozen contract_invalid batch (bulk-emitted, single old
                    # timestamp) is never a recurring pattern worth learning from —
                    # exclude regardless of how far back start_time itself reaches.
                    if status == "contract_invalid" and is_stale_contract_invalid(
                        contract_invalid_effective_timestamp(receipt)
                    ):
                        continue

                    # D3 data-quality filter: skip no-provider failure receipts.
                    # Targets receipts where 'provider' is explicitly set to a
                    # none-sentinel ("none", "unknown", "null", ""), e.g. the 9,052+
                    # unknown:unknown receipts. Receipts where the field is absent
                    # entirely (e.g. contract_invalid from the receipt processor)
                    # pass through — they carry usable governance provenance.
                    raw_provider = receipt.get("provider")
                    if raw_provider is not None and str(raw_provider).strip().lower() in (
                        "", "none", "null", "unknown"
                    ):
                        no_provider_skipped += 1
                        continue

                    # Window filter on the receipt timestamp. Fail-open on a
                    # missing/unparseable timestamp — only a receipt we can
                    # POSITIVELY date as older than start_time is excluded.
                    # Dropping on ts_dt is None here used to contradict the
                    # is_stale_contract_invalid() fail-open check just above
                    # (which lets a dateless contract_invalid receipt through):
                    # weekly_digest / check_active_drain never drop a record
                    # merely for lacking a timestamp, so this filter mirrors
                    # that convention instead of silently re-excluding what the
                    # staleness check just admitted.
                    ts_raw = receipt.get("timestamp")
                    ts_dt = _parse_receipt_timestamp(ts_raw)
                    if ts_dt is not None and ts_dt < start_time:
                        continue

                    failure_patterns.append({
                        "task": str(receipt.get("task_id") or receipt.get("task_description") or ""),
                        "terminal": str(receipt.get("terminal") or receipt.get("terminal_id") or ""),
                        "agent": str(receipt.get("provider") or receipt.get("model") or ""),
                        "error": self._failure_error_message(receipt),
                        "timestamp": ts_raw or datetime.now(timezone.utc).isoformat(),
                    })

        except OSError as e:
            print(f"⚠️ Error reading receipt stream {self.receipts_path}: {e}")

        post_filter = total_scanned - no_provider_skipped
        print(
            f"  Receipt corpus: {total_scanned} scanned, "
            f"{no_provider_skipped} no-provider filtered → {post_filter} effective; "
            f"{len(failure_patterns)} failures in window"
        )
        return failure_patterns

    def _failure_error_message(self, receipt: Dict) -> str:
        """Derive a stable error string from a failure receipt.

        Prefers the structured ``failure_reason`` (e.g. "Exhausted 3 retries"),
        then summarizes ``contract_violations`` for contract-invalid receipts,
        and finally falls back to scanning any free-text error field.
        """
        reason = receipt.get("failure_reason")
        if reason:
            return str(reason)[:200]

        status = str(receipt.get("status", "")).lower()
        if status == "contract_invalid":
            violations = receipt.get("contract_violations")
            summary = self._summarize_contract_violations(violations)
            if summary:
                return summary[:200]

        for field in ("error", "message", "detail", "terminal_response"):
            text = receipt.get(field)
            if isinstance(text, str) and text.strip():
                return self.extract_error_message(text)

        return "Unknown error"

    @staticmethod
    def _summarize_contract_violations(violations) -> str:
        """Render contract_violations (a list of missing headings) into a key."""
        if isinstance(violations, list) and violations:
            items = [str(v) for v in violations if str(v).strip()]
            if items:
                return "Contract violations: " + ", ".join(items)
        elif isinstance(violations, dict) and violations:
            return "Contract violations: " + ", ".join(sorted(str(k) for k in violations))
        elif isinstance(violations, str) and violations.strip():
            return "Contract violations: " + violations.strip()
        return ""

    def extract_error_message(self, response: str) -> str:
        """Extract error message from terminal response"""
        # Look for common error patterns
        error_patterns = [
            r'Error: (.+)',
            r'Exception: (.+)',
            r'Failed: (.+)',
            r'❌ (.+)',
            r'CRITICAL: (.+)'
        ]

        for pattern in error_patterns:
            match = re.search(pattern, response, re.IGNORECASE)
            if match:
                return match.group(1)[:200]  # Limit error message length

        # If no specific pattern, return first line with 'error'
        for line in response.split('\n'):
            if 'error' in line.lower():
                return line[:200]

        return "Unknown error"

    def generate_prevention_rules(self, failure_patterns: List[Dict]) -> List[Dict]:
        """Generate new prevention rules from failure patterns"""
        new_rules = []

        # Group failures by similar characteristics
        failure_groups = defaultdict(list)
        for failure in failure_patterns:
            # Create a key based on error type and context
            key = (failure['error'][:50], failure['terminal'], failure['agent'] or 'none')
            failure_groups[key].append(failure)

        # Generate rules for repeated failures
        for (error, terminal, agent), failures in failure_groups.items():
            if len(failures) >= 2:  # Only create rule if pattern repeats
                rule = {
                    'pattern': f"Error pattern: {error}",
                    'terminal_constraint': terminal,
                    'agent_constraint': agent if agent != 'none' else None,
                    'prevention': self.generate_prevention_suggestion(error, failures),
                    'confidence': min(len(failures) * 0.2, 0.9),  # Confidence based on frequency
                    'occurrence_count': len(failures)
                }
                new_rules.append(rule)

        return new_rules

    def generate_prevention_suggestion(self, error: str, failures: List[Dict]) -> str:
        """Generate prevention suggestion based on error pattern"""
        error_lower = error.lower()

        # Common error patterns and their preventions
        if 'agent' in error_lower and 'not found' in error_lower:
            return "Validate agent exists in agent_template_directory.yaml before dispatch"
        elif 'import' in error_lower or 'module' in error_lower:
            return "Check dependencies and imports before task execution"
        elif 'timeout' in error_lower:
            return "Increase timeout or break task into smaller chunks"
        elif 'memory' in error_lower or 'oom' in error_lower:
            return "Monitor memory usage and implement resource limits"
        elif 'permission' in error_lower:
            return "Verify file permissions and access rights"
        elif 'connection' in error_lower or 'network' in error_lower:
            return "Check network connectivity and retry with backoff"
        else:
            # Generic prevention based on frequency
            if len(failures) > 5:
                return f"High-frequency error: implement specific handling for this case"
            else:
                return f"Monitor for recurrence and gather more context"

    def update_terminal_constraints(self, new_rules: List[Dict]):
        """Queue new prevention rules for operator confirmation (G-L1: no auto-activation).

        Rules are written to pending_rules.json for operator review.
        They are NOT inserted directly into prevention_rules table.
        """
        if not new_rules:
            return
        try:
            paths = ensure_env()
            state_dir = Path(paths["VNX_STATE_DIR"]).expanduser().resolve()
            pending_path = state_dir / "pending_rules.json"

            # Load existing pending rules
            existing: List[Dict] = []
            if pending_path.exists():
                try:
                    data = json.loads(pending_path.read_text(encoding="utf-8"))
                    existing = data.get("pending_rules", [])
                except (json.JSONDecodeError, OSError):
                    existing = []

            now = datetime.now().isoformat()
            for rule in new_rules:
                queued = {
                    "id": f"rule-{hashlib.sha1((rule['pattern'] + rule.get('terminal_constraint', '')).encode()).hexdigest()[:8]}",
                    "created_at": now,
                    "source": "learning_loop",
                    "rule_type": "failure_prevention",
                    "pattern": rule["pattern"],
                    "terminal_constraint": rule.get("terminal_constraint", "any"),
                    "prevention": rule["prevention"],
                    "confidence": rule["confidence"],
                    "occurrence_count": rule.get("occurrence_count", 1),
                    "status": "pending",
                }
                # Deduplicate by id
                if not any(e.get("id") == queued["id"] for e in existing):
                    existing.append(queued)

            pending_path.write_text(
                json.dumps({"pending_rules": existing}, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            print(f"📋 Queued {len(new_rules)} prevention rules for operator review → {pending_path}")

        except Exception as e:
            print(f"❌ Error queuing prevention rules: {e}")

    def persist_to_intelligence_db(self):
        """Persist high-confidence patterns and failure data to intelligence DB tables.

        Bridges the gap between pattern_usage (learning loop internal) and
        success_patterns/antipatterns (intelligence_selector reads).

        - Patterns with used_count > 0 and confidence >= 0.6 → success_patterns
        - Failure patterns with occurrence >= 2 → antipatterns
        """
        now = datetime.now().isoformat()
        patterns_written = 0
        antipatterns_written = 0

        try:
            # Write high-confidence used patterns to success_patterns
            for pattern_id, metric in self.pattern_metrics.items():
                if metric.used_count > 0 and metric.confidence >= 0.6:
                    title = metric.pattern_title[:120]
                    # Use empty string category so intelligence_selector scope
                    # matching treats these as universal (empty scope = matches all)
                    category = ""

                    existing = self.conn.execute(
                        "SELECT id, usage_count FROM success_patterns "
                        "WHERE title = ? AND pattern_data LIKE '%learning_loop%'",
                        (title,),
                    ).fetchone()

                    if existing:
                        row = dict(existing)
                        self.conn.execute(
                            "UPDATE success_patterns SET usage_count = ?, "
                            "confidence_score = ?, last_used = ? WHERE id = ?",
                            (metric.used_count, min(metric.confidence, 1.0), now, row["id"]),
                        )
                    else:
                        self.conn.execute(
                            "INSERT INTO success_patterns "
                            "(pattern_type, category, title, description, pattern_data, "
                            " confidence_score, usage_count, source_dispatch_ids, first_seen, last_used, valid_from) "
                            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                            ("approach", category, title,
                             f"Learning loop pattern: {metric.pattern_title}",
                             json.dumps({"source": "learning_loop", "pattern_id": pattern_id}),
                             min(metric.confidence, 1.0), metric.used_count,
                             "[]", now, now, now),
                        )
                    patterns_written += 1

            # Write failure patterns with occurrence >= 2 to antipatterns
            failure_patterns = self.extract_failure_patterns()
            failure_groups = defaultdict(list)
            for failure in failure_patterns:
                key = failure['error'][:80]
                failure_groups[key].append(failure)

            for error_key, failures in failure_groups.items():
                if len(failures) < 2:
                    continue
                title = f"Recurring failure: {error_key}"[:120]
                category = ""

                existing = self.conn.execute(
                    "SELECT id, occurrence_count FROM antipatterns "
                    "WHERE title = ? AND pattern_data LIKE '%learning_loop%'",
                    (title,),
                ).fetchone()

                severity = "high" if len(failures) >= 5 else "medium"

                if existing:
                    row = dict(existing)
                    self.conn.execute(
                        "UPDATE antipatterns SET occurrence_count = ?, "
                        "severity = ?, last_seen = ? WHERE id = ?",
                        (len(failures), severity, now, row["id"]),
                    )
                else:
                    self.conn.execute(
                        "INSERT INTO antipatterns "
                        "(pattern_type, category, title, description, pattern_data, "
                        " why_problematic, severity, occurrence_count, "
                        " source_dispatch_ids, first_seen, last_seen, valid_from) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        ("approach", category, title,
                         f"Error seen {len(failures)} times: {error_key}",
                         json.dumps({"source": "learning_loop", "terminals": list({f['terminal'] for f in failures})}),
                         error_key, severity, len(failures),
                         "[]", now, now, now),
                    )
                antipatterns_written += 1

            self.conn.commit()
            print(f"💾 Persisted to intelligence DB: {patterns_written} success patterns, {antipatterns_written} antipatterns")

        except Exception as e:
            print(f"❌ Error persisting to intelligence DB: {e}")
            try:
                self.conn.rollback()
            except sqlite3.OperationalError as rb_err:
                log.debug("Failed to rollback after persist error: %s", rb_err)

    def ingest_approved_rules(self):
        """Ingest operator-approved prevention rules from pending_rules.json into DB.

        Respects G-L1: only rules with status == "approved" are inserted.
        After ingestion, status is updated to "ingested" in the JSON file.
        """
        try:
            paths = ensure_env()
            state_dir = Path(paths["VNX_STATE_DIR"]).expanduser().resolve()
            pending_path = state_dir / "pending_rules.json"

            if not pending_path.exists():
                return

            data = json.loads(pending_path.read_text(encoding="utf-8"))
            rules = data.get("pending_rules", [])

            approved = [r for r in rules if r.get("status") == "approved"]
            if not approved:
                return

            now = datetime.now().isoformat()
            ingested_count = 0

            for rule in approved:
                tag_combo = rule.get("terminal_constraint", "any")
                description = rule.get("pattern", "")[:200]
                recommendation = rule.get("prevention", "")[:500]
                confidence = rule.get("confidence", 0.5)

                # Check for duplicate
                existing = self.conn.execute(
                    "SELECT id FROM prevention_rules WHERE description = ? AND tag_combination = ?",
                    (description, tag_combo),
                ).fetchone()

                if not existing:
                    source_dispatch_id = rule.get("source_dispatch_id") or None
                    self.conn.execute(
                        "INSERT INTO prevention_rules "
                        "(tag_combination, rule_type, description, recommendation, "
                        " confidence, created_at, triggered_count, source_dispatch_id) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (tag_combo, "failure_prevention", description,
                         recommendation, confidence, now, 0, source_dispatch_id),
                    )
                    ingested_count += 1

                # Mark as ingested in JSON
                rule["status"] = "ingested"
                rule["ingested_at"] = now

            self.conn.commit()

            # Write back updated JSON
            pending_path.write_text(
                json.dumps(data, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )

            if ingested_count:
                print(f"✅ Ingested {ingested_count} approved prevention rules into DB")

        except Exception as e:
            print(f"❌ Error ingesting approved rules: {e}")

    def archive_unused_patterns(self, threshold_days: int = 30):
        """Queue unused low-confidence patterns for operator confirmation (G-L4: no auto-archival).

        Candidates are written to pending_archival.json — NOT auto-archived.
        """
        archive_date = datetime.now(timezone.utc) - timedelta(days=threshold_days)

        candidates = []
        for pattern_id, metric in self.pattern_metrics.items():
            aware_last_used = _to_aware_utc(metric.last_used)
            if not aware_last_used or aware_last_used < archive_date:
                if metric.confidence < 0.3:
                    candidates.append(pattern_id)

        if not candidates:
            return

        try:
            paths = ensure_env()
            state_dir = Path(paths["VNX_STATE_DIR"]).expanduser().resolve()
            pending_path = state_dir / "pending_archival.json"

            existing: List[Dict] = []
            if pending_path.exists():
                try:
                    data = json.loads(pending_path.read_text(encoding="utf-8"))
                    existing = data.get("pending_archival", [])
                except (json.JSONDecodeError, OSError):
                    existing = []

            existing_ids = {e.get("pattern_id") for e in existing}
            now = datetime.now().isoformat()
            added = 0
            for pattern_id in candidates:
                if pattern_id in existing_ids:
                    continue
                metric = self.pattern_metrics[pattern_id]
                existing.append({
                    "pattern_id": pattern_id,
                    "title": metric.pattern_title,
                    "last_used": metric.last_used.isoformat() if metric.last_used else None,
                    "confidence": round(metric.confidence, 4),
                    "used_count": metric.used_count,
                    "ignored_count": metric.ignored_count,
                    "reason": f"Unused for {threshold_days}+ days with confidence < 0.3",
                    "queued_at": now,
                    "status": "pending",
                })
                added += 1

            pending_path.write_text(
                json.dumps({"pending_archival": existing}, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            print(f"📋 Queued {added} patterns for archival confirmation → {pending_path}")
            self.learning_stats["patterns_archived"] = added

        except Exception as e:
            print(f"❌ Error queuing archival candidates: {e}")

    def generate_learning_report(self) -> Dict:
        """Generate comprehensive learning report"""
        report = {
            'timestamp': datetime.now().isoformat(),
            'learning_cycle': 'daily',
            'statistics': self.learning_stats,
            'pattern_metrics': {
                'total_patterns': len(self.pattern_metrics),
                'actively_used': sum(1 for m in self.pattern_metrics.values() if m.used_count > 0),
                'high_confidence': sum(1 for m in self.pattern_metrics.values() if m.confidence > 1.5),
                'low_confidence': sum(1 for m in self.pattern_metrics.values() if m.confidence < 0.5),
                'archived_today': self.learning_stats["patterns_archived"]
            },
            'top_patterns': [],
            'bottom_patterns': [],
            'new_prevention_rules': []
        }

        # Get top 5 most used patterns
        sorted_patterns = sorted(
            self.pattern_metrics.values(),
            key=lambda x: x.used_count * x.confidence,
            reverse=True
        )

        for pattern in sorted_patterns[:5]:
            report['top_patterns'].append({
                'id': pattern.pattern_id,
                'title': pattern.pattern_title,
                'used_count': pattern.used_count,
                'confidence': round(pattern.confidence, 3),
                'last_used': pattern.last_used.isoformat() if pattern.last_used else None
            })

        # Get bottom 5 least effective patterns
        for pattern in sorted_patterns[-5:]:
            report['bottom_patterns'].append({
                'id': pattern.pattern_id,
                'title': pattern.pattern_title,
                'ignored_count': pattern.ignored_count,
                'confidence': round(pattern.confidence, 3),
                'last_used': pattern.last_used.isoformat() if pattern.last_used else None
            })

        # Save report to state directory (via VNX_STATE_DIR)
        paths = ensure_env()
        state_dir = Path(paths["VNX_STATE_DIR"]).expanduser().resolve()
        report_file = state_dir / f"learning_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        with open(report_file, 'w') as f:
            json.dump(report, f, indent=2)

        print(f"📊 Learning report saved to {report_file}")
        return report

    def save_pattern_metrics(self):
        """Save pattern metrics back to database"""
        for pattern_id, metric in self.pattern_metrics.items():
            self.conn.execute('''
                INSERT OR REPLACE INTO pattern_usage
                (pattern_id, pattern_title, pattern_hash, used_count, ignored_count,
                 success_count, failure_count, last_used, confidence, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                pattern_id,
                metric.pattern_title,
                metric.pattern_hash,
                metric.used_count,
                metric.ignored_count,
                metric.success_count,
                metric.failure_count,
                metric.last_used.isoformat() if metric.last_used else None,
                metric.confidence,
                datetime.now().isoformat()
            ))

        self.conn.commit()

    def hash_pattern(self, pattern_id: str) -> str:
        """Create SHA1 hash of pattern_id, consistent with gather_intelligence.py"""
        import hashlib
        return hashlib.sha1(pattern_id.encode("utf-8")).hexdigest()

    def _supersede_stale_patterns(self) -> int:
        """Queue low-confidence stale patterns for operator-gated suppression (D3 G-L4 gate).

        Candidates are written to pending_archival.json with action="supersede" and
        status="pending". valid_until is NOT set automatically — operator approval is
        required before any pattern is suppressed. This closes the G-L4 ungated bypass.

        Applies to:
          - success_patterns (confidence_score < 0.3, older than 30 days)
          - prevention_rules  (confidence < 0.3, older than 30 days)
        Antipatterns have no numeric confidence column and are skipped.

        Off-switch: set VNX_LEARN_SUPERSEDE=0 to skip this step entirely.
        """
        if os.environ.get("VNX_LEARN_SUPERSEDE", "1") == "0":
            print("  VNX_LEARN_SUPERSEDE=0 — supersede step skipped")
            return 0

        total = 0
        try:
            paths = ensure_env()
            state_dir = Path(paths["VNX_STATE_DIR"]).expanduser().resolve()
            pending_path = state_dir / "pending_archival.json"

            existing: List[Dict] = []
            if pending_path.exists():
                try:
                    data = json.loads(pending_path.read_text(encoding="utf-8"))
                    existing = data.get("pending_archival", [])
                except (json.JSONDecodeError, OSError):
                    existing = []

            existing_keys = {
                (str(e.get("pattern_id", "")), e.get("source_table", ""))
                for e in existing
            }
            now = datetime.now().isoformat()
            added = 0

            # success_patterns candidates
            try:
                rows = self.conn.execute(
                    "SELECT id, title, confidence_score FROM success_patterns "
                    "WHERE confidence_score < 0.3 "
                    "AND valid_from < datetime('now', '-30 days') "
                    "AND valid_until IS NULL"
                ).fetchall()
                for row in rows:
                    key = (str(row["id"]), "success_patterns")
                    if key in existing_keys:
                        continue
                    existing.append({
                        "pattern_id": str(row["id"]),
                        "title": (row["title"] or "")[:120],
                        "confidence": round(float(row["confidence_score"]), 4),
                        "source_table": "success_patterns",
                        "action": "supersede",
                        "reason": "confidence_score < 0.3, older than 30 days",
                        "queued_at": now,
                        "status": "pending",
                    })
                    added += 1
            except Exception as e:
                log.debug("success_patterns supersede query failed: %s", e)

            # prevention_rules candidates
            try:
                rows = self.conn.execute(
                    "SELECT id, description AS title, confidence FROM prevention_rules "
                    "WHERE confidence < 0.3 "
                    "AND valid_from < datetime('now', '-30 days') "
                    "AND valid_until IS NULL"
                ).fetchall()
                for row in rows:
                    key = (str(row["id"]), "prevention_rules")
                    if key in existing_keys:
                        continue
                    existing.append({
                        "pattern_id": str(row["id"]),
                        "title": (row["title"] or "")[:120],
                        "confidence": round(float(row["confidence"]), 4),
                        "source_table": "prevention_rules",
                        "action": "supersede",
                        "reason": "confidence < 0.3, older than 30 days",
                        "queued_at": now,
                        "status": "pending",
                    })
                    added += 1
            except Exception as e:
                log.debug("prevention_rules supersede query failed: %s", e)

            total = added
            if added:
                pending_path.write_text(
                    json.dumps({"pending_archival": existing}, indent=2, ensure_ascii=False),
                    encoding="utf-8",
                )
                print(
                    f"  Queued {added} stale-pattern supersede candidates for operator review"
                    f" → {pending_path}"
                )
            else:
                print("  No stale patterns qualified for supersede")
        except Exception as e:
            print(f"❌ Error queuing stale patterns for supersede: {e}")
        return total

    def daily_learning_cycle(self, from_history: bool = False):
        """Run the complete daily learning cycle.

        Args:
            from_history: When True, mine the full receipt history (all-time window)
                instead of the default 24-hour window. Use for the first run against
                a historical trail (``vnx learning run --from-history``).
        """
        gate = evaluate_activation_gate(state_dir=self.db_path.parent)

        if gate["action"] == "dormant":
            print("💤 Learning loop dormant (VNX_LEARNING_LOOP_ENABLED=0) — no-op, no beacon")
            return {"status": "dormant", "gate": gate, "statistics": {}}

        if gate["action"] == "degraded":
            print(
                f"⛔ Learning loop gated: probe health={gate['probe_health']!r} "
                f"({gate['detail']}) — skipping pattern updates"
            )
            try:
                from health_beacon import HealthBeacon
                from vnx_paths import ensure_env as _hb_ensure_env
                _hb_paths = _hb_ensure_env()
                HealthBeacon(
                    Path(_hb_paths["VNX_DATA_DIR"]),
                    "learning_loop",
                    expected_interval_seconds=86400,
                ).heartbeat(
                    status="degraded",
                    details={"probe_health": gate["probe_health"], "reason": gate["detail"]},
                )
            except (ImportError, OSError, KeyError) as e:
                log.debug("HealthBeacon degraded heartbeat skipped: %s", e)
            return {
                "status": "degraded",
                "probe_health": gate["probe_health"],
                "gate": gate,
                "statistics": {},
            }

        mode = "FULL HISTORY" if from_history else "DAILY (24h)"
        print(f"\n🔄 Starting Learning Cycle [{mode}] at {datetime.now().isoformat()}")
        print("=" * 60)

        start_time = time.time()

        # 1. Analyze today's receipts
        print("\n📋 Step 1: Analyzing receipt patterns...")
        patterns_used = self.extract_used_patterns()
        patterns_ignored = self.extract_ignored_patterns()

        self.learning_stats["patterns_used"] = sum(len(v) for v in patterns_used.values())
        self.learning_stats["patterns_ignored"] = sum(patterns_ignored.values())

        print(f"  ✓ Found {len(patterns_used)} used patterns")
        print(f"  ✓ Found {len(patterns_ignored)} ignored patterns")

        # 2. Update confidence scores
        print("\n📊 Step 2: Updating confidence scores...")
        self.update_confidence_scores(patterns_used, patterns_ignored)

        # 3. Extract and learn from failures
        print("\n🔍 Step 3: Learning from failures...")
        failure_window = (
            datetime(2000, 1, 1, tzinfo=timezone.utc) if from_history
            else datetime.now(timezone.utc) - timedelta(hours=24)
        )
        failure_patterns = self.extract_failure_patterns(start_time=failure_window)
        new_rules = self.generate_prevention_rules(failure_patterns)

        if new_rules:
            print(f"  ✓ Generated {len(new_rules)} new prevention rules")
            self.update_terminal_constraints(new_rules)

        # 4. Archive stale patterns
        print("\n📦 Step 4: Archiving unused patterns...")
        self.archive_unused_patterns(threshold_days=30)

        # 5. Save updated metrics
        print("\n💾 Step 5: Saving pattern metrics...")
        self.save_pattern_metrics()

        # 5.5 Persist high-confidence patterns and failures to intelligence DB
        # Off-switch: VNX_LEARN_PERSIST=0 skips the auto-persist of observations.
        print("\n🔗 Step 5.5: Bridging patterns to intelligence DB...")
        if os.environ.get("VNX_LEARN_PERSIST", "1") != "0":
            self.persist_to_intelligence_db()
        else:
            print("  VNX_LEARN_PERSIST=0 — pattern persist skipped")
        self.ingest_approved_rules()

        # 5.6 Queue stale low-confidence patterns for operator-gated supersede (D3 G-L4).
        # valid_until is NOT set here — operator must approve via pending_archival.json.
        print("\n📋 Step 5.6: Queuing stale patterns for operator-gated supersede...")
        superseded = self._supersede_stale_patterns()
        if superseded:
            print(f"  ✓ {superseded} patterns queued for operator review")

        # 5.7 Close the feedback loop: sync pattern_usage stats back to
        # success_patterns.confidence_score so intelligence_selector reads
        # the current learning state rather than the static initial value.
        print("\n🔁 Step 5.7: Reconciling confidence scores...")
        try:
            from confidence_reconcile import reconcile_pattern_confidence
            reconciled = reconcile_pattern_confidence(self.db_path)
            print(f"  ✓ Reconciled {reconciled} success_patterns rows")
        except Exception as e:
            print(f"❌ Error reconciling confidence: {e}")

        # 5.8 Reason-aware injection-effectiveness evaluator (injection-effectiveness-eval-loop
        # PR-B): measure-only tuning proposals from the pattern_injection_outcome reason
        # distribution. Gated behind its OWN opt-in flags (VNX_INJECTION_WHY_ENABLED AND
        # VNX_INJECTION_FEEDBACK_ENABLED — the latter already required =1 to have reached this
        # point via evaluate_activation_gate). No new flag; never auto-applies a tuning change
        # (G-L1) — proposals land in pending_injection_tuning.json for operator review only.
        print("\n🔬 Step 5.8: Reason-aware injection tuning proposals...")
        reason_result = {"ran": False, "proposals_written": 0, "proposals_generated": 0}
        try:
            from injection_effectiveness_probe import run_reason_evaluator_and_propose
            reason_result = run_reason_evaluator_and_propose(state_dir=self.db_path.parent)
            if reason_result["ran"]:
                print(
                    f"  ✓ {reason_result['proposals_written']} new tuning proposal(s) queued "
                    f"({reason_result['proposals_generated']} generated this run)"
                )
            else:
                print("  Injection-tuning flags off — reason evaluator skipped")
        except Exception as e:
            print(f"❌ Error running reason evaluator: {e}")

        # 6. Generate report
        print("\n📈 Step 6: Generating learning report...")
        report = self.generate_learning_report()

        proposal_count = len(new_rules)
        self.learning_stats["proposal_count"] = proposal_count
        self.learning_stats["injection_tuning_proposals"] = reason_result.get("proposals_written", 0)

        elapsed = time.time() - start_time
        print(f"\n✅ Learning cycle completed in {elapsed:.2f} seconds")
        print(f"  • Patterns tracked: {len(self.pattern_metrics)}")
        print(f"  • Confidence adjustments: {self.learning_stats['confidence_adjustments']}")
        print(f"  • Proposals (pending rules): {proposal_count}")
        print(f"  • Patterns archived: {self.learning_stats['patterns_archived']}")
        print("=" * 60)

        try:
            from health_beacon import HealthBeacon
            from vnx_paths import ensure_env as _hb_ensure_env
            _hb_paths = _hb_ensure_env()
            HealthBeacon(
                Path(_hb_paths["VNX_DATA_DIR"]),
                "learning_loop",
                expected_interval_seconds=86400,
            ).heartbeat(
                status="ok",
                details={
                    "elapsed_seconds": round(elapsed, 2),
                    "patterns_tracked": len(self.pattern_metrics),
                    "confidence_adjustments": self.learning_stats.get("confidence_adjustments", 0),
                    "new_prevention_rules": len(new_rules),
                    "patterns_archived": self.learning_stats.get("patterns_archived", 0),
                },
            )
        except (ImportError, OSError, KeyError) as e:
            log.debug("HealthBeacon heartbeat skipped: %s", e)

        return report


def main():
    """Run learning loop manually or check status"""
    import sys

    from_history = "--from-history" in sys.argv
    argv = [a for a in sys.argv[1:] if a != "--from-history"]

    loop = LearningLoop()

    if argv:
        command = argv[0]

        if command == "run":
            report = loop.daily_learning_cycle(from_history=from_history)
            proposal_count = report.get("statistics", {}).get("proposal_count", 0)
            print(f"\n📊 Proposals (pending rules): {proposal_count}")
            print("\n📊 Learning Summary:")
            print(json.dumps(report['statistics'], indent=2))

        elif command == "status":
            print("📊 Pattern Metrics Status:")
            print(f"  Total patterns tracked: {len(loop.pattern_metrics)}")

            if loop.pattern_metrics:
                high_confidence = sum(1 for m in loop.pattern_metrics.values() if m.confidence > 1.5)
                low_confidence = sum(1 for m in loop.pattern_metrics.values() if m.confidence < 0.5)
                recently_used = sum(1 for m in loop.pattern_metrics.values()
                                  if m.last_used and m.last_used > datetime.now(timezone.utc) - timedelta(days=7))

                print(f"  High confidence (>1.5): {high_confidence}")
                print(f"  Low confidence (<0.5): {low_confidence}")
                print(f"  Recently used (7 days): {recently_used}")

        elif command == "test":
            print("Testing pattern extraction...")
            used = loop.extract_used_patterns(datetime.now(timezone.utc) - timedelta(hours=1))
            ignored = loop.extract_ignored_patterns(datetime.now(timezone.utc) - timedelta(hours=1))
            print(f"  Used patterns: {len(used)}")
            print(f"  Ignored patterns: {len(ignored)}")

        else:
            print(f"Unknown command: {command}")
            print("Usage: learning_loop.py [run|status|test] [--from-history]")
    else:
        print("VNX Learning Loop v1.0")
        print("Commands: run, status, test")
        print("Flags:    --from-history (mine full history instead of 24h window)")


if __name__ == "__main__":
    main()
