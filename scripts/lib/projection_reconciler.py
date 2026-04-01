#!/usr/bin/env python3
"""
Projection Reconciler — active dispatch vs projected state alignment.

Detects and repairs forbidden contradictions between canonical state surface
C-3 (dispatch filesystem) and projected surfaces P-2 (pr_queue_state.json)
and P-3 (progress_state.yaml).

Contract: docs/core/120_PROJECTION_CONSISTENCY_CONTRACT.md

Contradiction codes detected:
  FC-P1  Dispatch in active/ but progress_state.yaml shows track idle  [forbidden]
  FC-P2  No active dispatch but progress_state.yaml shows track working [warning]
  FC-Q1  Dispatch in active/ but pr_queue_state.json shows PR queued/blocked [forbidden]

Tie-break rules applied when repairing:
  TB-P1  C-3 wins over P-3 → update progress_state.yaml from dispatch filesystem
  TB-Q1  C-3 wins over P-2 → queue projection must be regenerated

Repair:
  - FC-P1: Set track status to 'working' and active_dispatch_id in progress_state.yaml.
           This directly addresses the observed 'In Progress: None' incident.
  - FC-Q1: Emits as forbidden warning. Full P-2 repair requires queue_reconciler
           (out of scope here — the reconciler detects and reports; the caller
           runs reconcile_queue_state.py --repair to fix P-2).
  - FC-P2: Emits warning. No automatic repair (stale working state is information
           for the operator).

Mismatch reports are written to {state_dir}/consistency_checks/ as NDJSON.

Design invariants:
  - Non-destructive: never modifies canonical surfaces (C-1 through C-5).
  - Idempotent: running twice with the same dispatch state produces identical P-3.
  - Deterministic: same canonical state always produces the same repair action.
  - Auditable: every repair is logged with before/after values and canonical evidence.
"""

from __future__ import annotations

import json
import re
import tempfile
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Tracks that map to terminals
TRACK_TO_TERMINAL: Dict[str, str] = {"A": "T1", "B": "T2", "C": "T3"}

# Progress status values
STATUS_WORKING = "working"
STATUS_IDLE = "idle"

# FC-* contradiction codes
FC_P1 = "FC-P1"  # active dispatch but progress shows idle
FC_P2 = "FC-P2"  # no active dispatch but progress shows working
FC_Q1 = "FC-Q1"  # active dispatch but queue shows queued/blocked

# Queue projection statuses that indicate "not active"
_NON_ACTIVE_QUEUE_STATUSES = frozenset({"queued", "blocked", "pending"})


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

@dataclass
class ActiveDispatch:
    """An active dispatch record parsed from the filesystem."""
    dispatch_id: str
    track: str                    # "A", "B", or "C"
    pr_id: Optional[str]          # e.g. "PR-1"
    gate: Optional[str]           # e.g. "gate_pr1_..."
    file_path: Path


@dataclass
class ProjectionMismatch:
    """A detected contradiction between canonical and projected state."""
    contradiction_id: str         # FC-* code
    severity: str                 # "forbidden" or "warning"
    canonical_surface: str        # e.g. "C-3 (dispatch filesystem)"
    canonical_value: str          # e.g. "active dispatch for track B"
    projected_surface: str        # e.g. "P-3 (progress_state.yaml)"
    projected_value: str          # e.g. "status=idle"
    tie_break_rule: str           # TB-* code
    recommended_action: str
    auto_resolved: bool
    timestamp: str
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "contradiction_id": self.contradiction_id,
            "severity": self.severity,
            "canonical_surface": self.canonical_surface,
            "canonical_value": self.canonical_value,
            "projected_surface": self.projected_surface,
            "projected_value": self.projected_value,
            "tie_break_rule": self.tie_break_rule,
            "recommended_action": self.recommended_action,
            "auto_resolved": self.auto_resolved,
            "timestamp": self.timestamp,
            "metadata": self.metadata,
        }


