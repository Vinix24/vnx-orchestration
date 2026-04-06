#!/usr/bin/env python3
"""
Quality Dashboard Integration
Version: 8.0.2 (Phase 2)
Purpose: Integrate quality intelligence metrics into VNX dashboard
"""

import sqlite3
import json
import sys
from pathlib import Path
from datetime import datetime
from typing import Dict, List

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR / "lib"))
try:
    from vnx_paths import ensure_env
except Exception as exc:
    raise SystemExit(f"Failed to load vnx_paths: {exc}")

# VNX Base Configuration
PATHS = ensure_env()
VNX_BASE = Path(PATHS["VNX_HOME"])
STATE_DIR = Path(PATHS["VNX_STATE_DIR"])
DB_PATH = STATE_DIR / "quality_intelligence.db"
DASHBOARD_PATH = STATE_DIR / "dashboard_status.json"

# Color codes
class Colors:
    GREEN = '\033[92m'
    RED = '\033[91m'
    YELLOW = '\033[93m'
    BLUE = '\033[94m'
    CYAN = '\033[96m'
    RESET = '\033[0m'


def log(level: str, message: str):
    """Log message with timestamp and color coding"""
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    color_map = {
        'INFO': Colors.BLUE,
        'SUCCESS': Colors.GREEN,
        'WARNING': Colors.YELLOW,
        'ERROR': Colors.RED
    }

    color = color_map.get(level, Colors.RESET)
    print(f"[{timestamp}] {color}[{level}]{Colors.RESET} {message}")


