#!/usr/bin/env python3
"""
VNX Unified State Manager V2 - CURSOR FIX
==========================================
CRITICAL BUG FIX: Stop re-adding same receipts every 5 seconds

PROBLEM (lines 299-303):
- Every 5 seconds, last 20 receipts are re-added to t0_intelligence.ndjson
- Causes 66MB file bloat and duplicate event processing
- Window-based processing instead of cursor-based

SOLUTION:
- Track last processed receipt/dispatch/dashboard offset
- Only process NEW events since last cycle
- Store cursors in VNX_STATE_DIR/manager_cursors.json

Changes:
1. Add cursor tracking (receipt_offset, dispatch_count, dashboard_timestamp)
2. Replace window-based [-20:] with cursor-based processing
3. Only add events that haven't been processed before
4. Atomic cursor updates after successful processing

Author: T-MANAGER (Bug Fix)
Date: 2026-01-07
Version: 2.2 - Cursor-based processing
"""

import os
import json
import glob
import time
import traceback
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List
import sys

# Add paths for T0 intelligence aggregator and VNX helpers
SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
sys.path.insert(0, str(SCRIPT_DIR / "lib"))
try:
    from vnx_paths import ensure_env
except Exception as exc:
    raise SystemExit(f"Failed to load vnx_paths: {exc}")
try:
    from python_singleton import enforce_python_singleton
except Exception as exc:
    raise SystemExit(f"Failed to load python_singleton helper: {exc}")

PATHS = ensure_env()

# Configuration
VNX_DIR = PATHS["VNX_HOME"]
STATE_DIR = PATHS["VNX_STATE_DIR"]
UNIFIED_STATE_PATH = os.path.join(STATE_DIR, 'unified_state.ndjson')
CURSOR_PATH = os.path.join(STATE_DIR, 'manager_cursors.json')

# Event sources
DISPATCH_JSON_DIR = os.path.join(STATE_DIR, 'dispatches')
RECEIPTS_FILE = os.path.join(STATE_DIR, 't0_receipts.ndjson')
REPORTS_INDEX = os.path.join(PATHS["VNX_REPORTS_DIR"], 'index.ndjson')
T0_BRIEF_SCRIPT = os.path.join(VNX_DIR, 'scripts', 'generate_t0_brief.sh')


class CursorManager:
    """Manages cursor state for idempotent event processing"""

    def __init__(self, cursor_path: str):
        self.cursor_path = cursor_path
        self.cursors = self.load_cursors()

    def load_cursors(self) -> Dict:
        """Load cursor state from disk"""
        if os.path.exists(self.cursor_path):
            try:
                with open(self.cursor_path, 'r') as f:
                    return json.load(f)
            except Exception as e:
                print(f"Warning: Could not load cursors: {e}")

        # Default cursors
        return {
            'receipt_offset': 0,  # Line number in t0_receipts.ndjson
            'dispatch_hashes': [],  # Already processed dispatch IDs
            'last_dashboard_timestamp': None,
            'last_update': datetime.now().isoformat()
        }

    def save_cursors(self):
        """Save cursor state to disk atomically"""
        self.cursors['last_update'] = datetime.now().isoformat()

        # Atomic write (temp file + rename)
        temp_path = f"{self.cursor_path}.tmp"
        try:
            with open(temp_path, 'w') as f:
                json.dump(self.cursors, f, indent=2)
            os.rename(temp_path, self.cursor_path)
        except Exception as e:
            print(f"Error saving cursors: {e}")
            if os.path.exists(temp_path):
                os.remove(temp_path)

    def get_receipt_offset(self) -> int:
        return self.cursors['receipt_offset']

    def set_receipt_offset(self, offset: int):
        self.cursors['receipt_offset'] = offset

    def is_dispatch_processed(self, dispatch_id: str) -> bool:
        return dispatch_id in self.cursors['dispatch_hashes']

    def mark_dispatch_processed(self, dispatch_id: str):
        if dispatch_id not in self.cursors['dispatch_hashes']:
            self.cursors['dispatch_hashes'].append(dispatch_id)
            # Keep only last 1000 to prevent unbounded growth
            if len(self.cursors['dispatch_hashes']) > 1000:
                self.cursors['dispatch_hashes'] = self.cursors['dispatch_hashes'][-1000:]

    def should_process_dashboard(self, timestamp: str) -> bool:
        """Check if dashboard timestamp is newer than last processed"""
        last = self.cursors['last_dashboard_timestamp']
        if last is None:
            return True
        return timestamp > last

    def mark_dashboard_processed(self, timestamp: str):
        self.cursors['last_dashboard_timestamp'] = timestamp


