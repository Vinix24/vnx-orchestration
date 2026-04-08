#!/usr/bin/env python3
"""
Heartbeat-Based ACK Monitor
============================
Monitor terminal activity via heartbeat changes to confirm task start through timestamp correlation.
Uses multiple signals including dashboard heartbeat, log changes, and terminal conversation logs.

Intelligent polling: Only active between dispatch send and final receipt.

Author: T-MANAGER
Date: 2025-09-25
Version: 2.0
"""

import os
import json
import time
import hashlib
import argparse
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Optional, List
import subprocess
import threading
import logging
import socket
import sys

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%H:%M:%S'
)
logger = logging.getLogger(__name__)

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR / "lib"))
try:
    from vnx_paths import ensure_env
except Exception as exc:
    raise SystemExit(f"Failed to load vnx_paths: {exc}")

try:
    from terminal_state_shadow import TerminalUpdate, default_lease_expires, update_terminal_state
    SHADOW_TERMINAL_STATE_AVAILABLE = True
except Exception as exc:
    TerminalUpdate = None  # type: ignore[assignment]
    default_lease_expires = None  # type: ignore[assignment]
    update_terminal_state = None  # type: ignore[assignment]
    SHADOW_TERMINAL_STATE_AVAILABLE = False
    logger.warning(f"[SHADOW] terminal_state_shadow unavailable; continuing without shadow writes: {exc}")

try:
    from append_receipt import AppendReceiptError, append_receipt_payload
except Exception as exc:
    raise SystemExit(f"Failed to load append_receipt helper: {exc}")
try:
    from python_singleton import enforce_python_singleton
except Exception as exc:
    raise SystemExit(f"Failed to load python_singleton helper: {exc}")


