#!/usr/bin/env python3
"""Feature state machine — parses FEATURE_PLAN.md and derives next dispatchable task.

Used by build_t0_state.py to populate the `feature_state` section of t0_state.json,
giving the headless T0 decision loop structured feature context.

Supports two PR header formats:
  - F46-style:  ### F46-PR1: Title (used in F46-F50 plan)
  - Legacy:     ## PR-N: Title (used in older feature plans)
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class PRStatus:
    pr_id: str                      # e.g. "F46-PR2" or "PR-1"
    title: str
    track_letter: Optional[str]     # "A", "B", or "C"
    role: Optional[str]             # "backend-developer", "test-engineer", etc.
    is_completed: bool              # True if all checkboxes are [x]
    total_checkboxes: int
    checked_checkboxes: int
    next_task_description: str      # first line of section body (trim header + metadata)


@dataclass
class FeatureState:
    feature_name: str
    total_prs: int
    completed_prs: int
    current_pr: Optional[str]       # first non-completed PR id
    next_task: Optional[str]        # description of current_pr from FEATURE_PLAN.md
    assigned_track: Optional[str]   # "A", "B", or "C"
    assigned_role: Optional[str]    # "backend-developer", "test-engineer", etc.
    status: str                     # "planned" | "in_progress" | "completed"
    completion_pct: int             # 0-100

    def as_dict(self) -> Dict[str, Any]:
        return {
            "feature_name": self.feature_name,
            "total_prs": self.total_prs,
            "completed_prs": self.completed_prs,
            "current_pr": self.current_pr,
            "next_task": self.next_task,
            "assigned_track": self.assigned_track,
            "assigned_role": self.assigned_role,
            "status": self.status,
            "completion_pct": self.completion_pct,
        }


# ---------------------------------------------------------------------------
# Regex patterns
# ---------------------------------------------------------------------------

# Matches ### F46-PR1: Title  or  ### F46-PR1 Title
_PR_HEADER_F46 = re.compile(
    r"^###\s+(F\d+-PR\d+)[:\s]+(.+)$",
    re.MULTILINE,
)

# Matches ## PR-1: Title  or  ## PR-1 Title (legacy)
_PR_HEADER_LEGACY = re.compile(
    r"^##\s+(PR-\d+)[:\s]+(.+)$",
    re.MULTILINE,
)

# Track line: **Track**: A (T1 backend-developer)  or just  **Track**: A
_TRACK_LINE = re.compile(
    r"\*\*Track\*\*:\s*([ABC])\s*(?:\(T[0-3]\s+([\w-]+)\))?",
    re.IGNORECASE,
)

# Checkbox lines
_CHECKED = re.compile(r"^\s*-\s+\[x\]", re.IGNORECASE | re.MULTILINE)
_UNCHECKED = re.compile(r"^\s*-\s+\[ \]", re.MULTILINE)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _extract_pr_sections(content: str) -> List[tuple[str, str, str]]:
    """Return list of (pr_id, title, section_body) tuples from markdown content."""
    # Try F46-style headers first; fall back to legacy if none found
    matches = list(_PR_HEADER_F46.finditer(content))
    if not matches:
        matches = list(_PR_HEADER_LEGACY.finditer(content))
    if not matches:
        return []

    sections: List[tuple[str, str, str]] = []
    for i, match in enumerate(matches):
        pr_id = match.group(1).strip()
        title = match.group(2).strip()
        body_start = match.end()
        body_end = matches[i + 1].start() if i + 1 < len(matches) else len(content)
        body = content[body_start:body_end]
        sections.append((pr_id, title, body))

    return sections


def _parse_track(body: str) -> tuple[Optional[str], Optional[str]]:
    """Extract (track_letter, role) from a PR section body."""
    m = _TRACK_LINE.search(body)
    if not m:
        return None, None
    track = m.group(1).upper()
    role = m.group(2) if m.group(2) else None
    return track, role


def _count_checkboxes(body: str) -> tuple[int, int]:
    """Return (total_checkboxes, checked_count) from a PR section body."""
    checked = len(_CHECKED.findall(body))
    unchecked = len(_UNCHECKED.findall(body))
    total = checked + unchecked
    return total, checked


def _extract_description(pr_id: str, title: str, body: str) -> str:
    """Build a concise task description for the next_task field."""
    # Strip metadata lines (bold key-value pairs, status, dependencies)
    _META = re.compile(
        r"^\s*\*\*(?:Track|Status|Estimated LOC|Dependencies|Priority|"
        r"Skill|Risk-Class|Merge-Policy|Review-Stack)\*\*:.*$",
        re.MULTILINE | re.IGNORECASE,
    )
    cleaned = _META.sub("", body).strip()
    # Take first non-empty paragraph line (up to 120 chars)
    for line in cleaned.splitlines():
        line = line.strip()
        if line and not line.startswith("#") and not line.startswith("-"):
            return f"{pr_id}: {title} — {line[:120]}"
    return f"{pr_id}: {title}"


def _parse_pr_status(pr_id: str, title: str, body: str) -> PRStatus:
    track, role = _parse_track(body)
    total, checked = _count_checkboxes(body)
    # A PR is completed when all its checkboxes are checked (and there is at least one)
    is_completed = total > 0 and checked == total
    description = _extract_description(pr_id, title, body)
    return PRStatus(
        pr_id=pr_id,
        title=title,
        track_letter=track,
        role=role,
        is_completed=is_completed,
        total_checkboxes=total,
        checked_checkboxes=checked,
        next_task_description=description,
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def parse_feature_plan(path: Path) -> FeatureState:
    """Parse FEATURE_PLAN.md and return a FeatureState.

    Handles both F46-style (### F46-PR1: Title) and legacy (## PR-1: Title) headers.
    Completion is derived from checkbox state: [x] = checked, [ ] = pending.
    """
    try:
        content = path.read_text(encoding="utf-8")
    except OSError:
        return FeatureState(
            feature_name="unknown",
            total_prs=0,
            completed_prs=0,
            current_pr=None,
            next_task=None,
            assigned_track=None,
            assigned_role=None,
            status="planned",
            completion_pct=0,
        )

    # Feature name from first H1
    feature_name = "Unknown"
    h1 = re.search(r"^#\s+(.+)$", content, re.MULTILINE)
    if h1:
        feature_name = h1.group(1).strip()

    sections = _extract_pr_sections(content)
    if not sections:
        return FeatureState(
            feature_name=feature_name,
            total_prs=0,
            completed_prs=0,
            current_pr=None,
            next_task=None,
            assigned_track=None,
            assigned_role=None,
            status="planned",
            completion_pct=0,
        )

    pr_statuses: List[PRStatus] = [
        _parse_pr_status(pr_id, title, body)
        for pr_id, title, body in sections
    ]

    total = len(pr_statuses)
    completed = sum(1 for p in pr_statuses if p.is_completed)
    completion_pct = int(completed * 100 / total) if total > 0 else 0

    # Current PR = first non-completed
    current: Optional[PRStatus] = None
    for p in pr_statuses:
        if not p.is_completed:
            current = p
            break

    if completed == total:
        status = "completed"
    elif completed > 0:
        status = "in_progress"
    else:
        status = "planned"

    return FeatureState(
        feature_name=feature_name,
        total_prs=total,
        completed_prs=completed,
        current_pr=current.pr_id if current else None,
        next_task=current.next_task_description if current else None,
        assigned_track=current.track_letter if current else None,
        assigned_role=current.role if current else None,
        status=status,
        completion_pct=completion_pct,
    )


def get_next_dispatchable(state_dir: Path) -> Optional[Dict[str, Any]]:
    """Combine feature state + terminal availability to find next dispatchable task.

    Reads t0_state.json from state_dir and FEATURE_PLAN.md from the project root
    (two directories up from state_dir, i.e. $VNX_DATA_DIR/../../).

    Returns a dict with terminal, track, task_description, pr_id, role — or None
    if no task is ready (all completed, or required terminal is busy/leased).
    """
    # Locate FEATURE_PLAN.md relative to state_dir
    # state_dir is typically $VNX_STATE_DIR
    # Walk up to find FEATURE_PLAN.md
    feature_plan: Optional[Path] = None
    candidate = state_dir
    for _ in range(6):
        candidate = candidate.parent
        fp = candidate / "FEATURE_PLAN.md"
        if fp.exists():
            feature_plan = fp
            break

    if feature_plan is None:
        return None

    feature_state = parse_feature_plan(feature_plan)
    if feature_state.current_pr is None:
        return None

    track = feature_state.assigned_track  # "A", "B", or "C"
    if track is None:
        return None

    # Map track letter to terminal
    _TRACK_TO_TERMINAL = {"A": "T1", "B": "T2", "C": "T3"}
    terminal = _TRACK_TO_TERMINAL.get(track)
    if terminal is None:
        return None

    # Check terminal availability from t0_state.json
    t0_state_path = state_dir / "t0_state.json"
    if t0_state_path.exists():
        try:
            t0_state = json.loads(t0_state_path.read_text(encoding="utf-8"))
            terminals = t0_state.get("terminals") or {}
            terminal_info = terminals.get(terminal) or {}
            lease_state = terminal_info.get("lease_state", "idle")
            if lease_state == "leased":
                return None  # terminal busy
        except Exception:
            pass  # proceed optimistically if state unreadable

    return {
        "terminal": terminal,
        "track": track,
        "task_description": feature_state.next_task,
        "pr_id": feature_state.current_pr,
        "role": feature_state.assigned_role,
    }