class UnifiedStateManagerV2:
    """Simplified unified state manager with cursor-based processing"""

    def __init__(self):
        """Initialize the state manager"""
        self.ensure_directories()
        self.cursor_manager = CursorManager(CURSOR_PATH)

    def ensure_directories(self):
        """Ensure required directories exist"""
        os.makedirs(STATE_DIR, exist_ok=True)
        os.makedirs(DISPATCH_JSON_DIR, exist_ok=True)

    def get_new_receipts(self, t0_aggregator) -> int:
        """
        Process ONLY NEW receipts since last cursor position
        Returns number of new receipts processed
        """
        if not os.path.exists(RECEIPTS_FILE):
            return 0

        offset = self.cursor_manager.get_receipt_offset()
        events_processed = 0

        try:
            with open(RECEIPTS_FILE, 'r') as f:
                # Skip to cursor position
                for _ in range(offset):
                    f.readline()

                # Process new lines
                for line_num, line in enumerate(f, start=offset):
                    line = line.strip()
                    if not line:
                        continue

                    try:
                        receipt = json.loads(line)
                        t0_aggregator.add_event(receipt)
                        events_processed += 1

                        # Update cursor after successful processing
                        self.cursor_manager.set_receipt_offset(line_num + 1)
                    except json.JSONDecodeError:
                        print(f"Warning: Invalid JSON at line {line_num + 1}")
                        continue

        except Exception as e:
            print(f"Error processing receipts: {e}")

        return events_processed

    def get_new_dispatches(self, t0_aggregator) -> int:
        """
        Process ONLY NEW dispatches that haven't been seen before
        Returns number of new dispatches processed
        """
        dispatch_files = glob.glob(os.path.join(DISPATCH_JSON_DIR, '*.json'))
        events_processed = 0

        for dispatch_file in dispatch_files:
            try:
                with open(dispatch_file, 'r') as f:
                    dispatch = json.load(f)

                dispatch_id = dispatch.get('dispatch_id')
                if not dispatch_id:
                    continue

                # Skip if already processed
                if self.cursor_manager.is_dispatch_processed(dispatch_id):
                    continue

                # Convert to event format
                dispatch_event = {
                    'event_type': 'dispatch',
                    'dispatch_id': dispatch_id,
                    'track': dispatch.get('metadata', {}).get('track'),
                    'terminal': f"T{['', '1', '2', '3'][ord(dispatch.get('metadata', {}).get('track', 'X')[0]) - ord('A') + 1] if dispatch.get('metadata', {}).get('track', '').upper() in ['A', 'B', 'C'] else '0'}",
                    'status': dispatch.get('state', {}).get('status', 'unknown'),
                    'summary': dispatch.get('instruction', {}).get('topic', 'No topic'),
                    'timestamp': dispatch.get('timestamp')
                }

                t0_aggregator.add_event(dispatch_event)
                self.cursor_manager.mark_dispatch_processed(dispatch_id)
                events_processed += 1

            except Exception as e:
                print(f"Error processing dispatch {dispatch_file}: {e}")

        return events_processed

    def process_dashboard_if_new(self, t0_aggregator) -> int:
        """
        Process dashboard ONLY if timestamp is newer than last processed
        Returns 1 if processed, 0 if skipped
        """
        dashboard_path = os.path.join(STATE_DIR, 'dashboard_status.json')

        if not os.path.exists(dashboard_path):
            return 0

        try:
            with open(dashboard_path, 'r') as f:
                dashboard = json.load(f)

            timestamp = dashboard.get('timestamp')
            if not timestamp:
                return 0

            # Skip if already processed this timestamp
            if not self.cursor_manager.should_process_dashboard(timestamp):
                return 0

            terminal_event = {
                'event_type': 'terminal_status',
                'timestamp': timestamp,
                'terminals': dashboard.get('terminals', {}),
                'processes': dashboard.get('processes', {}),
                'queues': dashboard.get('queues', {}),
                'locks': dashboard.get('locks', {})
            }

            t0_aggregator.add_event(terminal_event)
            self.cursor_manager.mark_dashboard_processed(timestamp)
            return 1

        except Exception as e:
            print(f"Error processing dashboard: {e}")
            return 0

    def get_recent_receipts(self, hours: int = 1) -> List[Dict]:
        """Get recent receipts for unified state (NOT for intelligence aggregation)"""
        receipts = []
        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)

        if not os.path.exists(RECEIPTS_FILE):
            return receipts

        try:
            with open(RECEIPTS_FILE, 'r') as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        receipt = json.loads(line)
                        timestamp_str = receipt.get('timestamp', '')
                        if not timestamp_str:
                            continue
                        # Parse and normalize to UTC
                        receipt_time = datetime.fromisoformat(timestamp_str.replace('Z', '+00:00'))
                        if receipt_time.tzinfo is None:
                            receipt_time = receipt_time.replace(tzinfo=timezone.utc)
                        if receipt_time >= cutoff:
                            receipts.append(receipt)
                    except (json.JSONDecodeError, ValueError):
                        continue
        except FileNotFoundError:
            pass

        return receipts

    def get_recent_dispatches(self, hours: int = 1) -> List[Dict]:
        """Get recent dispatches for unified state (NOT for intelligence aggregation)"""
        dispatches = []
        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)

        dispatch_files = glob.glob(os.path.join(DISPATCH_JSON_DIR, '*.json'))

        for dispatch_file in dispatch_files:
            try:
                with open(dispatch_file, 'r') as f:
                    dispatch = json.load(f)
                timestamp_str = dispatch.get('timestamp', '')
                if not timestamp_str:
                    continue
                # Parse and normalize to UTC
                dispatch_time = datetime.fromisoformat(timestamp_str.replace('Z', '+00:00'))
                if dispatch_time.tzinfo is None:
                    dispatch_time = dispatch_time.replace(tzinfo=timezone.utc)
                if dispatch_time >= cutoff:
                    dispatches.append(dispatch)
            except (FileNotFoundError, json.JSONDecodeError, ValueError):
                continue

        return sorted(dispatches, key=lambda x: x.get('timestamp', ''), reverse=True)

    def write_unified_state(self) -> Dict:
        """Write unified state snapshot (unchanged from original)"""
        # Get data
        receipts = self.get_recent_receipts(hours=24)
        dispatches = self.get_recent_dispatches(hours=24)

        # Analyze receipts for urgent issues
        urgent_issues = []
        completed_count = 0
        failed_count = 0

        for receipt in receipts:
            event_type = receipt.get('event_type') or receipt.get('event')

            if event_type == 'task_complete':
                completed_count += 1
            elif event_type in ['task_failed', 'task_timeout']:
                failed_count += 1
                urgent_issues.append({
                    'type': event_type,
                    'dispatch_id': receipt.get('dispatch_id'),
                    'task_id': receipt.get('task_id'),
                    'terminal': receipt.get('terminal'),
                    'severity': 'high' if event_type == 'task_failed' else 'medium',
                    'timestamp': receipt.get('timestamp')
                })

        # Count active dispatches
        active_dispatches = sum(1 for d in dispatches
                              if d.get('state', {}).get('status') in ['assigned', 'in_progress'])

        # Create aggregation
        agg = {
            'timestamp': datetime.now().isoformat(),
            'urgent_count': len(urgent_issues),
            'urgent_issues': sorted(urgent_issues, key=lambda x: x['timestamp'], reverse=True)[:10],
            'active_dispatches': active_dispatches,
            'recent_receipts': len(receipts),
            'completed_today': completed_count,
            'failed_today': failed_count,
            'total_reports': len(receipts),  # Simplified metric
            'terminal_status': {
                'T1': 'active' if any(r.get('terminal') == 'T1' for r in receipts[-5:]) else 'idle',
                'T2': 'active' if any(r.get('terminal') == 'T2' for r in receipts[-5:]) else 'idle',
                'T3': 'active' if any(r.get('terminal') == 'T3' for r in receipts[-5:]) else 'idle'
            }
        }

        # Write to file (append-only)
        with open(UNIFIED_STATE_PATH, 'a') as f:
            f.write(json.dumps(agg) + '\n')

        return agg

    def run(self):
        """Main loop with cursor-based T0 intelligence processing"""
        print("=" * 80)
        print("VNX Unified State Manager V2.2 - CURSOR FIX")
        print("=" * 80)
        print(f"State file: {UNIFIED_STATE_PATH}")
        print(f"Cursor file: {CURSOR_PATH}")
        print(f"Receipts: {RECEIPTS_FILE}")
        print(f"Dispatches: {DISPATCH_JSON_DIR}")
        print("")
        print("BUG FIX: Cursor-based processing prevents re-adding same events")
        print("  - Receipts: Track line offset in t0_receipts.ndjson")
        print("  - Dispatches: Track processed dispatch IDs")
        print("  - Dashboard: Track last processed timestamp")
        print("")

        # Try to import T0 Intelligence Aggregator
        try:
            from t0_intelligence_aggregator import T0IntelligenceAggregator
            t0_aggregator = T0IntelligenceAggregator()
            print(f"T0 Intelligence Aggregator integrated - writing to: {t0_aggregator.intelligence_file}")
            print(f"Initial cursor state:")
            print(f"  - Receipt offset: {self.cursor_manager.get_receipt_offset()}")
            print(f"  - Processed dispatches: {len(self.cursor_manager.cursors['dispatch_hashes'])}")
            print(f"  - Last dashboard: {self.cursor_manager.cursors['last_dashboard_timestamp']}")
        except ImportError:
            t0_aggregator = None
            print("T0 Intelligence Aggregator not available - continuing without it")

        print("")

        while True:
            try:
                # Create and write aggregation (unchanged)
                agg = self.write_unified_state()

                # Generate T0 brief (best-effort, non-fatal)
                try:
                    if os.path.exists(T0_BRIEF_SCRIPT):
                        subprocess.run([T0_BRIEF_SCRIPT], timeout=2, check=False)
                except Exception:
                    pass

                # Process ONLY NEW events through T0 Intelligence Aggregator
                if t0_aggregator:
                    try:
                        total_new_events = 0

                        # 1. Process NEW receipts only (cursor-based)
                        new_receipts = self.get_new_receipts(t0_aggregator)
                        total_new_events += new_receipts

                        # 2. Process NEW dispatches only (ID-based deduplication)
                        new_dispatches = self.get_new_dispatches(t0_aggregator)
                        total_new_events += new_dispatches

                        # 3. Process dashboard if timestamp changed
                        dashboard_processed = self.process_dashboard_if_new(t0_aggregator)
                        total_new_events += dashboard_processed

                        # Save cursors after successful processing
                        if total_new_events > 0:
                            self.cursor_manager.save_cursors()

                        print(f"T0 Intelligence updated: {total_new_events} NEW events processed")
                        print(f"  - New receipts: {new_receipts}")
                        print(f"  - New dispatches: {new_dispatches}")
                        print(f"  - Dashboard: {'updated' if dashboard_processed else 'unchanged'}")
                    except Exception as e:
                        print(f"T0 aggregator error (non-fatal): {e}")

                # Display summary
                print(f"[{datetime.now().isoformat()}] Aggregation written:")
                print(f"  - Urgent issues: {agg['urgent_count']}")
                print(f"  - Active dispatches: {agg['active_dispatches']}")
                print(f"  - Recent receipts: {agg['recent_receipts']}")
                print(f"  - Total reports: {agg['total_reports']}")

                if agg['urgent_count'] > 0:
                    print("  ⚠️ URGENT ISSUES:")
                    for issue in agg['urgent_issues'][:3]:
                        print(f"    - [{issue['severity']}] {issue['type']}: {issue.get('dispatch_id', issue.get('task_id'))}")

                # Wait 5 seconds
                time.sleep(5)

            except KeyboardInterrupt:
                print("\n[INFO] Shutting down unified state manager...")
                break
            except Exception as e:
                print(f"[ERROR] Unexpected error in main loop: {e}")
                print(f"[ERROR] Traceback:")
                traceback.print_exc()
                time.sleep(5)  # Back off on errors


if __name__ == '__main__':
    singleton_lock = enforce_python_singleton(
        "state_manager",
        PATHS["VNX_LOCKS_DIR"],
        PATHS["VNX_PIDS_DIR"],
        print,
    )
    if singleton_lock is None:
        raise SystemExit(0)

    manager = UnifiedStateManagerV2()
    manager.run()