@dataclass
class ReconcileResult:
    """Full result of a projection reconciliation pass."""
    checked_at: str
    active_dispatches: List[ActiveDispatch] = field(default_factory=list)
    mismatches: List[ProjectionMismatch] = field(default_factory=list)
    repairs: List[Dict[str, Any]] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)

    @property
    def has_forbidden(self) -> bool:
        return any(m.severity == "forbidden" for m in self.mismatches)

    @property
    def is_clean(self) -> bool:
        return len(self.mismatches) == 0

    @property
    def forbidden_mismatches(self) -> List[ProjectionMismatch]:
        return [m for m in self.mismatches if m.severity == "forbidden"]

    @property
    def warning_mismatches(self) -> List[ProjectionMismatch]:
        return [m for m in self.mismatches if m.severity == "warning"]

    def summary(self) -> str:
        lines = [
            f"Projection reconciliation at {self.checked_at}",
            f"  Active dispatches found: {len(self.active_dispatches)}",
            f"  Forbidden contradictions: {len(self.forbidden_mismatches)}",
            f"  Warning contradictions:   {len(self.warning_mismatches)}",
            f"  Repairs applied:          {len(self.repairs)}",
            f"  Errors:                   {len(self.errors)}",
        ]
        for m in self.mismatches:
            icon = "🔴" if m.severity == "forbidden" else "🟡"
            resolved = " [auto-resolved]" if m.auto_resolved else ""
            lines.append(f"  {icon} [{m.contradiction_id}] {m.recommended_action}{resolved}")
        return "\n".join(lines)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "checked_at": self.checked_at,
            "active_dispatch_count": len(self.active_dispatches),
            "has_forbidden": self.has_forbidden,
            "is_clean": self.is_clean,
            "mismatch_count": len(self.mismatches),
            "repair_count": len(self.repairs),
            "active_dispatches": [
                {
                    "dispatch_id": d.dispatch_id,
                    "track": d.track,
                    "pr_id": d.pr_id,
                    "gate": d.gate,
                }
                for d in self.active_dispatches
            ],
            "mismatches": [m.to_dict() for m in self.mismatches],
            "repairs": self.repairs,
            "errors": self.errors,
        }


# ---------------------------------------------------------------------------
# Dispatch filesystem scanner
# ---------------------------------------------------------------------------

def _parse_dispatch_metadata(dispatch_file: Path) -> Dict[str, Optional[str]]:
    """Extract Track, PR-ID, Gate, Dispatch-ID from a dispatch file."""
    meta: Dict[str, Optional[str]] = {
        "track": None,
        "pr_id": None,
        "gate": None,
        "dispatch_id": None,
    }
    try:
        content = dispatch_file.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return meta

    patterns = {
        "track": re.compile(r"^Track:\s*([A-C])\s*$", re.MULTILINE),
        "pr_id": re.compile(r"^PR-ID:\s*(\S+)", re.MULTILINE),
        "gate": re.compile(r"^Gate:\s*(\S+)", re.MULTILINE),
        "dispatch_id": re.compile(r"^Dispatch-ID:\s*(\S+)", re.MULTILINE),
    }
    # Fallback: extract track from [[TARGET:X]] header
    target_match = re.search(r"^\[\[TARGET:([A-C])\]\]", content, re.MULTILINE)
    if target_match:
        meta["track"] = target_match.group(1)

    for key, pattern in patterns.items():
        m = pattern.search(content)
        if m:
            meta[key] = m.group(1).strip()

    return meta


def scan_active_dispatches(dispatch_dir: Path) -> List[ActiveDispatch]:
    """Scan dispatch_dir/active/ and return ActiveDispatch records.

    Args:
        dispatch_dir: Path to the dispatches root (e.g. .vnx-data/dispatches/).

    Returns:
        List of ActiveDispatch records for all .md files in active/.
    """
    active_dir = dispatch_dir / "active"
    if not active_dir.is_dir():
        return []

    records: List[ActiveDispatch] = []
    for f in sorted(active_dir.iterdir()):
        if not f.is_file() or f.suffix != ".md":
            continue
        meta = _parse_dispatch_metadata(f)
        track = meta.get("track")
        if not track:
            continue  # Cannot reconcile without track
        records.append(
            ActiveDispatch(
                dispatch_id=meta.get("dispatch_id") or f.stem,
                track=track,
                pr_id=meta.get("pr_id"),
                gate=meta.get("gate"),
                file_path=f,
            )
        )
    return records


# ---------------------------------------------------------------------------
# Progress state reader / writer
# ---------------------------------------------------------------------------

def _load_progress_state(progress_path: Path) -> Dict[str, Any]:
    """Load progress_state.yaml. Returns empty dict on missing or parse error."""
    if not progress_path.is_file():
        return {}
    try:
        content = progress_path.read_text(encoding="utf-8")
        return yaml.safe_load(content) or {}
    except (OSError, yaml.YAMLError):
        return {}


