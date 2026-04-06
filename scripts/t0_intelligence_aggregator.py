#!/usr/bin/env python3
"""
T0 Intelligence Aggregator - Core Hub
=====================================
Core hub for T0 orchestration decisions with receipt correlation,
warnings, terminal insights, and tag-based report lookup.

Features:
- Receipt correlation and processing
- Warnings from correlation engine (error loops, issues)
- Terminal work insights aggregation
- Tag-based report lookup capability
- Progressive context loading for token management
- Rolling window with automatic archival

Author: T-MANAGER
Date: 2025-09-29
Version: 2.0
"""

import sys
import json
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Any
from collections import defaultdict, deque, Counter
import hashlib

class T0IntelligenceAggregator:
    """Core hub aggregating system state for T0 orchestration decisions"""

    def __init__(self, vnx_root: str = None):
        # Resolve runtime paths via vnx_paths
        lib_dir = str(Path(__file__).resolve().parent / "lib")
        if lib_dir not in sys.path:
            sys.path.insert(0, lib_dir)
        from vnx_paths import resolve_paths as _resolve_vnx_paths
        vnx_paths = _resolve_vnx_paths()

        self.vnx_root = Path(vnx_root) if vnx_root else Path(vnx_paths["VNX_HOME"])
        self.state_dir = Path(vnx_paths["VNX_STATE_DIR"])
        self.reports_dir = Path(vnx_paths["VNX_REPORTS_DIR"])

        # Output files
        self.intelligence_file = self.state_dir / "t0_intelligence.ndjson"
        self.intelligence_full = self.state_dir / "t0_intelligence_full.ndjson"
        self.intelligence_archive = self.state_dir / "t0_intelligence_archive.ndjson"

        # Configuration
        self.SNAPSHOT_SIZE = 3  # Last N events in snapshot (reduced from 10 for efficiency - 2026-01-17)
        self.ROLLING_WINDOW = 100  # Keep N events in main file
        self.ARCHIVE_THRESHOLD = 1000  # Archive after N events
        self.ERROR_LOOP_THRESHOLD = 3  # Consecutive errors to trigger warning
        self.WARNING_SEVERITY_LEVELS = ['low', 'medium', 'high', 'critical']

        # State tracking
        self.event_buffer = deque(maxlen=self.ROLLING_WINDOW)
        self.event_count = 0
        self.last_snapshot_time = time.time()

        # Receipt correlation tracking
        self.receipt_cache = {}
        self.processed_receipts = set()

        # Warning and correlation tracking
        self.error_patterns = defaultdict(list)  # Track error patterns
        self.terminal_activity = defaultdict(list)  # Track terminal work
        self.report_tags = {}  # Cache for report tags
        self.correlation_warnings = []  # Active warnings

        # Initialize files if needed
        self._initialize_files()

        # Load existing report tags
        self._load_report_tags()

    def _initialize_files(self):
        """Initialize output files if they don't exist"""
        for file_path in [self.intelligence_file, self.intelligence_full, self.intelligence_archive]:
            if not file_path.exists():
                file_path.touch()

    def _load_report_tags(self):
        """Load tags from existing reports for quick lookup"""
        if not self.reports_dir.exists():
            return

        # Parse index.ndjson for report metadata with tags
        index_file = self.reports_dir / "index.ndjson"
        if index_file.exists():
            try:
                with open(index_file, 'r') as f:
                    for line in f:
                        if line.strip():
                            try:
                                report_meta = json.loads(line)
                                report_id = report_meta.get('report_id', '')
                                tags = report_meta.get('tags', [])
                                if report_id and tags:
                                    self.report_tags[report_id] = tags
                                    # Also index by individual tags
                                    for tag in tags:
                                        if tag not in self.report_tags:
                                            self.report_tags[tag] = []
                                        self.report_tags[tag].append(report_id)
                            except json.JSONDecodeError:
                                continue
            except FileNotFoundError:
                pass

    def add_event(self, event: Dict[str, Any]) -> None:
        """Add a new event to the intelligence stream with correlation"""
        # Ensure event has required fields
        event = self._normalize_event(event)

        # Process correlations and warnings
        self._process_correlations(event)

        # Track terminal activity
        if event.get('terminal'):
            self.terminal_activity[event['terminal']].append({
                'timestamp': event.get('timestamp'),
                'event_type': event.get('event_type'),
                'status': event.get('status'),
                'task': event.get('task_id') or event.get('dispatch_id')
            })

        # Add to full history
        with open(self.intelligence_full, 'a') as f:
            f.write(json.dumps(event) + '\n')

        # Add to buffer
        self.event_buffer.append(event)
        self.event_count += 1

        # Check if archival needed
        if self.event_count >= self.ARCHIVE_THRESHOLD:
            self._archive_old_events()

        # Generate and write snapshot with correlations
        snapshot = self._generate_snapshot()
        self._write_rolling_window(snapshot)

    def _normalize_event(self, event: Dict[str, Any]) -> Dict[str, Any]:
        """Ensure event has all required fields"""
        # Normalize event type field (handle both 'event' and 'event_type')
        if 'event_type' not in event and 'event' in event:
            event['event_type'] = event['event']
        elif 'event_type' not in event:
            event['event_type'] = 'unknown'

        # Handle terminal_status events with nested terminals dict
        if event.get('event_type') == 'terminal_status' and 'terminals' in event:
            terminals_dict = event.get('terminals', {})
            if isinstance(terminals_dict, dict):
                # Convert dict to list format for storage
                terminals_list = []
                for term_id, term_data in terminals_dict.items():
                    if isinstance(term_data, dict):
                        term_data['terminal'] = term_id
                        terminals_list.append(term_data)
                event['terminals'] = terminals_list

        # Map track to terminal if present
        if 'track' in event and 'terminal' not in event:
            track_map = {'A': 'T1', 'B': 'T2', 'C': 'T3'}
            event['terminal'] = track_map.get(event['track'], f"T{event['track']}")

        # Add timestamp if missing
        if 'timestamp' not in event:
            event['timestamp'] = datetime.utcnow().isoformat() + 'Z'

        # Add event_id if missing
        if 'event_id' not in event:
            content = json.dumps(event, sort_keys=True)
            event['event_id'] = hashlib.sha256(content.encode()).hexdigest()[:12]

        return event

    def _generate_snapshot(self) -> Dict[str, Any]:
        """Generate comprehensive snapshot with correlations and warnings"""
        recent_events = list(self.event_buffer)[-self.SNAPSHOT_SIZE:]

        # Compute summary statistics with correlations
        summary = self._compute_summary(list(self.event_buffer))

        # Get active warnings
        active_warnings = self._get_active_warnings()

        # Get terminal insights
        terminal_insights = self._get_terminal_insights()

        # Get recent reports by tags
        tag_report_mapping = self._get_tag_report_mapping()

        # Load t0_brief for actionable orchestration data
        t0_brief = self._load_t0_brief()

        snapshot = {
            "event_type": "t0_intelligence_snapshot",
            "timestamp": datetime.utcnow().isoformat() + 'Z',
            "summary": summary,
            "last_events": recent_events,

            # ORCHESTRATION DATA (from t0_brief) - What T0 needs to make decisions
            "terminals": t0_brief.get("terminals", {}),
            "tracks": t0_brief.get("tracks", {}),
            "next_gates": t0_brief.get("next_gates", {}),
            "active_work": t0_brief.get("active_work", []),
            "blockers": t0_brief.get("blockers", []),
            "queues": t0_brief.get("queues", {}),

            "receipts": {
                "total_processed": len(self.processed_receipts),
                "recent": self._get_recent_receipts()
            },
            "correlations": {
                "warnings": active_warnings,
                "error_patterns": self._get_error_patterns(),
                "escalations": [w for w in active_warnings if w.get('severity') in ['high', 'critical']]
            },
            "terminal_insights": terminal_insights,
            "report_tags": tag_report_mapping,
            "context_pointers": {
                "full_history": str(self.intelligence_full),
                "t0_brief": str(self.state_dir / "t0_brief.json"),
                "dashboard": str(self.state_dir / "dashboard_status.json")
            },
            "metrics": {
                "total_events": self.event_count,
                "buffer_size": len(self.event_buffer),
                "snapshot_age_seconds": int(time.time() - self.last_snapshot_time),
                "active_warnings": len(active_warnings),
                "terminals_with_activity": len(terminal_insights)
            }
        }

        self.last_snapshot_time = time.time()
        return snapshot

    def _compute_summary(self, events: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Compute summary statistics from events"""
        if not events:
            return self._empty_summary()

        # Time window
        now = datetime.utcnow()
        one_hour_ago = now - timedelta(hours=1)

        # Filter recent events
        recent = [e for e in events if self._parse_timestamp(e.get('timestamp', '')) > one_hour_ago]

        # Count by type and status
        event_types = defaultdict(int)
        statuses = defaultdict(int)
        terminals = defaultdict(list)
        critical_issues = []

        # Find latest terminal_status event first
        latest_terminal_event = None
        for event in reversed(events):
            if event.get('event_type') == 'terminal_status' and 'terminals' in event:
                latest_terminal_event = event
                break

        for event in events:
            event_type = event.get('event_type', event.get('event', 'unknown'))
            event_types[event_type] += 1

            # For terminal_status events, extract from terminals[] array
            if event_type == 'terminal_status' and 'terminals' in event:
                for term_data in event.get('terminals', []):
                    terminal = term_data.get('terminal', 'unknown')
                    status = term_data.get('status', 'unknown')
                    if terminal != 'unknown':
                        terminals[terminal].append(status)
                        statuses[status] += 1

                        # Detect critical issues
                        if status in ['blocked', 'failed', 'error']:
                            critical_issues.append({
                                'terminal': terminal,
                                'status': status,
                                'event': event_type,
                                'time': event.get('timestamp', 'unknown')
                            })
            else:
                # For non-terminal_status events, use old logic
                status = event.get('status', 'unknown')
                statuses[status] += 1

                terminal = event.get('terminal', 'unknown')
                if terminal != 'unknown':
                    terminals[terminal].append(status)

                # Detect critical issues
                if status in ['blocked', 'failed', 'error']:
                    critical_issues.append({
                        'terminal': terminal,
                        'status': status,
                        'event': event_type,
                        'time': event.get('timestamp', 'unknown')
                    })

            # Check for high severity or confidence issues
            if event.get('severity') == 'critical' or event.get('confidence', 1.0) < 0.5:
                critical_issues.append({
                    'type': 'low_confidence' if event.get('confidence', 1.0) < 0.5 else 'critical',
                    'event': event_type,
                    'details': event.get('message', 'No details')
                })

        # Build terminal_status from latest terminal_status event
        terminal_status = {}
        if latest_terminal_event:
            terminals_list = latest_terminal_event.get('terminals', [])
            for term_data in terminals_list:
                terminal_id = term_data.get('terminal', 'unknown')
                if terminal_id != 'unknown':
                    # Get historical stats from terminals dict
                    historical = terminals.get(terminal_id, [])
                    terminal_status[terminal_id] = {
                        'status': term_data.get('status', 'unknown'),
                        'model': term_data.get('model', 'unknown'),
                        'is_active': term_data.get('is_active', False),
                        'last_update': term_data.get('last_update', 'never'),
                        'total_tasks': len(historical),
                        'successful': sum(1 for s in historical if s == 'success'),
                        'blocked': sum(1 for s in historical if s == 'blocked')
                    }

        return {
            "time_window": "last_hour",
            "total_events": len(events),
            "recent_events_1hr": len(recent),
            "event_types": dict(event_types),
            "status_distribution": dict(statuses),
            "terminal_status": terminal_status,
            "critical_issues": critical_issues[:5],  # Top 5 critical issues
            "terminals_active": sum(1 for t, s in terminal_status.items() if s['status'] not in ['idle', 'offline']),
            "terminals_idle": sum(1 for t, s in terminal_status.items() if s['status'] == 'idle'),
            "tasks_completed_1hr": sum(1 for e in recent if e.get('event_type') == 'task_complete'),
            "tasks_active": sum(1 for t, s in terminal_status.items() if s['status'] in ['working', 'in_progress'])
        }

    def _empty_summary(self) -> Dict[str, Any]:
        """Return empty summary structure"""
        return {
            "time_window": "last_hour",
            "total_events": 0,
            "recent_events_1hr": 0,
            "event_types": {},
            "status_distribution": {},
            "terminal_status": {},
            "critical_issues": [],
            "terminals_active": 0,
            "terminals_idle": 0,
            "tasks_completed_1hr": 0,
            "tasks_active": 0
        }

    def _parse_timestamp(self, timestamp_str: str) -> datetime:
        """Parse various timestamp formats"""
        if not timestamp_str:
            return datetime.min

        # Try ISO format first
        for fmt in [
            "%Y-%m-%dT%H:%M:%S.%fZ",
            "%Y-%m-%dT%H:%M:%SZ",
            "%Y-%m-%dT%H:%M:%S.%f",
            "%Y-%m-%dT%H:%M:%S"
        ]:
            try:
                return datetime.strptime(timestamp_str.replace('+00:00', ''), fmt)
            except ValueError:
                continue

        # Fallback to current time
        return datetime.utcnow()

    def _write_rolling_window(self, snapshot: Dict[str, Any]) -> None:
        """Write rolling window of events plus snapshot"""
        with open(self.intelligence_file, 'w') as f:
            # Write events (excluding snapshot)
            for event in self.event_buffer:
                if event.get('event_type') != 't0_intelligence_snapshot':
                    f.write(json.dumps(event) + '\n')

            # Write snapshot as last line
            f.write(json.dumps(snapshot) + '\n')

    def _archive_old_events(self) -> None:
        """Archive old events when threshold reached"""
        # Read full history
        with open(self.intelligence_full, 'r') as f:
            all_events = [json.loads(line) for line in f if line.strip()]

        # Keep recent, archive old
        if len(all_events) > self.ARCHIVE_THRESHOLD:
            to_archive = all_events[:-self.ROLLING_WINDOW]
            to_keep = all_events[-self.ROLLING_WINDOW:]

            # Append to archive
            with open(self.intelligence_archive, 'a') as f:
                for event in to_archive:
                    f.write(json.dumps(event) + '\n')

            # Rewrite full history with only recent
            with open(self.intelligence_full, 'w') as f:
                for event in to_keep:
                    f.write(json.dumps(event) + '\n')

            # Reset counter
            self.event_count = len(to_keep)

            print(f"Archived {len(to_archive)} events")

    def process_receipt(self, receipt_path: str) -> None:
        """Process receipts with correlation and enrichment"""
        receipt_file = Path(receipt_path)
        if not receipt_file.exists():
            return

        with open(receipt_file, 'r') as f:
            for line in f:
                if line.strip():
                    try:
                        receipt = json.loads(line)

                        # Skip if already processed (by receipt ID if available)
                        receipt_id = receipt.get('receipt_id') or receipt.get('event_id')
                        if receipt_id and receipt_id in self.processed_receipts:
                            continue

                        # Enrich receipt with correlation data
                        enriched = self._enrich_receipt(receipt)

                        # Add to intelligence stream
                        self.add_event(enriched)

                        # Mark as processed
                        if receipt_id:
                            self.processed_receipts.add(receipt_id)

                    except json.JSONDecodeError:
                        print(f"Skipping invalid JSON: {line[:50]}...")

    def _enrich_receipt(self, receipt: Dict[str, Any]) -> Dict[str, Any]:
        """Enrich receipt with additional context and correlations"""
        # Add correlation context
        if receipt.get('status') == 'error' or receipt.get('status') == 'failed':
            # Track error patterns
            terminal = receipt.get('terminal', 'unknown')
            self.error_patterns[terminal].append({
                'timestamp': receipt.get('timestamp'),
                'error_type': receipt.get('event_type'),
                'message': receipt.get('message', '')
            })

        # Add report tags if this is a report receipt
        if receipt.get('report_id'):
            tags = self.report_tags.get(receipt['report_id'], [])
            if tags:
                receipt['tags'] = tags

        return receipt

    def get_snapshot(self) -> Dict[str, Any]:
        """Get current snapshot without adding event"""
        return self._generate_snapshot()


    def _process_correlations(self, event: Dict[str, Any]) -> None:
        """Process event correlations and generate warnings"""
        # Check for error loops
        if event.get('status') in ['error', 'failed', 'blocked']:
            terminal = event.get('terminal', 'unknown')

            # Get recent errors for this terminal
            recent_errors = [
                e for e in self.error_patterns[terminal][-self.ERROR_LOOP_THRESHOLD:]
                if self._is_recent(e.get('timestamp'), minutes=10)
            ]

            # Generate warning if error loop detected
            if len(recent_errors) >= self.ERROR_LOOP_THRESHOLD:
                warning = {
                    'type': 'error_loop',
                    'severity': 'high',
                    'terminal': terminal,
                    'error_count': len(recent_errors),
                    'pattern': event.get('event_type'),
                    'message': f"Terminal {terminal} has {len(recent_errors)} errors in 10 minutes",
                    'timestamp': datetime.utcnow().isoformat() + 'Z'
                }
                self.correlation_warnings.append(warning)

        # Check for stuck terminals
        if event.get('status') == 'blocked':
            terminal = event.get('terminal', 'unknown')
            blocked_duration = self._get_blocked_duration(terminal)
            if blocked_duration > 300:  # 5 minutes
                warning = {
                    'type': 'terminal_stuck',
                    'severity': 'critical',
                    'terminal': terminal,
                    'blocked_seconds': blocked_duration,
                    'message': f"Terminal {terminal} blocked for {blocked_duration}s",
                    'timestamp': datetime.utcnow().isoformat() + 'Z'
                }
                self.correlation_warnings.append(warning)

    def _get_active_warnings(self) -> List[Dict[str, Any]]:
        """Get currently active warnings"""
        # Filter recent warnings (last hour)
        active = [
            w for w in self.correlation_warnings
            if self._is_recent(w.get('timestamp'), minutes=60)
        ]

        # Sort by severity
        severity_order = {'critical': 0, 'high': 1, 'medium': 2, 'low': 3}
        active.sort(key=lambda x: severity_order.get(x.get('severity', 'low'), 99))

        return active[:10]  # Top 10 warnings

    def _get_error_patterns(self) -> Dict[str, Any]:
        """Analyze and return error patterns"""
        patterns = {}
        for terminal, errors in self.error_patterns.items():
            if errors:
                recent = [e for e in errors if self._is_recent(e.get('timestamp'), minutes=60)]
                if recent:
                    # Count error types
                    error_types = Counter([e.get('error_type', 'unknown') for e in recent])
                    patterns[terminal] = {
                        'total_errors': len(recent),
                        'error_types': dict(error_types),
                        'most_common': error_types.most_common(1)[0] if error_types else None
                    }
        return patterns

    def _get_terminal_insights(self) -> Dict[str, Any]:
        """Get insights on what terminals have been working on"""
        insights = {}
        for terminal, activities in self.terminal_activity.items():
            if activities:
                recent = [a for a in activities if self._is_recent(a.get('timestamp'), minutes=60)]
                if recent:
                    # Analyze activity
                    task_types = Counter([a.get('event_type', 'unknown') for a in recent])
                    status_dist = Counter([a.get('status', 'unknown') for a in recent])

                    insights[terminal] = {
                        'total_activities': len(recent),
                        'task_types': dict(task_types),
                        'status_distribution': dict(status_dist),
                        'last_activity': recent[-1] if recent else None,
                        'success_rate': self._calculate_success_rate(status_dist)
                    }
        return insights

    def _get_tag_report_mapping(self) -> Dict[str, List[str]]:
        """Get mapping of tags to recent reports"""
        # Return top tags with their associated reports
        tag_mapping = {}
        for tag, reports in self.report_tags.items():
            if isinstance(reports, list) and reports:
                # Only include tags that point to report IDs
                tag_mapping[tag] = reports[:5]  # Top 5 reports per tag

        # Limit to top 20 tags
        return dict(list(tag_mapping.items())[:20])

    def _load_t0_brief(self) -> Dict[str, Any]:
        """Load t0_brief.json for actionable orchestration data"""
        brief_file = self.state_dir / "t0_brief.json"
        if brief_file.exists():
            try:
                with open(brief_file, 'r') as f:
                    return json.load(f)
            except (json.JSONDecodeError, IOError) as e:
                print(f"Warning: Could not load t0_brief.json: {e}")
        return {}

    def _get_recent_receipts(self) -> List[Dict[str, Any]]:
        """Get summary of recent receipts"""
        recent = []
        for event in list(self.event_buffer)[-20:]:  # Last 20 events
            if event.get('event_type') not in ['t0_intelligence_snapshot']:
                recent.append({
                    'timestamp': event.get('timestamp'),
                    'event_type': event.get('event_type'),
                    'terminal': event.get('terminal'),
                    'status': event.get('status')
                })
        return recent

    def _is_recent(self, timestamp: str, minutes: int = 60) -> bool:
        """Check if timestamp is within recent minutes"""
        if not timestamp:
            return False
        try:
            event_time = self._parse_timestamp(timestamp)
            cutoff = datetime.utcnow() - timedelta(minutes=minutes)
            return event_time > cutoff
        except:
            return False

    def _get_blocked_duration(self, terminal: str) -> int:
        """Get how long a terminal has been blocked in seconds"""
        activities = self.terminal_activity.get(terminal, [])
        if not activities:
            return 0

        # Find when blocking started
        blocked_start = None
        for activity in reversed(activities):
            if activity.get('status') == 'blocked':
                blocked_start = activity.get('timestamp')
            else:
                break

        if blocked_start:
            start_time = self._parse_timestamp(blocked_start)
            duration = (datetime.utcnow() - start_time).total_seconds()
            return int(duration)
        return 0

    def _calculate_success_rate(self, status_dist: Counter) -> float:
        """Calculate success rate from status distribution"""
        total = sum(status_dist.values())
        if total == 0:
            return 0.0
        success = status_dist.get('success', 0) + status_dist.get('completed', 0)
        return round((success / total) * 100, 1)

def main():
    """Process all intelligence sources for T0 core hub"""
    aggregator = T0IntelligenceAggregator()

    # Process production receipts
    lib_dir = str(Path(__file__).resolve().parent / "lib")
    if lib_dir not in sys.path:
        sys.path.insert(0, lib_dir)
    from vnx_paths import ensure_env  # noqa: E402
    paths = ensure_env()
    state_dir = Path(paths["VNX_STATE_DIR"])
    production_receipts = str(state_dir / "t0_receipts.ndjson")

    # Also process unified state for complete picture
    unified_state = str(state_dir / "unified_state.ndjson")

    print("Building T0 Intelligence Core Hub...")
    print("Processing production receipts...")
    aggregator.process_receipt(production_receipts)

    print("Processing unified state...")
    if Path(unified_state).exists():
        aggregator.process_receipt(unified_state)

    # Get and display snapshot
    snapshot = aggregator.get_snapshot()

    print("\n" + "="*60)
    print("T0 INTELLIGENCE CORE HUB SNAPSHOT")
    print("="*60)

    # Display key insights
    if snapshot.get('correlations', {}).get('warnings'):
        print("\n⚠️  ACTIVE WARNINGS:")
        for warning in snapshot['correlations']['warnings'][:3]:
            print(f"  - [{warning['severity'].upper()}] {warning['message']}")

    if snapshot.get('terminal_insights'):
        print("\n📊 TERMINAL ACTIVITY:")
        for terminal, insights in snapshot['terminal_insights'].items():
            print(f"  {terminal}: {insights['total_activities']} activities, {insights['success_rate']}% success")

    if snapshot.get('report_tags'):
        print("\n🏷️  TOP REPORT TAGS:")
        for tag, reports in list(snapshot['report_tags'].items())[:5]:
            print(f"  #{tag}: {len(reports)} reports")

    print(f"\n📁 Intelligence files:")
    print(f"  Core Hub: {aggregator.intelligence_file}")
    print(f"  Full History: {aggregator.intelligence_full}")
    print(f"\n✅ T0 Intelligence Core Hub ready for orchestration decisions!")


if __name__ == '__main__':
    main()
