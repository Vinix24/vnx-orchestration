#!/usr/bin/env python3
"""
VNX Recommendation Engine - Generates dispatch recommendations for T0
Based on Manager Block v2 metadata (On-Success, On-Failure, Dependencies, Conflicts)
Version: 1.1.0
Author: T-MANAGER
"""

import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Set
import argparse
import glob
import re
import yaml

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR / "lib"))
try:
    from vnx_paths import ensure_env
except Exception as exc:
    raise SystemExit(f"Failed to load vnx_paths: {exc}")

_PATHS = ensure_env()
VNX_ROOT = Path(_PATHS["VNX_HOME"]).expanduser().resolve()
STATE_DIR = Path(_PATHS["VNX_STATE_DIR"]).expanduser().resolve()
DISPATCHES_DIR = Path(_PATHS["VNX_DISPATCH_DIR"]).expanduser().resolve()
LEGACY_STATE_DIR = (VNX_ROOT / "state").resolve()
LEGACY_DISPATCHES_DIR = (VNX_ROOT / "dispatches").resolve()
RECEIPTS_FILE = STATE_DIR / "t0_receipts.ndjson"
RECOMMENDATIONS_FILE = STATE_DIR / "t0_recommendations.json"
ACTIVE_CONFLICTS_FILE = STATE_DIR / "active_conflicts.json"
PR_QUEUE_STATE_FILE = STATE_DIR / "pr_queue_state.yaml"
OPEN_ITEMS_DIGEST_FILE = STATE_DIR / "open_items_digest.json"
STAGING_SEEN_FILE = STATE_DIR / "staging_seen.json"
LEGACY_RECEIPTS_FILE = LEGACY_STATE_DIR / "t0_receipts.ndjson"
LEGACY_RECOMMENDATIONS_FILE = LEGACY_STATE_DIR / "t0_recommendations.json"
LEGACY_PR_QUEUE_STATE_FILE = LEGACY_STATE_DIR / "pr_queue_state.yaml"
LEGACY_OPEN_ITEMS_DIGEST_FILE = LEGACY_STATE_DIR / "open_items_digest.json"
LEGACY_STAGING_SEEN_FILE = LEGACY_STATE_DIR / "staging_seen.json"
ROLLBACK_ENV_FLAG = "VNX_STATE_SIMPLIFICATION_ROLLBACK"


def _env_flag(name: str) -> Optional[bool]:
    value = os.environ.get(name)
    if value is None:
        return None
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _rollback_mode_enabled() -> bool:
    rollback = _env_flag(ROLLBACK_ENV_FLAG)
    if rollback is None:
        rollback = _env_flag("VNX_STATE_DUAL_WRITE_LEGACY")
    return bool(rollback)


def _first_existing(paths: List[Path]) -> Optional[Path]:
    if not paths:
        return None

    ordered_paths = [paths[0]]
    if _rollback_mode_enabled():
        ordered_paths.extend(paths[1:])

    seen = set()
    for path in ordered_paths:
        key = str(path.resolve()) if path.exists() else str(path)
        if key in seen:
            continue
        seen.add(key)
        if path.exists():
            return path
    return None


def _dispatch_roots() -> List[Path]:
    roots: List[Path] = [DISPATCHES_DIR]
    if _rollback_mode_enabled():
        roots.append(LEGACY_DISPATCHES_DIR)

    deduped: List[Path] = []
    for root in roots:
        if root not in deduped:
            deduped.append(root)
    return deduped

