#!/usr/bin/env python3
"""
PR Queue Manager for VNX Evolution
Inspired by GSD's STATE.md tracking pattern
Manages PR queue with dependency tracking and state persistence
"""

import os
import sys
import json
import yaml
from datetime import datetime
from typing import Any, List, Dict, Optional
from pathlib import Path

# Ensure lib/ is on sys.path for vnx_paths import
_lib_dir = str(Path(__file__).resolve().parent / "lib")
if _lib_dir not in sys.path:
    sys.path.insert(0, _lib_dir)

from vnx_paths import resolve_paths as _resolve_vnx_paths
from pr_queue_state_snapshot import build_vnx_state_snapshot
from result_contract import (
    EXIT_DEPENDENCY,
    Result,
    result_error,
    result_exit_code,
    result_ok,
)


ROLLBACK_ENV_FLAG = "VNX_STATE_SIMPLIFICATION_ROLLBACK"
PR_QUEUE_RESULT_EXIT_CODE_MAP = {
    "missing_argument": 10,
    "invalid_argument": 10,
    "unknown_command": 10,
    "invalid_feature_plan": 10,
    "pr_not_found": 20,
    "dispatch_not_found": 20,
    "operation_failed": EXIT_DEPENDENCY,
}


def _env_flag(name: str) -> Optional[bool]:
    value = os.environ.get(name)
    if value is None:
        return None
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _rollback_mode_flag() -> Optional[bool]:
    value = _env_flag(ROLLBACK_ENV_FLAG)
    if value is None:
        value = _env_flag("VNX_STATE_DUAL_WRITE_LEGACY")
    return value

try:
    from state_integrity import write_checksum
except ImportError:
    # Non-critical in minimal runtime environments; queue state writes still proceed.
    write_checksum = None

