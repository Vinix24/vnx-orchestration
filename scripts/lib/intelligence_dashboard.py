"""
Intelligence Dashboard
======================
Builds and writes dashboard_status.json and intelligence_health.json.
"""

import json
import logging
import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, Optional


logger = logging.getLogger(__name__)


class DashboardBuilder:
    """Builds and writes dashboard_status.json and intelligence_health.json."""

    def __init__(
        self,
        state_dir: Path,
        legacy_state_dir: Path,
        rollback_mode: bool,
        dashboard_write_enabled: bool,
        pr_discovery,
        find_state_file: Callable,
        health_status: dict,
    ) -> None:
        self.state_dir = Path(state_dir)
        self.legacy_state_dir = Path(legacy_state_dir)
        self.rollback_mode = rollback_mode
        self.dashboard_write_enabled = dashboard_write_enabled
        self.pr_discovery = pr_discovery
        self._find_state_file = find_state_file
        self.health_status = health_status
        self.dashboard_file = self.state_dir / "dashboard_status.json"
        self.legacy_dashboard_file = self.legacy_state_dir / "dashboard_status.json"
        self.intelligence_health_file = self.state_dir / "intelligence_health.json"
        self.legacy_intelligence_health_file = self.legacy_state_dir / "intelligence_health.json"

    # ── Section builders ─────────────────────────────────────────────────────

    def _build_terminals_from_brief(self, brief: dict) -> dict:
        """Build terminal status dict from t0_brief data."""
        terminals = {"T0": {
            "status": "active", "gate": "ORCHESTRATION",
            "type": "ORCHESTRATOR", "ready": True,
        }}
        for tid, tdata in brief.get("terminals", {}).items():
            terminals[tid] = {
                "status": "active" if tdata.get("status") in ["working", "idle"] else "offline",
                "gate": tdata.get("track", ""),
                "current_task": tdata.get("current_task", ""),
                "ready": tdata.get("ready", False),
                "type": "WORKER",
            }
        return terminals

    def _build_open_items(self, brief: dict, pr_queue: dict) -> dict:
        """Build open items section from terminals with PR correlation."""
        gate_re = self.pr_discovery._PR_GATE_RE
        gate_to_pr_id = {}
        for pr in pr_queue.get("prs", []):
            for track_id, track_data in brief.get("tracks", {}).items():
                gate = track_data.get("current_gate", "")
                m = gate_re.match(gate)
                if m and f"PR{m.group(1)}" == pr["id"]:
                    gate_to_pr_id[gate] = pr["id"]

        open_items = []
        for tid, tdata in brief.get("terminals", {}).items():
            if tdata.get("current_task"):
                severity = "warning" if tdata.get("status") == "working" else "info"
                pr_id = None
                track_id = tdata.get("track", "")
                if track_id:
                    track_info = brief.get("tracks", {}).get(track_id, {})
                    pr_id = gate_to_pr_id.get(track_info.get("current_gate", ""))
                open_items.append({
                    "id": tdata["current_task"],
                    "title": f"{tdata.get('current_task', 'Task')} ({tid})",
                    "severity": severity, "pr_id": pr_id,
                })
        return {
            "open_count": len(open_items),
            "summary": {
                "open_count": len(open_items), "blocker_count": 0,
                "warn_count": sum(1 for i in open_items if i["severity"] == "warning"),
                "info_count": sum(1 for i in open_items if i["severity"] == "info"),
            },
            "top_blockers": [], "open_items": open_items,
        }

    def _build_intelligence_section(self) -> dict:
        """Build the intelligence_daemon section for the dashboard."""
        return {
            'status': self.health_status['status'],
            'last_extraction': self.health_status['last_extraction'],
            'patterns_available': self.health_status['patterns_available'],
            'extraction_errors': self.health_status['extraction_errors'],
            'uptime_seconds': self.health_status['uptime_seconds'],
            'last_update': datetime.now().isoformat(),
        }

    # ── Write methods ─────────────────────────────────────────────────────────

    def _write_json_atomic(self, destination: Path, payload: Dict) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode='w', dir=destination.parent, delete=False, suffix='.tmp') as tmp:
            json.dump(payload, tmp, indent=2)
            tmp_path = tmp.name
        os.replace(tmp_path, destination)

    def write_health_status(self):
        """Legacy dashboard projection (disabled by default for single-writer ownership)."""
        if not self.dashboard_write_enabled:
            return
        try:
            dashboard = {}
            dashboard_source = self._find_state_file("dashboard_status.json")
            if dashboard_source and dashboard_source.exists():
                with open(dashboard_source, 'r') as f:
                    dashboard = json.load(f)

            t0_brief_file = self._find_state_file("t0_brief.json")
            if t0_brief_file and t0_brief_file.exists():
                with open(t0_brief_file, 'r') as f:
                    brief = json.load(f)
                dashboard["terminals"] = self._build_terminals_from_brief(brief)
                pr_result = self.pr_discovery.build_pr_queue(brief, dashboard)
                dashboard["_pr_registry"] = pr_result.pop("_pr_registry", {})
                dashboard["pr_queue"] = pr_result
                dashboard["open_items"] = self._build_open_items(brief, dashboard["pr_queue"])
                dashboard["tracks"] = brief.get("tracks", {})
                dashboard["recent_receipts"] = brief.get("recent_receipts", [])
                dashboard["queues"] = brief.get("queues", {})

            dashboard['intelligence_daemon'] = self._build_intelligence_section()
            self._write_json_atomic(self.dashboard_file, dashboard)
            if self.rollback_mode and self.legacy_dashboard_file != self.dashboard_file:
                self._write_json_atomic(self.legacy_dashboard_file, dashboard)
            self.health_status['last_health_update'] = datetime.now()

        except Exception as e:
            logger.error(f"Failed to write health status: {e}")

    def write_intelligence_health(self):
        """Write to dedicated intelligence health file (PR #8 Fix - avoid dashboard races)."""
        health_data = {
            'timestamp': datetime.now().isoformat(),
            'daemon_running': True,
            'daemon_pid': os.getpid(),
            'patterns_available': self.health_status.get('patterns_available', 0),
            'last_extraction': self.health_status.get('last_extraction', 'never'),
            'extraction_errors': self.health_status.get('extraction_errors', 0),
            'uptime_seconds': self.health_status.get('uptime_seconds', 0),
            'status': self.health_status.get('status', 'unknown')
        }

        try:
            self._write_json_atomic(self.intelligence_health_file, health_data)
            if self.rollback_mode and self.legacy_intelligence_health_file != self.intelligence_health_file:
                self._write_json_atomic(self.legacy_intelligence_health_file, health_data)

        except Exception as e:
            logger.error(f"Failed to write intelligence health file: {e}")
