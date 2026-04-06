#!/usr/bin/env python3
"""
VNX Report Mining Pipeline
Extracts intelligence from unified reports and populates quality databases.
Implements progressive summarization based on report age.
"""

import json
import re
import sqlite3
import glob
from pathlib import Path
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any


class ReportMiner:
    """Extract and store learnings from VNX unified reports"""

    def __init__(self, db_path: Optional[Path] = None):
        """Initialize report miner with database connection"""
        self.vnx_path = Path(__file__).parent.parent

        # Resolve runtime paths via vnx_paths
        lib_dir = str(Path(__file__).resolve().parent / "lib")
        if lib_dir not in sys.path:
            sys.path.insert(0, lib_dir)
        from vnx_paths import resolve_paths as _resolve_vnx_paths
        vnx_paths = _resolve_vnx_paths()
        self.reports_path = Path(vnx_paths["VNX_REPORTS_DIR"])

        # Use provided path or default to quality_intelligence.db
        if db_path:
            self.db_path = db_path
        else:
            self.db_path = Path(vnx_paths["VNX_STATE_DIR"]) / "quality_intelligence.db"

        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self.cursor = self.conn.cursor()

        # Ensure tables exist
        self._ensure_tables()

    def _ensure_tables(self):
        """Ensure required tables exist in the database"""
        # Antipatterns table - using existing schema
        # Table already exists with different columns, we'll adapt to it

        # Prevention rules table - using existing schema
        # Table already exists with different columns, we'll adapt to it

        # Report findings table for tracking extractions
        self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS report_findings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                report_path TEXT NOT NULL,
                report_date TIMESTAMP,
                terminal TEXT,
                task_type TEXT,
                extracted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                patterns_found INTEGER DEFAULT 0,
                antipatterns_found INTEGER DEFAULT 0,
                prevention_rules_found INTEGER DEFAULT 0,
                tags_found TEXT,
                summary TEXT,
                age_category TEXT
            )
        ''')

        self.conn.commit()

    def extract_from_report(self, report_path: Path) -> Dict[str, Any]:
        """Extract structured intelligence from a markdown report"""
        with open(report_path, 'r') as f:
            content = f.read()

        findings = {
            "report_path": str(report_path),
            "report_name": report_path.name,
            "tags": self.extract_tags(content),
            "patterns": self.extract_code_patterns(content),
            "antipatterns": self.extract_antipatterns(content),
            "prevention_rules": self.extract_prevention_rules(content),
            "metadata": self.extract_metadata(content),
            "quality_context": self.extract_quality_context(content)
        }

        # Store findings in database
        self.store_findings(findings)

        return findings

    def extract_metadata(self, content: str) -> Dict[str, Any]:
        """Extract report metadata from header section"""
        metadata = {}

        # Extract terminal
        terminal_match = re.search(r'\*\*Terminal\*\*:\s*(\w+)', content)
        if terminal_match:
            metadata['terminal'] = terminal_match.group(1)

        # Extract date
        date_match = re.search(r'\*\*Date\*\*:\s*([^\n]+)', content)
        if date_match:
            metadata['date'] = date_match.group(1)

        # Extract task type from title or task ID
        task_match = re.search(r'#\s*([A-Z]+):\s*([^\n]+)', content)
        if task_match:
            metadata['task_type'] = task_match.group(1)
            metadata['task_description'] = task_match.group(2)

        # Extract confidence score
        confidence_match = re.search(r'\*\*Confidence\*\*:\s*([\d.]+)', content)
        if confidence_match:
            metadata['confidence'] = float(confidence_match.group(1))

        # Extract status
        status_match = re.search(r'\*\*Status\*\*:\s*(\w+)', content)
        if status_match:
            metadata['status'] = status_match.group(1)

        # Extract dispatch_id from report metadata
        dispatch_match = re.search(r'\|\s*\*\*Dispatch-ID\*\*\s*\|\s*([^\|]+?)\s*\|', content)
        if not dispatch_match:
            dispatch_match = re.search(r'Dispatch-ID:\s*(\S+)', content)
        if dispatch_match:
            metadata['dispatch_id'] = dispatch_match.group(1).strip()

        return metadata

    def extract_tags(self, content: str) -> List[str]:
        """Extract all hashtags from report"""
        # Find all hashtags
        tags = re.findall(r'#[\w-]+', content)

        # Categorize tags
        categorized = {
            'issue': [],
            'component': [],
            'solution': [],
            'general': []
        }

        # Look for categorized tag sections
        issue_section = re.search(r'\*\*Issue Tags\*\*:\s*([^\n]+)', content)
        if issue_section:
            categorized['issue'] = re.findall(r'#[\w-]+', issue_section.group(1))

        component_section = re.search(r'\*\*Component Tags\*\*:\s*([^\n]+)', content)
        if component_section:
            categorized['component'] = re.findall(r'#[\w-]+', component_section.group(1))

        solution_section = re.search(r'\*\*Solution Tags\*\*:\s*([^\n]+)', content)
        if solution_section:
            categorized['solution'] = re.findall(r'#[\w-]+', solution_section.group(1))

        # Add uncategorized tags
        all_categorized = categorized['issue'] + categorized['component'] + categorized['solution']
        for tag in tags:
            if tag not in all_categorized:
                categorized['general'].append(tag)

        return {
            'all': list(set(tags)),
            'categorized': categorized
        }

    def extract_code_patterns(self, content: str) -> List[Dict[str, Any]]:
        """Extract code patterns and their contexts"""
        patterns = []

        # Find code blocks
        code_blocks = re.findall(r'```(?:python|javascript|bash|sql)?\n(.*?)\n```', content, re.DOTALL)

        for code in code_blocks:
            # Extract pattern characteristics
            if 'assert' in code and 'is' in code:
                patterns.append({
                    'type': 'singleton_pattern',
                    'code': code[:500],  # Limit size
                    'category': 'design_pattern',
                    'description': 'Singleton instance verification'
                })

            if 'memory' in code.lower() and ('MB' in code or 'usage' in code):
                patterns.append({
                    'type': 'memory_monitoring',
                    'code': code[:500],
                    'category': 'performance',
                    'description': 'Memory usage monitoring pattern'
                })

            if 'thread' in code.lower() or 'concurrent' in code.lower():
                patterns.append({
                    'type': 'thread_safety',
                    'code': code[:500],
                    'category': 'concurrency',
                    'description': 'Thread safety or concurrent access pattern'
                })

        # Extract test patterns
        test_patterns = re.findall(r'(✅|❌)\s*([^:\n]+):\s*([^\n]+)', content)
        for status, test_name, result in test_patterns:
            patterns.append({
                'type': 'test_pattern',
                'name': test_name.strip(),
                'status': 'pass' if status == '✅' else 'fail',
                'result': result.strip(),
                'category': 'testing'
            })

        return patterns

    def extract_antipatterns(self, content: str) -> List[Dict[str, Any]]:
        """Extract antipatterns and issues from report"""
        antipatterns = []

        # Look for known issues section
        issues_section = re.search(r'###?\s*Known Issues(.*?)(?=##|\Z)', content, re.DOTALL | re.IGNORECASE)
        if issues_section:
            issues_text = issues_section.group(1)

            # Extract individual issues
            issue_items = re.findall(r'[•\-⚠️]\s*([^\n]+)', issues_text)
            for issue in issue_items:
                antipatterns.append({
                    'pattern': issue.strip(),
                    'category': 'known_issue',
                    'severity': 'warning' if '⚠️' in issue else 'info'
                })

        # Look for failures
        failures = re.findall(r'❌\s*([^:\n]+):\s*([^\n]+)', content)
        for test_name, failure_reason in failures:
            antipatterns.append({
                'pattern': f"Test failure: {test_name.strip()}",
                'description': failure_reason.strip(),
                'category': 'test_failure',
                'severity': 'error'
            })

        # Look for deprecation warnings
        deprecations = re.findall(r'[Dd]eprecated?\s*([^\n.]+)', content)
        for dep in deprecations:
            antipatterns.append({
                'pattern': f"Deprecated: {dep.strip()}",
                'category': 'deprecation',
                'severity': 'warning'
            })

        # Look for error patterns
        errors = re.findall(r'[Ee]rror:\s*([^\n]+)', content)
        for error in errors:
            antipatterns.append({
                'pattern': f"Error: {error.strip()}",
                'category': 'error',
                'severity': 'error'
            })

        return antipatterns

    def extract_prevention_rules(self, content: str) -> List[Dict[str, Any]]:
        """Extract prevention rules and recommendations"""
        rules = []

        # Look for next steps section
        next_steps = re.search(r'###?\s*Next Steps(.*?)(?=##|\Z)', content, re.DOTALL | re.IGNORECASE)
        if next_steps:
            steps_text = next_steps.group(1)

            # Extract action items
            action_items = re.findall(r'\d+\.\s*([^\n]+)', steps_text)
            for item in action_items:
                if 'fix' in item.lower() or 'update' in item.lower() or 'add' in item.lower():
                    rules.append({
                        'rule_type': 'remediation',
                        'rule_condition': f"When: {item.strip()}",
                        'prevention_action': item.strip(),
                        'category': 'maintenance',
                        'priority': 5
                    })

        # Extract from decisions made section
        decisions = re.search(r'###?\s*Decisions Made(.*?)(?=##|\Z)', content, re.DOTALL | re.IGNORECASE)
        if decisions:
            decision_text = decisions.group(1)
            decision_items = re.findall(r'[\-•]\s*([^\n]+)', decision_text)

            for decision in decision_items:
                if 'correct' in decision.lower() or 'should' in decision.lower():
                    rules.append({
                        'rule_type': 'best_practice',
                        'rule_condition': 'Architecture decision',
                        'prevention_action': decision.strip(),
                        'category': 'design',
                        'priority': 7
                    })

        # Extract requirements met
        requirements = re.findall(r'REQ-\d+[^:]*:\s*([^→\n]+)(?:→\s*([^\n]+))?', content)
        for req_desc, implementation in requirements:
            if implementation:
                rules.append({
                    'rule_type': 'requirement',
                    'rule_condition': req_desc.strip(),
                    'prevention_action': implementation.strip(),
                    'category': 'compliance',
                    'priority': 8
                })

        return rules

    def extract_quality_context(self, content: str) -> Dict[str, Any]:
        """Extract quality context information"""
        context = {
            'test_coverage': None,
            'performance_metrics': {},
            'memory_metrics': {},
            'success_rate': None
        }

        # Extract test coverage
        coverage_match = re.search(r'(\d+)/(\d+)\s*(?:tests?\s*)?(?:pass|PASS)', content)
        if coverage_match:
            passed = int(coverage_match.group(1))
            total = int(coverage_match.group(2))
            context['test_coverage'] = {
                'passed': passed,
                'total': total,
                'percentage': (passed / total * 100) if total > 0 else 0
            }

        # Extract memory metrics
        memory_patterns = re.findall(r'<?\s*(\d+)\s*MB\s*(?:usage|memory)', content)
        if memory_patterns:
            context['memory_metrics']['values'] = [int(m) for m in memory_patterns]
            context['memory_metrics']['max'] = max(context['memory_metrics']['values'])

        # Extract performance times
        time_patterns = re.findall(r'(\d+(?:\.\d+)?)\s*(?:ms|seconds?|s)\b', content)
        if time_patterns:
            context['performance_metrics']['response_times'] = time_patterns[:10]  # Limit to first 10

        # Extract success rate
        success_match = re.search(r'(\d+(?:\.\d+)?)\s*%\s*(?:success|pass|PASS)', content)
        if success_match:
            context['success_rate'] = float(success_match.group(1))

        return context

    def store_findings(self, findings: Dict[str, Any]) -> None:
        """Store extracted findings in the database"""
        report_path = findings['report_path']
        metadata = findings.get('metadata', {})

        # Check if report already processed
        self.cursor.execute(
            'SELECT id FROM report_findings WHERE report_path = ?',
            (report_path,)
        )
        if self.cursor.fetchone():
            print(f"Report already processed: {report_path}")
            return

        # Store antipatterns (adapted to existing schema)
        for antipattern in findings.get('antipatterns', []):
            pattern_text = antipattern.get('pattern', '')

            # Check if similar antipattern exists
            self.cursor.execute(
                'SELECT id, occurrence_count FROM antipatterns WHERE title = ? AND category = ?',
                (pattern_text[:100], antipattern.get('category', 'general'))
            )
            existing = self.cursor.fetchone()

            if existing:
                # Update occurrence count
                self.cursor.execute(
                    '''UPDATE antipatterns
                    SET occurrence_count = occurrence_count + 1,
                        last_seen = CURRENT_TIMESTAMP,
                        source_dispatch_ids = source_dispatch_ids || ?
                    WHERE id = ?''',
                    (f",{report_path}", existing['id'])
                )
            else:
                # Insert new antipattern (using existing schema)
                self.cursor.execute(
                    '''INSERT INTO antipatterns
                    (pattern_type, category, title, description, pattern_data,
                     problem_example, why_problematic, better_alternative,
                     occurrence_count, severity, source_dispatch_ids,
                     first_seen, last_seen)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)''',
                    ('mined', antipattern.get('category', 'general'),
                     pattern_text[:100], antipattern.get('description', ''),
                     json.dumps(antipattern), pattern_text,
                     'Found in report mining', 'Consider prevention rules',
                     1, antipattern.get('severity', 'info'),
                     report_path)
                )

        # Store prevention rules (adapted to existing schema)
        for rule in findings.get('prevention_rules', []):
            # Extract tags from rule
            tags = []
            if 'memory' in rule.get('prevention_action', '').lower():
                tags.append('memory-problem')
            if 'test' in rule.get('prevention_action', '').lower():
                tags.append('testing-phase')
            if not tags:
                tags = ['general']

            tag_combination = json.dumps(tags)

            # Check if similar rule exists
            self.cursor.execute(
                'SELECT id FROM prevention_rules WHERE tag_combination = ? AND rule_type = ?',
                (tag_combination, rule.get('rule_type', 'general'))
            )

            if not self.cursor.fetchone():
                # Insert new prevention rule (using existing schema)
                self.cursor.execute(
                    '''INSERT INTO prevention_rules
                    (tag_combination, rule_type, description, recommendation,
                     confidence, created_at, triggered_count, last_triggered)
                    VALUES (?, ?, ?, ?, ?, datetime('now'), ?, NULL)''',
                    (tag_combination,
                     rule.get('rule_type', 'general'),
                     rule.get('rule_condition', ''),
                     rule.get('prevention_action', ''),
                     rule.get('priority', 5) / 10.0,  # Convert priority to confidence
                     0)
                )

        # Store report finding record
        tags_data = findings.get('tags', {})
        all_tags = tags_data.get('all', []) if isinstance(tags_data, dict) else tags_data

        self.cursor.execute(
            '''INSERT INTO report_findings
            (report_path, report_date, terminal, task_type,
             patterns_found, antipatterns_found, prevention_rules_found,
             tags_found, summary, age_category, dispatch_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
            (report_path,
             metadata.get('date'),
             metadata.get('terminal'),
             metadata.get('task_type'),
             len(findings.get('patterns', [])),
             len(findings.get('antipatterns', [])),
             len(findings.get('prevention_rules', [])),
             json.dumps(all_tags),
             metadata.get('task_description', ''),
             self.get_age_category(report_path),
             metadata.get('dispatch_id'))
        )

        self.conn.commit()

    def get_age_category(self, report_path: str) -> str:
        """Determine age category of report for progressive summarization"""
        # Extract date from filename (format: YYYYMMDD-HHMMSS-)
        filename = Path(report_path).name
        date_match = re.match(r'(\d{8})-', filename)

        if not date_match:
            return 'unknown'

        report_date = datetime.strptime(date_match.group(1), '%Y%m%d')
        age_days = (datetime.now() - report_date).days

        if age_days <= 7:
            return 'recent'  # Full report available
        elif age_days <= 30:
            return 'compressed'  # Key findings extracted
        else:
            return 'archived'  # Only learnings in database

    def mine_all_reports(self, since_days: int = 30) -> Dict[str, Any]:
        """Process all reports from last N days"""
        if not self.reports_path.exists():
            print(f"Reports path not found: {self.reports_path}")
            return {'processed': 0, 'errors': []}

        reports = list(self.reports_path.glob('*.md'))
        processed = 0
        errors = []

        for report_path in reports:
            if self.is_recent(report_path, since_days):
                try:
                    findings = self.extract_from_report(report_path)
                    processed += 1
                    print(f"✅ Processed: {report_path.name}")

                    # Log summary
                    print(f"   - Antipatterns: {len(findings.get('antipatterns', []))}")
                    print(f"   - Prevention rules: {len(findings.get('prevention_rules', []))}")
                    print(f"   - Tags: {len(findings.get('tags', {}).get('all', []))}")

                except Exception as e:
                    error_msg = f"Error processing {report_path.name}: {str(e)}"
                    errors.append(error_msg)
                    print(f"❌ {error_msg}")

        # Get summary statistics
        stats = self.get_mining_statistics()

        return {
            'processed': processed,
            'errors': errors,
            'statistics': stats
        }

    def is_recent(self, report_path: Path, days: int) -> bool:
        """Check if report is within specified days"""
        # Extract date from filename
        filename = report_path.name
        date_match = re.match(r'(\d{8})-', filename)

        if not date_match:
            # If can't parse date, include it
            return True

        report_date = datetime.strptime(date_match.group(1), '%Y%m%d')
        cutoff_date = datetime.now() - timedelta(days=days)

        return report_date >= cutoff_date

    def get_mining_statistics(self) -> Dict[str, Any]:
        """Get statistics about mined data"""
        stats = {}

        # Count antipatterns
        self.cursor.execute('SELECT COUNT(*) as count FROM antipatterns')
        stats['total_antipatterns'] = self.cursor.fetchone()['count']

        # Count prevention rules
        self.cursor.execute('SELECT COUNT(*) as count FROM prevention_rules')
        stats['total_prevention_rules'] = self.cursor.fetchone()['count']

        # Count processed reports
        self.cursor.execute('SELECT COUNT(*) as count FROM report_findings')
        stats['total_reports_processed'] = self.cursor.fetchone()['count']

        # Get most common antipatterns
        self.cursor.execute('''
            SELECT title, category, occurrence_count
            FROM antipatterns
            ORDER BY occurrence_count DESC
            LIMIT 5
        ''')
        stats['top_antipatterns'] = [dict(row) for row in self.cursor.fetchall()]

        # Get high confidence prevention rules
        self.cursor.execute('''
            SELECT description, recommendation, confidence
            FROM prevention_rules
            WHERE confidence >= 0.7
            ORDER BY confidence DESC
            LIMIT 5
        ''')
        stats['high_priority_rules'] = [dict(row) for row in self.cursor.fetchall()]

        return stats

    def generate_quality_context(self, limit: int = 10) -> str:
        """Generate quality context string from mined data"""
        context_parts = []

        # Add recent antipatterns
        self.cursor.execute('''
            SELECT title, severity, occurrence_count
            FROM antipatterns
            ORDER BY last_seen DESC
            LIMIT ?
        ''', (limit,))

        antipatterns = self.cursor.fetchall()
        if antipatterns:
            context_parts.append("Recent antipatterns:")
            for ap in antipatterns:
                context_parts.append(f"  - {ap['title']} (severity: {ap['severity']}, occurrences: {ap['occurrence_count']})")

        # Add active prevention rules
        self.cursor.execute('''
            SELECT description, recommendation, confidence
            FROM prevention_rules
            WHERE confidence >= 0.6
            ORDER BY confidence DESC
            LIMIT ?
        ''', (limit,))

        rules = self.cursor.fetchall()
        if rules:
            context_parts.append("\nActive prevention rules:")
            for rule in rules:
                context_parts.append(f"  - {rule['description']} → {rule['recommendation']}")

        return '\n'.join(context_parts)

    def close(self):
        """Close database connection"""
        if self.conn:
            self.conn.close()