class PRQueueManager:
    """
    Manages PR queue similar to GSD's STATE.md but for PR workflow
    """
    def __init__(self):
        script_dir = Path(__file__).resolve().parent

        # Resolve dispatch paths via vnx_paths (Phase P packaging)
        vnx_paths = _resolve_vnx_paths()
        vnx_root = Path(vnx_paths["VNX_HOME"]).expanduser().resolve()
        project_root = Path(vnx_paths["PROJECT_ROOT"]).expanduser().resolve()
        self.dispatch_dir = Path(vnx_paths["VNX_DISPATCH_DIR"])
        self.vnx_state_dir = Path(vnx_paths["VNX_STATE_DIR"]).expanduser().resolve()
        self.legacy_state_dir = (vnx_root / "state").resolve()
        rollback_flag = _rollback_mode_flag()
        self.rollback_mode = bool(rollback_flag)
        dual_write_flag = _env_flag("VNX_PR_QUEUE_DUAL_WRITE_LEGACY")
        if dual_write_flag is None:
            dual_write_flag = _env_flag("VNX_STATE_DUAL_WRITE_LEGACY")
        if dual_write_flag is None:
            dual_write_flag = self.rollback_mode
        self.dual_write_legacy = bool(dual_write_flag)
        if self.dual_write_legacy:
            print(
                "[CUTOVER] WARNING: legacy PR queue mirror writes enabled "
                "(rollback mode). Canonical state remains primary.",
                file=sys.stderr,
            )

        self.queue_file = "PR_QUEUE.md"
        # Store state in project's VNX_STATE_DIR (not script-relative) to prevent
        # cross-project contamination when multiple projects share the same vnx-system.
        self.state_file = self.vnx_state_dir / "pr_queue_state.json"
        # NEW: VNX state integration
        self.vnx_state_file = self.vnx_state_dir / "pr_queue_state.yaml"
        self.vnx_queue_json = self.vnx_state_dir / "pr_queue.json"
        self.project_root = project_root
        self.state = {}
        self.load_queue()

    def load_queue(self):
        """Load existing queue from state file"""
        # Migrate from legacy script-relative location if needed
        legacy_state = Path(__file__).resolve().parent / ".pr_state.json"
        if not self.state_file.exists() and legacy_state.exists():
            import shutil
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(legacy_state, self.state_file)
            legacy_state.unlink()
        if self.state_file.exists():
            with open(self.state_file, 'r') as f:
                self.state = json.load(f)
        else:
            self.state = {
                "feature": None,
                "prs": [],
                "completed": [],
                "active": [],
                "blocked": [],
                "created_at": datetime.now().isoformat(),
                "updated_at": datetime.now().isoformat()
            }

        # Migrate legacy single-string 'active' to list format
        if isinstance(self.state.get('active'), str):
            self.state['active'] = [self.state['active']] if self.state['active'] else []
        elif self.state.get('active') is None:
            self.state['active'] = []

    def save_state(self):
        """Save current state to JSON file"""
        self.state["updated_at"] = datetime.now().isoformat()
        with open(self.state_file, 'w') as f:
            json.dump(self.state, f, indent=2)

        # NEW: Also save to VNX state YAML for recommendation engine
        self.save_vnx_state()

    def log_queue_event(self, event: str, pr_id: str, **kwargs):
        """
        SPRINT 1: Log queue events for T0 audit trail

        Args:
            event: Event type (promote, complete, reject, status_change)
            pr_id: PR identifier
            **kwargs: Additional event metadata (dispatch_id, terminal, from_status, to_status, etc.)
        """
        # Queue event log path
        event_log = self.vnx_state_dir / "queue_event_log.jsonl"

        # Build event record
        event_record = {
            "timestamp": datetime.now().isoformat() + "Z",
            "event": event,
            "pr_id": pr_id,
            **kwargs
        }

        # Append to JSONL log
        event_log.parent.mkdir(parents=True, exist_ok=True)
        with open(event_log, 'a') as f:
            f.write(json.dumps(event_record, separators=(',', ':')) + '\n')
        if self.dual_write_legacy:
            legacy_log = self.legacy_state_dir / "queue_event_log.jsonl"
            legacy_log.parent.mkdir(parents=True, exist_ok=True)
            with open(legacy_log, 'a') as f:
                f.write(json.dumps(event_record, separators=(',', ':')) + '\n')


    def save_vnx_state(self):
        """Save state to VNX YAML format for recommendation engine (NEW)"""
        # Get execution order and build canonical snapshot payload.
        success, execution_order, _ = self.get_execution_order()
        vnx_state = build_vnx_state_snapshot(self.state, success, execution_order)

        # Ensure state directory exists
        self.vnx_state_dir.mkdir(parents=True, exist_ok=True)

        with open(self.vnx_state_file, 'w') as f:
            yaml.dump(vnx_state, f, default_flow_style=False, sort_keys=False)
        with open(self.vnx_queue_json, 'w') as f:
            json.dump(vnx_state, f, indent=2)
        if write_checksum:
            try:
                write_checksum(self.vnx_queue_json)
            except Exception as exc:
                print(f"Warning: failed to write checksum for pr_queue.json: {exc}")

        if self.dual_write_legacy:
            self.legacy_state_dir.mkdir(parents=True, exist_ok=True)
            legacy_yaml = self.legacy_state_dir / "pr_queue_state.yaml"
            legacy_json = self.legacy_state_dir / "pr_queue.json"
            with open(legacy_yaml, 'w') as f:
                yaml.dump(vnx_state, f, default_flow_style=False, sort_keys=False)
            with open(legacy_json, 'w') as f:
                json.dump(vnx_state, f, indent=2)
            if write_checksum:
                try:
                    write_checksum(legacy_json)
                except Exception as exc:
                    print(f"Warning: failed to write checksum for legacy pr_queue.json: {exc}")

    def add_pr(self, pr_data: Dict):
        """
        Add PR to queue with dependency tracking (GSD pattern)

        pr_data = {
            'id': 'PR-1',
            'title': 'User model',
            'size': 200,
            'dependencies': [],
            'skill': '@backend-developer',
            'status': 'queued'
        }
        """
        # Set default status if not provided
        if 'status' not in pr_data:
            pr_data['status'] = 'queued'

        # Check for duplicates
        existing_ids = [pr['id'] for pr in self.state['prs']]
        if pr_data['id'] not in existing_ids:
            self.state['prs'].append(pr_data)
            self.save_state()
            self.update_markdown()

    def get_next_pr(self) -> Optional[Dict]:
        """
        Get next PR with all dependencies met (GSD dependency resolution)
        """
        for pr in self.state['prs']:
            if pr['status'] == 'queued':
                # Check dependencies
                deps_met = all(
                    dep in self.state['completed']
                    for dep in pr.get('dependencies', [])
                )
                if deps_met:
                    return pr
        return None

    def update_pr_status(self, pr_id: str, status: str):
        """Update PR status and track in state"""
        old_status = None
        for pr in self.state['prs']:
            if pr['id'] == pr_id:
                old_status = pr.get('status', 'unknown')
                pr['status'] = status

                if status == 'completed':
                    if pr_id not in self.state['completed']:
                        self.state['completed'].append(pr_id)
                    if pr_id in self.state['active']:
                        self.state['active'].remove(pr_id)
                    if pr_id in self.state['blocked']:
                        self.state['blocked'].remove(pr_id)

                    # SPRINT 1: Log completion event
                    self.log_queue_event("complete", pr_id, from_status=old_status, to_status=status)

                elif status == 'in_progress':
                    if pr_id not in self.state['active']:
                        self.state['active'].append(pr_id)
                    if pr_id in self.state['blocked']:
                        self.state['blocked'].remove(pr_id)

                    # SPRINT 1: Log activation event
                    self.log_queue_event("activate", pr_id, from_status=old_status, to_status=status)

                elif status == 'blocked':
                    if pr_id not in self.state['blocked']:
                        self.state['blocked'].append(pr_id)
                    if pr_id in self.state['active']:
                        self.state['active'].remove(pr_id)

                    # SPRINT 1: Log blocking event
                    self.log_queue_event("block", pr_id, from_status=old_status, to_status=status)

        self.save_state()
        self.update_markdown()

    def update_markdown(self):
        """Generate PR_QUEUE.md visualization (inspired by GSD's PLAN.md)"""
        total = len(self.state['prs'])
        complete = len(self.state['completed'])
        blocked = len(self.state['blocked'])
        active = len(self.state.get('active', []))
        queued = total - complete - active - blocked

        percent = int(complete / total * 100) if total > 0 else 0
        progress_bar = '█' * int(percent / 10) + '░' * (10 - int(percent / 10))

        content = f"""# PR Queue - Feature: {self.state.get('feature', 'Unknown')}

## Progress Overview
Total: {total} PRs | Complete: {complete} | Active: {active} | Queued: {queued} | Blocked: {blocked}
Progress: {progress_bar} {percent}%

## Status
"""

        # Completed PRs
        if self.state['completed']:
            content += "\n### ✅ Completed PRs\n"
            for pr_id in self.state['completed']:
                pr = next((p for p in self.state['prs'] if p['id'] == pr_id), None)
                if pr:
                    content += f"- {pr['id']}: {pr['title']}\n"

        # Active PRs (parallel tracks)
        if self.state.get('active'):
            content += f"\n### 🔄 Currently Active\n"
            for active_id in self.state['active']:
                pr = next((p for p in self.state['prs'] if p['id'] == active_id), None)
                if pr:
                    track = pr.get('track', '?')
                    content += f"- {pr['id']}: {pr['title']} (Track {track}, skill: {pr['skill']})\n"

        # Queued PRs
        queued_prs = [pr for pr in self.state['prs'] if pr['status'] == 'queued']
        if queued_prs:
            content += "\n### ⏳ Queued PRs\n"
            for pr in queued_prs:
                deps = f" (dependencies: {', '.join(pr['dependencies'])})" if pr.get('dependencies') else " (dependencies: none)"
                content += f"- {pr['id']}: {pr['title']}{deps}\n"

        # Blocked PRs
        if self.state.get('blocked'):
            content += "\n### 🚧 Blocked PRs\n"
            for pr_id in self.state['blocked']:
                pr = next((p for p in self.state['prs'] if p['id'] == pr_id), None)
                if pr:
                    content += f"- {pr['id']}: {pr['title']}\n"

        # Dependency flow
        if total > 0:
            content += "\n## Dependency Flow\n```\n"
            for pr in self.state['prs']:
                if not pr.get('dependencies'):
                    content += f"{pr['id']} (no dependencies)\n"
                else:
                    deps_str = " → ".join(pr['dependencies'])
                    content += f"{deps_str} → {pr['id']}\n"
            content += "```\n"

        with open(self.queue_file, 'w') as f:
            f.write(content)

    def set_feature(self, feature_name: str):
        """Set the feature name for this queue"""
        self.state['feature'] = feature_name
        self.save_state()
        self.update_markdown()

    def clear_queue(self):
        """Clear the queue and start fresh"""
        self.state = {
            "feature": None,
            "prs": [],
            "completed": [],
            "active": [],
            "blocked": [],
            "created_at": datetime.now().isoformat(),
            "updated_at": datetime.now().isoformat()
        }
        self.save_state()
        self.update_markdown()

    def get_status_summary(self) -> Dict:
        """Get a summary of the current queue status"""
        total = len(self.state['prs'])
        complete = len(self.state['completed'])

        return {
            'feature': self.state.get('feature', 'Unknown'),
            'total_prs': total,
            'completed': complete,
            'active': self.state.get('active', []),
            'blocked': len(self.state.get('blocked', [])),
            'progress_percent': int(complete / total * 100) if total > 0 else 0,
            'next_pr': self.get_next_pr()
        }

    def get_pr(self, pr_id: str) -> Optional[Dict]:
        """Get PR data by ID"""
        for pr in self.state['prs']:
            if pr['id'] == pr_id:
                return pr
        return None

    def is_complete(self, pr_id: str) -> bool:
        """Check if PR is completed"""
        return pr_id in self.state['completed']

    def check_pr_dependencies(self, pr_id: str) -> tuple[bool, str]:
        """
        Check if all dependencies are complete for a PR

        Args:
            pr_id: PR identifier

        Returns:
            Tuple of (dependencies_met: bool, message: str)
        """
        pr = self.get_pr(pr_id)
        if not pr:
            return False, f"PR {pr_id} not found"

        dependencies = pr.get('dependencies', [])
        if not dependencies:
            return True, "No dependencies"

        unmet = []
        for dep in dependencies:
            if not self.is_complete(dep):
                unmet.append(dep)

        if unmet:
            return False, f"Waiting for: {', '.join(unmet)}"
        return True, "Ready to start"

    def _detect_circular_dependencies(self, pr_id: str, visited: set, path: set) -> Optional[List[str]]:
        """
        Detect circular dependencies using DFS

        Args:
            pr_id: Current PR being checked
            visited: Set of all visited PRs
            path: Set of PRs in current path (for cycle detection)

        Returns:
            List of PRs forming the cycle, or None if no cycle
        """
        if pr_id in path:
            # Found a cycle - return the cycle path
            return [pr_id]

        if pr_id in visited:
            return None

        visited.add(pr_id)
        path.add(pr_id)

        pr = self.get_pr(pr_id)
        if pr:
            for dep in pr.get('dependencies', []):
                cycle = self._detect_circular_dependencies(dep, visited, path)
                if cycle is not None:
                    if cycle[0] == pr_id:
                        # Complete cycle found
                        return cycle
                    else:
                        # Building the cycle path
                        return [pr_id] + cycle

        path.remove(pr_id)
        return None

    def get_execution_order(self, feature_plan: Optional[Dict] = None) -> tuple[bool, List[str], Optional[str]]:
        """
        Generate valid execution order respecting dependencies using topological sort

        Args:
            feature_plan: Optional feature plan dict (if None, uses current queue)

        Returns:
            Tuple of (success: bool, ordered_prs: List[str], error_message: Optional[str])
        """
        # Use current queue PRs if no feature plan provided
        prs_to_order = self.state['prs'] if feature_plan is None else feature_plan.get('prs', [])

        # First, check for circular dependencies
        visited = set()
        for pr in prs_to_order:
            pr_id = pr['id']
            if pr_id not in visited:
                cycle = self._detect_circular_dependencies(pr_id, visited, set())
                if cycle:
                    cycle_str = ' → '.join(cycle + [cycle[0]])
                    return False, [], f"Circular dependency detected: {cycle_str}"

        # Perform topological sort using Kahn's algorithm
        # Build in-degree map and adjacency list
        in_degree = {pr['id']: 0 for pr in prs_to_order}
        adj_list = {pr['id']: [] for pr in prs_to_order}

        for pr in prs_to_order:
            pr_id = pr['id']
            for dep in pr.get('dependencies', []):
                if dep not in in_degree:
                    return False, [], f"PR {pr_id} depends on non-existent PR {dep}"
                adj_list[dep].append(pr_id)
                in_degree[pr_id] += 1

        # Start with PRs that have no dependencies
        queue = [pr_id for pr_id, degree in in_degree.items() if degree == 0]
        ordered = []

        while queue:
            # Process PR with no remaining dependencies
            current = queue.pop(0)
            ordered.append(current)

            # Reduce in-degree for dependent PRs
            for dependent in adj_list[current]:
                in_degree[dependent] -= 1
                if in_degree[dependent] == 0:
                    queue.append(dependent)

        # Check if all PRs were ordered (no cycles)
        if len(ordered) != len(prs_to_order):
            return False, [], "Unable to determine execution order (possible cycle or missing dependency)"

        return True, ordered, None

    def validate_feature_plan(self, plan_path: str) -> tuple[bool, Optional[str]]:
        """
        Validate FEATURE_PLAN.md structure and dependencies

        Args:
            plan_path: Path to FEATURE_PLAN.md file

        Returns:
            Tuple of (valid: bool, error_message: Optional[str])
        """
        try:
            with open(plan_path, 'r') as f:
                content = f.read()

            # Basic structure checks
            if '# Feature:' not in content and '## Feature:' not in content:
                return False, "Missing feature title (expected '# Feature:' or '## Feature:')"

            if 'Dependencies:' not in content and 'Dependencies**:' not in content:
                return False, "Missing dependencies section"

            # Extract PR definitions (simple regex-based parsing)
            import re
            pr_pattern = r'##\s+PR-(\d+):'
            pr_ids = re.findall(pr_pattern, content)

            if not pr_ids:
                return False, "No PRs found (expected format: '## PR-X:')"

            # Check for duplicate PR IDs
            if len(pr_ids) != len(set(pr_ids)):
                duplicates = [pid for pid in set(pr_ids) if pr_ids.count(pid) > 1]
                return False, f"Duplicate PR IDs found: {', '.join(f'PR-{d}' for d in duplicates)}"

            # Extract dependencies and validate they reference existing PRs
            dep_pattern = r'Dependencies:\s*\[([^\]]*)\]'
            dep_matches = re.findall(dep_pattern, content)

            valid_pr_ids = set(f'PR-{pid}' for pid in pr_ids)

            for dep_list in dep_matches:
                if dep_list.strip():
                    deps = [d.strip() for d in dep_list.split(',')]
                    for dep in deps:
                        if dep and dep not in valid_pr_ids:
                            return False, f"Invalid dependency reference: {dep} (not found in PR list)"

            return True, None

        except FileNotFoundError:
            return False, f"File not found: {plan_path}"
        except Exception as e:
            return False, f"Error parsing feature plan: {str(e)}"

    def _parse_pr_from_feature_plan(self, pr_id: str) -> Optional[Dict]:
        """
        Parse full PR details from FEATURE_PLAN.md

        Returns PR details including description, scope, success criteria, and quality gate
        """
        # Try multiple paths to find FEATURE_PLAN.md
        possible_paths = [
            Path(__file__).parent.parent.parent / "FEATURE_PLAN.md",  # From scripts/
            Path.cwd() / "FEATURE_PLAN.md",  # From current working directory
            Path.cwd() / ".." / "FEATURE_PLAN.md"  # One level up
        ]

        feature_plan_path = None
        for path in possible_paths:
            if path.exists():
                feature_plan_path = path
                break

        if not feature_plan_path:
            return None

        try:
            with open(feature_plan_path, 'r') as f:
                content = f.read()

            # Find PR section (matches ## PR-1: Title)
            pr_pattern = f'## {pr_id}:(.+?)(?=\n##\\s+PR-|$)'
            import re
            match = re.search(pr_pattern, content, re.DOTALL)

            if not match:
                return None

            pr_section = match.group(1)

            # SPRINT 2: Parse ALL metadata fields from FEATURE_PLAN.md
            details = {
                'title': '',
                'track': 'A',  # default
                'priority': 'P1',  # default
                'skill': 'developer',  # default
                'complexity': 'Medium',  # default
                'cognition': 'normal',  # default
                'requires_model': None,  # default
                'dependencies': [],  # default
                'description': '',
                'scope': '',
                'success_criteria': [],
                'quality_gate': []
            }

            # Extract title (first line after PR-X:)
            title_match = re.search(r'^([^\n]+)', pr_section.strip())
            if title_match:
                details['title'] = title_match.group(1).strip()

            # SPRINT 2: Extract metadata fields (Track, Priority, Skill, Complexity, etc.)
            # Pattern: **Field**: value
            for line in pr_section.split('\n')[:20]:  # Check first 20 lines for metadata
                line = line.strip()

                # Track
                track_match = re.match(r'\*\*Track\*\*:\s*(.+)', line, re.IGNORECASE)
                if track_match:
                    details['track'] = track_match.group(1).strip()

                # Priority
                priority_match = re.match(r'\*\*Priority\*\*:\s*(.+)', line, re.IGNORECASE)
                if priority_match:
                    details['priority'] = priority_match.group(1).strip()

                # Skill
                skill_match = re.match(r'\*\*Skill\*\*:\s*(.+)', line, re.IGNORECASE)
                if skill_match:
                    skill = skill_match.group(1).strip()
                    # Remove @ prefix if present
                    details['skill'] = skill.lstrip('@')

                # Complexity
                complexity_match = re.match(r'\*\*Complexity\*\*:\s*(.+)', line, re.IGNORECASE)
                if complexity_match:
                    details['complexity'] = complexity_match.group(1).strip()

                # Requires-Model (optional)
                model_match = re.match(r'\*\*Requires-Model\*\*:\s*(.+)', line, re.IGNORECASE)
                if model_match:
                    details['requires_model'] = model_match.group(1).strip()

            # SPRINT 2: Derive cognition from complexity
            complexity_lower = details['complexity'].lower()
            if 'high' in complexity_lower or 'critical' in complexity_lower:
                details['cognition'] = 'deep'
            elif 'low' in complexity_lower or 'simple' in complexity_lower:
                details['cognition'] = 'shallow'
            else:
                details['cognition'] = 'normal'

            # SPRINT 2: Extract dependencies
            deps_match = re.search(r'\*{0,2}Dependencies\*{0,2}:\s*\[([^\]]*)\]', pr_section, re.IGNORECASE)
            if deps_match:
                deps_str = deps_match.group(1).strip()
                if deps_str:
                    # Parse "PR-1, PR-2" or "PR-1" format
                    details['dependencies'] = [d.strip() for d in deps_str.split(',') if d.strip()]

            # Extract Description section
            desc_match = re.search(r'### Description\s*\n\n(.+?)(?=\n###|$)', pr_section, re.DOTALL)
            if desc_match:
                details['description'] = desc_match.group(1).strip()

            # Extract Scope section
            scope_match = re.search(r'### Scope\s*\n\n(.+?)(?=\n###|$)', pr_section, re.DOTALL)
            if scope_match:
                details['scope'] = scope_match.group(1).strip()

            # Extract Success Criteria (bullet points)
            success_section = re.search(r'### Success Criteria\s*\n\n(.+?)(?=\n###|$)', pr_section, re.DOTALL)
            if success_section:
                criteria_text = success_section.group(1)
                details['success_criteria'] = [
                    line.strip('- ').strip()
                    for line in criteria_text.split('\n')
                    if line.strip().startswith('-')
                ]

            # Extract Quality Gate checklist
            gate_section = re.search(r'### Quality Gate\s*\n(.+?)(?=\n---|$)', pr_section, re.DOTALL)
            if gate_section:
                gate_text = gate_section.group(1)
                details['quality_gate'] = [
                    line.strip('- ✅ ').strip()
                    for line in gate_text.split('\n')
                    if '✅' in line or '❌' in line or '⏳' in line
                ]

            return details

        except Exception as e:
            print(f"⚠️  Could not parse FEATURE_PLAN.md: {e}")
            return None

    @staticmethod
    def _classify_gate_severity(gate_item: str) -> str:
        """Classify a quality gate checklist item into blocker/warn/info severity."""
        item_lower = gate_item.lower()
        # Blocker: test pass requirements, 100% targets, E2E
        blocker_keywords = ['all tests pass', 'e2e', '100%', 'test suite passes']
        if any(kw in item_lower for kw in blocker_keywords):
            return 'blocker'
        # Warn: memory, performance, regression, numeric thresholds
        warn_keywords = ['memory', 'zombie', 'regression', '>=', 'no resource',
                         'shutdown', 'clean', 'speed', 'improvement']
        if any(kw in item_lower for kw in warn_keywords):
            return 'warn'
        # Default: info
        return 'info'

    def _parse_all_prs_from_plan(self, content: str) -> List[Dict]:
        """
        Parse ALL PR metadata directly from FEATURE_PLAN.md content.

        Handles both bold-markdown format (**Track**: B) and plain format (Track: B).
        Returns list of dicts with: id, title, title_from_header, track, priority,
        complexity, risk, skill, estimated_time, dependencies, gate.
        """
        import re

        prs = []

        # Split content into PR sections
        pr_sections = re.split(r'(?=## PR-\d+:)', content)

        for section in pr_sections:
            # Must start with ## PR-X:
            header_match = re.match(r'## (PR-\d+):\s*(.+?)(?:\n|$)', section)
            if not header_match:
                continue

            pr_id = header_match.group(1)
            title_from_header = header_match.group(2).strip()

            def extract_field(name, default=None):
                """Extract field value from bold-markdown or plain format."""
                # Try bold markdown: **Field**: Value
                match = re.search(rf'\*\*{name}\*\*:\s*(.+?)(?:\n|$)', section)
                if match:
                    return match.group(1).strip()
                # Try plain: Field: Value (at start of line, not inside ### headers)
                match = re.search(rf'^{name}:\s*(.+?)(?:\n|$)', section, re.MULTILINE)
                if match:
                    return match.group(1).strip()
                return default

            # Parse dependencies: Dependencies: [PR-1, PR-2] or Dependencies: []
            deps = []
            dep_match = re.search(r'\*{0,2}Dependencies\*{0,2}:\s*\[([^\]]*)\]', section)
            if dep_match and dep_match.group(1).strip():
                deps = [d.strip() for d in dep_match.group(1).split(',') if d.strip()]

            # Parse gate name from `gate_name`:
            gate = 'implementation'
            gate_match = re.search(r'`(gate_\w+)`', section)
            if gate_match:
                gate = gate_match.group(1)

            # Parse title from **Title**: field (overrides header title if present)
            explicit_title = extract_field('Title')

            pr_data = {
                'id': pr_id,
                'title': explicit_title if explicit_title else title_from_header,
                'title_from_header': title_from_header,
                'track': extract_field('Track', 'A'),
                'priority': extract_field('Priority', 'P1'),
                'complexity': extract_field('Complexity', 'Medium'),
                'risk': extract_field('Risk', 'Medium'),
                'skill': extract_field('Skill', 'backend-developer'),
                'estimated_time': extract_field('Estimated Time', 'unknown'),
                'dependencies': deps,
                'gate': gate,
            }

            # Parse quality gate checklist items for open item generation
            quality_gate_items = []
            gate_section = re.search(r'### Quality Gate\s*\n(.+?)(?=\n---|$)', section, re.DOTALL)
            if gate_section:
                gate_text = gate_section.group(1)
                for line in gate_text.split('\n'):
                    line = line.strip()
                    # Match checklist items: - [ ] item or - [x] item
                    checklist_match = re.match(r'^-\s*\[[ x]\]\s*(.+)$', line)
                    if checklist_match:
                        quality_gate_items.append(checklist_match.group(1).strip())

            pr_data['quality_gate_items'] = quality_gate_items

            prs.append(pr_data)

        return prs

    def create_dispatch_from_pr(self, pr_id: str) -> Optional[str]:
        """
        Create dispatch file for next PR (writes to dispatches/queue/)

        Args:
            pr_id: PR identifier (e.g., 'PR-1')

        Returns:
            Dispatch ID if created, None if PR not found
        """
        pr = self.get_pr(pr_id)
        if not pr:
            print(f"❌ PR not found: {pr_id}")
            return None

        # Check dependencies are met
        deps_met, msg = self.check_pr_dependencies(pr_id)
        if not deps_met:
            print(f"❌ Cannot create dispatch: {msg}")
            return None

        # Parse full details from FEATURE_PLAN.md
        pr_details = self._parse_pr_from_feature_plan(pr_id)

        # SPRINT 2: Use parsed metadata from FEATURE_PLAN.md with fallbacks to queue state
        # Priority order: FEATURE_PLAN.md > queue state > hardcoded defaults
        track = (pr_details.get('track') if pr_details else None) or pr.get('track', 'A')
        skill = (pr_details.get('skill') if pr_details else None) or pr.get('skill', 'developer')
        if skill:
            skill = skill.lstrip('@')
        priority = (pr_details.get('priority') if pr_details else None) or pr.get('priority', 'P1')
        cognition = (pr_details.get('cognition') if pr_details else None) or 'normal'
        requires_model = (pr_details.get('requires_model') if pr_details else None) or None

        # Generate dispatch ID
        timestamp = datetime.now().strftime('%Y%m%d-%H%M%S')
        pr_descriptor = pr['title'].lower().replace(' ', '-')[:30]
        dispatch_id = f"{timestamp}-{pr_descriptor}-{track}"

        # SPRINT 2: Map track to terminal (A→T1, B→T2, C→T3)
        terminal_map = {'A': 'T1', 'B': 'T2', 'C': 'T3'}
        terminal = terminal_map.get(track, 'T1')

        # Build instruction - include FULL PR section from FEATURE_PLAN.md
        instruction = ""

        if pr_details:
            # Extract full PR section from FEATURE_PLAN.md for complete context
            feature_plan_path = Path(__file__).parent.parent.parent / "FEATURE_PLAN.md"
            for path in [feature_plan_path, Path.cwd() / "FEATURE_PLAN.md"]:
                if path.exists():
                    with open(path, 'r') as f:
                        content = f.read()
                    # Find full PR section
                    import re
                    pr_pattern = f'## {pr_id}:(.+?)(?=\n##\\s+PR-|$)'
                    match = re.search(pr_pattern, content, re.DOTALL)
                    if match:
                        instruction = match.group(1).strip()
                    break

        # Fallback if parsing failed
        if not instruction:
            instruction = f"# {pr['title']}\n\n{pr.get('description', 'See FEATURE_PLAN.md for full details')}"

        # No separate done section - it's already in the instruction

        # SPRINT 2: Build Manager Block with parsed metadata
        manager_block = f"""[[TARGET:{track}]]
Manager Block

Role: {skill}
Track: {track}
Terminal: {terminal}
Gate: {pr.get('gate', 'implementation')}
Priority: {priority}
Cognition: {cognition}"""

        # SPRINT 2: Add Requires-Model if specified
        if requires_model:
            manager_block += f"\nRequires-Model: {requires_model}"

        manager_block += f"""
Dispatch-ID: {dispatch_id}
PR-ID: {pr_id}
Parent-Dispatch: none
On-Success: review
On-Failure: investigation
Reason: {pr['title']} from PR queue
Status: pending-approval

Context: [[@FEATURE_PLAN.md]]

Instruction:
{instruction}"""

        # SPRINT 2: Add dependencies from parsed data
        dependencies = (pr_details.get('dependencies') if pr_details else None) or pr.get('dependencies', [])
        if dependencies:
            manager_block += f"\nDependencies: {', '.join(dependencies)}"
        else:
            manager_block += "\nDependencies: None"

        manager_block += f"""
Size Estimate: {pr.get('size', 'unknown')} lines
[[DONE]]
"""

        dispatch_content = manager_block

        # Write to STAGING directory (NOT queue/)
        # T0 must explicitly promote to queue/ for popup detection
        staging_dir = self.dispatch_dir / "staging"
        staging_dir.mkdir(parents=True, exist_ok=True)

        dispatch_file = staging_dir / f"{dispatch_id}.md"
        dispatch_file.write_text(dispatch_content)

        print(f"✅ Created staging dispatch: {dispatch_id}")
        print(f"📁 Location: {dispatch_file}")
        print(f"⚠️  Status: STAGING (not yet in popup queue)")
        print(f"📋 Next: T0 must promote via: python pr_queue_manager.py promote {dispatch_id}")

        return dispatch_id


    def promote_dispatch(self, dispatch_id: str, force: bool = False) -> bool:
        """
        Promote a staging dispatch to queue for popup approval (atomic + idempotent)

        NOW WITH DEPENDENCY CHECKING: Blocks promotion if PR dependencies not met

        Args:
            dispatch_id: Dispatch ID or filename (with or without .md)
            force: If True, skip dependency check and overwrite existing queue file

        Returns:
            True if promoted successfully
        """
        # Normalize dispatch_id
        if not dispatch_id.endswith('.md'):
            dispatch_id_file = f"{dispatch_id}.md"
        else:
            dispatch_id_file = dispatch_id
            dispatch_id = dispatch_id[:-3]

        staging_dir = self.dispatch_dir / "staging"
        queue_dir = self.dispatch_dir / "queue"

        staging_file = staging_dir / dispatch_id_file
        queue_file = queue_dir / dispatch_id_file

        # Safety check: staging file must exist
        if not staging_file.exists():
            print(f"❌ Staging dispatch not found: {dispatch_id}")
            print(f"   Looked in: {staging_file}")
            return False

        # Extract PR-ID for dependency check
        metadata = self.extract_v2_metadata(staging_file)
        pr_id = metadata.get('pr_id')

        # NEW: Dependency-aware promotion
        if pr_id and not force:
            pr = self.get_pr(pr_id)
            if pr:
                deps_met, msg = self.check_pr_dependencies(pr_id)
                if not deps_met:
                    print(f"❌ Cannot promote {pr_id}: Dependencies not met")
                    print(f"   {msg}")
                    print(f"   Use --force to override")
                    return False

        # Safety check: prevent duplicate promotion
        if queue_file.exists() and not force:
            print(f"❌ Dispatch already in queue: {dispatch_id}")
            print(f"   Use --force to overwrite")
            return False

        # Auto-approve mode: when popup is disabled, go directly to pending
        auto_approve = os.environ.get('VNX_QUEUE_POPUP_ENABLED', '1') == '0'
        target_dir = self.dispatch_dir / ("pending" if auto_approve else "queue")
        target_file = target_dir / dispatch_id_file

        # Ensure target directory exists
        target_dir.mkdir(parents=True, exist_ok=True)

        # Safety check: prevent duplicate promotion
        if target_file.exists() and not force:
            target_name = "pending" if auto_approve else "queue"
            print(f"❌ Dispatch already in {target_name}: {dispatch_id}")
            print(f"   Use --force to overwrite")
            return False

        # Atomic move: temp + rename pattern
        import shutil

        temp_file = None
        try:
            # Extract PR-ID for audit
            pr_id = self.extract_v2_metadata(staging_file).get('pr_id')

            # Copy to target with temp name (atomic preparation)
            temp_file = target_dir / f".{dispatch_id_file}.tmp"
            shutil.copy2(staging_file, temp_file)

            # Atomic rename (OS-level atomic operation)
            if force and target_file.exists():
                target_file.unlink()
            temp_file.rename(target_file)

            # Remove from staging (cleanup phase)
            staging_file.unlink()

            target_name = "pending (auto-approved)" if auto_approve else "queue"

            # Log promotion with JSONL
            self._log_audit({
                'timestamp': datetime.now().isoformat(),
                'action': 'promote_dispatch',
                'dispatch_id': dispatch_id,
                'pr_id': pr_id,
                'from': str(staging_file),
                'to': str(target_file),
                'auto_approved': auto_approve,
                'forced': force,
                'actor': 'T0'
            })

            # SPRINT 1: Log queue event for T0 audit trail
            if pr_id:
                self.log_queue_event("promote", pr_id, dispatch_id=dispatch_id, from_location="staging", to_location=target_name, forced=force)

            print(f"✅ Promoted dispatch: {dispatch_id}")
            print(f"📁 Now in {target_name}: {target_file}")
            if not auto_approve:
                print(f"🔔 Popup will detect shortly")

            return True

        except Exception as e:
            print(f"❌ Promotion failed: {e}")
            # Cleanup temp file if exists
            if temp_file and temp_file.exists():
                temp_file.unlink()
            return False

    def _log_audit(self, entry: Dict) -> None:
        """Write audit entry to JSONL log"""
        audit_log = self.vnx_state_dir / "dispatch_audit.jsonl"
        audit_log.parent.mkdir(parents=True, exist_ok=True)

        with open(audit_log, 'a') as f:
            json.dump(entry, f)
            f.write('\n')

        if self.dual_write_legacy:
            legacy_log = self.legacy_state_dir / "dispatch_audit.jsonl"
            legacy_log.parent.mkdir(parents=True, exist_ok=True)
            with open(legacy_log, 'a') as f:
                json.dump(entry, f)
                f.write('\n')

    def extract_v2_metadata(self, dispatch_file: Path) -> Dict[str, str]:
        """
        Extract metadata from Manager Block v2 dispatch file

        Args:
            dispatch_file: Path to dispatch file

        Returns:
            Dict with extracted metadata
        """
        try:
            content = dispatch_file.read_text()
            metadata = {}

            import re
            # Extract PR-ID
            pr_match = re.search(r'PR-ID:\s*(.+)', content)
            if pr_match:
                metadata['pr_id'] = pr_match.group(1).strip()

            # Extract Role
            role_match = re.search(r'Role:\s*(.+)', content)
            if role_match:
                metadata['role'] = role_match.group(1).strip()

            # Extract Track
            track_match = re.search(r'Track:\s*(.+)', content)
            if track_match:
                metadata['track'] = track_match.group(1).strip()

            # Extract Gate
            gate_match = re.search(r'Gate:\s*(.+)', content)
            if gate_match:
                metadata['gate'] = gate_match.group(1).strip()

            # Extract Priority
            priority_match = re.search(r'Priority:\s*(.+)', content)
            if priority_match:
                metadata['priority'] = priority_match.group(1).strip()

            return metadata

        except Exception as e:
            print(f"⚠️  Failed to extract metadata: {e}")
            return {}

    def show_dispatch(self, dispatch_id: str) -> bool:
        """
        Show staging dispatch details

        Args:
            dispatch_id: Dispatch ID or filename (with or without .md)

        Returns:
            True if found and displayed
        """
        # Normalize dispatch_id
        if not dispatch_id.endswith('.md'):
            dispatch_id_file = f"{dispatch_id}.md"
        else:
            dispatch_id_file = dispatch_id
            dispatch_id = dispatch_id[:-3]

        staging_dir = self.dispatch_dir / "staging"
        staging_file = staging_dir / dispatch_id_file

        if not staging_file.exists():
            print(f"❌ Staging dispatch not found: {dispatch_id}")
            print(f"   Looked in: {staging_file}")
            return False

        try:
            content = staging_file.read_text()

            # Extract metadata for summary
            metadata = self.extract_v2_metadata(staging_file)

            print(f"\n📋 Staging Dispatch: {dispatch_id}")
            print(f"📁 Path: {staging_file}")
            print(f"📊 Size: {len(content)} chars")
            print(f"\n🏷️  Metadata:")
            print(f"   PR-ID: {metadata.get('pr_id', 'N/A')}")
            print(f"   Role: {metadata.get('role', 'N/A')}")
            print(f"   Track: {metadata.get('track', 'N/A')}")
            print(f"   Gate: {metadata.get('gate', 'N/A')}")
            print(f"   Priority: {metadata.get('priority', 'N/A')}")

            # Show first 30 lines of content
            lines = content.split('\n')
            preview_lines = min(30, len(lines))
            print(f"\n📄 Preview (first {preview_lines} lines):")
            print("─" * 60)
            print('\n'.join(lines[:preview_lines]))
            if len(lines) > 30:
                print(f"... ({len(lines) - 30} more lines)")
            print("─" * 60)

            return True

        except Exception as e:
            print(f"❌ Failed to show dispatch: {e}")
            return False

    def patch_dispatch(self, dispatch_id: str, patches: Dict[str, str]) -> bool:
        """
        Patch staging dispatch fields, instruction body, context, and dependencies.

        Supports:
          Header fields:  --set role=backend-developer --set priority=P0
          Instruction:    --set-instruction "new body" OR --append-instruction "extra"
          Context:        --set context="[[@FILE1.md, @FILE2.md]]"
          Dependencies:   --set dependencies="PR-1, PR-2"
          Any header:     --set field=value (no whitelist restriction)

        Args:
            dispatch_id: Dispatch ID or filename (with or without .md)
            patches: Dict of field_name -> new_value
                     Special keys: 'instruction', 'append-instruction',
                     'prepend-instruction', 'context', 'dependencies'

        Returns:
            True if patched successfully
        """
        import re

        # Normalize dispatch_id
        if not dispatch_id.endswith('.md'):
            dispatch_id_file = f"{dispatch_id}.md"
        else:
            dispatch_id_file = dispatch_id
            dispatch_id = dispatch_id[:-3]

        staging_dir = self.dispatch_dir / "staging"
        staging_file = staging_dir / dispatch_id_file

        if not staging_file.exists():
            print(f"❌ Staging dispatch not found: {dispatch_id}")
            return False

        try:
            content = staging_file.read_text()
            changes = []

            for field, new_value in patches.items():

                # === INSTRUCTION BODY: replace entirely ===
                if field == 'instruction':
                    match = re.search(
                        r'(Instruction:\n)(.+?)(\n\nDependencies:|\n\n\[\[DONE\]\])',
                        content, re.DOTALL
                    )
                    if match:
                        old_body = match.group(2).strip()
                        content = content[:match.start(2)] + new_value + '\n' + content[match.end(2):]
                        changes.append(f"instruction: replaced ({len(old_body)} → {len(new_value)} chars)")
                    else:
                        print(f"⚠️  Could not locate Instruction body in dispatch")

                # === INSTRUCTION BODY: append ===
                elif field == 'append-instruction':
                    match = re.search(
                        r'(Instruction:\n.+?)(\n\nDependencies:|\n\n\[\[DONE\]\])',
                        content, re.DOTALL
                    )
                    if match:
                        insert_pos = match.end(1)
                        content = content[:insert_pos] + '\n\n' + new_value + content[insert_pos:]
                        changes.append(f"instruction: appended {len(new_value)} chars")
                    else:
                        print(f"⚠️  Could not locate Instruction body in dispatch")

                # === INSTRUCTION BODY: prepend ===
                elif field == 'prepend-instruction':
                    match = re.search(r'Instruction:\n', content)
                    if match:
                        insert_pos = match.end()
                        content = content[:insert_pos] + new_value + '\n\n' + content[insert_pos:]
                        changes.append(f"instruction: prepended {len(new_value)} chars")
                    else:
                        print(f"⚠️  Could not locate Instruction header in dispatch")

                # === INSTRUCTION FROM FILE ===
                elif field == 'instruction-file':
                    file_path = Path(new_value)
                    if not file_path.is_absolute():
                        file_path = self.project_root / new_value
                    if not file_path.exists():
                        print(f"❌ Instruction file not found: {new_value}")
                        continue
                    file_content = file_path.read_text().strip()
                    match = re.search(
                        r'(Instruction:\n)(.+?)(\n\nDependencies:|\n\n\[\[DONE\]\])',
                        content, re.DOTALL
                    )
                    if match:
                        content = content[:match.start(2)] + file_content + '\n' + content[match.end(2):]
                        changes.append(f"instruction: replaced from file {file_path.name} ({len(file_content)} chars)")
                    else:
                        print(f"⚠️  Could not locate Instruction body in dispatch")

                # === HEADER FIELDS (any Manager Block field) ===
                else:
                    field_capitalized = field.replace('-', ' ').title().replace(' ', '-')
                    pattern = f"{field_capitalized}: (.+)"
                    match = re.search(pattern, content)
                    if match:
                        old_value = match.group(1)
                        content = content.replace(
                            f"{field_capitalized}: {old_value}",
                            f"{field_capitalized}: {new_value}",
                            1  # only first occurrence
                        )
                        changes.append(f"{field}: {old_value} → {new_value}")
                    else:
                        # Field not found - insert it before Dispatch-ID line
                        insert_match = re.search(r'(Dispatch-ID:)', content)
                        if insert_match:
                            insert_line = f"{field_capitalized}: {new_value}\n"
                            content = content[:insert_match.start()] + insert_line + content[insert_match.start():]
                            changes.append(f"{field}: (added) {new_value}")
                        else:
                            print(f"⚠️  Field '{field}' not found and could not insert")

            if not changes:
                print("❌ No changes applied")
                return False

            # Write patched content
            staging_file.write_text(content)

            # Log patch
            self._log_audit({
                'timestamp': datetime.now().isoformat(),
                'action': 'patch_dispatch',
                'dispatch_id': dispatch_id,
                'pr_id': self.extract_v2_metadata(staging_file).get('pr_id'),
                'staging_file': str(staging_file),
                'patch_fields': patches,
                'changes': changes,
                'actor': 'T0'
            })

            print(f"✅ Patched dispatch: {dispatch_id}")
            print("📝 Changes:")
            for change in changes:
                print(f"   {change}")

            return True

        except Exception as e:
            print(f"❌ Patch failed: {e}")
            import traceback
            traceback.print_exc()
            return False

    def init_feature_batch(self, feature_plan_path: str, force: bool = False) -> tuple[bool, int]:
        """
        Batch generate ALL dispatches from FEATURE_PLAN.md to staging directory.

        Parses all metadata directly from FEATURE_PLAN.md (not from stale internal state).
        Resets internal state with the new feature's PR data.

        Args:
            feature_plan_path: Path to FEATURE_PLAN.md
            force: If True, overwrite existing staging files

        Returns:
            Tuple of (success: bool, count: int)
        """
        import re

        # Validate feature plan first
        valid, error = self.validate_feature_plan(feature_plan_path)
        if not valid:
            print(f"❌ Invalid feature plan: {error}")
            return False, 0

        try:
            with open(feature_plan_path, 'r') as f:
                content = f.read()

            # Parse feature name
            feature_match = re.search(r'#\s*Feature:\s*(.+?)(?:\n|$)', content)
            feature_name = feature_match.group(1).strip() if feature_match else "Unknown Feature"

            # Parse ALL PR metadata directly from plan content
            parsed_prs = self._parse_all_prs_from_plan(content)

            if not parsed_prs:
                print("❌ No PRs found in feature plan")
                return False, 0

            # Track→Terminal mapping
            track_to_terminal = {'A': 'T1', 'B': 'T2', 'C': 'T3'}
            # Complexity→Cognition mapping
            complexity_to_cognition = {'High': 'deep', 'Medium': 'normal', 'Low': 'normal'}

            print(f"\n📋 Feature: {feature_name}")
            print(f"📋 Found {len(parsed_prs)} PRs in feature plan")
            print(f"🎯 Generating batch dispatches to staging/\n")

            # Reset internal state with new feature data
            self.state = {
                "feature": feature_name,
                "prs": [],
                "completed": [],
                "active": [],
                "blocked": [],
                "created_at": datetime.now().isoformat(),
                "updated_at": datetime.now().isoformat()
            }

            for pr_data in parsed_prs:
                self.state['prs'].append({
                    'id': pr_data['id'],
                    'title': pr_data['title_from_header'],
                    'size': pr_data.get('estimated_time', 'unknown'),
                    'dependencies': pr_data['dependencies'],
                    'skill': pr_data['skill'],
                    'status': 'queued',
                    'track': pr_data['track'],
                    'priority': pr_data['priority'],
                    'gate': pr_data['gate'],
                })

            self.save_state()

            staging_dir = self.dispatch_dir / "staging"
            staging_dir.mkdir(parents=True, exist_ok=True)

            created = 0
            skipped = 0

            for pr_data in parsed_prs:
                pr_id = pr_data['id']

                # Check if dispatch already exists in staging
                existing_files = list(staging_dir.glob(f"*{pr_id.lower()}*.md"))
                if existing_files and not force:
                    print(f"⏭️  Skipping {pr_id} (already in staging)")
                    skipped += 1
                    continue

                # Generate dispatch ID
                timestamp = datetime.now().strftime('%Y%m%d-%H%M%S')
                pr_descriptor = pr_data['title_from_header'].lower().replace(' ', '-')[:30]
                track = pr_data['track']
                terminal = track_to_terminal.get(track, 'T1')
                dispatch_id = f"{timestamp}-{pr_descriptor}-{track}"

                # Get skill (remove @ prefix for Manager Block)
                skill = pr_data['skill']
                if skill.startswith('@'):
                    skill = skill[1:]

                # Map cognition from complexity
                cognition = complexity_to_cognition.get(pr_data['complexity'], 'normal')

                # Extract full PR section from FEATURE_PLAN.md for instruction
                instruction = ""
                pr_pattern_single = f'## {pr_id}:(.+?)(?=\n##\\s+PR-|$)'
                match = re.search(pr_pattern_single, content, re.DOTALL)
                if match:
                    instruction = match.group(1).strip()
                else:
                    instruction = f"# {pr_data['title']}\n\nSee FEATURE_PLAN.md for details"

                # Build dependencies string
                deps_str = ', '.join(pr_data['dependencies']) if pr_data['dependencies'] else 'None'

                # Create dispatch markdown
                dispatch_content = f"""[[TARGET:{track}]]
Manager Block

Role: {skill}
Track: {track}
Terminal: {terminal}
Gate: {pr_data['gate']}
Priority: {pr_data['priority']}
Cognition: {cognition}
Dispatch-ID: {dispatch_id}
PR-ID: {pr_id}
Parent-Dispatch: none
On-Success: review
On-Failure: investigation
Reason: {pr_data['title_from_header']} from PR queue
Status: pending-approval

Context: [[@FEATURE_PLAN.md]]

Instruction:
{instruction}

Dependencies: {deps_str}
Size Estimate: {pr_data['estimated_time']}

[[DONE]]
"""

                dispatch_file = staging_dir / f"{dispatch_id}.md"
                dispatch_file.write_text(dispatch_content)

                print(f"✅ Created {pr_id}: {dispatch_id}")
                print(f"   Role: {skill} | Track: {track} | Terminal: {terminal}")
                print(f"   Priority: {pr_data['priority']} | Cognition: {cognition}")
                print(f"   Dependencies: {deps_str}")
                created += 1

            # Generate open items from quality gate checklist items
            oi_created = 0
            oi_script = Path(__file__).resolve().parent / "open_items_manager.py"
            if oi_script.exists():
                print(f"\n📋 Generating open items from quality gates...")
                for pr_data in parsed_prs:
                    pr_id = pr_data['id']
                    for gate_item in pr_data.get('quality_gate_items', []):
                        severity = self._classify_gate_severity(gate_item)
                        try:
                            import subprocess
                            subprocess.run([
                                sys.executable, str(oi_script), 'add',
                                '--dispatch', f'init-feature-{pr_id}',
                                '--title', gate_item,
                                '--severity', severity,
                                '--pr', pr_id,
                                '--details', f'Quality gate deliverable from {pr_data.get("gate", "implementation")}'
                            ], check=True, capture_output=True)
                            oi_created += 1
                        except subprocess.CalledProcessError as e:
                            print(f"⚠️  Failed to create OI for {pr_id}: {gate_item}")
            else:
                print(f"⚠️  open_items_manager.py not found, skipping OI generation")

            print(f"\n📊 Batch Summary:")
            print(f"   Dispatches created: {created}")
            print(f"   Dispatches skipped: {skipped}")
            print(f"   Open items created: {oi_created}")
            print(f"   Total PRs: {len(parsed_prs)}")
            print(f"\n📁 Location: {staging_dir}")
            print(f"📋 Next: Use 'staging-list' to review, then 'promote' when ready")
            print(f"📋 Open items: Use 'python3 open_items_manager.py list' to review deliverables")

            return True, created

        except Exception as e:
            print(f"❌ Batch generation failed: {e}")
            import traceback
            traceback.print_exc()
            return False, 0

    def list_staging_dispatches(self) -> None:
        """
        List all staging dispatches with dependency status
        """
        staging_dir = self.dispatch_dir / "staging"

        if not staging_dir.exists():
            print("📭 No staging directory found")
            return

        dispatch_files = sorted(staging_dir.glob("*.md"))

        if not dispatch_files:
            print("📭 No dispatches in staging")
            return

        print(f"\n📋 Staging Dispatches ({len(dispatch_files)} total)\n")

        ready = []
        waiting = []

        for dispatch_file in dispatch_files:
            metadata = self.extract_v2_metadata(dispatch_file)
            pr_id = metadata.get('pr_id', 'Unknown')

            # Check dependencies
            pr = self.get_pr(pr_id)
            if pr:
                deps_met, msg = self.check_pr_dependencies(pr_id)

                info = {
                    'pr_id': pr_id,
                    'file': dispatch_file.name,
                    'role': metadata.get('role', 'Unknown'),
                    'priority': metadata.get('priority', 'Unknown'),
                    'deps_met': deps_met,
                    'msg': msg
                }

                if deps_met:
                    ready.append(info)
                else:
                    waiting.append(info)
            else:
                # PR not in queue, probably orphaned
                print(f"⚠️  {pr_id}: No PR in queue (orphaned dispatch)")

        # Print ready dispatches
        if ready:
            print("✅ Ready to Promote (dependencies met):\n")
            for d in ready:
                print(f"   {d['pr_id']}")
                print(f"      File: {d['file']}")
                print(f"      Role: {d['role']} | Priority: {d['priority']}")
                print(f"      Status: {d['msg']}")
                print()

        # Print waiting dispatches
        if waiting:
            print("⏳ Waiting (dependencies not met):\n")
            for d in waiting:
                print(f"   {d['pr_id']}")
                print(f"      File: {d['file']}")
                print(f"      Role: {d['role']} | Priority: {d['priority']}")
                print(f"      Blocked by: {d['msg']}")
                print()

        # Summary
        print(f"📊 Summary:")
        print(f"   Ready: {len(ready)}")
        print(f"   Waiting: {len(waiting)}")
        print(f"   Total: {len(dispatch_files)}\n")

    def reject_dispatch(self, dispatch_id: str, reason: Optional[str] = None, defer_pr: Optional[str] = None) -> bool:
        """
        Reject a staging dispatch (moves to rejected/)

        Args:
            dispatch_id: Dispatch ID or filename (with or without .md)
            reason: Rejection reason for audit log
            defer_pr: Optional PR-ID to defer to

        Returns:
            True if rejected successfully
        """
        # Normalize dispatch_id
        if not dispatch_id.endswith('.md'):
            dispatch_id_file = f"{dispatch_id}.md"
        else:
            dispatch_id_file = dispatch_id
            dispatch_id = dispatch_id[:-3]

        staging_dir = self.dispatch_dir / "staging"
        rejected_dir = self.dispatch_dir / "rejected"
        staging_file = staging_dir / dispatch_id_file
        rejected_file = rejected_dir / dispatch_id_file

        if not staging_file.exists():
            print(f"❌ Staging dispatch not found: {dispatch_id}")
            return False

        try:
            # Ensure rejected directory exists
            rejected_dir.mkdir(parents=True, exist_ok=True)

            # Move to rejected (not delete)
            import shutil
            shutil.move(str(staging_file), str(rejected_file))

            # Extract PR-ID for audit
            pr_id = self.extract_v2_metadata(rejected_file).get('pr_id')

            # Log rejection
            self._log_audit({
                'timestamp': datetime.now().isoformat(),
                'action': 'reject_dispatch',
                'dispatch_id': dispatch_id,
                'pr_id': pr_id,
                'from': str(staging_file),
                'to': str(rejected_file),
                'reason': reason or 'No reason provided',
                'defer_pr': defer_pr,
                'actor': 'T0'
            })

            # SPRINT 1: Log queue event for T0 audit trail
            if pr_id:
                self.log_queue_event("reject", pr_id, dispatch_id=dispatch_id, reason=reason or 'No reason provided', defer_pr=defer_pr)

            print(f"✅ Rejected dispatch: {dispatch_id}")
            print(f"📁 Moved to: {rejected_file}")
            if reason:
                print(f"📝 Reason: {reason}")
            if defer_pr:
                print(f"⏭️  Deferred to: {defer_pr}")

            return True

        except Exception as e:
            print(f"❌ Rejection failed: {e}")
            return False


