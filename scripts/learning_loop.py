#!/usr/bin/env python3
"""
VNX Learning Loop & Optimization System
Tracks pattern usage, adjusts confidence scores, and optimizes intelligence delivery.
Runs daily at 18:00 to analyze receipts and update pattern effectiveness.
"""

import hashlib
import json
import sqlite3
import time
import sys
from pathlib import Path
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple, Any
from dataclasses import dataclass
from collections import defaultdict
import re

script_dir = Path(__file__).resolve().parent
sys.path.insert(0, str(script_dir / "lib"))
try:
    from vnx_paths import ensure_env
except Exception as exc:
    raise SystemExit(f"Failed to load vnx_paths: {exc}")


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
        self.receipts_path = self.vnx_path / "terminals" / "file_bus" / "receipts"
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
                    last_used=datetime.fromisoformat(row['last_used']) if row['last_used'] else None,
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
            start_time = datetime.now() - timedelta(hours=24)

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
            start_time = datetime.now() - timedelta(hours=24)

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

        except Exception:
            pass

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
            metric.last_used = datetime.now()

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
            except Exception:
                pass

            self.learning_stats["confidence_adjustments"] += 1
            print(f"📉 Decayed {pattern_id}: {old_confidence:.3f} → {metric.confidence:.3f}")

        # Commit ignored_count updates
        try:
            self.conn.commit()
        except Exception:
            pass

    def extract_failure_patterns(self, start_time: datetime = None) -> List[Dict]:
        """Extract new failure patterns from recent terminal errors"""
        if not start_time:
            start_time = datetime.now() - timedelta(hours=24)

        failure_patterns = []

        # Scan receipts for failures and extract patterns
        for receipt_file in self.receipts_path.glob("*.ndjson"):
            if receipt_file.stat().st_mtime < start_time.timestamp():
                continue

            try:
                with open(receipt_file, 'r') as f:
                    for line in f:
                        try:
                            receipt = json.loads(line.strip())

                            # Check for error indicators
                            if receipt.get('outcome') == 'error' or 'error' in str(receipt.get('terminal_response', '')).lower():
                                # Extract failure context
                                failure_pattern = {
                                    'task': receipt.get('task_description', ''),
                                    'terminal': receipt.get('terminal', ''),
                                    'agent': receipt.get('agent', ''),
                                    'error': self.extract_error_message(receipt.get('terminal_response', '')),
                                    'timestamp': receipt.get('timestamp', datetime.now().isoformat())
                                }
                                failure_patterns.append(failure_pattern)

                        except json.JSONDecodeError:
                            continue

            except Exception as e:
                print(f"⚠️ Error extracting failure patterns: {e}")

        return failure_patterns

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

    def archive_unused_patterns(self, threshold_days: int = 30):
        """Queue unused low-confidence patterns for operator confirmation (G-L4: no auto-archival).

        Candidates are written to pending_archival.json — NOT auto-archived.
        """
        archive_date = datetime.now() - timedelta(days=threshold_days)

        candidates = []
        for pattern_id, metric in self.pattern_metrics.items():
            if not metric.last_used or metric.last_used < archive_date:
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

    def daily_learning_cycle(self):
        """Run the complete daily learning cycle"""
        print(f"\n🔄 Starting Daily Learning Cycle at {datetime.now().isoformat()}")
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
        failure_patterns = self.extract_failure_patterns()
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

        # 6. Generate report
        print("\n📈 Step 6: Generating learning report...")
        report = self.generate_learning_report()

        elapsed = time.time() - start_time
        print(f"\n✅ Learning cycle completed in {elapsed:.2f} seconds")
        print(f"  • Patterns tracked: {len(self.pattern_metrics)}")
        print(f"  • Confidence adjustments: {self.learning_stats['confidence_adjustments']}")
        print(f"  • New prevention rules: {len(new_rules)}")
        print(f"  • Patterns archived: {self.learning_stats['patterns_archived']}")
        print("=" * 60)

        return report


def main():
    """Run learning loop manually or check status"""
    import sys

    loop = LearningLoop()

    if len(sys.argv) > 1:
        command = sys.argv[1]

        if command == "run":
            # Run full learning cycle
            report = loop.daily_learning_cycle()
            print("\n📊 Learning Summary:")
            print(json.dumps(report['statistics'], indent=2))

        elif command == "status":
            # Show current status
            print(f"📊 Pattern Metrics Status:")
            print(f"  Total patterns tracked: {len(loop.pattern_metrics)}")

            if loop.pattern_metrics:
                high_confidence = sum(1 for m in loop.pattern_metrics.values() if m.confidence > 1.5)
                low_confidence = sum(1 for m in loop.pattern_metrics.values() if m.confidence < 0.5)
                recently_used = sum(1 for m in loop.pattern_metrics.values()
                                  if m.last_used and m.last_used > datetime.now() - timedelta(days=7))

                print(f"  High confidence (>1.5): {high_confidence}")
                print(f"  Low confidence (<0.5): {low_confidence}")
                print(f"  Recently used (7 days): {recently_used}")

        elif command == "test":
            # Test pattern extraction
            print("Testing pattern extraction...")
            used = loop.extract_used_patterns(datetime.now() - timedelta(hours=1))
            ignored = loop.extract_ignored_patterns(datetime.now() - timedelta(hours=1))
            print(f"  Used patterns: {len(used)}")
            print(f"  Ignored patterns: {len(ignored)}")

        else:
            print(f"Unknown command: {command}")
            print("Usage: learning_loop.py [run|status|test]")
    else:
        print("VNX Learning Loop v1.0")
        print("Commands: run, status, test")


if __name__ == "__main__":
    main()
