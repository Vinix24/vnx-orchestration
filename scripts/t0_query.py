#!/usr/bin/env python3
"""
T0 Query Interface
==================
Provides urgent issue detection and system intelligence for T0 orchestrator.

Author: T-MANAGER
Date: 2025-09-25
Version: 1.0
"""

import os
import json
import sqlite3
import argparse
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, List

# Configuration
SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR / "lib") not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR / "lib"))
try:
    from vnx_paths import ensure_env
except Exception as exc:
    raise SystemExit(f"Failed to load vnx_paths: {exc}")

PATHS = ensure_env()
DATABASE_PATH = os.path.join(PATHS["VNX_DB_DIR"], 'unified_state.db')

class T0QueryInterface:
    """Query interface for T0 orchestrator intelligence"""

    def __init__(self):
        """Initialize query interface"""
        if not os.path.exists(DATABASE_PATH):
            print(f"❌ Database not found: {DATABASE_PATH}")
            print("   Please ensure unified_state_manager.py is running")
            exit(1)

        self.db = sqlite3.connect(DATABASE_PATH)
        self.db.row_factory = sqlite3.Row

    def get_urgent_issues(self) -> List[Dict]:
        """Get all urgent issues requiring immediate attention"""
        urgent = []

        # Failed tasks
        failed = self.db.execute("""
            SELECT * FROM events
            WHERE status IN ('failed', 'error', 'rejected', 'timeout')
            AND timestamp > datetime('now', '-1 hour')
            ORDER BY timestamp DESC
            LIMIT 10
        """).fetchall()

        for event in failed:
            urgent.append({
                'severity': 'CRITICAL',
                'type': 'Task Failure',
                'event_id': event['event_id'],
                'terminal': event['terminal'],
                'track': event['track'],
                'timestamp': event['timestamp'],
                'status': event['status']
            })

        # Stuck dispatches (pending >10 minutes)
        stuck = self.db.execute("""
            SELECT * FROM events
            WHERE event_type = 'dispatch'
            AND status = 'pending'
            AND timestamp < datetime('now', '-10 minutes')
            ORDER BY timestamp ASC
        """).fetchall()

        for event in stuck:
            urgent.append({
                'severity': 'HIGH',
                'type': 'Stuck Dispatch',
                'event_id': event['event_id'],
                'terminal': event['terminal'],
                'track': event['track'],
                'timestamp': event['timestamp'],
                'age': self._calculate_age(event['timestamp'])
            })

        # No receipts for dispatches (>5 minutes old)
        uncorrelated = self.db.execute("""
            SELECT * FROM events
            WHERE event_type = 'dispatch'
            AND correlation_id IS NULL
            AND timestamp < datetime('now', '-5 minutes')
            ORDER BY timestamp ASC
            LIMIT 5
        """).fetchall()

        for event in uncorrelated:
            urgent.append({
                'severity': 'MEDIUM',
                'type': 'No Receipt',
                'event_id': event['event_id'],
                'terminal': event['terminal'],
                'track': event['track'],
                'timestamp': event['timestamp'],
                'age': self._calculate_age(event['timestamp'])
            })

        return urgent

    def get_terminal_status(self) -> Dict:
        """Get current terminal status and workload"""
        terminals = {}

        for terminal in ['T0', 'T1', 'T2', 'T3']:
            # Last activity
            last = self.db.execute("""
                SELECT * FROM events
                WHERE terminal = ?
                ORDER BY timestamp DESC
                LIMIT 1
            """, (terminal,)).fetchone()

            # Active tasks (dispatches without completion)
            active = self.db.execute("""
                SELECT COUNT(*) as count FROM events
                WHERE terminal = ?
                AND event_type = 'dispatch'
                AND status = 'active'
            """, (terminal,)).fetchone()['count']

            # Pending tasks
            pending = self.db.execute("""
                SELECT COUNT(*) as count FROM events
                WHERE terminal = ?
                AND event_type = 'dispatch'
                AND status = 'pending'
            """, (terminal,)).fetchone()['count']

            # Success rate (last hour)
            completed = self.db.execute("""
                SELECT COUNT(*) as count FROM events
                WHERE terminal = ?
                AND status = 'completed'
                AND timestamp > datetime('now', '-1 hour')
            """, (terminal,)).fetchone()['count']

            failed = self.db.execute("""
                SELECT COUNT(*) as count FROM events
                WHERE terminal = ?
                AND status IN ('failed', 'error', 'rejected')
                AND timestamp > datetime('now', '-1 hour')
            """, (terminal,)).fetchone()['count']

            total = completed + failed
            success_rate = (completed / total * 100) if total > 0 else 100

            terminals[terminal] = {
                'last_activity': last['timestamp'] if last else 'Never',
                'active_tasks': active,
                'pending_tasks': pending,
                'success_rate': f"{success_rate:.1f}%",
                'status': self._determine_terminal_status(last, active, pending)
            }

        return terminals

    def get_recent_reports(self, limit: int = 5) -> List[Dict]:
        """Get recent reports with summary"""
        reports = self.db.execute("""
            SELECT * FROM events
            WHERE event_type = 'report'
            ORDER BY timestamp DESC
            LIMIT ?
        """, (limit,)).fetchall()

        summaries = []
        for report in reports:
            data = json.loads(report['data']) if report['data'] else {}
            summaries.append({
                'timestamp': report['timestamp'],
                'terminal': report['terminal'],
                'report_id': report['event_id'],
                'filepath': data.get('filepath', 'N/A')
            })

        return summaries

    def get_system_bottlenecks(self) -> Dict:
        """Identify system bottlenecks and performance issues"""
        bottlenecks = {}

        # Track queue sizes
        for track in ['A', 'B', 'C']:
            queue_size = self.db.execute("""
                SELECT COUNT(*) as count FROM events
                WHERE track = ?
                AND status = 'pending'
            """, (track,)).fetchone()['count']

            avg_wait = self.db.execute("""
                SELECT AVG(
                    CAST((julianday('now') - julianday(timestamp)) * 24 * 60 AS REAL)
                ) as avg_wait
                FROM events
                WHERE track = ?
                AND status = 'pending'
            """, (track,)).fetchone()['avg_wait']

            bottlenecks[f"Track_{track}"] = {
                'queue_size': queue_size,
                'avg_wait_minutes': round(avg_wait, 1) if avg_wait else 0
            }

        # Overall system metrics
        total_pending = self.db.execute("""
            SELECT COUNT(*) as count FROM events
            WHERE status = 'pending'
        """).fetchone()['count']

        total_stuck = self.db.execute("""
            SELECT COUNT(*) as count FROM events
            WHERE status = 'pending'
            AND timestamp < datetime('now', '-10 minutes')
        """).fetchone()['count']

        bottlenecks['system'] = {
            'total_pending': total_pending,
            'stuck_tasks': total_stuck,
            'health': self._calculate_health_score(total_pending, total_stuck)
        }

        return bottlenecks

    def get_correlation_summary(self) -> Dict:
        """Get correlation statistics"""
        total_correlations = self.db.execute(
            "SELECT COUNT(*) as count FROM correlations"
        ).fetchone()['count']

        # Dispatches with successful correlations
        correlated = self.db.execute("""
            SELECT COUNT(DISTINCT correlation_id) as count
            FROM events
            WHERE correlation_id IS NOT NULL
        """).fetchone()['count']

        # Uncorrelated dispatches
        uncorrelated = self.db.execute("""
            SELECT COUNT(*) as count FROM events
            WHERE event_type = 'dispatch'
            AND correlation_id IS NULL
            AND timestamp < datetime('now', '-5 minutes')
        """).fetchone()['count']

        return {
            'total_correlations': total_correlations,
            'successful_correlations': correlated,
            'pending_correlations': uncorrelated,
            'correlation_rate': f"{(correlated/(correlated+uncorrelated)*100) if (correlated+uncorrelated) > 0 else 100:.1f}%"
        }

    def _calculate_age(self, timestamp: str) -> str:
        """Calculate age of event"""
        try:
            event_time = datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
            age = datetime.now(event_time.tzinfo) - event_time

            if age.total_seconds() < 60:
                return f"{int(age.total_seconds())}s"
            elif age.total_seconds() < 3600:
                return f"{int(age.total_seconds() / 60)}m"
            else:
                return f"{int(age.total_seconds() / 3600)}h"
        except:
            return "unknown"

    def _determine_terminal_status(self, last_event, active: int, pending: int) -> str:
        """Determine terminal status based on activity"""
        if not last_event:
            return "offline"

        try:
            last_time = datetime.fromisoformat(last_event['timestamp'].replace('Z', '+00:00'))
            age = (datetime.now(last_time.tzinfo) - last_time).total_seconds()

            if age > 3600:  # >1 hour
                return "idle"
            elif active > 0:
                return "working"
            elif pending > 0:
                return "ready"
            else:
                return "available"
        except:
            return "unknown"

    def _calculate_health_score(self, pending: int, stuck: int) -> str:
        """Calculate system health score"""
        if stuck > 5:
            return "CRITICAL"
        elif stuck > 2 or pending > 20:
            return "WARNING"
        elif pending > 10:
            return "MODERATE"
        else:
            return "HEALTHY"

    def display_dashboard(self):
        """Display comprehensive T0 dashboard"""
        print("\n" + "="*80)
        print(" T0 INTELLIGENCE DASHBOARD ".center(80))
        print("="*80)

        # Urgent Issues
        urgent = self.get_urgent_issues()
        print(f"\n🚨 URGENT ISSUES ({len(urgent)} total)")
        print("-" * 40)

        if urgent:
            for issue in urgent[:5]:  # Show top 5
                severity_emoji = {
                    'CRITICAL': '🔴',
                    'HIGH': '🟡',
                    'MEDIUM': '🟢'
                }.get(issue['severity'], '⚪')

                print(f"{severity_emoji} [{issue['severity']}] {issue['type']}")
                print(f"   Event: {issue['event_id']}")
                print(f"   Terminal: {issue.get('terminal', 'N/A')} | Track: {issue.get('track', 'N/A')}")
                print(f"   Time: {issue['timestamp']}")
                if 'age' in issue:
                    print(f"   Age: {issue['age']}")
                print()
        else:
            print("   ✅ No urgent issues detected")

        # Terminal Status
        terminals = self.get_terminal_status()
        print("\n📊 TERMINAL STATUS")
        print("-" * 40)

        for terminal, status in terminals.items():
            status_emoji = {
                'working': '🔄',
                'available': '✅',
                'ready': '⏳',
                'idle': '😴',
                'offline': '❌'
            }.get(status['status'], '❓')

            print(f"{status_emoji} {terminal}: {status['status'].upper()}")
            print(f"   Active: {status['active_tasks']} | Pending: {status['pending_tasks']}")
            print(f"   Success Rate: {status['success_rate']}")
            print(f"   Last Activity: {status['last_activity']}")
            print()

        # System Bottlenecks
        bottlenecks = self.get_system_bottlenecks()
        print("\n⚡ SYSTEM BOTTLENECKS")
        print("-" * 40)

        health_emoji = {
            'HEALTHY': '✅',
            'MODERATE': '🟢',
            'WARNING': '🟡',
            'CRITICAL': '🔴'
        }.get(bottlenecks['system']['health'], '❓')

        print(f"System Health: {health_emoji} {bottlenecks['system']['health']}")
        print(f"Total Pending: {bottlenecks['system']['total_pending']}")
        print(f"Stuck Tasks: {bottlenecks['system']['stuck_tasks']}")
        print()

        for track in ['Track_A', 'Track_B', 'Track_C']:
            if track in bottlenecks:
                print(f"{track}: Queue={bottlenecks[track]['queue_size']}, "
                      f"Avg Wait={bottlenecks[track]['avg_wait_minutes']}min")

        # Correlations
        correlations = self.get_correlation_summary()
        print("\n🔗 CORRELATIONS")
        print("-" * 40)
        print(f"Total: {correlations['total_correlations']}")
        print(f"Successful: {correlations['successful_correlations']}")
        print(f"Pending: {correlations['pending_correlations']}")
        print(f"Rate: {correlations['correlation_rate']}")

        # Recent Reports
        reports = self.get_recent_reports(3)
        print("\n📄 RECENT REPORTS")
        print("-" * 40)

        if reports:
            for report in reports:
                print(f"• {report['timestamp']} - {report['terminal']}")
                print(f"  {report['report_id']}")
        else:
            print("   No recent reports")

        print("\n" + "="*80)
        print(f"Generated: {datetime.now().isoformat()}")
        print("="*80 + "\n")

    def export_urgent_json(self, filepath: str):
        """Export urgent issues to JSON for T0 consumption"""
        data = {
            'timestamp': datetime.now().isoformat(),
            'urgent_issues': self.get_urgent_issues(),
            'terminal_status': self.get_terminal_status(),
            'bottlenecks': self.get_system_bottlenecks(),
            'correlations': self.get_correlation_summary(),
            'recent_reports': self.get_recent_reports()
        }

        with open(filepath, 'w') as f:
            json.dump(data, f, indent=2)

        print(f"✅ Exported urgent issues to: {filepath}")

