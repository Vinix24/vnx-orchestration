#!/usr/bin/env python3
"""
Tag Intelligence Engine - PR #3
Analyzes tag combinations from receipts/reports to detect patterns and generate prevention rules.
"""

import json
import re
import sqlite3
import sys
from pathlib import Path
from typing import Dict, List, Tuple, Optional, Any
from datetime import datetime
from collections import defaultdict

script_dir = Path(__file__).resolve().parent
sys.path.insert(0, str(script_dir / "lib"))
try:
    from vnx_paths import ensure_env
except Exception as exc:
    raise SystemExit(f"Failed to load vnx_paths: {exc}")

class TagIntelligenceEngine:
    """Analyzes tag combinations to detect patterns and generate prevention rules"""

    def __init__(self, db_path: Optional[Path] = None):
        """Initialize tag intelligence engine with database connection"""
        if db_path is None:
            paths = ensure_env()
            state_dir = Path(paths["VNX_STATE_DIR"]).expanduser().resolve()
            db_path = state_dir / "quality_intelligence.db"

        self.db_path = db_path
        self.db = None

        # In-memory tracking for session
        self.combination_patterns = defaultdict(lambda: {
            "count": 0,
            "phases": [],
            "outcomes": [],
            "terminals": []
        })

        # Connect to database
        self._connect_db()
        self._ensure_tables_exist()

    def _connect_db(self):
        """Connect to quality intelligence database"""
        try:
            self.db = sqlite3.connect(self.db_path)
            self.db.row_factory = sqlite3.Row
        except Exception as e:
            print(f"⚠️ Warning: Could not connect to database: {e}")
            self.db = None

    def _ensure_tables_exist(self):
        """Ensure tag intelligence tables exist in database"""
        if not self.db:
            return

        try:
            # Table for tag combination tracking
            self.db.execute("""
                CREATE TABLE IF NOT EXISTS tag_combinations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tag_tuple TEXT NOT NULL UNIQUE,
                    occurrence_count INTEGER DEFAULT 0,
                    first_seen TEXT NOT NULL,
                    last_seen TEXT NOT NULL,
                    phases TEXT,
                    terminals TEXT,
                    outcomes TEXT
                )
            """)

            # Table for prevention rules
            self.db.execute("""
                CREATE TABLE IF NOT EXISTS prevention_rules (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tag_combination TEXT NOT NULL,
                    rule_type TEXT NOT NULL,
                    description TEXT NOT NULL,
                    recommendation TEXT NOT NULL,
                    confidence REAL DEFAULT 0.0,
                    created_at TEXT NOT NULL,
                    triggered_count INTEGER DEFAULT 0,
                    last_triggered TEXT
                )
            """)

            # Index for faster lookups
            self.db.execute("""
                CREATE INDEX IF NOT EXISTS idx_tag_tuple
                ON tag_combinations(tag_tuple)
            """)

            self.db.execute("""
                CREATE INDEX IF NOT EXISTS idx_rule_combination
                ON prevention_rules(tag_combination)
            """)

            self.db.commit()
        except Exception as e:
            print(f"⚠️ Warning: Could not create tables: {e}")

    # Compound tags that pass through normalization as-is (already specific)
    COMPOUND_TAGS = frozenset([
        'sse-streaming', 'browser-pool', 'kvk-validation',
        'btw-validation', 'memory-budget', 'prompt-caching',
        'receipt-processing', 'dispatch-routing', 'quality-gate',
        'crawler-component', 'storage-component', 'api-component',
        'dutch-market', 'frontend-component',
    ])

    def normalize_tags(self, tags: List[str]) -> Tuple[str, ...]:
        """Normalize tags to standardized taxonomy"""
        normalized = []

        for tag in tags:
            tag_lower = tag.lower().strip()

            # Compound tags pass through as-is (already specific)
            if tag_lower in self.COMPOUND_TAGS:
                normalized.append(tag_lower)
            # Map to standardized taxonomy
            elif tag_lower in ['design', 'planning', 'architecture']:
                normalized.append('design-phase')
            elif tag_lower in ['implementation', 'coding', 'development']:
                normalized.append('implementation-phase')
            elif tag_lower in ['testing', 'validation', 'qa']:
                normalized.append('testing-phase')
            elif tag_lower in ['production', 'deployment', 'release']:
                normalized.append('production-phase')
            elif tag_lower in ['crawler', 'scraping', 'web']:
                normalized.append('crawler-component')
            elif tag_lower in ['storage', 'database', 'persistence']:
                normalized.append('storage-component')
            elif tag_lower in ['api', 'endpoint', 'controller']:
                normalized.append('api-component')
            elif tag_lower in ['validation-error', 'invalid-data']:
                normalized.append('validation-error')
            elif tag_lower in ['performance', 'slow', 'optimization']:
                normalized.append('performance-issue')
            elif tag_lower in ['memory', 'memory-leak', 'oom']:
                normalized.append('memory-problem')
            elif tag_lower in ['race', 'concurrency', 'threading']:
                normalized.append('race-condition')
            elif tag_lower in ['critical', 'blocker', 'urgent']:
                normalized.append('critical-blocker')
            elif tag_lower in ['high', 'important']:
                normalized.append('high-priority')
            elif tag_lower in ['medium', 'moderate']:
                normalized.append('medium-impact')
            elif tag_lower in ['refactor', 'technical-debt']:
                normalized.append('needs-refactor')
            elif tag_lower in ['validation', 'verify']:
                normalized.append('needs-validation')
            elif tag_lower in ['retry', 'resilience']:
                normalized.append('needs-retry-logic')
            else:
                # Keep unknown tags as-is
                normalized.append(tag_lower)

        return tuple(sorted(set(normalized)))

    # Compound tag detection patterns for instruction text
    _COMPOUND_KEYWORD_MAP = [
        (re.compile(r'SSE\s+stream', re.IGNORECASE), 'sse-streaming'),
        (re.compile(r'browser\s+pool', re.IGNORECASE), 'browser-pool'),
        (re.compile(r'KvK\s+valid', re.IGNORECASE), 'kvk-validation'),
        (re.compile(r'BTW\s+valid', re.IGNORECASE), 'btw-validation'),
        (re.compile(r'memory\s+budget', re.IGNORECASE), 'memory-budget'),
        (re.compile(r'prompt\s+cach', re.IGNORECASE), 'prompt-caching'),
        (re.compile(r'receipt\s+process', re.IGNORECASE), 'receipt-processing'),
        (re.compile(r'dispatch\s+rout', re.IGNORECASE), 'dispatch-routing'),
        (re.compile(r'quality\s+gate', re.IGNORECASE), 'quality-gate'),
    ]

    def extract_tags_from_dispatch(self, dispatch_path: Path) -> List[str]:
        """Extract tags from a completed dispatch file's metadata and instruction text.

        Reads structured metadata fields (Priority, Gate, Role, Reason) and
        detects compound tags from the instruction body.
        """
        tags: List[str] = []

        try:
            text = dispatch_path.read_text(encoding='utf-8', errors='replace')
        except OSError:
            return tags

        # --- Metadata field extraction ---
        # Priority: P0 → critical-blocker
        priority_match = re.search(r'^Priority:\s*(P\d)', text, re.MULTILINE)
        if priority_match:
            p = priority_match.group(1).upper()
            if p == 'P0':
                tags.append('critical-blocker')
            elif p == 'P1':
                tags.append('high-priority')

        # Gate: implementation → implementation-phase
        gate_match = re.search(r'^Gate:\s*(\S+)', text, re.MULTILINE)
        if gate_match:
            gate = gate_match.group(1).lower()
            gate_map = {
                'implementation': 'implementation-phase',
                'testing': 'testing-phase',
                'review': 'needs-validation',
                'investigation': 'testing-phase',
                'design': 'design-phase',
                'production': 'production-phase',
            }
            if gate in gate_map:
                tags.append(gate_map[gate])

        # Role: → component tag based on role
        role_match = re.search(r'^Role:\s*(.+)', text, re.MULTILINE)
        if role_match:
            role = role_match.group(1).strip().lower()
            role_map = {
                'backend-developer': 'implementation-phase',
                'api-developer': 'api-component',
                'debugger': 'testing-phase',
                'debugging-specialist': 'testing-phase',
                'quality-engineer': 'needs-validation',
                'test-engineer': 'testing-phase',
                'reviewer': 'needs-validation',
                'performance-profiler': 'performance-issue',
                'security-engineer': 'needs-validation',
            }
            if role in role_map:
                tags.append(role_map[role])

        # On-Failure: review → needs-validation
        on_failure_match = re.search(r'^On-Failure:\s*(\S+)', text, re.MULTILINE)
        if on_failure_match:
            action = on_failure_match.group(1).lower()
            if action == 'review':
                tags.append('needs-validation')

        # Reason: keyword extraction
        reason_match = re.search(r'^Reason:\s*(.+)', text, re.MULTILINE)
        if reason_match:
            reason = reason_match.group(1).lower()
            if 'memory' in reason or 'oom' in reason:
                tags.append('memory-problem')
            if 'race' in reason or 'concurren' in reason:
                tags.append('race-condition')
            if 'crawler' in reason:
                tags.append('crawler-component')
            if 'storage' in reason or 'database' in reason:
                tags.append('storage-component')
            if 'sse' in reason or 'streaming' in reason:
                tags.append('sse-streaming')

        # --- Compound tag detection from instruction text ---
        instruction_match = re.search(r'Instruction:\s*\n(.*?)(?:\[\[DONE\]\]|$)', text, re.DOTALL)
        instruction_text = instruction_match.group(1) if instruction_match else text

        for pattern, tag in self._COMPOUND_KEYWORD_MAP:
            if pattern.search(instruction_text):
                tags.append(tag)

        return list(set(tags))

    def analyze_multi_tag_patterns(
        self,
        tags: List[str],
        phase: Optional[str] = None,
        terminal: Optional[str] = None,
        outcome: Optional[str] = None
    ) -> Dict[str, Any]:
        """Analyze tag combinations and generate prevention rules if needed"""

        if not tags:
            return {"analyzed": False, "reason": "no_tags"}

        # Normalize tags to standard taxonomy
        tag_tuple = self.normalize_tags(tags)

        if len(tag_tuple) == 0:
            return {"analyzed": False, "reason": "no_valid_tags"}

        # Update in-memory tracking
        pattern = self.combination_patterns[tag_tuple]
        pattern["count"] += 1

        if phase:
            pattern["phases"].append(phase)
        if terminal:
            pattern["terminals"].append(terminal)
        if outcome:
            pattern["outcomes"].append(outcome)

        # Update database
        self._store_combination(tag_tuple, phase, terminal, outcome)

        result = {
            "analyzed": True,
            "tag_combination": tag_tuple,
            "occurrence_count": pattern["count"],
            "prevention_rule_generated": False
        }

        # Generate prevention rule if combination seen 2+ times
        if pattern["count"] >= 2:
            rule = self._generate_prevention_rule(tag_tuple, pattern)
            if rule:
                result["prevention_rule_generated"] = True
                result["prevention_rule"] = rule

        return result

    def _store_combination(
        self,
        tag_tuple: Tuple[str, ...],
        phase: Optional[str],
        terminal: Optional[str],
        outcome: Optional[str]
    ):
        """Store or update tag combination in database"""
        if not self.db:
            return

        try:
            tag_str = json.dumps(tag_tuple)
            now = datetime.now().isoformat()

            # Check if combination exists
            existing = self.db.execute(
                "SELECT id, occurrence_count, phases, terminals, outcomes FROM tag_combinations WHERE tag_tuple = ?",
                (tag_str,)
            ).fetchone()

            if existing:
                # Update existing
                new_count = existing['occurrence_count'] + 1

                # Parse existing JSON arrays
                phases = json.loads(existing['phases']) if existing['phases'] else []
                terminals = json.loads(existing['terminals']) if existing['terminals'] else []
                outcomes = json.loads(existing['outcomes']) if existing['outcomes'] else []

                # Append new values
                if phase:
                    phases.append(phase)
                if terminal:
                    terminals.append(terminal)
                if outcome:
                    outcomes.append(outcome)

                self.db.execute("""
                    UPDATE tag_combinations
                    SET occurrence_count = ?,
                        last_seen = ?,
                        phases = ?,
                        terminals = ?,
                        outcomes = ?
                    WHERE id = ?
                """, (
                    new_count,
                    now,
                    json.dumps(phases),
                    json.dumps(terminals),
                    json.dumps(outcomes),
                    existing['id']
                ))
            else:
                # Insert new
                self.db.execute("""
                    INSERT INTO tag_combinations
                    (tag_tuple, occurrence_count, first_seen, last_seen, phases, terminals, outcomes)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (
                    tag_str,
                    1,
                    now,
                    now,
                    json.dumps([phase] if phase else []),
                    json.dumps([terminal] if terminal else []),
                    json.dumps([outcome] if outcome else [])
                ))

            self.db.commit()
        except Exception as e:
            print(f"⚠️ Warning: Could not store tag combination: {e}")

    def _generate_prevention_rule(
        self,
        tag_tuple: Tuple[str, ...],
        pattern: Dict
    ) -> Optional[Dict[str, Any]]:
        """Generate prevention rule for recurring tag combination"""

        # Determine rule type based on tags
        rule_type = self._classify_rule_type(tag_tuple)

        # Generate human-readable description
        description = f"Recurring pattern detected: {', '.join(tag_tuple)}"

        # Generate recommendation based on tags
        recommendation = self._generate_recommendation(tag_tuple, pattern)

        # Calculate confidence based on occurrences
        confidence = min(pattern["count"] / 10.0, 1.0)  # Max confidence at 10 occurrences

        rule = {
            "tag_combination": tag_tuple,
            "rule_type": rule_type,
            "description": description,
            "recommendation": recommendation,
            "confidence": confidence,
            "occurrence_count": pattern["count"]
        }

        # Store rule in database
        self._store_prevention_rule(rule)

        return rule

    def _classify_rule_type(self, tag_tuple: Tuple[str, ...]) -> str:
        """Classify prevention rule type based on tags"""
        tags_str = ' '.join(tag_tuple)

        if 'critical-blocker' in tags_str:
            return 'critical-prevention'
        elif 'validation-error' in tags_str:
            return 'validation-check'
        elif 'performance-issue' in tags_str:
            return 'performance-optimization'
        elif 'memory-problem' in tags_str:
            return 'memory-management'
        elif 'race-condition' in tags_str:
            return 'concurrency-control'
        else:
            return 'general-prevention'

    def _generate_recommendation(
        self,
        tag_tuple: Tuple[str, ...],
        pattern: Dict
    ) -> str:
        """Generate actionable recommendation based on tag combination"""
        tags_str = ' '.join(tag_tuple)

        recommendations = []

        # Phase-specific recommendations
        if 'design-phase' in tags_str and 'validation-error' in tags_str:
            recommendations.append("Add input validation design early in planning")
        elif 'implementation-phase' in tags_str and 'memory-problem' in tags_str:
            recommendations.append("Implement memory profiling during development")
        elif 'testing-phase' in tags_str and 'race-condition' in tags_str:
            recommendations.append("Add concurrency tests before production")

        # Component-specific recommendations
        if 'crawler-component' in tags_str and 'performance-issue' in tags_str:
            recommendations.append("Profile crawler operations, consider async patterns")
        elif 'storage-component' in tags_str and 'validation-error' in tags_str:
            recommendations.append("Add schema validation before database writes")
        elif 'api-component' in tags_str and 'memory-problem' in tags_str:
            recommendations.append("Implement request streaming and pagination")

        # Action tags
        if 'needs-refactor' in tags_str:
            recommendations.append("Schedule refactoring before adding new features")
        elif 'needs-validation' in tags_str:
            recommendations.append("Add validation layer with comprehensive tests")
        elif 'needs-retry-logic' in tags_str:
            recommendations.append("Implement exponential backoff retry mechanism")

        # Default recommendation
        if not recommendations:
            recommendations.append(f"Review code patterns for: {', '.join(tag_tuple)}")

        return "; ".join(recommendations)

    def _store_prevention_rule(self, rule: Dict[str, Any]):
        """Store prevention rule in database"""
        if not self.db:
            return

        try:
            tag_str = json.dumps(rule["tag_combination"])
            now = datetime.now().isoformat()

            # Check if rule exists
            existing = self.db.execute(
                "SELECT id FROM prevention_rules WHERE tag_combination = ?",
                (tag_str,)
            ).fetchone()

            if not existing:
                self.db.execute("""
                    INSERT INTO prevention_rules
                    (tag_combination, rule_type, description, recommendation, confidence, created_at, triggered_count)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (
                    tag_str,
                    rule["rule_type"],
                    rule["description"],
                    rule["recommendation"],
                    rule["confidence"],
                    now,
                    rule["occurrence_count"]
                ))
                self.db.commit()
        except Exception as e:
            print(f"⚠️ Warning: Could not store prevention rule: {e}")

    def query_prevention_rules(
        self,
        tags: Optional[List[str]] = None,
        min_confidence: float = 0.0
    ) -> List[Dict[str, Any]]:
        """Query prevention rules, optionally filtered by tags"""
        if not self.db:
            return []

        try:
            if tags:
                # Normalize query tags
                tag_tuple = self.normalize_tags(tags)
                tag_str = json.dumps(tag_tuple)

                query = """
                    SELECT tag_combination, rule_type, description, recommendation,
                           confidence, triggered_count, created_at, last_triggered
                    FROM prevention_rules
                    WHERE tag_combination = ? AND confidence >= ?
                    ORDER BY confidence DESC, triggered_count DESC
                """
                cursor = self.db.execute(query, (tag_str, min_confidence))
            else:
                # Get all rules above confidence threshold
                query = """
                    SELECT tag_combination, rule_type, description, recommendation,
                           confidence, triggered_count, created_at, last_triggered
                    FROM prevention_rules
                    WHERE confidence >= ?
                    ORDER BY confidence DESC, triggered_count DESC
                """
                cursor = self.db.execute(query, (min_confidence,))

            rules = []
            for row in cursor:
                rule = dict(row)
                rule['tag_combination'] = json.loads(rule['tag_combination'])
                rules.append(rule)

            return rules
        except Exception as e:
            print(f"⚠️ Warning: Could not query prevention rules: {e}")
            return []

    def get_statistics(self) -> Dict[str, Any]:
        """Get statistics about tag combinations and prevention rules"""
        if not self.db:
            return {"error": "database_not_connected"}

        try:
            # Count combinations
            combo_count = self.db.execute(
                "SELECT COUNT(*) FROM tag_combinations"
            ).fetchone()[0]

            # Count rules
            rule_count = self.db.execute(
                "SELECT COUNT(*) FROM prevention_rules"
            ).fetchone()[0]

            # Top combinations
            top_combos = self.db.execute("""
                SELECT tag_tuple, occurrence_count
                FROM tag_combinations
                ORDER BY occurrence_count DESC
                LIMIT 5
            """).fetchall()

            # High confidence rules
            high_conf_rules = self.db.execute("""
                SELECT COUNT(*) FROM prevention_rules WHERE confidence >= 0.7
            """).fetchone()[0]

            return {
                "total_combinations": combo_count,
                "total_rules": rule_count,
                "high_confidence_rules": high_conf_rules,
                "top_combinations": [
                    {
                        "tags": json.loads(row['tag_tuple']),
                        "count": row['occurrence_count']
                    }
                    for row in top_combos
                ]
            }
        except Exception as e:
            return {"error": str(e)}

    def close(self):
        """Close database connection"""
        if self.db:
            self.db.close()