def test_dependency_resolution():
    """Test dependency resolution and validation"""
    print("\n" + "="*60)
    print("Testing Dependency Resolution")
    print("="*60)

    manager = PRQueueManager()
    manager.clear_queue()
    manager.set_feature("Dependency Testing")

    # Test 1: Simple dependency chain
    print("\n[Test 1] Simple dependency chain")
    prs = [
        {'id': 'PR-1', 'title': 'Base', 'dependencies': [], 'skill': '@backend-developer', 'size': 100},
        {'id': 'PR-2', 'title': 'Middle', 'dependencies': ['PR-1'], 'skill': '@backend-developer', 'size': 100},
        {'id': 'PR-3', 'title': 'Top', 'dependencies': ['PR-2'], 'skill': '@backend-developer', 'size': 100}
    ]
    for pr in prs:
        manager.add_pr(pr)

    # Check PR-3 dependencies before PR-1 and PR-2 are complete
    deps_met, msg = manager.check_pr_dependencies('PR-3')
    assert not deps_met, "PR-3 should not be ready (depends on PR-2)"
    print(f"  ✓ PR-3 not ready: {msg}")

    # Complete PR-1
    manager.update_pr_status('PR-1', 'completed')
    deps_met, msg = manager.check_pr_dependencies('PR-2')
    assert deps_met, "PR-2 should be ready after PR-1 completes"
    print(f"  ✓ PR-2 ready: {msg}")

    # Test 2: Execution order
    print("\n[Test 2] Execution order (topological sort)")
    success, order, error = manager.get_execution_order()
    assert success, f"Execution order should succeed: {error}"
    assert order == ['PR-1', 'PR-2', 'PR-3'], f"Order should be PR-1→PR-2→PR-3, got {order}"
    print(f"  ✓ Correct order: {' → '.join(order)}")

    # Test 3: Circular dependency detection
    print("\n[Test 3] Circular dependency detection")
    manager.clear_queue()
    circular_prs = [
        {'id': 'PR-A', 'title': 'A', 'dependencies': ['PR-C'], 'skill': '@backend-developer', 'size': 100},
        {'id': 'PR-B', 'title': 'B', 'dependencies': ['PR-A'], 'skill': '@backend-developer', 'size': 100},
        {'id': 'PR-C', 'title': 'C', 'dependencies': ['PR-B'], 'skill': '@backend-developer', 'size': 100}
    ]
    for pr in circular_prs:
        manager.add_pr(pr)

    success, order, error = manager.get_execution_order()
    assert not success, "Execution order should fail with circular dependencies"
    assert "Circular dependency" in error, f"Error should mention circular dependency: {error}"
    print(f"  ✓ Detected circular dependency: {error}")

    # Test 4: Complex dependency graph
    print("\n[Test 4] Complex dependency graph")
    manager.clear_queue()
    complex_prs = [
        {'id': 'PR-1', 'title': 'Base 1', 'dependencies': [], 'skill': '@backend-developer', 'size': 100},
        {'id': 'PR-2', 'title': 'Base 2', 'dependencies': [], 'skill': '@backend-developer', 'size': 100},
        {'id': 'PR-3', 'title': 'Depends on 1', 'dependencies': ['PR-1'], 'skill': '@backend-developer', 'size': 100},
        {'id': 'PR-4', 'title': 'Depends on 1,2', 'dependencies': ['PR-1', 'PR-2'], 'skill': '@api-developer', 'size': 100},
        {'id': 'PR-5', 'title': 'Depends on 3,4', 'dependencies': ['PR-3', 'PR-4'], 'skill': '@api-developer', 'size': 100}
    ]
    for pr in complex_prs:
        manager.add_pr(pr)

    success, order, error = manager.get_execution_order()
    assert success, f"Complex graph should have valid execution order: {error}"
    # PR-1 and PR-2 can be in any order (both have no deps)
    # PR-3 must come after PR-1
    # PR-4 must come after both PR-1 and PR-2
    # PR-5 must come last
    assert order[-1] == 'PR-5', "PR-5 should be last"
    assert order.index('PR-3') > order.index('PR-1'), "PR-3 must come after PR-1"
    assert order.index('PR-4') > order.index('PR-1') and order.index('PR-4') > order.index('PR-2'), "PR-4 must come after PR-1 and PR-2"
    print(f"  ✓ Valid complex order: {' → '.join(order)}")

    # Test 5: Invalid dependency reference
    print("\n[Test 5] Invalid dependency reference")
    manager.clear_queue()
    invalid_prs = [
        {'id': 'PR-1', 'title': 'Valid', 'dependencies': [], 'skill': '@backend-developer', 'size': 100},
        {'id': 'PR-2', 'title': 'Invalid', 'dependencies': ['PR-99'], 'skill': '@backend-developer', 'size': 100}
    ]
    for pr in invalid_prs:
        manager.add_pr(pr)

    success, order, error = manager.get_execution_order()
    assert not success, "Should fail with invalid dependency reference"
    assert "non-existent" in error, f"Error should mention non-existent PR: {error}"
    print(f"  ✓ Detected invalid reference: {error}")

    print("\n" + "="*60)
    print("✅ All dependency resolution tests passed!")
    print("="*60)