def main():
    """Main execution"""
    parser = argparse.ArgumentParser(description='T0 Query Interface for VNX System Intelligence')
    parser.add_argument('--urgent', action='store_true', help='Show only urgent issues')
    parser.add_argument('--terminals', action='store_true', help='Show terminal status')
    parser.add_argument('--reports', action='store_true', help='Show recent reports')
    parser.add_argument('--bottlenecks', action='store_true', help='Show system bottlenecks')
    parser.add_argument('--export', metavar='PATH', help='Export data to JSON file')
    parser.add_argument('--dashboard', action='store_true', help='Show full dashboard (default)')

    args = parser.parse_args()

    interface = T0QueryInterface()

    # If no specific flag, show dashboard
    if not any([args.urgent, args.terminals, args.reports, args.bottlenecks, args.export]):
        args.dashboard = True

    if args.dashboard:
        interface.display_dashboard()
    else:
        if args.urgent:
            urgent = interface.get_urgent_issues()
            print(f"\n🚨 URGENT ISSUES: {len(urgent)}")
            for issue in urgent:
                print(f"  [{issue['severity']}] {issue['type']} - {issue['event_id']}")

        if args.terminals:
            terminals = interface.get_terminal_status()
            print("\n📊 TERMINAL STATUS:")
            for t, status in terminals.items():
                print(f"  {t}: {status['status']} (Active: {status['active_tasks']}, Success: {status['success_rate']})")

        if args.reports:
            reports = interface.get_recent_reports()
            print("\n📄 RECENT REPORTS:")
            for report in reports:
                print(f"  {report['timestamp']} - {report['terminal']} - {report['report_id']}")

        if args.bottlenecks:
            bottlenecks = interface.get_system_bottlenecks()
            print(f"\n⚡ SYSTEM HEALTH: {bottlenecks['system']['health']}")
            print(f"  Pending: {bottlenecks['system']['total_pending']}, Stuck: {bottlenecks['system']['stuck_tasks']}")

    if args.export:
        interface.export_urgent_json(args.export)

if __name__ == '__main__':
    main()