class HeartbeatACKMonitor:
    """Monitor terminal activity using heartbeat changes and multiple log sources"""

    def __init__(self):
        """Initialize monitor with paths and configurations"""
        paths = ensure_env()
        self.project_root = paths["PROJECT_ROOT"]
        self.vnx_dir = paths["VNX_HOME"]
        self.state_dir = paths["VNX_STATE_DIR"]
        self.logs_dir = paths["VNX_LOGS_DIR"]

        # Support shadow mode via environment variable
        # IMPORTANT: Use t0_receipts.ndjson as the main production file
        default_receipt_file = os.path.join(self.state_dir, 't0_receipts.ndjson')
        self.receipts_file = os.environ.get('RECEIPT_FILE', default_receipt_file)
        self.is_shadow_mode = os.environ.get('SHADOW_MODE', '').lower() == 'true'

        if self.is_shadow_mode:
            logger.info(f"[SHADOW MODE] Writing receipts to: {self.receipts_file}")
        else:
            logger.info(f"[PRODUCTION] Writing ACK receipts to: {self.receipts_file}")

        # Core monitoring sources
        self.dashboard_file = os.path.join(self.state_dir, 'dashboard_status.json')
        self.terminal_status_file = os.path.join(self.state_dir, 'terminal_status.ndjson')

        # Terminal conversation logs
        self.terminal_logs = {
            'T0': os.path.join(self.state_dir, 't0_conversation.log'),
            'T1': os.path.join(self.state_dir, 't1_conversation.log'),
            'T2': os.path.join(self.state_dir, 't2_conversation.log'),
            'T3': os.path.join(self.state_dir, 't3_conversation.log')
        }

        # Tracking structures
        self.active_dispatches = {}  # dispatch_id -> dispatch_info
        self.terminal_heartbeats = {}  # terminal -> last_update_time
        self.log_checksums = {}  # log_path -> last_checksum
        self.polling_threads = {}  # dispatch_id -> thread

        # Configuration
        self.heartbeat_poll_interval = 2  # seconds
        self.confirmation_threshold = 3  # seconds after dispatch
        self.timeout_seconds = 60  # max wait for confirmation
        self.dispatch_lease_seconds = int(os.environ.get("VNX_DISPATCH_LEASE_SECONDS", "600"))
        self._shadow_terminal_state_enabled = SHADOW_TERMINAL_STATE_AVAILABLE
        self._terminal_state_update = update_terminal_state
        self._terminal_update_type = TerminalUpdate
        self._default_lease_expires = default_lease_expires

        # Initialize baseline heartbeats
        self._initialize_heartbeats()

    def _get_t0_pane_id(self) -> str:
        """Resolve the current T0 tmux pane id from panes.json (created by VNX_HYBRID_FINAL.sh)."""
        panes_file = os.path.join(self.state_dir, 'panes.json')
        try:
            if os.path.exists(panes_file):
                with open(panes_file, 'r') as f:
                    panes = json.load(f)
                return (
                    panes.get('t0', {}).get('pane_id')
                    or panes.get('T0', {}).get('pane_id')
                    or "%0"
                )
        except Exception as e:
            logger.warning(f"Failed to read panes.json for T0 pane id: {e}")
        return "%0"

    def _initialize_heartbeats(self):
        """Load initial heartbeat values from dashboard"""
        try:
            if os.path.exists(self.dashboard_file):
                with open(self.dashboard_file, 'r') as f:
                    dashboard = json.load(f)

                    terminals = dashboard.get('terminals', {})
                    for terminal, info in terminals.items():
                        last_update = info.get('last_update', 'never')
                        if last_update != 'never':
                            try:
                                dt = datetime.fromisoformat(last_update.replace('Z', '+00:00'))
                                self.terminal_heartbeats[terminal] = dt
                                logger.info(f"Initialized {terminal} heartbeat: {last_update}")
                            except Exception:
                                pass
        except Exception as e:
            logger.error(f"Error initializing heartbeats: {e}")

    def track_dispatch(self, dispatch_id: str, terminal: str, task_id: str, sent_time: datetime, pr_id: str = ''):
        """Register a dispatch and start monitoring for activity"""

        # Ensure sent_time has timezone info (fix timezone-naive comparison bug)
        if sent_time.tzinfo is None:
            sent_time = sent_time.replace(tzinfo=timezone.utc)

        # Store dispatch info
        dispatch_info = {
            'dispatch_id': dispatch_id,
            'task_id': task_id,
            'pr_id': pr_id,  # SPRINT 2: Store PR-ID for auto-completion matching
            'terminal': terminal,
            'sent_time': sent_time,
            'timeout_time': sent_time + timedelta(seconds=self.timeout_seconds),
            'confirmed': False,
            'confirmation_time': None,
            'confirmation_method': None,
            'signals_detected': []
        }

        self.active_dispatches[dispatch_id] = dispatch_info

        logger.info(f"[TRACK] Dispatch {dispatch_id} → {terminal} at {sent_time.isoformat()}")

        # Shadow write: dispatch claim.
        if self._shadow_terminal_state_enabled:
            try:
                self._terminal_state_update(
                    self.state_dir,
                    self._terminal_update_type(
                        terminal_id=terminal,
                        status="claimed",
                        claimed_by=dispatch_id,
                        claimed_at=sent_time.isoformat(),
                        lease_expires_at=(sent_time + timedelta(seconds=self.dispatch_lease_seconds)).isoformat(),
                        last_activity=sent_time.isoformat(),
                    ),
                )
            except Exception as exc:
                logger.warning(f"[SHADOW] Failed to write terminal_state claim for {terminal}: {exc}")

        # Start dedicated polling thread for this dispatch
        thread = threading.Thread(
            target=self._monitor_dispatch,
            args=(dispatch_id,),
            daemon=True
        )
        thread.start()
        self.polling_threads[dispatch_id] = thread

    def _is_subprocess_terminal(self, terminal: str) -> bool:
        """Check if terminal uses subprocess adapter (not tmux).

        VNX_ADAPTER_T1=subprocess skips tmux-based monitoring because the
        subprocess adapter has its own event pipeline and any tmux pane
        activity reflects the subprocess launcher, not the actual worker.
        """
        env_key = f"VNX_ADAPTER_{terminal}"
        return os.environ.get(env_key, "tmux").lower() == "subprocess"

    def _monitor_dispatch(self, dispatch_id: str):
        """Dedicated monitoring thread for a specific dispatch"""

        dispatch_info = self.active_dispatches.get(dispatch_id)
        if not dispatch_info:
            return

        terminal = dispatch_info['terminal']

        if self._is_subprocess_terminal(terminal):
            logger.debug(f"[MONITOR] Skipping subprocess-adapter terminal {terminal} for {dispatch_id}")
            return
        sent_time = dispatch_info['sent_time']
        timeout_time = dispatch_info['timeout_time']

        logger.info(f"[MONITOR] Starting monitor for {dispatch_id} → {terminal}")

        # Capture initial state
        initial_heartbeat = self.terminal_heartbeats.get(terminal)
        initial_log_checksum = self._get_log_checksum(terminal)

        while datetime.now(timezone.utc) < timeout_time and not dispatch_info['confirmed']:
            signals = []

            # Check Signal 1: Dashboard heartbeat change
            heartbeat_signal = self._check_heartbeat_change(terminal, sent_time)
            if heartbeat_signal:
                signals.append(heartbeat_signal)

            # Check Signal 2: Terminal log file change
            log_signal = self._check_log_change(terminal, initial_log_checksum, sent_time)
            if log_signal:
                signals.append(log_signal)

            # Check Signal 3: Terminal status NDJSON update
            status_signal = self._check_terminal_status(terminal, sent_time)
            if status_signal:
                signals.append(status_signal)

            # Check Signal 4: Directory change or process activity
            activity_signal = self._check_terminal_activity(terminal, sent_time)
            if activity_signal:
                signals.append(activity_signal)

            # Store detected signals
            dispatch_info['signals_detected'] = signals

            # Confirm dispatch when we have reliable evidence of activity.
            # Use a single strong signal (log/terminal_status/process_activity),
            # or fall back to the original 2-signal rule to avoid false positives
            # from weak heartbeat-only detection.
            signal_types = {s.get('type') for s in signals if isinstance(s, dict)}
            reliable_signals = {"log_change", "log_mtime", "terminal_status", "process_activity"}

            if signal_types & reliable_signals or len(signals) >= 2:
                dispatch_info['confirmed'] = True
                dispatch_info['confirmation_time'] = datetime.now(timezone.utc)
                dispatch_info['confirmation_method'] = f"{len(signals)} signals: {', '.join([s['type'] for s in signals])}"

                logger.info(f"[CONFIRMED] {dispatch_id} via {dispatch_info['confirmation_method']}")
                self._generate_ack_receipt(dispatch_info, signals)
                break

            time.sleep(self.heartbeat_poll_interval)

        # Handle timeout
        if not dispatch_info['confirmed']:
            logger.warning(f"[TIMEOUT] {dispatch_id} - no confirmation after {self.timeout_seconds}s")
            self._generate_timeout_receipt(dispatch_info)

        # Cleanup
        del self.active_dispatches[dispatch_id]
        if dispatch_id in self.polling_threads:
            del self.polling_threads[dispatch_id]

    def _check_heartbeat_change(self, terminal: str, after_time: datetime) -> Optional[Dict]:
        """Check if terminal heartbeat updated after dispatch time"""

        try:
            if os.path.exists(self.dashboard_file):
                with open(self.dashboard_file, 'r') as f:
                    dashboard = json.load(f)

                terminal_info = dashboard.get('terminals', {}).get(terminal, {})
                last_update_str = terminal_info.get('last_update', 'never')

                if last_update_str != 'never':
                    last_update = datetime.fromisoformat(last_update_str.replace('Z', '+00:00'))
                    if last_update.tzinfo is None:
                        last_update = last_update.replace(tzinfo=timezone.utc)

                    # Ensure after_time is timezone-aware for comparison
                    if after_time.tzinfo is None:
                        # Convert naive to UTC-aware
                        after_time = after_time.replace(tzinfo=timezone.utc)

                    # Check if this is a new heartbeat
                    old_heartbeat = self.terminal_heartbeats.get(terminal)

                    # Ensure old_heartbeat is timezone-aware for comparison
                    if old_heartbeat and old_heartbeat.tzinfo is None:
                        old_heartbeat = old_heartbeat.replace(tzinfo=timezone.utc)

                    if last_update > after_time:
                        # Update stored heartbeat
                        self.terminal_heartbeats[terminal] = last_update

                        if old_heartbeat and last_update > old_heartbeat:
                            # Shadow write: heartbeat activity.
                            if self._shadow_terminal_state_enabled:
                                try:
                                    self._terminal_state_update(
                                        self.state_dir,
                                        self._terminal_update_type(
                                            terminal_id=terminal,
                                            status="active",
                                            lease_expires_at=self._default_lease_expires(self.dispatch_lease_seconds),
                                            last_activity=last_update.isoformat(),
                                        ),
                                    )
                                except Exception as exc:
                                    logger.warning(f"[SHADOW] Failed to write terminal_state heartbeat for {terminal}: {exc}")

                            delay = (last_update - after_time).total_seconds()
                            logger.debug(f"Heartbeat change for {terminal}: {delay:.1f}s after dispatch")

                            return {
                                'type': 'heartbeat',
                                'timestamp': last_update,
                                'delay_seconds': delay,
                                'status': terminal_info.get('status', 'unknown')
                            }
        except Exception as e:
            logger.error(f"Error checking heartbeat: {e}")

        return None

    def _get_terminal_metrics(self, terminal: str) -> Dict:
        """Get terminal performance metrics from ledger analytics

        Returns terminal metrics for ACK receipt enhancement (Phase 1A)
        """
        metrics_file = Path(self.state_dir) / "ledger" / "analytics" / "terminal_metrics.json"

        if not metrics_file.exists():
            return {}

        try:
            with open(metrics_file) as f:
                all_metrics = json.load(f)
                return all_metrics.get(terminal, {})
        except Exception as e:
            logger.debug(f"Could not load terminal metrics: {e}")
            return {}

    def _get_log_checksum(self, terminal: str) -> Optional[str]:
        """Get checksum of terminal log file"""

        log_path = self.terminal_logs.get(terminal)
        if not log_path or not os.path.exists(log_path):
            return None

        try:
            # Read last 10KB of log for efficiency
            with open(log_path, 'rb') as f:
                f.seek(0, 2)  # Go to end
                file_size = f.tell()
                read_size = min(10240, file_size)
                f.seek(max(0, file_size - read_size))
                content = f.read()

            return hashlib.md5(content).hexdigest()
        except Exception as e:
            logger.error(f"Error getting log checksum: {e}")
            return None

    def _check_log_change(self, terminal: str, initial_checksum: Optional[str], after_time: datetime) -> Optional[Dict]:
        """Check if terminal log changed after dispatch"""

        if not initial_checksum:
            log_path = self.terminal_logs.get(terminal)
            if log_path and os.path.exists(log_path):
                try:
                    log_mtime = datetime.fromtimestamp(os.path.getmtime(log_path), tz=timezone.utc)
                    if log_mtime > after_time:
                        delay = (log_mtime - after_time).total_seconds()
                        return {
                            'type': 'log_mtime',
                            'timestamp': log_mtime,
                            'delay_seconds': delay,
                            'checksum_changed': True
                        }
                except Exception:
                    pass
            return None

        current_checksum = self._get_log_checksum(terminal)
        if current_checksum and current_checksum != initial_checksum:
            # Log has changed - this indicates activity
            delay = (datetime.now(timezone.utc) - after_time).total_seconds()

            logger.debug(f"Log change detected for {terminal}: {delay:.1f}s after dispatch")

            return {
                'type': 'log_change',
                'timestamp': datetime.now(timezone.utc),
                'delay_seconds': delay,
                'checksum_changed': True
            }

        return None

    def _check_terminal_status(self, terminal: str, after_time: datetime) -> Optional[Dict]:
        """Check terminal_status.ndjson for updates"""

        if not os.path.exists(self.terminal_status_file):
            return None

        try:
            # Read last 10 lines looking for our terminal
            with open(self.terminal_status_file, 'r') as f:
                lines = f.readlines()[-10:]

            for line in reversed(lines):
                if not line.strip():
                    continue

                try:
                    entry = json.loads(line)
                    if entry.get('terminal') == terminal:
                        timestamp_str = entry.get('timestamp')
                        if timestamp_str:
                            timestamp = datetime.fromisoformat(timestamp_str.replace('Z', '+00:00'))
                            if timestamp.tzinfo is None:
                                timestamp = timestamp.replace(tzinfo=timezone.utc)
                            if after_time.tzinfo is None:
                                after_time = after_time.replace(tzinfo=timezone.utc)

                            if timestamp > after_time:
                                delay = (timestamp - after_time).total_seconds()

                                return {
                                    'type': 'terminal_status',
                                    'timestamp': timestamp,
                                    'delay_seconds': delay,
                                    'status': entry.get('status', 'unknown')
                                }
                except Exception:
                    continue

        except Exception as e:
            logger.error(f"Error checking terminal status: {e}")

        return None

    def _check_terminal_activity(self, terminal: str, after_time: datetime) -> Optional[Dict]:
        """Check for terminal process activity via tmux"""

        if self._is_subprocess_terminal(terminal):
            logger.debug(f"Skipping subprocess-adapter terminal {terminal}")
            return None

        try:
            # Get pane info from tmux
            cmd = f"tmux list-panes -a -F '#{{session_name}}:#{{window_name}} #{{pane_current_command}}' | grep -i {terminal}"
            result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=1)

            if result.returncode == 0 and result.stdout:
                current_command = result.stdout.strip().split()[-1]

                # If command is not idle (bash/zsh), terminal is active
                if current_command not in ['bash', 'zsh', 'sh']:
                    delay = (datetime.now(timezone.utc) - after_time).total_seconds()

                    return {
                        'type': 'process_activity',
                        'timestamp': datetime.now(timezone.utc),
                        'delay_seconds': delay,
                        'command': current_command
                    }

        except Exception as e:
            logger.error(f"Error checking terminal activity: {e}")

        return None

    def _generate_ack_receipt(self, dispatch_info: Dict, signals: List[Dict]):
        """Generate enhanced ACK receipt for confirmed task start

        Enhanced format includes terminal load, quality context, and metadata
        for intelligence system integration (Phase 1A)
        """

        terminal = dispatch_info['terminal']
        confidence = min(1.0, len(signals) * 0.35)

        # Get terminal metrics from analytics
        terminal_metrics = self._get_terminal_metrics(terminal)

        receipt = {
            # Core fields (existing)
            'event_type': 'task_started',
            'event': 'task_started',
            'dispatch_id': dispatch_info['dispatch_id'],
            'task_id': dispatch_info['task_id'],
            'pr_id': dispatch_info.get('pr_id', ''),  # SPRINT 2: Add PR-ID for auto-completion matching
            'terminal': terminal,
            'timestamp': dispatch_info['confirmation_time'].isoformat(),
            'sent_time': dispatch_info['sent_time'].isoformat(),
            'confirmed_time': dispatch_info['confirmation_time'].isoformat(),
            'confirmation_method': dispatch_info['confirmation_method'],
            'signals': [
                {
                    'type': s['type'],
                    'delay': s['delay_seconds'],
                    'timestamp': s['timestamp'].isoformat() if isinstance(s['timestamp'], datetime) else s['timestamp']
                }
                for s in signals
            ],
            'status': 'confirmed',
            'auto_generated': True,
            'source': 'heartbeat_ack_monitor',
            'confidence': confidence,

            # Enhanced fields (Phase 1A intelligence)
            'terminal_load': {
                'current_tasks': len([d for d in self.active_dispatches.values() if d['terminal'] == terminal]),
                'avg_completion_time': terminal_metrics.get('avg_duration_seconds', 0),
                'recent_success_rate': terminal_metrics.get('success_rate', 1.0)
            },
            'estimated_duration': dispatch_info.get('estimated_duration', 'unknown'),
            'dependencies_ready': True,  # Will be enhanced when dependency tracking added
            'quality_context': dispatch_info.get('quality_context', {})
        }

        if not self._append_receipt(receipt):
            logger.error(f"[ACK] Failed to persist start receipt for {dispatch_info['dispatch_id']}")
            return

        logger.info(f"[ACK] Generated start receipt for {dispatch_info['dispatch_id']} (confidence: {receipt['confidence']:.2f})")

        # Refresh terminal_state lease/last_activity on confirmed start.
        if self._shadow_terminal_state_enabled:
            try:
                self._terminal_state_update(
                    self.state_dir,
                    self._terminal_update_type(
                        terminal_id=terminal,
                        status="working",
                        lease_expires_at=self._default_lease_expires(self.dispatch_lease_seconds),
                        last_activity=dispatch_info['confirmation_time'].isoformat(),
                    ),
                )
            except Exception as exc:
                logger.warning(f"[SHADOW] Failed to refresh terminal_state after ACK for {terminal}: {exc}")

        # Send direct ACK notification to T0 for immediate visibility.
        # This monitor is now the reliable ACK source in modern runtime flows.
        # Can be disabled explicitly via VNX_ACK_DIRECT_NOTIFY=0.
        if os.environ.get("VNX_ACK_DIRECT_NOTIFY", "1") != "0":
            self._notify_t0_ack(receipt, signals)

    def _check_intelligence_daemon_health(self) -> Dict:
        """Check intelligence daemon health from dashboard"""
        try:
            if os.path.exists(self.dashboard_file):
                with open(self.dashboard_file, 'r') as f:
                    dashboard = json.load(f)

                daemon_info = dashboard.get('intelligence_daemon', {})
                if daemon_info:
                    status = daemon_info.get('status', 'unknown')
                    patterns = daemon_info.get('patterns_available', 0)
                    last_extraction = daemon_info.get('last_extraction')

                    return {
                        'healthy': status == 'healthy',
                        'status': status,
                        'patterns_available': patterns,
                        'last_extraction': last_extraction
                    }
        except Exception as e:
            logger.debug(f"Could not check intelligence daemon health: {e}")

        return {'healthy': False, 'status': 'unknown'}

    def _notify_t0_ack(self, receipt: Dict, signals: List[Dict]):
        """Send ACK notification to T0 using tmux buffer paste"""

        try:
            # Resolve T0 pane id from panes.json (pane_id is not stable across tmux restarts)
            t0_pane = self._get_t0_pane_id()

            # Create enhanced ACK notification for T0
            terminal = receipt['terminal']
            dispatch_id = receipt['dispatch_id']
            # Ultra-minimal ACK notification - only for user confirmation that NDJSON works
            # Removed: confidence, signal_types, health_status, patterns_info (not needed for ACK)
            notification = f"""# ✅ Task Accepted - {terminal}

**Dispatch ID**: {dispatch_id}
"""

            # Send to T0 pane using tmux buffer
            import tempfile
            with tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='.txt') as tmp:
                tmp.write(notification)
                tmp_path = tmp.name

            # Load buffer and paste to T0
            subprocess.run(['tmux', 'load-buffer', tmp_path], check=True, capture_output=True)
            subprocess.run(['tmux', 'paste-buffer', '-t', t0_pane], check=True, capture_output=True)

            # Press Enter twice to submit (handle bracketed paste)
            # Added delay to ensure CLI processes the text before Enter
            # Matched to receipt_notifier.sh timing for consistency
            time.sleep(0.8)  # Wait for text to be processed (was 0.5, now matches task_completed)
            subprocess.run(['tmux', 'send-keys', '-t', t0_pane, 'Enter'], check=True, capture_output=True)
            time.sleep(0.3)  # Wait between Enter keys
            subprocess.run(['tmux', 'send-keys', '-t', t0_pane, 'Enter'], check=True, capture_output=True)

            # Clean up temp file
            os.unlink(tmp_path)

            logger.info(f"[T0] ACK notification sent for {dispatch_id}")

        except Exception as e:
            logger.error(f"Failed to send T0 notification: {e}")

    def _generate_timeout_receipt(self, dispatch_info: Dict):
        """Generate timeout receipt for unconfirmed dispatch"""

        receipt = {
            'event_type': 'task_timeout',
            'event': 'task_timeout',
            'dispatch_id': dispatch_info['dispatch_id'],
            'task_id': dispatch_info['task_id'],
            'terminal': dispatch_info['terminal'],
            'timestamp': dispatch_info['timeout_time'].isoformat(),
            'sent_time': dispatch_info['sent_time'].isoformat(),
            'timeout_time': dispatch_info['timeout_time'].isoformat(),
            'signals_detected': len(dispatch_info.get('signals_detected', [])),
            'status': 'no_confirmation',
            'auto_generated': True,
            'source': 'heartbeat_ack_monitor',
            'action_required': 'check_terminal_health',
            'recommendation': 'Verify terminal responsive, check tmux pane, consider retry'
        }

        if not self._append_receipt(receipt):
            logger.error(f"[TIMEOUT] Failed to persist timeout receipt for {dispatch_info['dispatch_id']}")
            return

        # Timeout/no_confirmation should keep a short lease so terminals don't
        # flip to idle while waiting for user confirmation.
        if self._shadow_terminal_state_enabled:
            try:
                self._terminal_state_update(
                    self.state_dir,
                    self._terminal_update_type(
                        terminal_id=dispatch_info['terminal'],
                        status='blocked',
                        claimed_by=dispatch_info.get('dispatch_id'),
                        claimed_at=dispatch_info.get('sent_time').isoformat() if dispatch_info.get('sent_time') else None,
                        lease_expires_at=self._default_lease_expires(self.dispatch_lease_seconds),
                        last_activity=dispatch_info['timeout_time'].isoformat(),
                    ),
                )
            except Exception as exc:
                logger.warning(
                    f"[SHADOW] Failed to release terminal claim after timeout for "
                    f"{dispatch_info['dispatch_id']}: {exc}"
                )

        logger.warning(f"[TIMEOUT] Generated timeout receipt for {dispatch_info['dispatch_id']}")

    def _append_receipt(self, receipt: Dict) -> bool:
        """Canonical receipt append path shared with shell runtime scripts."""

        try:
            result = append_receipt_payload(receipt, receipts_file=self.receipts_file)
        except AppendReceiptError as exc:
            logger.error(f"[RECEIPT] append_receipt failed ({exc.code}): {exc.message}")
            return False
        except Exception as exc:
            logger.error(f"[RECEIPT] append_receipt unexpected failure: {exc}")
            return False

        if result.status == "duplicate":
            logger.info(
                f"[RECEIPT] Duplicate receipt skipped for dispatch={receipt.get('dispatch_id', 'unknown')}"
            )
        return True

    def stop_monitoring(self, dispatch_id: str):
        """Stop monitoring a specific dispatch (e.g., when final receipt received)"""

        if dispatch_id in self.active_dispatches:
            self.active_dispatches[dispatch_id]['confirmed'] = True  # Stop the monitor loop
            logger.info(f"[STOP] Stopped monitoring {dispatch_id}")

    def _socket_server(self, socket_path: str):
        """Unix socket server to receive dispatch notifications from dispatcher"""

        # Remove existing socket if present
        try:
            os.unlink(socket_path)
        except FileNotFoundError:
            pass

        # Create socket
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(socket_path)
        server.listen(5)
        logger.info(f"[SOCKET] Listening on {socket_path}")

        while True:
            try:
                conn, _ = server.accept()
                data = conn.recv(1024).decode().strip()

                if not data:
                    conn.send(b'ERROR: Empty message\n')
                    conn.close()
                    continue

                # Parse message
                try:
                    message = json.loads(data)
                    action = message.get('action')

                    if action == 'track_dispatch':
                        dispatch_id = message['dispatch_id']
                        terminal = message['terminal']
                        task_id = message['task_id']
                        pr_id = message.get('pr_id', '')  # SPRINT 2: Extract PR-ID
                        sent_time_str = message['sent_time']

                        # Parse sent_time
                        sent_time = datetime.fromisoformat(sent_time_str)

                        # Track dispatch
                        self.track_dispatch(dispatch_id, terminal, task_id, sent_time, pr_id)

                        conn.send(b'OK\n')
                        logger.info(f"[SOCKET] Registered dispatch {dispatch_id} → {terminal} (PR: {pr_id or 'none'})")

                    elif action == 'stop':
                        dispatch_id = message.get('dispatch_id')
                        if dispatch_id:
                            self.stop_monitoring(dispatch_id)
                        conn.send(b'OK\n')

                    else:
                        conn.send(f'ERROR: Unknown action {action}\n'.encode())

                except (json.JSONDecodeError, KeyError) as e:
                    conn.send(f'ERROR: Invalid message format: {e}\n'.encode())
                    logger.error(f"[SOCKET] Invalid message: {data}")

                conn.close()

            except Exception as e:
                logger.error(f"[SOCKET] Error handling connection: {e}")

    def start_socket_server(self, socket_path: str = '/tmp/heartbeat_ack_monitor.sock'):
        """Start socket server in background thread"""

        thread = threading.Thread(
            target=self._socket_server,
            args=(socket_path,),
            daemon=True
        )
        thread.start()
        logger.info("[SOCKET] Socket server started in background")