def main():
    """CLI interface for testing tag intelligence"""
    import sys

    engine = TagIntelligenceEngine()

    if len(sys.argv) > 1:
        command = sys.argv[1]

        if command == "analyze":
            if len(sys.argv) < 3:
                print("Usage: tag_intelligence.py analyze tag1 tag2 tag3 [--phase design] [--terminal T1] [--outcome success]")
                sys.exit(1)

            # Parse tags and optional arguments
            tags = []
            phase = None
            terminal = None
            outcome = None

            i = 2
            while i < len(sys.argv):
                arg = sys.argv[i]
                if arg == "--phase" and i + 1 < len(sys.argv):
                    phase = sys.argv[i + 1]
                    i += 2
                elif arg == "--terminal" and i + 1 < len(sys.argv):
                    terminal = sys.argv[i + 1]
                    i += 2
                elif arg == "--outcome" and i + 1 < len(sys.argv):
                    outcome = sys.argv[i + 1]
                    i += 2
                else:
                    tags.append(arg)
                    i += 1

            result = engine.analyze_multi_tag_patterns(tags, phase, terminal, outcome)
            print(json.dumps(result, indent=2))

        elif command == "rules":
            min_conf = 0.0
            tags = None

            if len(sys.argv) > 2 and sys.argv[2] == "--tags":
                tags = sys.argv[3:]
            elif len(sys.argv) > 2 and sys.argv[2] == "--min-confidence":
                min_conf = float(sys.argv[3])

            rules = engine.query_prevention_rules(tags, min_conf)
            print(f"\nFound {len(rules)} prevention rules:\n")
            for rule in rules:
                print(f"Tags: {', '.join(rule['tag_combination'])}")
                print(f"Type: {rule['rule_type']}")
                print(f"Confidence: {rule['confidence']:.2f}")
                print(f"Recommendation: {rule['recommendation']}")
                print()

        elif command == "stats":
            stats = engine.get_statistics()
            print(json.dumps(stats, indent=2))

        else:
            print(f"Unknown command: {command}")
            print("Available commands: analyze, rules, stats")
    else:
        print("VNX Tag Intelligence Engine v1.0")
        stats = engine.get_statistics()
        print(f"Tag combinations tracked: {stats.get('total_combinations', 0)}")
        print(f"Prevention rules generated: {stats.get('total_rules', 0)}")
        print(f"High confidence rules: {stats.get('high_confidence_rules', 0)}")
        print("\nCommands: analyze, rules, stats")

    engine.close()


if __name__ == "__main__":
    main()