class RecommendationEngine:
    """Generates dispatch recommendations based on task outcomes and v2 metadata"""

    def __init__(self, lookback_minutes: int = 30):
        self.lookback_minutes = lookback_minutes
        self.recommendations = []
        self.recommendation_keys = set()
        self.active_dispatches = {}
        self.completed_dispatches = {}
        self.active_conflicts = {}
        self.pending_dependencies = {}
        self.pr_queue = None  # NEW: PR queue state integration
        self.open_items_digest = None  # NEW: Open items tracking
        self.staging_seen = set()  # NEW: Track seen staging files
        self.rollback_mode = _rollback_mode_enabled()
        self._rollback_warning_emitted = False  # Avoid warning spam in --watch mode.

    def _emit_rollback_warning(self):
        if self.rollback_mode and not self._rollback_warning_emitted:
            print(
                "[CUTOVER] WARNING: rollback mode enabled "
                f"({ROLLBACK_ENV_FLAG}=1). Legacy fallback reads are active."
            )
            self._rollback_warning_emitted = True

    def load_recent_receipts(self) -> List[Dict]:
        """Load receipts from the last N minutes"""
        receipts_source = _first_existing([RECEIPTS_FILE, LEGACY_RECEIPTS_FILE])
        if not receipts_source:
            return []

        from datetime import timezone
        cutoff_time = datetime.now(timezone.utc) - timedelta(minutes=self.lookback_minutes)
        recent_receipts = []

        with open(receipts_source, 'r') as f:
            for line in f:
                try:
                    receipt = json.loads(line.strip())
                    # Parse timestamp
                    if 'timestamp' in receipt:
                        ts = receipt['timestamp']
                        # Handle different timestamp formats
                        if 'T' in ts:
                            # ISO format
                            ts_clean = ts.replace('Z', '+00:00')
                            if '.' in ts_clean:
                                # Split and handle fractional seconds
                                base, frac = ts_clean.rsplit('.', 1)
                                # Extract just fractional seconds and timezone
                                if '+' in frac or '-' in frac:
                                    # Has timezone
                                    for tz_marker in ['+', '-']:
                                        if tz_marker in frac:
                                            frac_part, tz_part = frac.split(tz_marker, 1)
                                            ts_clean = base + '.' + frac_part[:6] + tz_marker + tz_part
                                            break
                                else:
                                    # No timezone in fractional part
                                    ts_clean = base + '.' + frac[:6]
                            try:
                                receipt_time = datetime.fromisoformat(ts_clean)
                            except:
                                # Fallback: parse without microseconds
                                receipt_time = datetime.fromisoformat(ts.split('.')[0] + '+00:00')
                        else:
                            # Try parsing as string
                            receipt_time = datetime.strptime(ts[:19], '%Y-%m-%d %H:%M:%S')
                            receipt_time = receipt_time.replace(tzinfo=timezone.utc)

                        # Ensure timezone aware
                        if receipt_time.tzinfo is None:
                            receipt_time = receipt_time.replace(tzinfo=timezone.utc)

                        if receipt_time > cutoff_time:
                            recent_receipts.append(receipt)
                except (json.JSONDecodeError, ValueError, KeyError) as e:
                    continue

        return recent_receipts

    def extract_v2_metadata(self, dispatch_file: Path) -> Dict:
        """Extract Manager Block v2 fields from dispatch markdown file"""
        if not dispatch_file.exists():
            return {}

        with open(dispatch_file, 'r') as f:
            content = f.read()

        metadata = {}
        patterns = {
            'program': r'^Program:\s*(.+)$',
            'dispatch_id_explicit': r'^Dispatch-ID:\s*(.+)$',
            'parent_dispatch': r'^Parent-Dispatch:\s*(.+)$',
            'gate': r'^Gate:\s*(.+)$',
            'on_success': r'^On-Success:\s*(.+)$',
            'on_failure': r'^On-Failure:\s*(.+)$',
            'reason': r'^Reason:\s*(.+)$',
            'depends_on': r'^Depends-On:\s*(.+)$',
            'conflict_key': r'^Conflict-Key:\s*(.+)$',
            'allow_overlap': r'^Allow-Overlap:\s*(.+)$',
            'requires_model': r'^Requires-Model:\s*(.+)$',
            'requires_capability': r'^Requires-Capability:\s*(.+)$',
        }

        for key, pattern in patterns.items():
            match = re.search(pattern, content, re.MULTILINE)
            if match:
                value = match.group(1).strip()
                if value and value.lower() != 'none':
                    metadata[key] = value

        # Extract role and instruction
        role_match = re.search(r'^Role:\s*(.+)$', content, re.MULTILINE)
        if role_match:
            metadata['role'] = role_match.group(1).strip()

        # Extract instruction section
        instruction_match = re.search(r'^Instruction:\s*\n(.*?)(?=\n\[|\n#|\Z)',
                                     content, re.MULTILINE | re.DOTALL)
        if instruction_match:
            metadata['instruction_preview'] = instruction_match.group(1).strip()[:200]

        return metadata

    def analyze_task_completion(self, receipt: Dict):
        """Analyze a task completion and generate recommendations"""
        if receipt.get('event_type') != 'task_complete':
            return

        dispatch_id = receipt.get('dispatch_id', '')
        status = receipt.get('status', '')
        terminal = receipt.get('terminal', '')
        gate = receipt.get('gate', '')
        receipt_timestamp = receipt.get('timestamp')

        # Find the dispatch file
        dispatch_file = None
        for dispatch_root in _dispatch_roots():
            dispatch_patterns = [
                f"{dispatch_root}/completed/{dispatch_id}.md",
                f"{dispatch_root}/completed/{dispatch_id}-*.md",
                f"{dispatch_root}/completed/*{dispatch_id}*.md",
            ]
            for pattern in dispatch_patterns:
                files = glob.glob(str(pattern))
                if files:
                    dispatch_file = Path(files[0])
                    break
            if dispatch_file:
                break

        if not dispatch_file:
            return

        # Extract v2 metadata
        metadata = self.extract_v2_metadata(dispatch_file)

        status_normalized = str(status).lower()
        is_success = status_normalized == 'success'
        is_failure = status_normalized in ['fail', 'failed', 'error', 'blocked']

        # Generate recommendations based on explicit v2 metadata
        if is_success and 'on_success' in metadata:
            self.add_gate_progression_recommendation(
                dispatch_id, metadata['on_success'],
                f"Task {dispatch_id} succeeded", metadata,
                source_timestamp=receipt_timestamp, receipt=receipt
            )
        elif is_failure and 'on_failure' in metadata:
            self.add_gate_progression_recommendation(
                dispatch_id, metadata['on_failure'],
                f"Task {dispatch_id} failed", metadata, is_failure=True,
                source_timestamp=receipt_timestamp, receipt=receipt
            )
        else:
            # Fallback recommendations when v2 fields are missing
            if is_success:
                next_gate = self.get_fallback_next_gate(gate)
                if next_gate:
                    self.add_gate_progression_recommendation(
                        dispatch_id, next_gate,
                        f"Task {dispatch_id} succeeded (no On-Success found)", metadata,
                        source_timestamp=receipt_timestamp, receipt=receipt
                    )
            elif is_failure:
                self.add_gate_progression_recommendation(
                    dispatch_id, "investigation",
                    f"Task {dispatch_id} failed (no On-Failure found)", metadata,
                    is_failure=True, source_timestamp=receipt_timestamp, receipt=receipt
                )

        # Check if this completion resolves any dependencies
        self.check_dependency_resolution(dispatch_id)

        # Clear any conflicts if this task had a conflict key
        if 'conflict_key' in metadata:
            self.clear_conflict(metadata['conflict_key'], dispatch_id)

    def add_gate_progression_recommendation(self, dispatch_id: str, next_gate: str,
                                           reason: str, metadata: Dict,
                                           is_failure: bool = False,
                                           source_timestamp: Optional[str] = None,
                                           receipt: Optional[Dict] = None):
        """Add a recommendation to progress to the next gate"""
        priority = "P0" if is_failure else "P1"

        # Build suggested instruction based on context
        if is_failure:
            action_verb = "Investigate"
            context = "failure in"
        else:
            action_verb = self.get_gate_action(next_gate)
            context = "completed"

        program = metadata.get('program', 'system')
        role = self.get_role_for_gate(next_gate, is_failure=is_failure)
        suggestion_note = self.extract_receipt_recommendation(receipt) if receipt else None

        recommendation = {
            "trigger": "task_failure" if is_failure else "task_success",
            "dispatch_id": dispatch_id,
            "action": "create_dispatch",
            "gate": next_gate,
            "reason": reason,
            "priority": priority,
            "suggested_role": role,
            "suggested_program": program,
            "suggested_instruction": f"{action_verb} {context} {dispatch_id}",
            "parent_dispatch": dispatch_id,
            "timestamp": source_timestamp or datetime.now().isoformat()
        }

        if suggestion_note:
            recommendation["suggested_instruction"] += f". Start with: {suggestion_note}"

        # Add model requirement if specified
        if 'requires_model' in metadata:
            recommendation['requires_model'] = metadata['requires_model']

        if receipt:
            if receipt.get('report_path'):
                recommendation['report_path'] = receipt['report_path']
            if receipt.get('terminal'):
                recommendation['terminal'] = receipt['terminal']

        self.add_recommendation(recommendation)

    def check_dependency_resolution(self, completed_dispatch: str):
        """Check if any pending dispatches can now proceed"""
        # Look for dispatches waiting on this one
        roots = _dispatch_roots()
        for root in roots:
            pending_dir = root / "pending"
            if not pending_dir.exists():
                continue

            for dispatch_file in pending_dir.glob("*.md"):
                metadata = self.extract_v2_metadata(dispatch_file)
                depends_on = metadata.get('depends_on', '')

                if depends_on:
                    dependencies = [d.strip() for d in depends_on.split(',')]
                    if completed_dispatch in dependencies or \
                       any(completed_dispatch in dep for dep in dependencies):
                        self.add_recommendation({
                            "trigger": "dependency_resolved",
                            "dispatch_id": dispatch_file.stem,
                            "action": "unblock_dispatch",
                            "reason": f"Dependency {completed_dispatch} completed",
                            "resolved_dependency": completed_dispatch,
                            "timestamp": datetime.now().isoformat()
                        })

    def check_active_conflicts(self):
        """Check for active file conflicts and queue recommendations"""
        for root in _dispatch_roots():
            active_dir = root / "active"
            if active_dir.exists():
                for dispatch_file in active_dir.glob("*.md"):
                    metadata = self.extract_v2_metadata(dispatch_file)
                    conflict_key = metadata.get('conflict_key')

                    if conflict_key and conflict_key != 'none':
                        if conflict_key not in self.active_conflicts:
                            self.active_conflicts[conflict_key] = []
                        self.active_conflicts[conflict_key].append(dispatch_file.stem)

            pending_dir = root / "pending"
            if pending_dir.exists():
                for dispatch_file in pending_dir.glob("*.md"):
                    metadata = self.extract_v2_metadata(dispatch_file)
                    conflict_key = metadata.get('conflict_key')

                    if conflict_key and conflict_key in self.active_conflicts:
                        self.add_recommendation({
                            "trigger": "conflict_detected",
                            "dispatch_id": dispatch_file.stem,
                            "action": "queue_dispatch",
                            "reason": f"Conflict on {conflict_key}",
                            "conflicting_dispatches": self.active_conflicts[conflict_key],
                            "conflict_pattern": conflict_key,
                            "timestamp": datetime.now().isoformat()
                        })

    def clear_conflict(self, conflict_key: str, dispatch_id: str):
        """Clear a conflict when a task completes"""
        if conflict_key in self.active_conflicts:
            self.active_conflicts[conflict_key] = [
                d for d in self.active_conflicts[conflict_key]
                if d != dispatch_id
            ]
            if not self.active_conflicts[conflict_key]:
                del self.active_conflicts[conflict_key]

            # Check if any queued dispatches can now proceed
            self.add_recommendation({
                "trigger": "conflict_cleared",
                "action": "check_queue",
                "conflict_pattern": conflict_key,
                "cleared_by": dispatch_id,
                "timestamp": datetime.now().isoformat()
            })

    def get_gate_action(self, gate: str) -> str:
        """Get the appropriate action verb for a gate"""
        gate_actions = {
            'investigation': 'Investigate',
            'planning': 'Plan',
            'implementation': 'Implement',
            'review': 'Review',
            'testing': 'Test',
            'integration': 'Integrate',
            'quality_gate': 'Validate quality for'
        }
        return gate_actions.get(gate, 'Process')

    def get_role_for_gate(self, gate: str, is_failure: bool = False) -> str:
        """Suggest appropriate role for a gate"""
        if is_failure:
            return 'debugger'

        gate_roles = {
            'investigation': 'data-analyst',
            'planning': 'architect',
            'implementation': 'backend-developer',
            'review': 'reviewer',
            'testing': 'quality-engineer',
            'integration': 'api-developer',
            'quality_gate': 'quality-engineer'
        }
        return gate_roles.get(gate, 'backend-developer')

    def get_fallback_next_gate(self, current_gate: str) -> Optional[str]:
        """Fallback gate progression when On-Success is missing"""
        if not current_gate:
            return None

        gate_flow = {
            'analysis': 'planning',
            'architecture': 'planning',
            'planning': 'implementation',
            'implementation': 'review',
            'review': 'testing',
            'testing': 'integration',
            'integration': 'quality_gate',
            'validation': 'quality_gate'
        }

        return gate_flow.get(str(current_gate).lower())

    def extract_receipt_recommendation(self, receipt: Dict) -> Optional[str]:
        """Extract a short recommendation hint from a receipt"""
        if not receipt:
            return None

        recommendations = receipt.get('recommendations')
        if isinstance(recommendations, str) and recommendations.strip():
            return recommendations.strip()[:200]

        if isinstance(recommendations, list) and recommendations:
            return str(recommendations[0])[:200]

        if isinstance(recommendations, dict):
            for key in ['immediate', 'next_phase', 'warnings', 'short_term', 'long_term']:
                values = recommendations.get(key)
                if isinstance(values, list) and values:
                    return str(values[0])[:200]
                if isinstance(values, str) and values.strip():
                    return values.strip()[:200]

        return None

    def load_pr_queue_state(self):
        """Load PR queue state if available (NEW)"""
        source = _first_existing([PR_QUEUE_STATE_FILE, LEGACY_PR_QUEUE_STATE_FILE])
        if not source:
            return

        try:
            with open(source, 'r') as f:
                self.pr_queue = yaml.safe_load(f)
        except (yaml.YAMLError, OSError) as e:
            print(f"⚠️  Failed to load PR queue state: {e}")
            self.pr_queue = None

    def load_open_items_digest(self):
        """Load open items digest if available"""
        source = _first_existing([OPEN_ITEMS_DIGEST_FILE, LEGACY_OPEN_ITEMS_DIGEST_FILE])
        if not source:
            return

        try:
            with open(source, 'r') as f:
                self.open_items_digest = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            print(f"⚠️  Failed to load open items digest: {e}")
            self.open_items_digest = None

    def load_staging_seen(self):
        """Load previously seen staging files"""
        source = _first_existing([STAGING_SEEN_FILE, LEGACY_STAGING_SEEN_FILE])
        if not source:
            return

        try:
            with open(source, 'r') as f:
                data = json.load(f)
                self.staging_seen = set(data.get('seen', []))
        except (json.JSONDecodeError, OSError):
            self.staging_seen = set()

    def save_staging_seen(self):
        """Save seen staging files"""
        STAGING_SEEN_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(STAGING_SEEN_FILE, 'w') as f:
            json.dump({'seen': list(self.staging_seen), 'updated': datetime.now().isoformat()}, f)

    def check_open_items(self):
        """Check for blocking open items and add recommendations"""
        if not self.open_items_digest:
            return

        summary = self.open_items_digest.get('summary', {})
        top_blockers = self.open_items_digest.get('top_blockers', [])

        # Add blocker recommendations
        for blocker in top_blockers[:2]:  # Show top 2 blockers max
            self.add_recommendation({
                "type": "BLOCKER_OPEN_ITEM",
                "trigger": "open_item_blocker",
                "action": "resolve_blocker",
                "item_id": blocker['id'],
                "title": blocker['title'],
                "pr_id": blocker.get('pr_id'),
                "reason": f"Blocker must be resolved: {blocker['title']}",
                "priority": "P0",
                "message": f"🔴 Blocker: {blocker['id']} - {blocker['title']}",
                "commands": [
                    f"python {VNX_ROOT / 'scripts/open_items_manager.py'} close {blocker['id']} --reason '<reason>'",
                    f"python {VNX_ROOT / 'scripts/open_items_manager.py'} defer {blocker['id']} --reason '<reason>'",
                ],
                "timestamp": datetime.now().isoformat()
            })

        # Add summary if there are many open items
        if summary.get('open_count', 0) > 5:
            self.add_recommendation({
                "type": "OPEN_ITEMS_SUMMARY",
                "trigger": "open_items_high_count",
                "action": "review_open_items",
                "count": summary['open_count'],
                "blockers": summary.get('blocker_count', 0),
                "warnings": summary.get('warn_count', 0),
                "priority": "P2",
                "message": f"📊 Open Items: {summary['open_count']} total ({summary['blocker_count']} blockers)",
                "command": f"python {VNX_ROOT / 'scripts/open_items_manager.py'} list --status open",
                "timestamp": datetime.now().isoformat()
            })

    def check_staging_dispatches(self):
        """Check for NEW dispatches in staging that need T0 notification (NOT promotion to popup)"""
        for root in _dispatch_roots():
            staging_dir = root / "staging"
            if not staging_dir.exists():
                continue

            staging_files = list(staging_dir.glob("*.md"))

            for dispatch_file in staging_files:
                dispatch_id = dispatch_file.stem
                file_key = f"{dispatch_id}:{dispatch_file.stat().st_mtime}"

                # Skip if we've already seen this file
                if file_key in self.staging_seen:
                    continue

                # Mark as seen
                self.staging_seen.add(file_key)

                # Extract PR-ID from dispatch
                metadata = self.extract_v2_metadata(dispatch_file)
                pr_id = metadata.get('pr_id', 'unknown')

                # Add notification recommendation (NOT a queue promotion)
                self.add_recommendation({
                    "type": "STAGING_READY",
                    "trigger": "staging_proposal",
                    "action": "review_staging",
                    "dispatch_id": dispatch_id,
                    "pr_id": pr_id,
                    "reason": f"New staging proposal ready for review",
                    "priority": "P1",
                    "message": f"📥 New staging proposal: {dispatch_id} (PR: {pr_id})",
                    "commands": [
                        f"python {VNX_ROOT / 'scripts/pr_queue_manager.py'} show {dispatch_id}",
                        f"python {VNX_ROOT / 'scripts/pr_queue_manager.py'} patch {dispatch_id} --file <patch>",
                        f"python {VNX_ROOT / 'scripts/pr_queue_manager.py'} promote {dispatch_id}",
                        f"python {VNX_ROOT / 'scripts/pr_queue_manager.py'} reject {dispatch_id} --reason '<reason>'",
                    ],
                    "staging_file": str(dispatch_file),
                    "timestamp": datetime.now().isoformat()
                })

        # Save seen staging files
        if self.staging_seen:
            self.save_staging_seen()

    def add_pr_dependency_recommendation(self):
        """Add PR queue recommendations (NEW)"""
        if not self.pr_queue:
            return

        # Check if current PR dependencies are met (supports parallel tracks)
        in_progress = self.pr_queue.get('in_progress') or []
        # Backward compat: legacy string format
        if isinstance(in_progress, str):
            in_progress = [in_progress] if in_progress else []
        if in_progress:
            # Import here to avoid circular dependency
            from pr_queue_manager import PRQueueManager
            manager = PRQueueManager()
            for active_pr in in_progress:
                deps_met, msg = manager.check_pr_dependencies(active_pr)

                if not deps_met:
                    self.add_recommendation({
                        "trigger": "pr_blocked",
                        "action": "wait_for_dependencies",
                        "pr_id": active_pr,
                        "reason": msg,
                        "priority": "P0",
                        "timestamp": datetime.now().isoformat()
                    })

        # Recommend next PR if available
        next_available = self.pr_queue.get('next_available', [])
        if next_available and not in_progress:
            next_pr = next_available[0]
            self.add_recommendation({
                "trigger": "pr_ready",
                "action": "start_next_pr",
                "pr_id": next_pr,
                "reason": f"Ready to start: {next_pr}",
                "priority": "P1",
                "suggested_instruction": f"Start work on {next_pr}",
                "command": f"python {VNX_ROOT / 'scripts/pr_queue_manager.py'} dispatch {next_pr}",
                "timestamp": datetime.now().isoformat()
            })

    def add_recommendation(self, recommendation: Dict):
        """Add a recommendation if it is not a duplicate"""
        key = self.make_recommendation_key(recommendation)
        if key in self.recommendation_keys:
            return
        self.recommendation_keys.add(key)
        self.recommendations.append(recommendation)

    def make_recommendation_key(self, recommendation: Dict) -> tuple:
        """Create a stable key to de-duplicate recommendations"""
        return (
            recommendation.get('trigger'),
            recommendation.get('action'),
            recommendation.get('dispatch_id'),
            recommendation.get('gate'),
            recommendation.get('parent_dispatch'),
            recommendation.get('resolved_dependency'),
            recommendation.get('conflict_pattern'),
            recommendation.get('cleared_by')
        )

    def save_recommendations(self):
        """Save recommendations to JSON file"""
        existing_keys = set()
        existing_conflicts = {}
        existing_source = _first_existing([RECOMMENDATIONS_FILE, LEGACY_RECOMMENDATIONS_FILE])
        if existing_source:
            try:
                with open(existing_source, 'r') as f:
                    existing = json.load(f)
                existing_conflicts = existing.get('active_conflicts', {}) or {}
                for rec in existing.get('recommendations', []):
                    existing_keys.add(self.make_recommendation_key(rec))
            except (json.JSONDecodeError, OSError):
                existing_conflicts = {}
                existing_keys = set()

        new_keys = {self.make_recommendation_key(rec) for rec in self.recommendations}

        if new_keys == existing_keys and self.active_conflicts == existing_conflicts:
            print("ℹ️  No recommendation changes detected - skipping write")
            return

        output = {
            "timestamp": datetime.now().isoformat(),
            "engine_version": "1.1.0",
            "lookback_minutes": self.lookback_minutes,
            "total_recommendations": len(self.recommendations),
            "recommendations": self.recommendations,
            "active_conflicts": self.active_conflicts,
            "metadata": {
                "gates_available": [
                    "investigation", "planning", "implementation",
                    "review", "testing", "integration", "quality_gate"
                ],
                "priority_levels": ["P0", "P1", "P2"],
                "action_types": [
                    "create_dispatch", "unblock_dispatch",
                    "queue_dispatch", "check_queue"
                ]
            }
        }

        RECOMMENDATIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(RECOMMENDATIONS_FILE, 'w') as f:
            json.dump(output, f, indent=2)

        # Also save active conflicts separately for reference
        with open(ACTIVE_CONFLICTS_FILE, 'w') as f:
            json.dump(self.active_conflicts, f, indent=2)

    def run(self):
        """Main execution flow"""
        self.rollback_mode = _rollback_mode_enabled()
        self._emit_rollback_warning()
        print(f"🚀 VNX Recommendation Engine v1.2.0")
        print(f"📊 Analyzing receipts from last {self.lookback_minutes} minutes...")

        # Load PR queue state (NEW)
        self.load_pr_queue_state()
        if self.pr_queue:
            print(f"   Found PR queue: {self.pr_queue.get('active_feature', {}).get('name', 'Unknown')}")

        # Load open items digest (NEW)
        self.load_open_items_digest()
        if self.open_items_digest:
            summary = self.open_items_digest.get('summary', {})
            print(f"   Open items: {summary.get('open_count', 0)} ({summary.get('blocker_count', 0)} blockers)")

        # Load staging seen cache (NEW)
        self.load_staging_seen()

        # Load and analyze recent receipts
        receipts = self.load_recent_receipts()
        print(f"   Found {len(receipts)} recent receipts")

        # Analyze each receipt for recommendations
        for receipt in receipts:
            self.analyze_task_completion(receipt)

        # Check for conflicts and dependencies
        self.check_active_conflicts()

        # Check for open items (NEW)
        self.check_open_items()

        # Check for staging dispatches (NEW - updated)
        self.check_staging_dispatches()

        # Add PR queue recommendations (NEW)
        self.add_pr_dependency_recommendation()

        # Save recommendations
        self.save_recommendations()

        print(f"✅ Generated {len(self.recommendations)} recommendations")
        print(f"📁 Saved to: {RECOMMENDATIONS_FILE}")

        # Print summary
        if self.recommendations:
            print("\n📋 Recommendation Summary:")
            for rec in self.recommendations[:5]:  # Show first 5
                trigger = rec.get('trigger', '')
                action = rec.get('action', '')
                dispatch = rec.get('dispatch_id', '')
                print(f"   • {trigger}: {action} for {dispatch}")

            if len(self.recommendations) > 5:
                print(f"   ... and {len(self.recommendations) - 5} more")

def main():
    parser = argparse.ArgumentParser(
        description="VNX Recommendation Engine - Generate dispatch recommendations for T0"
    )
    parser.add_argument(
        '--lookback',
        type=int,
        default=1440,
        help='Minutes to look back for receipts (default: 1440 = 24h)'
    )
    parser.add_argument(
        '--watch',
        action='store_true',
        help='Run continuously, updating every 30 seconds'
    )

    args = parser.parse_args()

    engine = RecommendationEngine(lookback_minutes=args.lookback)

    if args.watch:
        import time
        print("👀 Watching for changes (Ctrl+C to stop)...")
        while True:
            engine.run()
            time.sleep(30)
    else:
        engine.run()

if __name__ == "__main__":
    main()