def test_feature_plan_validation():
    """Test FEATURE_PLAN.md validation"""
    print("\n" + "="*60)
    print("Testing FEATURE_PLAN.md Validation")
    print("="*60)

    manager = PRQueueManager()

    # Create valid test plan
    valid_plan = """# Feature: Test Feature

## PR-1: First PR
Dependencies: []

## PR-2: Second PR
Dependencies: [PR-1]

## PR-3: Third PR
Dependencies: [PR-1, PR-2]
"""

    # Create invalid plans
    no_feature = """## PR-1: First PR
Dependencies: []
"""

    no_deps = """# Feature: Test
## PR-1: First PR
"""

    invalid_dep = """# Feature: Test
## PR-1: First PR
Dependencies: [PR-99]
"""

    duplicate_pr = """# Feature: Test
## PR-1: First PR
Dependencies: []
## PR-1: Duplicate
Dependencies: []
"""

    # Test valid plan
    print("\n[Test 1] Valid feature plan")
    test_file = "/tmp/test_valid_plan.md"
    with open(test_file, 'w') as f:
        f.write(valid_plan)
    valid, error = manager.validate_feature_plan(test_file)
    assert valid, f"Valid plan should pass: {error}"
    print("  ✓ Valid plan accepted")

    # Test missing feature title
    print("\n[Test 2] Missing feature title")
    test_file = "/tmp/test_no_feature.md"
    with open(test_file, 'w') as f:
        f.write(no_feature)
    valid, error = manager.validate_feature_plan(test_file)
    assert not valid, "Should reject plan without feature title"
    print(f"  ✓ Rejected: {error}")

    # Test missing dependencies
    print("\n[Test 3] Missing dependencies section")
    test_file = "/tmp/test_no_deps.md"
    with open(test_file, 'w') as f:
        f.write(no_deps)
    valid, error = manager.validate_feature_plan(test_file)
    assert not valid, "Should reject plan without dependencies"
    print(f"  ✓ Rejected: {error}")

    # Test invalid dependency reference
    print("\n[Test 4] Invalid dependency reference")
    test_file = "/tmp/test_invalid_dep.md"
    with open(test_file, 'w') as f:
        f.write(invalid_dep)
    valid, error = manager.validate_feature_plan(test_file)
    assert not valid, "Should reject plan with invalid dependency"
    print(f"  ✓ Rejected: {error}")

    # Test duplicate PR IDs
    print("\n[Test 5] Duplicate PR IDs")
    test_file = "/tmp/test_duplicate.md"
    with open(test_file, 'w') as f:
        f.write(duplicate_pr)
    valid, error = manager.validate_feature_plan(test_file)
    assert not valid, "Should reject plan with duplicate PR IDs"
    print(f"  ✓ Rejected: {error}")

    print("\n" + "="*60)
    print("✅ All feature plan validation tests passed!")
    print("="*60)


