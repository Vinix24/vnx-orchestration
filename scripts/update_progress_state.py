#!/usr/bin/env python3
"""
Progress State Updater - Atomic YAML updates for machine-owned progress ledger
==============================================================================

Purpose: Provide atomic, type-safe updates to progress_state.yaml

Usage:
  update_progress_state.py --track A --gate implementation --status working --dispatch-id 20260108-1234
  update_progress_state.py --track B --advance-gate --receipt-status success
  update_progress_state.py --track C --status blocked --receipt-event task_complete

Guarantees:
- Atomic writes (temp file + rename)
- Schema validation
- History tracking (last 10 transitions per track)
- Idempotent (safe to run multiple times)

Author: T-MANAGER
Version: 1.0
Date: 2026-01-08
"""

import os
import sys
import yaml
import argparse
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional

# Configuration
SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR / "lib"))
sys.path.insert(0, str(SCRIPT_DIR))
try:
    from vnx_paths import ensure_env
except Exception as exc:
    raise SystemExit(f"Failed to load vnx_paths: {exc}")
try:
    from state_integrity import write_checksum
except Exception:
    write_checksum = None

PATHS = ensure_env()
STATE_DIR = PATHS["VNX_STATE_DIR"]
PROGRESS_STATE_PATH = os.path.join(STATE_DIR, 'progress_state.yaml')

# Gate progression flow
GATE_PROGRESSION = {
    'investigation': 'planning',
    'planning': 'implementation',
    'implementation': 'review',
    'review': 'testing',
    'testing': 'integration',
    'integration': 'quality_gate',
    'quality_gate': 'planning',  # Next cycle
    'validation': 'planning',     # Alternate completion
    'escalation': None,           # Requires T0 decision
}


class ProgressStateManager:
    """Manages atomic updates to progress_state.yaml"""

    def __init__(self, state_path: str):
        self.state_path = state_path
        self.state = self.load_state()

    def load_state(self) -> Dict:
        """Load current state or initialize if missing"""
        if not os.path.exists(self.state_path):
            return self.initialize_state()

        try:
            with open(self.state_path, 'r') as f:
                state = yaml.safe_load(f) or {}

            # Validate schema
            if 'tracks' not in state:
                print(f"Warning: Invalid schema, reinitializing", file=sys.stderr)
                return self.initialize_state()

            return state
        except Exception as e:
            print(f"Error loading state: {e}", file=sys.stderr)
            return self.initialize_state()

    def initialize_state(self) -> Dict:
        """Initialize default state structure"""
        return {
            'version': '1.0',
            'updated_at': datetime.now(timezone.utc).isoformat(),
            'updated_by': 'initialization',
            'tracks': {
                'A': self._default_track_state(),
                'B': self._default_track_state(),
                'C': self._default_track_state(),
            }
        }

    def _default_track_state(self) -> Dict:
        """Default state for a track"""
        return {
            'current_gate': 'planning',
            'status': 'idle',
            'active_dispatch_id': None,
            'last_receipt': {
                'event_type': None,
                'status': None,
                'timestamp': None,
                'dispatch_id': None,
            },
            'history': []
        }

    def update_track(
        self,
        track: str,
        gate: Optional[str] = None,
        status: Optional[str] = None,
        dispatch_id: Optional[str] = None,
        advance_gate: bool = False,
        receipt_event: Optional[str] = None,
        receipt_status: Optional[str] = None,
        receipt_timestamp: Optional[str] = None,
        receipt_dispatch_id: Optional[str] = None,
        updated_by: str = 'unknown'
    ):
        """
        Update track state with atomic write

        Args:
            track: Track identifier (A/B/C)
            gate: New gate to set (planning/implementation/review/testing/integration/quality_gate)
            status: New status (idle/working/blocked)
            dispatch_id: Active dispatch ID
            advance_gate: If True, advance to next gate in progression
            receipt_event: Receipt event type (task_complete, task_started, etc)
            receipt_status: Receipt status (success, blocked, failed)
            receipt_timestamp: Receipt timestamp
            receipt_dispatch_id: Receipt dispatch ID
            updated_by: Source of update (dispatcher, receipt_processor, etc)
        """
        if track not in ['A', 'B', 'C']:
            raise ValueError(f"Invalid track: {track}. Must be A, B, or C")

        track_state = self.state['tracks'][track]
        old_gate = track_state['current_gate']
        old_status = track_state['status']

        # Update gate
        if advance_gate:
            new_gate = GATE_PROGRESSION.get(old_gate)
            if new_gate:
                track_state['current_gate'] = new_gate
                print(f"Advanced Track {track}: {old_gate} → {new_gate}")
            else:
                print(f"Warning: Cannot advance from gate '{old_gate}' (requires manual decision)", file=sys.stderr)
        elif gate:
            track_state['current_gate'] = gate
            print(f"Set Track {track} gate: {gate}")

        # Update status
        if status:
            track_state['status'] = status
            print(f"Set Track {track} status: {status}")

        # Update active dispatch
        if dispatch_id is not None:  # Allow clearing with empty string
            track_state['active_dispatch_id'] = dispatch_id if dispatch_id else None

        # Update last receipt
        if receipt_event or receipt_status:
            track_state['last_receipt'] = {
                'event_type': receipt_event or track_state['last_receipt'].get('event_type'),
                'status': receipt_status or track_state['last_receipt'].get('status'),
                'timestamp': receipt_timestamp or datetime.now(timezone.utc).isoformat(),
                'dispatch_id': receipt_dispatch_id or track_state['last_receipt'].get('dispatch_id'),
            }

        # Add to history
        if gate or advance_gate or status:
            history_entry = {
                'timestamp': datetime.now(timezone.utc).isoformat(),
                'gate': track_state['current_gate'],
                'status': track_state['status'],
                'dispatch_id': track_state['active_dispatch_id'],
                'updated_by': updated_by,
            }

            # Keep last 10 entries
            track_state['history'] = ([history_entry] + track_state['history'])[:10]

        # Update metadata
        self.state['updated_at'] = datetime.now(timezone.utc).isoformat()
        self.state['updated_by'] = updated_by

        # Save atomically
        self.save_state()

    def save_state(self):
        """Save state atomically (temp file + rename)"""
        temp_path = f"{self.state_path}.tmp"

        try:
            # Write to temp file
            with open(temp_path, 'w') as f:
                yaml.dump(self.state, f, default_flow_style=False, sort_keys=False)

            # Atomic rename
            os.rename(temp_path, self.state_path)
            print(f"✅ Progress state saved: {self.state_path}")
            if write_checksum:
                try:
                    write_checksum(self.state_path)
                except Exception as exc:
                    print(f"Warning: failed to write checksum: {exc}", file=sys.stderr)

        except Exception as e:
            print(f"❌ Error saving state: {e}", file=sys.stderr)
            if os.path.exists(temp_path):
                os.remove(temp_path)
            raise

    def get_track_state(self, track: str) -> Dict:
        """Get current state for a track"""
        return self.state['tracks'].get(track, {})

    def print_track_state(self, track: str):
        """Print track state for debugging"""
        state = self.get_track_state(track)
        print(f"\n📊 Track {track} State:")
        print(f"   Gate: {state.get('current_gate')}")
        print(f"   Status: {state.get('status')}")
        print(f"   Active Dispatch: {state.get('active_dispatch_id')}")
        print(f"   Last Receipt: {state.get('last_receipt', {}).get('status')} at {state.get('last_receipt', {}).get('timestamp')}")