def _parse_dispatch_payload(input_data: str) -> Dict[str, object]:
    if not input_data:
        raise ValueError("No input data received")

    try:
        dispatch_info = json.loads(input_data)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON input: {exc}") from exc

    dispatch_id = dispatch_info.get('dispatch_id')
    terminal = dispatch_info.get('terminal')
    task_id = dispatch_info.get('task_id') or dispatch_id
    sent_time_str = dispatch_info.get('sent_time')
    pr_id = dispatch_info.get('pr_id') or ''

    if not dispatch_id or not terminal or not sent_time_str:
        raise ValueError("Missing required fields (dispatch_id, terminal, sent_time)")

    sent_time = datetime.fromisoformat(str(sent_time_str).replace('Z', '+00:00'))

    return {
        "dispatch_id": dispatch_id,
        "terminal": terminal,
        "task_id": task_id,
        "sent_time": sent_time,
        "pr_id": pr_id,
    }


def run_stdin_monitor(input_data: str, monitor: Optional[HeartbeatACKMonitor] = None) -> int:
    try:
        payload = _parse_dispatch_payload(input_data)
    except ValueError as exc:
        sys.stderr.write(f"Error: {exc}\n")
        return 1

    monitor = monitor or HeartbeatACKMonitor()
    monitor.track_dispatch(
        payload["dispatch_id"],
        payload["terminal"],
        payload["task_id"],
        payload["sent_time"],
        payload["pr_id"],
    )

    dispatch_id = payload["dispatch_id"]
    while dispatch_id in monitor.active_dispatches:
        time.sleep(1)

    return 0