class QualityDashboardIntegrator:
    """Integrate quality metrics into VNX dashboard"""

    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.conn = None

    def connect(self):
        """Connect to database"""
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row

    def close(self):
        """Close database connection"""
        if self.conn:
            self.conn.close()

    def get_quality_summary(self) -> Dict:
        """Get overall quality summary"""
        cursor = self.conn.cursor()

        # Total files analyzed
        cursor.execute("SELECT COUNT(*) as total FROM vnx_code_quality")
        total_files = cursor.fetchone()['total']

        # Quality distribution
        cursor.execute("""
            SELECT
                COUNT(CASE WHEN complexity_score <= 30 THEN 1 END) as excellent,
                COUNT(CASE WHEN complexity_score > 30 AND complexity_score <= 50 THEN 1 END) as good,
                COUNT(CASE WHEN complexity_score > 50 AND complexity_score <= 70 THEN 1 END) as fair,
                COUNT(CASE WHEN complexity_score > 70 THEN 1 END) as poor
            FROM vnx_code_quality
        """)

        distribution = cursor.fetchone()

        # Average complexity
        cursor.execute("SELECT AVG(complexity_score) as avg_complexity FROM vnx_code_quality")
        avg_complexity = cursor.fetchone()['avg_complexity'] or 0

        # Critical issues
        cursor.execute("SELECT SUM(critical_issues) as total_critical FROM vnx_code_quality")
        total_critical = cursor.fetchone()['total_critical'] or 0

        # Warning issues
        cursor.execute("SELECT SUM(warning_issues) as total_warnings FROM vnx_code_quality")
        total_warnings = cursor.fetchone()['total_warnings'] or 0

        # Track assignments
        cursor.execute("""
            SELECT
                suggested_track,
                COUNT(*) as count
            FROM vnx_code_quality
            WHERE suggested_track IS NOT NULL
            GROUP BY suggested_track
        """)

        tracks = {row['suggested_track']: row['count'] for row in cursor.fetchall()}

        return {
            'total_files': total_files,
            'distribution': {
                'excellent': distribution['excellent'],
                'good': distribution['good'],
                'fair': distribution['fair'],
                'poor': distribution['poor']
            },
            'avg_complexity': round(avg_complexity, 2),
            'issues': {
                'critical': int(total_critical),
                'warnings': int(total_warnings),
                'total': int(total_critical + total_warnings)
            },
            'track_assignments': {
                'track_A': tracks.get('A', 0),
                'track_B': tracks.get('B', 0),
                'track_C': tracks.get('C', 0),
                'unassigned': tracks.get(None, 0)
            }
        }

    def get_snippet_summary(self) -> Dict:
        """Get code snippet summary"""
        cursor = self.conn.cursor()

        # Total snippets
        cursor.execute("SELECT COUNT(*) as total FROM snippet_metadata")
        total_snippets = cursor.fetchone()['total']

        # Average quality
        cursor.execute("SELECT AVG(quality_score) as avg_quality FROM snippet_metadata")
        avg_quality = cursor.fetchone()['avg_quality'] or 0

        # High quality snippets (>= 80)
        cursor.execute("SELECT COUNT(*) as high_quality FROM snippet_metadata WHERE quality_score >= 80")
        high_quality = cursor.fetchone()['high_quality']

        # Most used snippets
        cursor.execute("""
            SELECT COUNT(*) as frequently_used
            FROM snippet_metadata
            WHERE usage_count > 0
        """)
        frequently_used = cursor.fetchone()['frequently_used']

        # Unique source files
        cursor.execute("SELECT COUNT(DISTINCT file_path) as unique_files FROM snippet_metadata")
        unique_files = cursor.fetchone()['unique_files']

        return {
            'total_snippets': total_snippets,
            'avg_quality': round(avg_quality, 2),
            'high_quality_count': high_quality,
            'frequently_used': frequently_used,
            'source_files': unique_files
        }

    def get_top_issues(self, limit: int = 5) -> List[Dict]:
        """Get files with most critical issues"""
        cursor = self.conn.cursor()

        cursor.execute("""
            SELECT
                relative_path,
                complexity_score,
                critical_issues,
                warning_issues,
                suggested_track
            FROM vnx_code_quality
            WHERE critical_issues > 0 OR complexity_score > 70
            ORDER BY critical_issues DESC, complexity_score DESC
            LIMIT ?
        """, (limit,))

        return [
            {
                'file': row['relative_path'],
                'complexity': round(row['complexity_score'], 1),
                'critical': row['critical_issues'],
                'warnings': row['warning_issues'],
                'track': row['suggested_track']
            }
            for row in cursor.fetchall()
        ]

    def get_recent_scans(self) -> Dict:
        """Get recent scan information"""
        cursor = self.conn.cursor()

        # Latest scan time
        cursor.execute("SELECT MAX(last_scan) as latest_scan FROM vnx_code_quality")
        latest_scan = cursor.fetchone()['latest_scan']

        # Scan history
        cursor.execute("""
            SELECT
                scan_type,
                files_scanned,
                issues_found,
                scan_duration_seconds,
                started_at,
                status
            FROM scan_history
            ORDER BY started_at DESC
            LIMIT 1
        """)

        latest_history = cursor.fetchone()

        scan_info = {
            'latest_scan': latest_scan,
            'history': None
        }

        if latest_history:
            scan_info['history'] = {
                'type': latest_history['scan_type'],
                'files_scanned': latest_history['files_scanned'],
                'issues_found': latest_history['issues_found'],
                'duration_seconds': latest_history['scan_duration_seconds'],
                'started_at': latest_history['started_at'],
                'status': latest_history['status']
            }

        return scan_info

    def integrate_with_dashboard(self) -> bool:
        """Add quality metrics to existing dashboard JSON"""
        try:
            # Load current dashboard
            if not DASHBOARD_PATH.exists():
                log('ERROR', f'Dashboard file not found: {DASHBOARD_PATH}')
                return False

            with open(DASHBOARD_PATH, 'r') as f:
                dashboard = json.load(f)

            # Get quality metrics
            quality_summary = self.get_quality_summary()
            snippet_summary = self.get_snippet_summary()
            top_issues = self.get_top_issues()
            scan_info = self.get_recent_scans()

            # Add quality intelligence section
            dashboard['quality_intelligence'] = {
                'code_quality': quality_summary,
                'snippets': snippet_summary,
                'top_issues': top_issues,
                'scan_info': scan_info,
                'database_path': str(DB_PATH),
                'last_updated': datetime.now().strftime('%Y-%m-%dT%H:%M:%SZ')
            }

            # Write updated dashboard
            with open(DASHBOARD_PATH, 'w') as f:
                json.dump(dashboard, f, indent=4)

            log('SUCCESS', 'Quality metrics integrated into dashboard')
            return True

        except Exception as e:
            log('ERROR', f'Dashboard integration failed: {e}')
            return False

    def print_summary(self):
        """Print quality summary to console"""
        quality = self.get_quality_summary()
        snippets = self.get_snippet_summary()

        print(f"\n{Colors.CYAN}{'='*70}")
        print(f"Quality Intelligence Summary")
        print(f"{'='*70}{Colors.RESET}\n")

        print(f"{Colors.BLUE}Code Quality:{Colors.RESET}")
        print(f"  Total Files: {quality['total_files']}")
        print(f"  Average Complexity: {quality['avg_complexity']}/100")
        print(f"  Critical Issues: {quality['issues']['critical']}")
        print(f"  Warnings: {quality['issues']['warnings']}")

        print(f"\n{Colors.BLUE}Quality Distribution:{Colors.RESET}")
        dist = quality['distribution']
        print(f"  {Colors.GREEN}Excellent (≤30):{Colors.RESET} {dist['excellent']} files")
        print(f"  {Colors.GREEN}Good (31-50):{Colors.RESET} {dist['good']} files")
        print(f"  {Colors.YELLOW}Fair (51-70):{Colors.RESET} {dist['fair']} files")
        print(f"  {Colors.RED}Poor (>70):{Colors.RESET} {dist['poor']} files")

        print(f"\n{Colors.BLUE}Track Assignments:{Colors.RESET}")
        tracks = quality['track_assignments']
        print(f"  Track A (Storage): {tracks['track_A']} files")
        print(f"  Track B (Refactor): {tracks['track_B']} files")
        print(f"  Track C (Investigation): {tracks['track_C']} files")

        print(f"\n{Colors.BLUE}Code Snippets:{Colors.RESET}")
        print(f"  Total Snippets: {snippets['total_snippets']}")
        print(f"  Average Quality: {snippets['avg_quality']}/100")
        print(f"  High Quality (≥80): {snippets['high_quality_count']}")
        print(f"  Source Files: {snippets['source_files']}")

        print(f"\n{Colors.YELLOW}Top Issues:{Colors.RESET}")
        for issue in self.get_top_issues(3):
            print(f"  {issue['file']}")
            print(f"    Complexity: {issue['complexity']} | Critical: {issue['critical']} | Warnings: {issue['warnings']}")

        print()


def main():
    """Main execution"""
    print(f"\n{Colors.BLUE}{'='*70}")
    print(f"VNX Quality Dashboard Integration")
    print(f"Version: 8.0.2 (Phase 2)")
    print(f"{'='*70}{Colors.RESET}\n")

    # Verify database exists
    if not DB_PATH.exists():
        log('ERROR', f'Quality database not found: {DB_PATH}')
        log('INFO', 'Run code_quality_scanner.py first')
        return 1

    # Create integrator
    integrator = QualityDashboardIntegrator(DB_PATH)

    # Connect to database
    integrator.connect()

    try:
        # Print summary
        integrator.print_summary()

        # Integrate with dashboard
        success = integrator.integrate_with_dashboard()

        if success:
            log('SUCCESS', f'Dashboard updated: {DASHBOARD_PATH}')
            return 0
        else:
            return 1

    finally:
        integrator.close()


if __name__ == "__main__":
    exit(main())