def main():
    parser = argparse.ArgumentParser(description='Update progress_state.yaml atomically')

    # Required
    parser.add_argument('--track', required=True, choices=['A', 'B', 'C'], help='Track to update')

    # Gate updates
    parser.add_argument('--gate', help='Set gate (planning/implementation/review/testing/integration/quality_gate)')
    parser.add_argument('--advance-gate', action='store_true', help='Advance to next gate in progression')

    # Status updates
    parser.add_argument('--status', choices=['idle', 'working', 'blocked'], help='Set status')
    parser.add_argument('--dispatch-id', help='Set active dispatch ID')

    # Receipt updates
    parser.add_argument('--receipt-event', help='Receipt event type (task_complete, task_started, etc)')
    parser.add_argument('--receipt-status', help='Receipt status (success, blocked, failed)')
    parser.add_argument('--receipt-timestamp', help='Receipt timestamp (ISO format)')
    parser.add_argument('--receipt-dispatch-id', help='Receipt dispatch ID')

    # Metadata
    parser.add_argument('--updated-by', default='manual', help='Source of update')
    parser.add_argument('--print-state', action='store_true', help='Print track state after update')

    args = parser.parse_args()

    # Initialize manager
    manager = ProgressStateManager(PROGRESS_STATE_PATH)

    # Perform update
    manager.update_track(
        track=args.track,
        gate=args.gate,
        status=args.status,
        dispatch_id=args.dispatch_id,
        advance_gate=args.advance_gate,
        receipt_event=args.receipt_event,
        receipt_status=args.receipt_status,
        receipt_timestamp=args.receipt_timestamp,
        receipt_dispatch_id=args.receipt_dispatch_id,
        updated_by=args.updated_by
    )

    # Print state if requested
    if args.print_state:
        manager.print_track_state(args.track)


if __name__ == '__main__':
    main()