def _run_production_mode() -> int:
    """Production mode: Start socket server and run indefinitely"""

    paths = ensure_env()
    lock_handle = enforce_python_singleton(
        "heartbeat_ack_monitor",
        paths["VNX_LOCKS_DIR"],
        paths["VNX_PIDS_DIR"],
        logger.info,
    )
    if lock_handle is None:
        return 0

    monitor = HeartbeatACKMonitor()

    logger.info("=" * 60)
    logger.info("Heartbeat-Based ACK Monitor - PRODUCTION MODE")
    logger.info("=" * 60)

    # Start socket server to receive dispatch notifications from dispatcher
    socket_path = '/tmp/heartbeat_ack_monitor.sock'
    monitor.start_socket_server(socket_path)

    logger.info(f"[READY] ACK monitor ready to receive dispatches via {socket_path}")
    logger.info("[READY] Press Ctrl+C to stop")

    try:
        # Keep main thread alive
        while True:
            time.sleep(60)

            # Periodic status log
            active_count = len(monitor.active_dispatches)
            if active_count > 0:
                logger.info(f"[STATUS] Active dispatches: {active_count}")

    except KeyboardInterrupt:
        logger.info("[SHUTDOWN] Stopping ACK monitor...")
        # Socket cleanup handled by OS on process exit
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Heartbeat ACK monitor")
    parser.add_argument(
        "--stdin",
        action="store_true",
        help="Read a single dispatch payload from stdin and monitor until completion.",
    )
    args = parser.parse_args()

    if args.stdin:
        return run_stdin_monitor(sys.stdin.read())

    return _run_production_mode()


if __name__ == '__main__':
    raise SystemExit(main())