def test_queue_operations():
    """Test PR queue manager operations"""
    print("Testing PR Queue Manager...")

    # Initialize manager
    manager = PRQueueManager()
    manager.clear_queue()
    manager.set_feature("User Authentication")

    # Add PRs
    prs = [
        {
            'id': 'PR-1',
            'title': 'User model',
            'size': 200,
            'dependencies': [],
            'skill': '@backend-developer',
            'status': 'queued'
        },
        {
            'id': 'PR-2',
            'title': 'JWT utilities',
            'size': 150,
            'dependencies': ['PR-1'],
            'skill': '@backend-developer',
            'status': 'queued'
        },
        {
            'id': 'PR-3',
            'title': 'Login endpoint',
            'size': 250,
            'dependencies': ['PR-1', 'PR-2'],
            'skill': '@api-developer',
            'status': 'queued'
        }
    ]

    for pr in prs:
        manager.add_pr(pr)
        print(f"Added {pr['id']}: {pr['title']}")

    # Test dependency resolution
    next_pr = manager.get_next_pr()
    assert next_pr['id'] == 'PR-1', "First PR should have no dependencies"
    print(f"Next PR: {next_pr['id']} (correct)")

    # Start PR-1
    manager.update_pr_status('PR-1', 'in_progress')
    print("PR-1 set to in_progress")

    # Complete PR-1
    manager.update_pr_status('PR-1', 'completed')
    print("PR-1 completed")

    # Now PR-2 should be available
    next_pr = manager.get_next_pr()
    assert next_pr['id'] == 'PR-2', "PR-2 should be available after PR-1 completes"
    print(f"Next PR: {next_pr['id']} (correct)")

    # Get status summary
    summary = manager.get_status_summary()
    print(f"\nStatus Summary:")
    print(f"Feature: {summary['feature']}")
    print(f"Progress: {summary['progress_percent']}%")
    print(f"Completed: {summary['completed']}/{summary['total_prs']}")

    print("\n✅ All basic tests passed!")
    print(f"Check {manager.queue_file} for visual representation")