def _write_progress_state_atomic(progress_path: Path, state: Dict[str, Any]) -> None:
    """Write progress_state.yaml atomically (temp-file + rename)."""
    progress_path.parent.mkdir(parents=True, exist_ok=True)
    content = yaml.dump(state, default_flow_style=False, sort_keys=False)
    tmp_fd, tmp_path = tempfile.mkstemp(
        dir=str(progress_path.parent),
        prefix=".progress_state_",
        suffix=".tmp",
    )
    try:
        os.write(tmp_fd, content.encode("utf-8"))
        os.close(tmp_fd)
        os.replace(tmp_path, str(progress_path))
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _get_track_status(state: Dict[str, Any], track: str) -> Optional[str]:
    """Return the status for a track from progress_state dict, or None."""
    return (
        state.get("tracks", {}).get(track, {}).get("status")
    )


def _get_track_active_dispatch_id(state: Dict[str, Any], track: str) -> Optional[str]:
    """Return the active_dispatch_id for a track, or None."""
    return (
        state.get("tracks", {}).get(track, {}).get("active_dispatch_id")
    )


# ---------------------------------------------------------------------------
# Queue projection reader
# ---------------------------------------------------------------------------

def _load_queue_projection(queue_state_path: Path) -> Dict[str, str]:
    """Load pr_queue_state.json and return {pr_id: status} map.

    Returns empty dict on missing or parse error.
    """
    if not queue_state_path.is_file():
        return {}
    try:
        data = json.loads(queue_state_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}

    result: Dict[str, str] = {}
    completed_set = set(data.get("completed", []))
    active_set = set(data.get("active", []))
    blocked_set = set(data.get("blocked", []))

    for pr in data.get("prs", []):
        pid = pr.get("id") or pr.get("pr_id")
        if not pid:
            continue
        status = pr.get("status", "queued")
        if pid in completed_set or status == "completed":
            result[pid] = "completed"
        elif pid in active_set or status == "in_progress":
            result[pid] = "active"
        elif pid in blocked_set or status == "blocked":
            result[pid] = "blocked"
        else:
            result[pid] = "queued"

    return result


# ---------------------------------------------------------------------------
# Mismatch event writer
# ---------------------------------------------------------------------------

def _write_mismatch_event(
    consistency_dir: Path,
    mismatch: ProjectionMismatch,
) -> None:
    """Append mismatch event to consistency_checks/ NDJSON log."""
    consistency_dir.mkdir(parents=True, exist_ok=True)
    log_path = consistency_dir / "projection_mismatches.ndjson"
    line = json.dumps(mismatch.to_dict(), separators=(",", ":")) + "\n"
    with open(log_path, "a", encoding="utf-8") as fh:
        fh.write(line)


# ---------------------------------------------------------------------------
# ProjectionReconciler
# ---------------------------------------------------------------------------

