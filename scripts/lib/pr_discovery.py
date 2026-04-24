"""
PR Auto-Discovery
=================
Discovers active and historical PRs from gate names, receipt history,
and dispatch IDs. Used by IntelligenceDaemon to build the PR queue.
"""

import json
import re
import logging
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


class PRDiscovery:
    """Discovers and tracks PRs from gate names, receipts, and dispatch IDs."""

    _PR_GATE_RE = re.compile(r"gate_pr(\d+)_(.*)", re.IGNORECASE)
    _PR_REF_RE = re.compile(r"PR[- ]?(\d+)", re.IGNORECASE)

    def __init__(self, compat_state_dirs: List[Path]) -> None:
        self.compat_state_dirs = list(compat_state_dirs)

    def _find_state_file(self, filename: str) -> Optional[Path]:
        for state_dir in self.compat_state_dirs:
            candidate = state_dir / filename
            if candidate.exists():
                return candidate
        return None

    @staticmethod
    def _register_gate(discovered: dict, gate: str, gate_re) -> None:
        """Register a PR from a gate name if it matches the pattern."""
        m = gate_re.match(gate)
        if m:
            pr_num = int(m.group(1))
            desc = m.group(2).replace("_", " ").strip().title()
            if pr_num not in discovered:
                discovered[pr_num] = {
                    "id": f"PR{pr_num}", "num": pr_num, "description": desc,
                    "gate_trigger": gate, "receipt_done": False,
                }

    def _discover_from_track_history(self, discovered: dict) -> None:
        """Discover PRs from progress_state.yaml track history."""
        progress_file = self._find_state_file("progress_state.yaml")
        if not (progress_file and progress_file.exists()):
            return
        try:
            import yaml
            with open(progress_file, "r") as f:
                progress = yaml.safe_load(f) or {}
            for track_id, track_data in progress.get("tracks", {}).items():
                self._register_gate(discovered, track_data.get("current_gate", ""), self._PR_GATE_RE)
                for entry in track_data.get("history", []):
                    self._register_gate(discovered, entry.get("gate", ""), self._PR_GATE_RE)
        except Exception as e:
            logger.error(f"Error reading progress_state.yaml: {e}")

    def _discover_from_receipts(self, discovered: dict) -> None:
        """Discover PRs from t0_receipts.ndjson receipt history."""
        receipts_file = self._find_state_file("t0_receipts.ndjson")
        if not (receipts_file and receipts_file.exists()):
            return
        try:
            with open(receipts_file, "r") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        receipt = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    self._process_receipt_pr_refs(discovered, receipt)
        except Exception as e:
            logger.error(f"Error scanning receipts for PR discovery: {e}")

    def _process_receipt_pr_refs(self, discovered: dict, receipt: dict) -> None:
        """Extract and register PR references from a single receipt."""
        searchable = " ".join(
            str(receipt.get(k, "")) for k in ("type", "title", "gate", "task_id", "dispatch_id")
        )
        for m in self._PR_REF_RE.finditer(searchable):
            pr_num = int(m.group(1))
            is_done = (
                receipt.get("event_type") == "task_complete"
                and receipt.get("status") in ("success", "unknown")
            )
            if pr_num not in discovered:
                title = receipt.get("title", "")
                desc = self._PR_REF_RE.sub("", title).strip(" -\u2013\u2014:").strip() or f"PR {pr_num}"
                discovered[pr_num] = {
                    "id": f"PR{pr_num}", "num": pr_num, "description": desc,
                    "gate_trigger": f"gate_pr{pr_num}_unknown", "receipt_done": is_done,
                }
            elif is_done:
                discovered[pr_num]["receipt_done"] = True
            receipt_gate = receipt.get("gate", "")
            gm = self._PR_GATE_RE.match(receipt_gate)
            if gm and int(gm.group(1)) == pr_num:
                discovered[pr_num]["gate_trigger"] = receipt_gate
                if not discovered[pr_num]["description"] or discovered[pr_num]["description"] == f"PR {pr_num}":
                    discovered[pr_num]["description"] = gm.group(2).replace("_", " ").strip().title()

    def _auto_discover_prs(self, brief: dict) -> dict:
        """Auto-discover PRs from tracks, track history, receipts, and dispatches.

        Returns dict: { pr_num: { id, num, description, gate_trigger, receipt_done } }
        """
        discovered = {}

        # Source 1: Current track gates
        for track_id, track_data in brief.get("tracks", {}).items():
            self._register_gate(discovered, track_data.get("current_gate", ""), self._PR_GATE_RE)

        # Source 1b: Track history
        self._discover_from_track_history(discovered)

        # Source 2: Receipt history
        self._discover_from_receipts(discovered)

        # Source 3: Dispatch IDs in brief
        for receipt in brief.get("recent_receipts", []):
            dispatch_id = receipt.get("dispatch_id", "")
            for m in self._PR_REF_RE.finditer(dispatch_id):
                pr_num = int(m.group(1))
                if pr_num not in discovered:
                    discovered[pr_num] = {
                        "id": f"PR{pr_num}", "num": pr_num, "description": f"PR {pr_num}",
                        "gate_trigger": f"gate_pr{pr_num}_unknown", "receipt_done": False,
                    }

        return discovered

    def _determine_pr_statuses(self, discovered: dict, tracks: dict) -> dict:
        """Determine status for all discovered PRs.

        Priority:
        1. Live track gate match (working → in_progress, idle → done)
        2. Receipt history completion → done
        3. Dependency inference (if higher PR active, lower numbered deps are done)
        4. Default → pending
        """
        status_map = {}

        # First pass: track data + receipt signals
        for pr_num, pr_info in discovered.items():
            pr_id = pr_info["id"]
            gate = pr_info["gate_trigger"]
            status_map[pr_id] = "pending"

            # Check live track data (highest priority)
            for track_id, track_data in tracks.items():
                current_gate = track_data.get("current_gate", "")
                track_status = track_data.get("status", "")

                if current_gate == gate:
                    if track_status in ("working", "active"):
                        status_map[pr_id] = "in_progress"
                    elif track_status == "idle":
                        status_map[pr_id] = "done"
                    break

            # Fallback: receipt history
            if status_map[pr_id] == "pending" and pr_info.get("receipt_done"):
                status_map[pr_id] = "done"

        # Second pass: sequential inference
        sorted_nums = sorted(discovered.keys())
        for pr_num in sorted_nums:
            pr_id = discovered[pr_num]["id"]
            if status_map[pr_id] in ("in_progress", "done"):
                gate = discovered[pr_num]["gate_trigger"]
                pr_track = None
                for track_id, track_data in tracks.items():
                    if track_data.get("current_gate", "") == gate:
                        pr_track = track_id
                        break

                for lower_num in sorted_nums:
                    if lower_num >= pr_num:
                        break
                    lower_id = discovered[lower_num]["id"]
                    lower_gate = discovered[lower_num]["gate_trigger"]

                    if pr_track:
                        if status_map[lower_id] == "pending":
                            lower_gate_match = self._PR_GATE_RE.match(lower_gate)
                            current_gate_match = self._PR_GATE_RE.match(gate)
                            if lower_gate_match and current_gate_match:
                                status_map[lower_id] = "done"

        return status_map

    @staticmethod
    def _merge_pr_registry(discovered: dict, registry: dict) -> None:
        """Merge persisted PR registry into discovered dict (enriches, never removes)."""
        for num_str, pr_info in registry.items():
            pr_num = int(num_str)
            if pr_num not in discovered:
                discovered[pr_num] = pr_info
            else:
                if discovered[pr_num]["description"] == f"PR {pr_num}":
                    discovered[pr_num]["description"] = pr_info.get("description", discovered[pr_num]["description"])
                if pr_info.get("receipt_done"):
                    discovered[pr_num]["receipt_done"] = True

    @staticmethod
    def _build_pr_list(discovered: dict, status_map: dict) -> List[dict]:
        """Build sorted PR list with dependencies and blocked status."""
        all_prs = []
        sorted_nums = sorted(discovered.keys())
        for pr_num in sorted_nums:
            pr_info = discovered[pr_num]
            pr_id = pr_info["id"]
            status = status_map.get(pr_id, "pending")
            deps = []
            if pr_num > 1:
                prev_num = None
                for n in sorted_nums:
                    if n < pr_num:
                        prev_num = n
                    else:
                        break
                if prev_num is not None and prev_num in discovered:
                    deps = [discovered[prev_num]["id"]]
            blocked = status == "pending" and deps and any(status_map.get(d) != "done" for d in deps)
            all_prs.append({
                "id": pr_id, "description": pr_info["description"],
                "status": status, "deps": deps, "blocked": blocked,
            })
        return all_prs

    def build_pr_queue(self, brief: dict, existing_dashboard: dict) -> dict:
        """Build PR queue from auto-discovered data with persistence.

        Merges newly discovered PRs with previously persisted registry so
        PRs that were on a track gate in the past are not lost when the
        track moves forward.
        """
        tracks = brief.get("tracks", {})
        discovered = self._auto_discover_prs(brief)
        self._merge_pr_registry(discovered, existing_dashboard.get("_pr_registry", {}))

        if not discovered:
            return {
                "active_feature": "Active Development", "total_prs": 0,
                "completed_prs": 0, "progress_percent": 0, "prs": [], "_pr_registry": {},
            }

        status_map = self._determine_pr_statuses(discovered, tracks)
        all_prs = self._build_pr_list(discovered, status_map)
        completed_count = sum(1 for pr in all_prs if pr["status"] == "done")
        total_count = len(all_prs)

        return {
            "active_feature": "Active Development", "total_prs": total_count,
            "completed_prs": completed_count,
            "progress_percent": int((completed_count / total_count * 100)) if total_count > 0 else 0,
            "prs": all_prs,
            "_pr_registry": {str(k): v for k, v in discovered.items()},
        }