def main():
    """Main entry point for report mining"""
    import argparse

    parser = argparse.ArgumentParser(description='Mine intelligence from VNX reports')
    parser.add_argument('--days', type=int, default=30, help='Process reports from last N days')
    parser.add_argument('--stats', action='store_true', help='Show statistics only')
    parser.add_argument('--context', action='store_true', help='Generate quality context')

    args = parser.parse_args()

    miner = ReportMiner()

    try:
        if args.stats:
            # Show statistics
            stats = miner.get_mining_statistics()
            print("\n📊 Mining Statistics:")
            print(f"  Total reports processed: {stats['total_reports_processed']}")
            print(f"  Total antipatterns found: {stats['total_antipatterns']}")
            print(f"  Total prevention rules: {stats['total_prevention_rules']}")

            if stats.get('top_antipatterns'):
                print("\n🔍 Top Antipatterns:")
                for ap in stats['top_antipatterns']:
                    print(f"  - {ap['title'][:80]}... ({ap['occurrence_count']} occurrences)")

            if stats.get('high_priority_rules'):
                print("\n⚡ High Priority Rules:")
                for rule in stats['high_priority_rules']:
                    print(f"  - {rule['description'][:40]}... → {rule['recommendation'][:40]}...")

        elif args.context:
            # Generate quality context
            context = miner.generate_quality_context()
            print("\n📝 Quality Context:")
            print(context)

        else:
            # Mine reports
            print(f"🔍 Mining reports from last {args.days} days...")
            result = miner.mine_all_reports(since_days=args.days)

            print(f"\n✅ Processing complete!")
            print(f"  Reports processed: {result['processed']}")

            if result['errors']:
                print(f"\n⚠️ Errors encountered:")
                for error in result['errors']:
                    print(f"  - {error}")

            # Show statistics
            stats = result.get('statistics', {})
            if stats:
                print(f"\n📊 Database Statistics:")
                print(f"  Total antipatterns: {stats.get('total_antipatterns', 0)}")
                print(f"  Total prevention rules: {stats.get('total_prevention_rules', 0)}")

    finally:
        miner.close()


if __name__ == '__main__':
    main()