class ProjectionReconciler:
    """Detects and repairs forbidden contradictions between C-3 and P-2/P-3.

    Implements the reconciliation protocol from contract Section 8 for the
    projection surfaces that directly affect operator dispatch decisions.

    Args:
        dispatch_dir:    Path to dispatch root (.vnx-data/dispatches/).
        state_dir:       Path to VNX state directory (.vnx-data/state/).
        consistency_dir: Path for mismatch event log (default: state_dir/consistency_checks/).
    """

    def __init__(
        self,
        dispatch_dir: Path | str,
        state_dir: Path | str,
        consistency_dir: Optional[Path | str] = None,
    ) -> None:
        self._dispatch_dir = Path(dispatch_dir)
        self._state_dir = Path(state_dir)
        self._consistency_dir = (
            Path(consistency_dir)
            if consistency_dir
            else self._state_dir / "consistency_checks"
        )
        self._progress_path = self._state_dir / "progress_state.yaml"
        self._queue_state_path = self._state_dir / "pr_queue_state.json"

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def reconcile(self, *, repair: bool = False) -> ReconcileResult:
        """Run a full projection reconciliation pass.

        Args:
            repair: When True, apply canonical-wins repairs to P-3
                    (update progress_state.yaml from dispatch filesystem).
                    When False, detect and report only.

        Returns:
            ReconcileResult with all detected mismatches and repair log.
        """
        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"
        result = ReconcileResult(checked_at=now_iso)

        # Load canonical source (C-3)
        try:
            active_dispatches = scan_active_dispatches(self._dispatch_dir)
        except Exception as exc:
            result.errors.append(f"Failed to scan active dispatch directory: {exc}")
            return result

        result.active_dispatches = active_dispatches

        # Load projected surfaces
        progress_state = _load_progress_state(self._progress_path)
        queue_projection = _load_queue_projection(self._queue_state_path)

        # Index active dispatches by track and PR
        active_by_track: Dict[str, ActiveDispatch] = {}
        for d in active_dispatches:
            active_by_track[d.track] = d  # latest wins for each track

        # --- FC-P1: Active dispatch but progress shows track idle ---
        self._check_fc_p1(result, active_by_track, progress_state, repair=repair)

        # --- FC-P2: No active dispatch but progress shows working ---
        self._check_fc_p2(result, active_by_track, progress_state)

        # --- FC-Q1: Active dispatch but queue projection shows queued/blocked ---
        self._check_fc_q1(result, active_dispatches, queue_projection)

        # Write mismatch events to audit log
        for mismatch in result.mismatches:
            try:
                _write_mismatch_event(self._consistency_dir, mismatch)
            except Exception as exc:
                result.errors.append(f"Failed to write mismatch event: {exc}")

        return result

    # ------------------------------------------------------------------
    # FC-P1 detection and repair (the primary 'In Progress: None' fix)
    # ------------------------------------------------------------------

    def _check_fc_p1(
        self,
        result: ReconcileResult,
        active_by_track: Dict[str, "ActiveDispatch"],
        progress_state: Dict[str, Any],
        *,
        repair: bool,
    ) -> None:
        """FC-P1: active dispatch in C-3 but P-3 shows track idle.

        TB-P1 (C-3 wins): update progress_state.yaml to show 'working'
        with active_dispatch_id matching the active dispatch.
        """
        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"

        for track, dispatch in active_by_track.items():
            current_status = _get_track_status(progress_state, track)
            current_dispatch_id = _get_track_active_dispatch_id(progress_state, track)

            # Check if projection is consistent with canonical truth
            status_ok = current_status == STATUS_WORKING
            dispatch_id_ok = current_dispatch_id == dispatch.dispatch_id

            if status_ok and dispatch_id_ok:
                continue  # Projection is consistent — no contradiction

            # Determine what is wrong
            if not status_ok:
                projected_value = f"status={current_status or 'missing'}"
                canonical_value = (
                    f"active dispatch {dispatch.dispatch_id!r} for track {track}"
                )
            else:
                projected_value = f"active_dispatch_id={current_dispatch_id!r}"
                canonical_value = (
                    f"active dispatch {dispatch.dispatch_id!r} for track {track} "
                    f"(dispatch_id mismatch)"
                )

            auto_resolved = False
            if repair:
                try:
                    self._repair_progress_fc_p1(
                        track, dispatch, progress_state, now_iso
                    )
                    auto_resolved = True
                    result.repairs.append({
                        "contradiction_id": FC_P1,
                        "track": track,
                        "dispatch_id": dispatch.dispatch_id,
                        "before_status": current_status,
                        "after_status": STATUS_WORKING,
                        "before_dispatch_id": current_dispatch_id,
                        "after_dispatch_id": dispatch.dispatch_id,
                        "timestamp": now_iso,
                    })
                except Exception as exc:
                    result.errors.append(
                        f"FC-P1 repair failed for track {track}: {exc}"
                    )

            result.mismatches.append(ProjectionMismatch(
                contradiction_id=FC_P1,
                severity="forbidden",
                canonical_surface="C-3 (dispatch filesystem active/)",
                canonical_value=canonical_value,
                projected_surface="P-3 (progress_state.yaml)",
                projected_value=projected_value,
                tie_break_rule="TB-P1 (C-3 wins → update P-3)",
                recommended_action=(
                    f"Set progress_state.yaml track {track} to "
                    f"status=working, active_dispatch_id={dispatch.dispatch_id!r}. "
                    f"Run reconcile --repair or dispatch update_progress_state.py."
                ),
                auto_resolved=auto_resolved,
                timestamp=now_iso,
                metadata={
                    "track": track,
                    "dispatch_id": dispatch.dispatch_id,
                    "pr_id": dispatch.pr_id,
                    "gate": dispatch.gate,
                    "current_status": current_status,
                    "current_dispatch_id": current_dispatch_id,
                },
            ))

    def _repair_progress_fc_p1(
        self,
        track: str,
        dispatch: "ActiveDispatch",
        progress_state: Dict[str, Any],
        now_iso: str,
    ) -> None:
        """Apply TB-P1 repair: update progress_state.yaml to working.

        Modifies progress_state in-place and writes atomically to disk.
        """
        # Ensure structure exists
        if "tracks" not in progress_state:
            progress_state["tracks"] = {}
        if track not in progress_state["tracks"]:
            progress_state["tracks"][track] = {
                "current_gate": dispatch.gate or "implementation",
                "status": STATUS_IDLE,
                "active_dispatch_id": None,
                "last_receipt": {
                    "event_type": None,
                    "status": None,
                    "timestamp": None,
                    "dispatch_id": None,
                },
                "history": [],
            }

        track_state = progress_state["tracks"][track]

        # Record transition in history (cap at 10)
        history_entry = {
            "timestamp": now_iso,
            "from_status": track_state.get("status"),
            "to_status": STATUS_WORKING,
            "dispatch_id": dispatch.dispatch_id,
            "updated_by": "projection_reconciler:FC-P1",
        }
        history = track_state.get("history", [])
        history = [history_entry] + history[:9]  # Prepend, keep last 10
        track_state["history"] = history

        # Apply repair
        track_state["status"] = STATUS_WORKING
        track_state["active_dispatch_id"] = dispatch.dispatch_id
        if dispatch.gate:
            track_state["current_gate"] = dispatch.gate

        progress_state["updated_at"] = now_iso
        progress_state["updated_by"] = "projection_reconciler:FC-P1"

        _write_progress_state_atomic(self._progress_path, progress_state)

    # ------------------------------------------------------------------
    # FC-P2 detection (no active dispatch but progress shows working)
    # ------------------------------------------------------------------

    def _check_fc_p2(
        self,
        result: ReconcileResult,
        active_by_track: Dict[str, "ActiveDispatch"],
        progress_state: Dict[str, Any],
    ) -> None:
        """FC-P2: P-3 shows track working but no dispatch in active/ (C-3)."""
        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"

        for track in TRACK_TO_TERMINAL:
            current_status = _get_track_status(progress_state, track)
            if current_status != STATUS_WORKING:
                continue  # Not projecting working — no issue

            if track in active_by_track:
                continue  # Active dispatch exists — consistent

            stale_dispatch_id = _get_track_active_dispatch_id(progress_state, track)
            result.mismatches.append(ProjectionMismatch(
                contradiction_id=FC_P2,
                severity="warning",
                canonical_surface="C-3 (dispatch filesystem active/)",
                canonical_value=f"no active dispatch for track {track}",
                projected_surface="P-3 (progress_state.yaml)",
                projected_value=f"status=working, active_dispatch_id={stale_dispatch_id!r}",
                tie_break_rule="TB-P1 (C-3 wins — stale working state)",
                recommended_action=(
                    f"Clear progress_state.yaml track {track} to status=idle "
                    f"(dispatch completed or failed without projection update). "
                    f"Run update_progress_state.py --track {track} --status idle."
                ),
                auto_resolved=False,
                timestamp=now_iso,
                metadata={
                    "track": track,
                    "stale_dispatch_id": stale_dispatch_id,
                },
            ))

    # ------------------------------------------------------------------
    # FC-Q1 detection (active dispatch but queue shows queued/blocked)
    # ------------------------------------------------------------------

    def _check_fc_q1(
        self,
        result: ReconcileResult,
        active_dispatches: List["ActiveDispatch"],
        queue_projection: Dict[str, str],
    ) -> None:
        """FC-Q1: dispatch in active/ (C-3) but P-2 shows PR as queued/blocked.

        This creates duplicate dispatch risk: T0 may re-dispatch a PR that
        already has a live dispatch executing.
        """
        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"

        if not queue_projection:
            return  # No projection to compare against

        for dispatch in active_dispatches:
            if not dispatch.pr_id:
                continue  # Cannot check without PR reference

            proj_status = queue_projection.get(dispatch.pr_id)
            if proj_status is None:
                continue  # PR not in projection (new feature plan)

            if proj_status in _NON_ACTIVE_QUEUE_STATUSES:
                result.mismatches.append(ProjectionMismatch(
                    contradiction_id=FC_Q1,
                    severity="forbidden",
                    canonical_surface="C-3 (dispatch filesystem active/)",
                    canonical_value=(
                        f"active dispatch {dispatch.dispatch_id!r} for {dispatch.pr_id}"
                    ),
                    projected_surface="P-2 (pr_queue_state.json)",
                    projected_value=f"status={proj_status}",
                    tie_break_rule="TB-Q1 (C-3 wins → regenerate P-2)",
                    recommended_action=(
                        f"Run reconcile_queue_state.py --repair to regenerate "
                        f"pr_queue_state.json. {dispatch.pr_id} must be shown as "
                        f"active to prevent duplicate dispatch."
                    ),
                    auto_resolved=False,
                    timestamp=now_iso,
                    metadata={
                        "dispatch_id": dispatch.dispatch_id,
                        "pr_id": dispatch.pr_id,
                        "track": dispatch.track,
                        "projected_status": proj_status,
                    },
                ))
