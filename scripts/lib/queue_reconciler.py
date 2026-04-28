#!/usr/bin/env python3
"""Queue state reconciler — derives PR queue status from canonical runtime evidence.

Implements the derivation algorithm from docs/core/70_QUEUE_TRUTH_CONTRACT.md.

Priority hierarchy (higher = more authoritative):
  1. FEATURE_PLAN.md          — valid PR IDs and dependency graph
  2. Dispatch filesystem       — active/completed/pending/staging/rejected
  3. Receipts                  — terminal event confirmation
  4. Queue projection files    — cached views only (never primary truth)
  5. Progress projections      — advisory only

Usage:
  from queue_reconciler import QueueReconciler
  r = QueueReconciler(dispatch_dir, receipts_file, feature_plan_path)
  result = r.reconcile()
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

DISPATCH_STATE_DIRS = ("active", "completed", "pending", "staging", "rejected")

# Terminal receipt event types that confirm a dispatch finished
RECEIPT_TERMINAL_EVENTS = frozenset({
    "task_complete",
    "task_finished",
    "done",
    "completed",
    "worker_complete",
    "dispatch_complete",
})


@dataclass
class PREntry:
    pr_id: str
    title: str
    dependencies: List[str]
    track: str = "?"
    gate: str = ""
    skill: str = ""
    risk_class: str = "medium"
    merge_policy: str = "human"
    review_stack: List[str] = field(default_factory=list)


@dataclass
class DriftWarning:
    pr_id: str
    severity: str  # "blocking" | "warning" | "info"
    derived_state: str
    projected_state: str
    message: str


@dataclass
class PRReconciled:
    pr_id: str
    state: str  # completed | active | pending | blocked
    provenance: Dict[str, Any]
    metadata: Dict[str, Any]


@dataclass
class ReconcileResult:
    feature_name: str
    prs: List[PRReconciled]
    drift_warnings: List[DriftWarning]
    reconciled_at: str
    has_blocking_drift: bool

    def as_dict(self) -> Dict[str, Any]:
        return {
            "feature_name": self.feature_name,
            "reconciled_at": self.reconciled_at,
            "has_blocking_drift": self.has_blocking_drift,
            "prs": [
                {
                    "pr_id": p.pr_id,
                    "state": p.state,
                    "provenance": p.provenance,
                    "metadata": p.metadata,
                }
                for p in self.prs
            ],
            "drift_warnings": [
                {
                    "pr_id": w.pr_id,
                    "severity": w.severity,
                    "derived_state": w.derived_state,
                    "projected_state": w.projected_state,
                    "message": w.message,
                }
                for w in self.drift_warnings
            ],
        }


# ---------------------------------------------------------------------------
# Feature plan parser
# ---------------------------------------------------------------------------

def parse_feature_plan(plan_path: Path) -> tuple[str, List[PREntry]]:
    """Parse FEATURE_PLAN.md and return (feature_name, list of PREntry).

    Supports both legacy (## PR-N: Title) and new (### F<N>-PR<M>: Title) formats,
    including mixed files where both appear.
    """
    content = plan_path.read_text(encoding="utf-8")

    # Feature name — legacy '# Feature: X' first, else first document heading
    feature_name = "Unknown"
    name_match = re.search(r"^#\s+Feature:\s*(.+)$", content, re.MULTILINE)
    if name_match:
        feature_name = name_match.group(1).strip()
    else:
        first_heading = re.search(r"^#\s+(.+)$", content, re.MULTILINE)
        if first_heading:
            feature_name = first_heading.group(1).strip()

    # Collect PR section headers from both formats, ordered by file position
    legacy_pat = re.compile(r"^##\s+(PR-\d+):\s*(.+)$", re.MULTILINE)
    new_pat = re.compile(r"^###\s+(F\d+-PR\d+):\s*(.+)$", re.MULTILINE)

    # (file_position, pr_id, title, section_content_start)
    all_pr_headers: List[tuple[int, str, str, int]] = []
    for m in (*legacy_pat.finditer(content), *new_pat.finditer(content)):
        all_pr_headers.append((m.start(), m.group(1), m.group(2).strip(), m.end()))
    all_pr_headers.sort(key=lambda x: x[0])

    prs: List[PREntry] = []
    for i, (_, pr_id, title, section_start) in enumerate(all_pr_headers):
        section_end = all_pr_headers[i + 1][0] if i + 1 < len(all_pr_headers) else len(content)
        section = content[section_start:section_end]

        def extract(pattern: str, default: str = "") -> str:
            m = re.search(pattern, section, re.MULTILINE | re.IGNORECASE)
            return m.group(1).strip() if m else default

        # Dependencies: [PR-0, PR-1] or []
        deps_raw = extract(r"\*\*Dependencies\*\*:\s*\[([^\]]*)\]")
        if not deps_raw:
            deps_raw = extract(r"^Dependencies:\s*\[([^\]]*)\]")
        dependencies: List[str] = []
        if deps_raw.strip():
            dependencies = [d.strip() for d in deps_raw.split(",") if d.strip()]

        track = extract(r"\*\*Track\*\*:\s*(\S+)", "?")
        gate = extract(r"\*\*Quality Gate\*\*\s*\n[`]([^`]+)[`]")
        if not gate:
            g = re.search(r"`(gate_\w+)`", section)
            gate = g.group(1) if g else ""
        skill = extract(r"\*\*Skill\*\*:\s*(\S+)")
        risk_class = extract(r"\*\*Risk-Class\*\*:\s*(\S+)", "medium").lower()
        merge_policy = extract(r"\*\*Merge-Policy\*\*:\s*(\S+)", "human").lower()
        review_raw = extract(r"\*\*Review-Stack\*\*:\s*(.+)")
        review_stack = [r.strip() for r in review_raw.split(",") if r.strip()] if review_raw else []

        prs.append(
            PREntry(
                pr_id=pr_id,
                title=title,
                dependencies=dependencies,
                track=track,
                gate=gate,
                skill=skill,
                risk_class=risk_class,
                merge_policy=merge_policy,
                review_stack=review_stack,
            )
        )

    return feature_name, prs


# ---------------------------------------------------------------------------
# Dispatch directory scanner
# ---------------------------------------------------------------------------

def _parse_dispatch_pr_id(dispatch_file: Path) -> Optional[str]:
    """Extract PR-ID from a dispatch file's metadata block."""
    try:
        content = dispatch_file.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    m = re.search(r"^PR-ID:\s*(\S+)", content, re.MULTILINE)
    return m.group(1).strip() if m else None


def _parse_dispatch_id(dispatch_file: Path) -> Optional[str]:
    """Extract Dispatch-ID from a dispatch file's metadata block."""
    try:
        content = dispatch_file.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    m = re.search(r"^Dispatch-ID:\s*(\S+)", content, re.MULTILINE)
    return m.group(1).strip() if m else None


@dataclass
class DispatchRecord:
    dispatch_id: str
    pr_id: Optional[str]
    dir_state: str  # active | completed | pending | staging | rejected
    file_path: Path


def scan_dispatch_dirs(dispatch_dir: Path) -> List[DispatchRecord]:
    """Scan all dispatch state directories and return records."""
    records: List[DispatchRecord] = []
    for dir_name in DISPATCH_STATE_DIRS:
        sub = dispatch_dir / dir_name
        if not sub.is_dir():
            continue
        for f in sorted(sub.iterdir()):
            if not f.is_file() or not f.suffix == ".md":
                continue
            dispatch_id = f.stem  # filename without extension
            pr_id = _parse_dispatch_pr_id(f)
            records.append(
                DispatchRecord(
                    dispatch_id=dispatch_id,
                    pr_id=pr_id,
                    dir_state=dir_name,
                    file_path=f,
                )
            )
    return records


# ---------------------------------------------------------------------------
# Receipt scanner
# ---------------------------------------------------------------------------

def load_receipt_dispatch_ids(receipts_file: Path) -> Set[str]:
    """Return the set of dispatch IDs that have a terminal receipt event."""
    confirmed: Set[str] = set()
    if not receipts_file.is_file():
        return confirmed
    try:
        for line in receipts_file.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            event_type = record.get("event_type", "")
            status = record.get("status", "")
            # Terminal event OR explicit success status on a completion-type event
            is_terminal = event_type in RECEIPT_TERMINAL_EVENTS
            is_success_completion = status in {"success", "pass", "completed", "done"}
            dispatch_id = (
                record.get("dispatch_id")
                or record.get("Dispatch-ID")
                or record.get("id")
            )
            if dispatch_id and (is_terminal or (is_success_completion and event_type)):
                confirmed.add(str(dispatch_id))
    except OSError:
        pass
    return confirmed


# ---------------------------------------------------------------------------
# Core reconciler
# ---------------------------------------------------------------------------

class QueueReconciler:
    """Derives PR queue state from canonical runtime evidence.

    Args:
        dispatch_dir:    Path to $VNX_DISPATCH_DIR
        receipts_file:   Path to $VNX_STATE_DIR/t0_receipts.ndjson
        feature_plan:    Path to FEATURE_PLAN.md
        projection_file: Path to pr_queue_state.json (for drift detection)
    """

    def __init__(
        self,
        dispatch_dir: Path,
        receipts_file: Path,
        feature_plan: Path,
        projection_file: Optional[Path] = None,
    ) -> None:
        self.dispatch_dir = dispatch_dir
        self.receipts_file = receipts_file
        self.feature_plan = feature_plan
        self.projection_file = projection_file

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def reconcile(self) -> ReconcileResult:
        """Run full reconciliation and return the result."""
        feature_name, plan_prs = parse_feature_plan(self.feature_plan)
        dispatch_records = scan_dispatch_dirs(self.dispatch_dir)
        confirmed_dispatch_ids = load_receipt_dispatch_ids(self.receipts_file)
        projected = self._load_projection()

        # Build lookup: pr_id -> list of DispatchRecord
        pr_dispatches: Dict[str, List[DispatchRecord]] = {pr.pr_id: [] for pr in plan_prs}
        foreign_dispatches: List[DispatchRecord] = []
        valid_pr_ids = {pr.pr_id for pr in plan_prs}

        for rec in dispatch_records:
            if rec.pr_id and rec.pr_id in valid_pr_ids:
                pr_dispatches[rec.pr_id].append(rec)
            elif rec.pr_id:
                foreign_dispatches.append(rec)

        # Derive state for each PR (must process in dependency order for
        # blocked/pending resolution based on derived state of dependencies)
        ordered_pr_ids = _topological_sort([p.pr_id for p in plan_prs], {p.pr_id: p.dependencies for p in plan_prs})
        pr_by_id = {p.pr_id: p for p in plan_prs}
        derived_states: Dict[str, str] = {}
        reconciled_prs: List[PRReconciled] = []

        for pr_id in ordered_pr_ids:
            pr_entry = pr_by_id[pr_id]
            dispatches = pr_dispatches.get(pr_id, [])
            state, provenance = self._derive_state(
                pr_id=pr_id,
                dispatches=dispatches,
                confirmed_dispatch_ids=confirmed_dispatch_ids,
                dependency_states={dep: derived_states.get(dep, "blocked") for dep in pr_entry.dependencies},
            )
            derived_states[pr_id] = state

            reconciled_prs.append(
                PRReconciled(
                    pr_id=pr_id,
                    state=state,
                    provenance=provenance,
                    metadata={
                        "title": pr_entry.title,
                        "track": pr_entry.track,
                        "dependencies": pr_entry.dependencies,
                        "gate": pr_entry.gate,
                        "skill": pr_entry.skill,
                        "risk_class": pr_entry.risk_class,
                        "merge_policy": pr_entry.merge_policy,
                        "review_stack": pr_entry.review_stack,
                    },
                )
            )

        # Drift detection
        drift_warnings = self._detect_drift(reconciled_prs, projected)

        # Warn about foreign dispatches (EC-4)
        for fd in foreign_dispatches:
            drift_warnings.append(
                DriftWarning(
                    pr_id=fd.pr_id or "(unknown)",
                    severity="warning",
                    derived_state="(not in feature plan)",
                    projected_state="(unknown)",
                    message=f"Dispatch {fd.dispatch_id} references PR {fd.pr_id!r} which is not in FEATURE_PLAN.md. Stale or foreign dispatch.",
                )
            )

        has_blocking = any(w.severity == "blocking" for w in drift_warnings)

        return ReconcileResult(
            feature_name=feature_name,
            prs=reconciled_prs,
            drift_warnings=drift_warnings,
            reconciled_at=datetime.now(tz=timezone.utc).isoformat(),
            has_blocking_drift=has_blocking,
        )

    # ------------------------------------------------------------------
    # State derivation (Section 3.2 algorithm from contract)
    # ------------------------------------------------------------------

    def _derive_state(
        self,
        pr_id: str,
        dispatches: List[DispatchRecord],
        confirmed_dispatch_ids: Set[str],
        dependency_states: Dict[str, str],
    ) -> tuple[str, Dict[str, Any]]:
        """Apply the contract's 5-step derivation algorithm.

        Returns (state, provenance_dict).
        """
        # Step 2: active/ → state = active
        active_dispatches = [d for d in dispatches if d.dir_state == "active"]
        if active_dispatches:
            d = active_dispatches[0]
            return "active", {
                "source": "dispatch_filesystem",
                "dir": "active",
                "evidence": str(d.file_path),
                "dispatch_id": d.dispatch_id,
                "receipt_confirmed": False,
            }

        # Step 3: completed/ with successful outcome → state = completed
        completed_dispatches = [d for d in dispatches if d.dir_state == "completed"]
        if completed_dispatches:
            # Any completed dispatch is sufficient (multiple attempts allowed)
            d = completed_dispatches[0]
            receipt_confirmed = d.dispatch_id in confirmed_dispatch_ids
            return "completed", {
                "source": "dispatch_filesystem",
                "dir": "completed",
                "evidence": str(d.file_path),
                "dispatch_id": d.dispatch_id,
                "receipt_confirmed": receipt_confirmed,
                "unconfirmed_completion": not receipt_confirmed,
            }

        # Step 4: all dependencies completed → pending
        all_deps_completed = all(
            dependency_states.get(dep) == "completed"
            for dep in dependency_states
        )
        if all_deps_completed:
            # Check if there is a dispatch in pending/staging
            inflight_dispatches = [
                d for d in dispatches if d.dir_state in ("pending", "staging")
            ]
            evidence_dispatch = inflight_dispatches[0] if inflight_dispatches else None
            return "pending", {
                "source": "feature_plan_dependency_graph",
                "dir": inflight_dispatches[0].dir_state if evidence_dispatch else None,
                "evidence": str(evidence_dispatch.file_path) if evidence_dispatch else None,
                "dispatch_id": evidence_dispatch.dispatch_id if evidence_dispatch else None,
                "receipt_confirmed": False,
                "dependencies_satisfied": list(dependency_states.keys()),
            }

        # Step 5: blocked
        unmet = [dep for dep, s in dependency_states.items() if s != "completed"]
        return "blocked", {
            "source": "feature_plan_dependency_graph",
            "dir": None,
            "evidence": None,
            "dispatch_id": None,
            "receipt_confirmed": False,
            "blocking_dependencies": unmet,
        }

    # ------------------------------------------------------------------
    # Drift detection (Section 4)
    # ------------------------------------------------------------------

    def _load_projection(self) -> Dict[str, str]:
        """Load projected state from pr_queue_state.json.

        Returns dict of pr_id -> state string.
        """
        if not self.projection_file or not self.projection_file.is_file():
            return {}
        try:
            data = json.loads(self.projection_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}

        projected: Dict[str, str] = {}

        # Handle the pr_queue_state.json format:
        # { "prs": [...], "completed": [...], "active": [...], ... }
        if "prs" in data:
            completed_set = set(data.get("completed", []))
            active_set = set(data.get("active", []))
            blocked_set = set(data.get("blocked", []))
            for pr in data.get("prs", []):
                pid = pr.get("id") or pr.get("pr_id")
                if not pid:
                    continue
                status = pr.get("status", "queued")
                if pid in completed_set or status == "completed":
                    projected[pid] = "completed"
                elif pid in active_set or status == "in_progress":
                    projected[pid] = "active"
                elif pid in blocked_set or status == "blocked":
                    projected[pid] = "blocked"
                else:
                    projected[pid] = "pending"

        return projected

    def _detect_drift(
        self,
        reconciled: List[PRReconciled],
        projected: Dict[str, str],
    ) -> List[DriftWarning]:
        warnings: List[DriftWarning] = []
        if not projected:
            return warnings

        for pr in reconciled:
            proj_state = projected.get(pr.pr_id)
            if proj_state is None:
                continue  # PR not in projection (could be new)
            if pr.state == proj_state:
                # States agree; check for unconfirmed completion (info)
                if pr.state == "completed" and not pr.provenance.get("receipt_confirmed"):
                    warnings.append(
                        DriftWarning(
                            pr_id=pr.pr_id,
                            severity="info",
                            derived_state=pr.state,
                            projected_state=proj_state,
                            message=f"{pr.pr_id}: completion dispatch exists but no terminal receipt found. States agree but evidence chain is incomplete.",
                        )
                    )
                continue

            # States disagree — classify severity
            severity = _drift_severity(pr.pr_id, pr.state, proj_state)
            warnings.append(
                DriftWarning(
                    pr_id=pr.pr_id,
                    severity=severity,
                    derived_state=pr.state,
                    projected_state=proj_state,
                    message=_drift_message(pr.pr_id, pr.state, proj_state),
                )
            )

        return warnings


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _drift_severity(pr_id: str, derived: str, projected: str) -> str:
    """Classify drift severity per Section 4.3 of the contract."""
    # Blocking cases
    if derived == "active" and projected in ("pending", "blocked", "queued"):
        return "blocking"
    if derived == "completed" and projected == "active":
        return "blocking"
    # Warning cases
    if derived == "pending" and projected in ("blocked", "queued"):
        return "warning"
    if derived == "blocked" and projected == "pending":
        return "warning"
    # Default to warning for any other mismatch
    return "warning"


def _drift_message(pr_id: str, derived: str, projected: str) -> str:
    templates = {
        ("active", "pending"): f"{pr_id}: dispatch exists in active/ but projection says pending — re-dispatch could create duplicate",
        ("active", "blocked"): f"{pr_id}: dispatch exists in active/ but projection says blocked — promotion would create duplicate",
        ("active", "queued"): f"{pr_id}: dispatch exists in active/ but projection says queued",
        ("completed", "active"): f"{pr_id}: dispatch is in completed/ but projection still shows active — dependents may be unnecessarily blocked",
        ("pending", "blocked"): f"{pr_id}: all dependencies are satisfied but projection still shows blocked",
        ("pending", "queued"): f"{pr_id}: derived state is pending but projection says queued",
        ("blocked", "pending"): f"{pr_id}: dependencies are not completed but projection says pending",
    }
    key = (derived, projected)
    return templates.get(key, f"{pr_id}: derived={derived} but projection={projected}")


def _topological_sort(pr_ids: List[str], deps: Dict[str, List[str]]) -> List[str]:
    """Return PR IDs in topological order (dependencies before dependents).

    If there is a cycle or unknown dep, returns the input order unchanged.
    """
    in_degree: Dict[str, int] = {pid: 0 for pid in pr_ids}
    adj: Dict[str, List[str]] = {pid: [] for pid in pr_ids}

    for pid in pr_ids:
        for dep in deps.get(pid, []):
            if dep in in_degree:
                adj[dep].append(pid)
                in_degree[pid] += 1

    queue = [pid for pid in pr_ids if in_degree[pid] == 0]
    ordered: List[str] = []
    while queue:
        current = queue.pop(0)
        ordered.append(current)
        for dep in adj.get(current, []):
            in_degree[dep] -= 1
            if in_degree[dep] == 0:
                queue.append(dep)

    if len(ordered) != len(pr_ids):
        return pr_ids  # Fallback on cycle
    return ordered
