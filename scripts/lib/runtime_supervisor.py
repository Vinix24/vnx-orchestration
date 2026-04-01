#!/usr/bin/env python3
"""
VNX Runtime Supervisor — Stall detection, exit classification, and anomaly escalation.

Implements the supervision layer from the Runtime State Machine Contract
(docs/core/130_RUNTIME_STATE_MACHINE_CONTRACT.md):

  - No-output stall detection per §5.2
  - Anomaly classification matrix per §7.1
  - Open item auto-creation per §7.2 / §7.3
  - Tie-break detection for zombie_lease, ghost_dispatch per §6.3
  - Dead heartbeat escalation per §4.3 (H-3)

This module is read-evaluate-act: it reads current state from the runtime DB,
evaluates anomaly conditions, and acts by driving state transitions and
creating open items. It does NOT run as a daemon — callers invoke
supervise_all() or supervise_terminal() on a schedule or event trigger.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from runtime_coordination import (
    get_connection,
    init_schema,
)
from worker_state_manager import (
    TERMINAL_WORKER_STATES,
    WorkerStateManager,
    classify_heartbeat,
    validate_worker_transition,
    InvalidWorkerTransitionError,
    DEFAULT_HEARTBEAT_DEAD_THRESHOLD,
    DEFAULT_HEARTBEAT_STALE_THRESHOLD,
    DEFAULT_STARTUP_GRACE_PERIOD,
    DEFAULT_STALL_THRESHOLD,
    DEFAULT_IDLE_BETWEEN_TASKS_GRACE,
    DEFAULT_INTERACTIVE_STALL_MULTIPLIER,
)


# ---------------------------------------------------------------------------
# Anomaly types (§7.1)
# ---------------------------------------------------------------------------

ANOMALY_TYPES = {
    "startup_stall":            {"severity": "warning",  "description": "Worker in initializing beyond startup_grace_period"},
    "progress_stall":           {"severity": "warning",  "description": "Worker in working with no output beyond stall_threshold"},
    "inter_task_stall":         {"severity": "warning",  "description": "Worker in idle_between_tasks beyond grace period"},
    "dead_worker":              {"severity": "blocking", "description": "Heartbeat crossed dead threshold"},
    "zombie_lease":             {"severity": "blocking", "description": "Lease is leased but dispatch is in terminal state"},
    "ghost_dispatch":           {"severity": "blocking", "description": "Dispatch is active but no lease exists for target terminal"},
    "phantom_activity":         {"severity": "info",     "description": "Terminal shows activity but lease is idle and no dispatch active"},
    "heartbeat_without_output": {"severity": "warning",  "description": "Heartbeat fresh but no output for extended period"},
    "output_without_heartbeat": {"severity": "warning",  "description": "Output detected but heartbeat is stale"},
    "recovery_timeout":         {"severity": "blocking", "description": "Terminal stuck in expired/recovering beyond recovery_timeout"},
    "bad_exit_no_artifacts":    {"severity": "warning",  "description": "Worker exited with code 0 but expected artifacts missing"},
}

# Dispatch states where the dispatch is finished — zombie detection trigger
_TERMINAL_DISPATCH_STATES = frozenset({
    "completed", "expired", "dead_letter",
})

# Dispatch states where work is actively claimed
_ACTIVE_DISPATCH_STATES = frozenset({
    "claimed", "delivering", "accepted", "running",
})

DEFAULT_RECOVERY_TIMEOUT = 600


# ---------------------------------------------------------------------------
# Anomaly result
# ---------------------------------------------------------------------------

@dataclass
class AnomalyRecord:
    """A detected runtime anomaly with full evidence."""
    anomaly_type: str
    severity: str
    terminal_id: str
    dispatch_id: Optional[str]
    worker_state: Optional[str]
    lease_state: str
    evidence: Dict[str, Any]
    detected_at: str = field(default_factory=lambda: _now_utc())

    def to_open_item_dict(self) -> Dict[str, Any]:
        """Format per §7.2 open item auto-creation contract."""
        return {
            "id": str(uuid.uuid4()),
            "type": "runtime_anomaly",
            "anomaly": self.anomaly_type,
            "severity": self.severity,
            "terminal_id": self.terminal_id,
            "dispatch_id": self.dispatch_id,
            "worker_state": self.worker_state,
            "lease_state": self.lease_state,
            "detected_at": self.detected_at,
            "evidence": self.evidence,
            "auto_created": True,
            "resolution": None,
            "resolved_at": None,
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


def _parse_ts(ts: Optional[str]) -> Optional[datetime]:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def _age_seconds(ts: Optional[str], now: datetime) -> Optional[float]:
    parsed = _parse_ts(ts)
    if parsed is None:
        return None
    return (now - parsed).total_seconds()


# ---------------------------------------------------------------------------
# RuntimeSupervisor
# ---------------------------------------------------------------------------

class RuntimeSupervisor:
    """Detects runtime anomalies and drives state transitions + open item creation.

    Usage::

        supervisor = RuntimeSupervisor(state_dir)
        anomalies = supervisor.supervise_all()
        for a in anomalies:
            print(f"{a.anomaly_type}: {a.terminal_id} — {a.severity}")
    """

    def __init__(
        self,
        state_dir: str | Path,
        *,
        auto_init: bool = True,
        startup_grace: int = DEFAULT_STARTUP_GRACE_PERIOD,
        stall_threshold: int = DEFAULT_STALL_THRESHOLD,
        idle_grace: int = DEFAULT_IDLE_BETWEEN_TASKS_GRACE,
        dead_threshold: int = DEFAULT_HEARTBEAT_DEAD_THRESHOLD,
        stale_threshold: int = DEFAULT_HEARTBEAT_STALE_THRESHOLD,
        recovery_timeout: int = DEFAULT_RECOVERY_TIMEOUT,
        interactive_multiplier: float = DEFAULT_INTERACTIVE_STALL_MULTIPLIER,
    ) -> None:
        self.state_dir = Path(state_dir)
        self._auto_init = auto_init
        self._initialized = False
        self.startup_grace = startup_grace
        self.stall_threshold = stall_threshold
        self.idle_grace = idle_grace
        self.dead_threshold = dead_threshold
        self.stale_threshold = stale_threshold
        self.recovery_timeout = recovery_timeout
        self.interactive_multiplier = interactive_multiplier
        self._worker_mgr = WorkerStateManager(state_dir, auto_init=auto_init)

    def _ensure_init(self) -> None:
        if not self._initialized and self._auto_init:
            init_schema(self.state_dir)
            self._initialized = True

    def supervise_all(self, *, now: Optional[datetime] = None) -> List[AnomalyRecord]:
        """Run full supervision sweep across all terminals.

        Returns list of detected anomalies. Also drives state transitions
        (stall → stalled, dead heartbeat → exited_bad) and emits coordination events.
        """
        self._ensure_init()
        if now is None:
            now = datetime.now(timezone.utc)

        anomalies: List[AnomalyRecord] = []

        with get_connection(self.state_dir) as conn:
            leases = conn.execute("SELECT * FROM terminal_leases").fetchall()
            workers = conn.execute("SELECT * FROM worker_states").fetchall()
            dispatches = conn.execute("SELECT * FROM dispatches").fetchall()

        lease_map = {r["terminal_id"]: dict(r) for r in leases}
        worker_map = {r["terminal_id"]: dict(r) for r in workers}
        dispatch_map = {r["dispatch_id"]: dict(r) for r in dispatches}

        for tid, lease in lease_map.items():
            terminal_anomalies = self._check_terminal(
                tid, lease, worker_map.get(tid), dispatch_map, now
            )
            anomalies.extend(terminal_anomalies)

        # Ghost dispatch detection: active dispatches targeting terminals with idle leases
        anomalies.extend(self._detect_ghost_dispatches(lease_map, dispatch_map, now))

        return anomalies

    def supervise_terminal(
        self,
        terminal_id: str,
        *,
        now: Optional[datetime] = None,
    ) -> List[AnomalyRecord]:
        """Run supervision for a single terminal."""
        self._ensure_init()
        if now is None:
            now = datetime.now(timezone.utc)

        with get_connection(self.state_dir) as conn:
            lease_row = conn.execute(
                "SELECT * FROM terminal_leases WHERE terminal_id = ?",
                (terminal_id,),
            ).fetchone()
            worker_row = conn.execute(
                "SELECT * FROM worker_states WHERE terminal_id = ?",
                (terminal_id,),
            ).fetchone()
            dispatches = conn.execute("SELECT * FROM dispatches").fetchall()

        if lease_row is None:
            return []

        lease = dict(lease_row)
        worker = dict(worker_row) if worker_row else None
        dispatch_map = {r["dispatch_id"]: dict(r) for r in dispatches}

        return self._check_terminal(terminal_id, lease, worker, dispatch_map, now)

    # -----------------------------------------------------------------------
    # Internal detection logic
    # -----------------------------------------------------------------------

    def _check_terminal(
        self,
        terminal_id: str,
        lease: Dict[str, Any],
        worker: Optional[Dict[str, Any]],
        dispatch_map: Dict[str, Dict[str, Any]],
        now: datetime,
    ) -> List[AnomalyRecord]:
        anomalies: List[AnomalyRecord] = []
        lease_state = lease["state"]

        # Recovery timeout check (§7.1)
        if lease_state in ("expired", "recovering"):
            anomalies.extend(
                self._check_recovery_timeout(terminal_id, lease, now)
            )
            return anomalies

        if lease_state != "leased":
            return anomalies

        dispatch_id = lease.get("dispatch_id")
        dispatch = dispatch_map.get(dispatch_id) if dispatch_id else None

        # Zombie lease: lease is leased but dispatch is terminal (§6.3)
        if dispatch and dispatch["state"] in _TERMINAL_DISPATCH_STATES:
            anomalies.append(AnomalyRecord(
                anomaly_type="zombie_lease",
                severity="blocking",
                terminal_id=terminal_id,
                dispatch_id=dispatch_id,
                worker_state=worker["state"] if worker else None,
                lease_state=lease_state,
                evidence={
                    "dispatch_state": dispatch["state"],
                    "last_heartbeat_at": lease.get("last_heartbeat_at"),
                    "lease_held_since": lease.get("leased_at"),
                },
            ))
            return anomalies

        if worker is None:
            return anomalies

        worker_state = worker["state"]
        if worker_state in TERMINAL_WORKER_STATES:
            return anomalies

        hb_class = classify_heartbeat(
            lease.get("last_heartbeat_at"),
            now=now,
            stale_threshold=self.stale_threshold,
            dead_threshold=self.dead_threshold,
        )

        # Dead heartbeat → exited_bad (§4.4 H-3)
        if hb_class == "dead":
            anomalies.append(self._escalate_dead_worker(
                terminal_id, worker, lease, now
            ))
            return anomalies

        # Stall detection per §5.2
        anomalies.extend(
            self._check_stall(terminal_id, worker, lease, hb_class, now)
        )

        # Heartbeat-output divergence (§7.1)
        anomalies.extend(
            self._check_heartbeat_output_divergence(terminal_id, worker, lease, hb_class, now)
        )

        return anomalies

    def _check_stall(
        self,
        terminal_id: str,
        worker: Dict[str, Any],
        lease: Dict[str, Any],
        hb_class: str,
        now: datetime,
    ) -> List[AnomalyRecord]:
        anomalies: List[AnomalyRecord] = []
        worker_state = worker["state"]

        # Determine output silence duration
        last_output = worker.get("last_output_at")
        state_entered = worker.get("state_entered_at")
        reference_ts = last_output or state_entered
        silence = _age_seconds(reference_ts, now)

        if silence is None:
            return anomalies

        if worker_state == "initializing":
            if silence >= self.startup_grace:
                # Startup stall → transition to stalled
                self._safe_transition(terminal_id, "stalled",
                                      actor="stall_detector",
                                      reason=f"startup stall: no output for {silence:.0f}s")
                anomalies.append(AnomalyRecord(
                    anomaly_type="startup_stall",
                    severity="warning",
                    terminal_id=terminal_id,
                    dispatch_id=worker["dispatch_id"],
                    worker_state="stalled",
                    lease_state=lease["state"],
                    evidence=self._build_evidence(worker, lease, now, silence),
                ))

        elif worker_state == "working":
            if silence >= self.stall_threshold:
                # Progress stall → transition to stalled
                self._safe_transition(terminal_id, "stalled",
                                      actor="stall_detector",
                                      reason=f"progress stall: no output for {silence:.0f}s")
                anomalies.append(AnomalyRecord(
                    anomaly_type="progress_stall",
                    severity="warning",
                    terminal_id=terminal_id,
                    dispatch_id=worker["dispatch_id"],
                    worker_state="stalled",
                    lease_state=lease["state"],
                    evidence=self._build_evidence(worker, lease, now, silence),
                ))

        elif worker_state == "idle_between_tasks":
            if silence >= self.idle_grace:
                # Inter-task stall → transition to stalled
                self._safe_transition(terminal_id, "stalled",
                                      actor="stall_detector",
                                      reason=f"inter-task stall: idle for {silence:.0f}s")
                anomalies.append(AnomalyRecord(
                    anomaly_type="inter_task_stall",
                    severity="warning",
                    terminal_id=terminal_id,
                    dispatch_id=worker["dispatch_id"],
                    worker_state="stalled",
                    lease_state=lease["state"],
                    evidence=self._build_evidence(worker, lease, now, silence),
                ))

        return anomalies

    def _escalate_dead_worker(
        self,
        terminal_id: str,
        worker: Dict[str, Any],
        lease: Dict[str, Any],
        now: datetime,
    ) -> AnomalyRecord:
        """Dead heartbeat → transition to exited_bad, create blocking anomaly."""
        hb_age = _age_seconds(lease.get("last_heartbeat_at"), now)
        self._safe_transition(terminal_id, "exited_bad",
                              actor="stall_detector",
                              reason=f"dead worker: heartbeat age {hb_age:.0f}s exceeds threshold")
        return AnomalyRecord(
            anomaly_type="dead_worker",
            severity="blocking",
            terminal_id=terminal_id,
            dispatch_id=worker["dispatch_id"],
            worker_state="exited_bad",
            lease_state=lease["state"],
            evidence=self._build_evidence(worker, lease, now,
                                          _age_seconds(worker.get("last_output_at") or worker.get("state_entered_at"), now)),
        )

    def _check_heartbeat_output_divergence(
        self,
        terminal_id: str,
        worker: Dict[str, Any],
        lease: Dict[str, Any],
        hb_class: str,
        now: datetime,
    ) -> List[AnomalyRecord]:
        anomalies: List[AnomalyRecord] = []
        worker_state = worker["state"]

        if worker_state not in ("working", "initializing"):
            return anomalies

        last_output = worker.get("last_output_at")
        output_silence = _age_seconds(last_output or worker.get("state_entered_at"), now)

        # heartbeat_without_output: fresh heartbeat but long output silence (§7.1)
        if hb_class == "fresh" and output_silence is not None and output_silence >= (self.stall_threshold * 2):
            anomalies.append(AnomalyRecord(
                anomaly_type="heartbeat_without_output",
                severity="warning",
                terminal_id=terminal_id,
                dispatch_id=worker["dispatch_id"],
                worker_state=worker_state,
                lease_state=lease["state"],
                evidence=self._build_evidence(worker, lease, now, output_silence),
            ))

        # output_without_heartbeat: output flowing but heartbeat stale (§7.1)
        if hb_class == "stale" and last_output:
            output_age = _age_seconds(last_output, now)
            if output_age is not None and output_age < self.stall_threshold:
                anomalies.append(AnomalyRecord(
                    anomaly_type="output_without_heartbeat",
                    severity="warning",
                    terminal_id=terminal_id,
                    dispatch_id=worker["dispatch_id"],
                    worker_state=worker_state,
                    lease_state=lease["state"],
                    evidence=self._build_evidence(worker, lease, now, output_age),
                ))

        return anomalies

    def _check_recovery_timeout(
        self,
        terminal_id: str,
        lease: Dict[str, Any],
        now: datetime,
    ) -> List[AnomalyRecord]:
        # Check how long the terminal has been in expired/recovering state
        # Use last_heartbeat_at or leased_at as proxy for when recovery started
        recovery_start = lease.get("last_heartbeat_at") or lease.get("leased_at")
        age = _age_seconds(recovery_start, now)
        if age is not None and age >= self.recovery_timeout:
            return [AnomalyRecord(
                anomaly_type="recovery_timeout",
                severity="blocking",
                terminal_id=terminal_id,
                dispatch_id=lease.get("dispatch_id"),
                worker_state=None,
                lease_state=lease["state"],
                evidence={
                    "last_heartbeat_at": lease.get("last_heartbeat_at"),
                    "recovery_age_seconds": age,
                    "recovery_timeout": self.recovery_timeout,
                },
            )]
        return []

    def _detect_ghost_dispatches(
        self,
        lease_map: Dict[str, Dict[str, Any]],
        dispatch_map: Dict[str, Dict[str, Any]],
        now: datetime,
    ) -> List[AnomalyRecord]:
        anomalies: List[AnomalyRecord] = []
        for dispatch_id, dispatch in dispatch_map.items():
            if dispatch["state"] not in _ACTIVE_DISPATCH_STATES:
                continue
            target_tid = dispatch.get("terminal_id")
            if not target_tid:
                continue
            lease = lease_map.get(target_tid)
            if lease and lease["state"] == "idle":
                anomalies.append(AnomalyRecord(
                    anomaly_type="ghost_dispatch",
                    severity="blocking",
                    terminal_id=target_tid,
                    dispatch_id=dispatch_id,
                    worker_state=None,
                    lease_state="idle",
                    evidence={
                        "dispatch_state": dispatch["state"],
                        "dispatch_created_at": dispatch.get("created_at"),
                    },
                ))
        return anomalies

    # -----------------------------------------------------------------------
    # Helpers
    # -----------------------------------------------------------------------

    def _safe_transition(
        self,
        terminal_id: str,
        to_state: str,
        *,
        actor: str = "stall_detector",
        reason: Optional[str] = None,
    ) -> bool:
        """Attempt a worker state transition, returning False if illegal."""
        try:
            self._worker_mgr.transition(
                terminal_id, to_state, actor=actor, reason=reason
            )
            return True
        except (InvalidWorkerTransitionError, KeyError):
            return False

    def _build_evidence(
        self,
        worker: Dict[str, Any],
        lease: Dict[str, Any],
        now: datetime,
        output_silence: Optional[float],
    ) -> Dict[str, Any]:
        hb = lease.get("last_heartbeat_at")
        hb_age = _age_seconds(hb, now)
        return {
            "last_heartbeat_at": hb,
            "last_output_at": worker.get("last_output_at"),
            "heartbeat_age_seconds": round(hb_age, 1) if hb_age is not None else None,
            "output_silence_seconds": round(output_silence, 1) if output_silence is not None else None,
            "dispatch_state": None,
        }


# ---------------------------------------------------------------------------
# Open item integration
# ---------------------------------------------------------------------------

def create_open_items_for_anomalies(
    anomalies: List[AnomalyRecord],
    open_items_path: Path,
) -> List[Dict[str, Any]]:
    """Write anomaly-driven open items per §7.2/§7.3.

    OI-5: Does not create duplicates — if an anomaly type already has an
    unresolved open item for the same terminal+dispatch, updates evidence instead.

    Returns list of created/updated open item dicts.
    """
    data = _load_open_items(open_items_path)
    results: List[Dict[str, Any]] = []

    for anomaly in anomalies:
        existing = _find_existing_anomaly_item(
            data, anomaly.anomaly_type, anomaly.terminal_id, anomaly.dispatch_id
        )
        if existing:
            # OI-5: update evidence, don't duplicate
            existing["evidence"] = anomaly.evidence
            existing["detected_at"] = anomaly.detected_at
            results.append(existing)
        else:
            item = anomaly.to_open_item_dict()
            data["items"].append(item)
            results.append(item)

    _save_open_items(data, open_items_path)
    return results


def _find_existing_anomaly_item(
    data: Dict[str, Any],
    anomaly_type: str,
    terminal_id: str,
    dispatch_id: Optional[str],
) -> Optional[Dict[str, Any]]:
    """Find unresolved runtime_anomaly open item matching terminal+dispatch+type."""
    for item in data.get("items", []):
        if item.get("resolved_at") is not None:
            continue
        if item.get("type") != "runtime_anomaly":
            continue
        if (item.get("anomaly") == anomaly_type
                and item.get("terminal_id") == terminal_id
                and item.get("dispatch_id") == dispatch_id):
            return item
    return None


def _load_open_items(path: Path) -> Dict[str, Any]:
    if path.exists():
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"items": []}


def _save_open_items(data: Dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.write("\n")


# ---------------------------------------------------------------------------
# Module-level factory
# ---------------------------------------------------------------------------

def load_supervisor(state_dir: str | Path, **kwargs) -> RuntimeSupervisor:
    """Return a RuntimeSupervisor for state_dir, auto-initializing the schema."""
    return RuntimeSupervisor(state_dir, auto_init=True, **kwargs)