def _ok_result(data: Optional[Dict[str, Any]] = None) -> Result:
    return result_ok(data)


def _error_result(error_code: str, error_msg: str, data: Optional[Dict[str, Any]] = None) -> Result:
    return result_error(error_code, error_msg, data)


def _result_from_bool(
    success: bool,
    *,
    error_code: str = "operation_failed",
    error_msg: str = "Operation failed",
    data: Optional[Dict[str, Any]] = None,
) -> Result:
    if success:
        return _ok_result(data)
    return _error_result(error_code, error_msg, data)


def _exit_from_result(result: Result) -> None:
    raise SystemExit(
        result_exit_code(
            result,
            error_code_map=PR_QUEUE_RESULT_EXIT_CODE_MAP,
            default_error_exit_code=EXIT_DEPENDENCY,
        )
    )


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        if sys.argv[1] == "test":
            test_queue_operations()
        elif sys.argv[1] == "test-deps":
            test_dependency_resolution()
        elif sys.argv[1] == "test-plan":
            test_feature_plan_validation()
        elif sys.argv[1] == "test-all":
            test_queue_operations()
            test_dependency_resolution()
            test_feature_plan_validation()
        elif sys.argv[1] == "dispatch":
            # Create dispatch from PR
            if len(sys.argv) < 3:
                print("❌ Usage: python pr_queue_manager.py dispatch PR-X")
                _exit_from_result(_error_result("missing_argument", "dispatch requires <pr_id>"))

            pr_id = sys.argv[2]
            manager = PRQueueManager()
            dispatch_id = manager.create_dispatch_from_pr(pr_id)

            if dispatch_id:
                print(f"\n✅ Dispatch created successfully!")
                print(f"   Dispatch ID: {dispatch_id}")
                print(f"   Status: STAGING (requires T0 promotion)")
                _exit_from_result(_ok_result({"dispatch_id": dispatch_id}))
            else:
                print(f"\n❌ Failed to create dispatch")
                _exit_from_result(_error_result("operation_failed", "Failed to create dispatch"))
        elif sys.argv[1] == "show":
            # Show staging dispatch
            if len(sys.argv) < 3:
                print("❌ Usage: python pr_queue_manager.py show <dispatch_id>")
                _exit_from_result(_error_result("missing_argument", "show requires <dispatch_id>"))

            dispatch_id = sys.argv[2]
            manager = PRQueueManager()
            success = manager.show_dispatch(dispatch_id)
            _exit_from_result(
                _result_from_bool(
                    success,
                    error_code="dispatch_not_found",
                    error_msg=f"Staging dispatch not found: {dispatch_id}",
                )
            )
        elif sys.argv[1] == "patch":
            # Patch staging dispatch - header fields, instruction body, context
            if len(sys.argv) < 4:
                print("❌ Usage: python pr_queue_manager.py patch <dispatch_id> [options]")
                print()
                print("  Header fields:")
                print("    --set key=value              Set any header field (role, track, priority, ...)")
                print()
                print("  Instruction body:")
                print("    --set-instruction \"text\"      Replace entire instruction body")
                print("    --append-instruction \"text\"   Append to instruction body")
                print("    --prepend-instruction \"text\"  Prepend to instruction body")
                print("    --instruction-file path       Replace instruction from file content")
                print()
                print("  Examples:")
                print("    patch <id> --set role=backend-developer --set priority=P0")
                print("    patch <id> --append-instruction \"Extra context: check browser_pool.py\"")
                print("    patch <id> --instruction-file /tmp/custom_instruction.md")
                print("    patch <id> --set cognition=deep --append-instruction \"High risk merge\"")
                _exit_from_result(_error_result("missing_argument", "patch requires <dispatch_id> and patch arguments"))

            dispatch_id = sys.argv[2]
            patches = {}

            # Parse all argument types
            i = 3
            while i < len(sys.argv):
                if sys.argv[i] == '--set' and i + 1 < len(sys.argv):
                    kv = sys.argv[i + 1]
                    if '=' in kv:
                        key, value = kv.split('=', 1)
                        patches[key] = value
                    i += 2
                elif sys.argv[i] == '--set-instruction' and i + 1 < len(sys.argv):
                    patches['instruction'] = sys.argv[i + 1]
                    i += 2
                elif sys.argv[i] == '--append-instruction' and i + 1 < len(sys.argv):
                    patches['append-instruction'] = sys.argv[i + 1]
                    i += 2
                elif sys.argv[i] == '--prepend-instruction' and i + 1 < len(sys.argv):
                    patches['prepend-instruction'] = sys.argv[i + 1]
                    i += 2
                elif sys.argv[i] == '--instruction-file' and i + 1 < len(sys.argv):
                    patches['instruction-file'] = sys.argv[i + 1]
                    i += 2
                else:
                    i += 1

            if not patches:
                print("❌ No patches provided. Use --set, --set-instruction, --append-instruction, etc.")
                _exit_from_result(_error_result("missing_argument", "No patch arguments provided"))

            manager = PRQueueManager()
            success = manager.patch_dispatch(dispatch_id, patches)
            _exit_from_result(
                _result_from_bool(
                    success,
                    error_code="operation_failed",
                    error_msg=f"Failed to patch dispatch: {dispatch_id}",
                )
            )
        elif sys.argv[1] == "promote":
            # Promote staging dispatch to queue
            if len(sys.argv) < 3:
                print("❌ Usage: python pr_queue_manager.py promote <dispatch_id> [--force]")
                _exit_from_result(_error_result("missing_argument", "promote requires <dispatch_id>"))

            dispatch_id = sys.argv[2]
            force = '--force' in sys.argv
            manager = PRQueueManager()
            success = manager.promote_dispatch(dispatch_id, force=force)
            _exit_from_result(
                _result_from_bool(
                    success,
                    error_code="operation_failed",
                    error_msg=f"Failed to promote dispatch: {dispatch_id}",
                )
            )
        elif sys.argv[1] == "status":
            # Show PR queue status
            manager = PRQueueManager()
            vnx_state_file = manager.vnx_state_file

            if vnx_state_file.exists():
                with open(vnx_state_file, 'r') as f:
                    state = yaml.safe_load(f)

                print(f"📊 PR Queue Status")
                print(f"=" * 50)
                active_feature_name = state.get('active_feature', {}).get('name')
                if active_feature_name:
                    print(f"Active Feature: {active_feature_name}")
                else:
                    print(f"Active Feature: None")
                print(f"Completed PRs: {len(state.get('completed_prs', []))}")
                in_prog = state.get('in_progress') or []
                print(f"In Progress: {', '.join(in_prog) if in_prog else 'None'}")
                print(f"Blocked: {len(state.get('blocked', []))} PRs")

                # Show next available only if there are uncompleted PRs
                total_prs = len(state.get('execution_order', []))
                completed_count = len(state.get('completed_prs', []))
                remaining_prs = total_prs - completed_count
                if remaining_prs > 0:
                    print(f"Remaining PRs: {remaining_prs}/{total_prs}")

                if state.get('next_available'):
                    print(f"\n📋 Next PRs to work on:")
                    for pr_id in state['next_available'][:3]:
                        print(f"  • {pr_id}")
                    if len(state['next_available']) > 3:
                        print(f"  ... and {len(state['next_available']) - 3} more")

                print(f"\n⏰ Last updated: {state.get('updated_at', 'Unknown')}")
            else:
                print("❌ No PR queue state found")
                print(f"   Expected at: {vnx_state_file}")
            _exit_from_result(_ok_result())

        elif sys.argv[1] == "list":
            # List all PRs in the queue
            manager = PRQueueManager()

            # Load the feature plan to get PR details
            if manager.state.get('feature'):
                print(f"\n📋 PR Queue for: {manager.state['feature']}")
                print("=" * 50)

                # Show execution order
                success, execution_order, _ = manager.get_execution_order()
                if success and execution_order:
                    print("\n📊 Execution Order:")
                    for i, pr_id in enumerate(execution_order, 1):
                        status = "✅ DONE" if pr_id in manager.state.get('completed', []) else "⏳ PENDING"
                        print(f"  {i}. {pr_id} - {status}")

                # Show active PRs (parallel tracks)
                active_list = manager.state.get('active', [])
                if active_list:
                    print(f"\n🔄 Currently Active: {', '.join(active_list)}")

                # Show blocked PRs
                if manager.state.get('blocked'):
                    print(f"\n🚫 Blocked PRs:")
                    for pr_id in manager.state['blocked']:
                        print(f"  • {pr_id}")
            else:
                print("ℹ️  No active feature in queue")
            _exit_from_result(_ok_result())

        elif sys.argv[1] == "complete":
            # Mark PR as completed
            if len(sys.argv) < 3:
                print("❌ Usage: python pr_queue_manager.py complete PR-X")
                _exit_from_result(_error_result("missing_argument", "complete requires <pr_id>"))

            pr_id = sys.argv[2]
            manager = PRQueueManager()

            # Validate PR exists in execution order
            success, execution_order, _ = manager.get_execution_order()
            if not success or pr_id not in execution_order:
                print(f"❌ PR not found in execution order: {pr_id}")
                print(f"   Valid PRs: {', '.join(execution_order) if success else 'None'}")
                _exit_from_result(_error_result("pr_not_found", f"PR not found in execution order: {pr_id}"))

            # Check if already completed
            if pr_id in manager.state.get('completed', []):
                print(f"ℹ️  {pr_id} already marked as completed")
                _exit_from_result(_ok_result({"pr_id": pr_id, "already_completed": True}))

            # Update PR status (updates pr['status'] and internal state)
            manager.update_pr_status(pr_id, 'completed')

            manager.save_state()
            print(f"✅ Marked {pr_id} as completed")
            print(f"   Completed PRs: {len(manager.state['completed'])}/{len(execution_order)}")
            _exit_from_result(_ok_result({"pr_id": pr_id, "status": "completed"}))

        elif sys.argv[1] == "log-completion-attempt":
            # SPRINT 2: Log auto-completion attempt for debugging
            if len(sys.argv) < 7:
                print("❌ Usage: python pr_queue_manager.py log-completion-attempt <pr_id> <dispatch_id> <success> <reason> <extraction_method>")
                _exit_from_result(
                    _error_result(
                        "missing_argument",
                        "log-completion-attempt requires <pr_id> <dispatch_id> <success> <reason> <extraction_method>",
                    )
                )

            pr_id = sys.argv[2]
            dispatch_id = sys.argv[3]
            success = sys.argv[4].lower() == 'true'
            reason = sys.argv[5]
            extraction_method = sys.argv[6]

            manager = PRQueueManager()
            manager.log_queue_event(
                event="completion_attempt",
                pr_id=pr_id,
                dispatch_id=dispatch_id,
                auto_completed=success,
                reason=reason,
                extraction_method=extraction_method,
            )
            # Silent success - this is called from receipt processor
            _exit_from_result(_ok_result({"pr_id": pr_id, "dispatch_id": dispatch_id}))

        elif sys.argv[1] == "start":
            # Mark PR as in progress
            if len(sys.argv) < 3:
                print("❌ Usage: python pr_queue_manager.py start PR-X")
                _exit_from_result(_error_result("missing_argument", "start requires <pr_id>"))

            pr_id = sys.argv[2]
            manager = PRQueueManager()

            # Validate PR exists in execution order
            success, execution_order, _ = manager.get_execution_order()
            if not success or pr_id not in execution_order:
                print(f"❌ PR not found in execution order: {pr_id}")
                print(f"   Valid PRs: {', '.join(execution_order) if success else 'None'}")
                _exit_from_result(_error_result("pr_not_found", f"PR not found in execution order: {pr_id}"))

            # Check if already completed
            if pr_id in manager.state.get('completed', []):
                print(f"⚠️  {pr_id} is already completed!")
                _exit_from_result(_error_result("invalid_argument", f"PR already completed: {pr_id}"))

            # Check if this PR is already in progress
            current_active = manager.state.get('active', [])
            if pr_id in current_active:
                print(f"ℹ️  {pr_id} is already in progress")
                _exit_from_result(_ok_result({"pr_id": pr_id, "status": "in_progress", "already_active": True}))

            # Update PR status (updates pr['status'] and internal state)
            manager.update_pr_status(pr_id, 'in_progress')

            manager.save_state()
            print(f"✅ Started {pr_id}")
            print(f"   Status: In Progress")
            _exit_from_result(_ok_result({"pr_id": pr_id, "status": "in_progress"}))

        elif sys.argv[1] == "reject":
            # Reject staging dispatch
            if len(sys.argv) < 3:
                print("❌ Usage: python pr_queue_manager.py reject <dispatch_id> --reason \"...\" [--defer PR-X]")
                _exit_from_result(_error_result("missing_argument", "reject requires <dispatch_id>"))

            dispatch_id = sys.argv[2]
            reason = None
            defer_pr = None

            # Parse --reason and --defer
            i = 3
            while i < len(sys.argv):
                if sys.argv[i] == '--reason' and i + 1 < len(sys.argv):
                    reason = sys.argv[i + 1]
                    i += 2
                elif sys.argv[i] == '--defer' and i + 1 < len(sys.argv):
                    defer_pr = sys.argv[i + 1]
                    i += 2
                else:
                    i += 1

            manager = PRQueueManager()
            success = manager.reject_dispatch(dispatch_id, reason, defer_pr)
            _exit_from_result(
                _result_from_bool(
                    success,
                    error_code="operation_failed",
                    error_msg=f"Failed to reject dispatch: {dispatch_id}",
                )
            )

        elif sys.argv[1] == "init-feature":
            # NEW: Batch generate all dispatches from FEATURE_PLAN.md
            if len(sys.argv) < 3:
                print("❌ Usage: python pr_queue_manager.py init-feature <feature_plan.md> [--force]")
                _exit_from_result(_error_result("missing_argument", "init-feature requires <feature_plan_path>"))

            feature_plan_path = sys.argv[2]
            force = '--force' in sys.argv

            manager = PRQueueManager()
            success, count = manager.init_feature_batch(feature_plan_path, force=force)
            _exit_from_result(
                _result_from_bool(
                    success,
                    error_code="invalid_feature_plan",
                    error_msg=f"Failed to initialize feature batch: {feature_plan_path}",
                    data={"created_dispatches": count},
                )
            )

        elif sys.argv[1] == "staging-list":
            # NEW: List all staging dispatches with dependency status
            manager = PRQueueManager()
            manager.list_staging_dispatches()
            _exit_from_result(_ok_result())

        else:
            print(f"Unknown command: {sys.argv[1]}")
            _exit_from_result(
                _error_result(
                    "unknown_command",
                    f"Unknown command: {sys.argv[1]}",
                    {"command": sys.argv[1]},
                )
            )
    else:
        print("Usage:")
        print("  PR Queue Status:")
        print("  python pr_queue_manager.py status       # Show PR queue status")
        print("  python pr_queue_manager.py list         # List all PRs in execution order")
        print("")
        print("  PR Lifecycle:")
        print("  python pr_queue_manager.py start PR-X   # Mark PR as in progress")
        print("  python pr_queue_manager.py complete PR-X # Mark PR as completed")
        print("")
        print("  🆕 Batch Workflow (Option 3):")
        print("  python pr_queue_manager.py init-feature FEATURE_PLAN.md [--force]  # Generate all dispatches to staging")
        print("  python pr_queue_manager.py staging-list  # List staging with dependency status")
        print("  python pr_queue_manager.py show <id>    # Show staging dispatch details")
        print("  python pr_queue_manager.py patch <id> --set key=value  # Patch staging dispatch fields")
        print("  python pr_queue_manager.py promote <id> [--force]  # Promote staging to queue (with dep check)")
        print("  python pr_queue_manager.py reject <id> --reason \"...\" [--defer PR-X]  # Reject staging dispatch")
        print("")
        print("  Legacy Single Dispatch:")
        print("  python pr_queue_manager.py dispatch PR-X # Create single dispatch (not batch)")
        print("")
        print("  Test commands:")
        print("  python pr_queue_manager.py test         # Basic queue operations")
        print("  python pr_queue_manager.py test-deps    # Dependency resolution")
        print("  python pr_queue_manager.py test-plan    # Feature plan validation")
        print("  python pr_queue_manager.py test-all     # All tests")
        print("\nOr import and use PRQueueManager class directly")
