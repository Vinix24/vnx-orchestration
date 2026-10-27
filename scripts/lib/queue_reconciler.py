#!/usr/bin/env python3
"""Queue state reconciler — derives PR queue status from canonical runtime evidence.

Implements the derivation algorithm from docs/core/70_QUEUE_TRUTH_CONTRACT.md,
with the plan source retired by ADR-047 §4: a PR is in the queue when a
dispatch names it. No plan file is read, and PRs carry no dependencies of
their own: dependencies exist only between tracks (ADR-047 §5).

Priority hierarchy (higher = more authoritative):
  1. Dispatch filesystem       — PR identity (PR-ID line) and active/completed/pending/staging/rejected
  2. Receipts                  — terminal event confirmation
  3. Queue projection files    — cached views only (never primary truth)

Usage:
  from queue_reconciler import QueueReconciler
  r = QueueReconciler(dispatch_dir, receipts_file)
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
    prs: List[PRReconciled]
    drift_warnings: List[DriftWarning]
    reconciled_at: str
    has_blocking_drift: bool

    def as_dict(self) -> Dict[str, Any]:
        return {
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
# Dispatch directory scanner
# ---------------------------------------------------------------------------

_PR_ID_RE = re.compile(r"^PR-ID:\s*(\S+)", re.MULTILINE)
_MANAGER_FIELD_RE = re.compile(
    r"^(Track|Gate|Role|Risk-Class|Merge-Policy|Review-Stack):[ \t]*(\S.*)$", re.MULTILINE
)
_TITLE_RE = re.compile(r"^#[ \t]+(.+)$", re.MULTILINE)


def _parse_dispatch_metadata(content: str) -> Dict[str, Any]:
    """The queue metadata a dispatch file declares in its Manager Block.

    ADR-047: where the risk class cannot be derived it is ``high``, never
    ``medium``; an absent merge policy is the stricter ``human``.
    """
    fields: Dict[str, str] = {}
    for m in _MANAGER_FIELD_RE.finditer(content):
        fields.setdefault(m.group(1), m.group(2).strip())
    title = _TITLE_RE.search(content)
    review_stack = [
        gate.strip() for gate in fields.get("Review-Stack", "").split(",")
        if gate.strip() and gate.strip().lower() != "none"
    ]
    return {
        "title": title.group(1).strip() if title else "",
        "track": fields.get("Track", "?"),
        "dependencies": [],
        "gate": fields.get("Gate", ""),
        "skill": fields.get("Role", ""),
        "risk_class": fields.get("Risk-Class", "high").lower(),
        "merge_policy": fields.get("Merge-Policy", "human").lower(),
        "review_stack": review_stack,
    }


@dataclass
class DispatchRecord:
    dispatch_id: str
    pr_id: Optional[str]
    dir_state: str  # active | completed | pending | staging | rejected
    file_path: Path
    metadata: Dict[str, Any] = field(default_factory=dict)


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
            try:
                content = f.read_text(encoding="utf-8", errors="replace")
            except OSError:
                content = ""
            pr_match = _PR_ID_RE.search(content)
            records.append(
                DispatchRecord(
                    dispatch_id=f.stem,  # filename without extension
                    pr_id=pr_match.group(1).strip() if pr_match else None,
                    dir_state=dir_name,
                    file_path=f,
                    metadata=_parse_dispatch_metadata(content),
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
            # D2: pytest-fixture receipts (source == "pytest") are test noise
            # from runs that appended onto the live ledger instead of an
            # isolated tmp_path — never real dispatch work. Measured
            # 2026-09-04: 7,738 of 28,944 lines on the real t0_receipts.ndjson.
            # A dispatch must not be marked "confirmed complete" by one of
            # these, or the completion_pct this feeds is polluted.
            if str(record.get("source") or "").strip().lower() == "pytest":
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
        projection_file: Path to pr_queue_state.json (for drift detection)
    """

    def __init__(
        self,
        dispatch_dir: Path,
        receipts_file: Path,
        projection_file: Optional[Path] = None,
    ) -> None:
        self.dispatch_dir = dispatch_dir
        self.receipts_file = receipts_file
        self.projection_file = projection_file

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def reconcile(self) -> ReconcileResult:
        """Run full reconciliation and return the result.

        Every PR a dispatch names is a queue row (ADR-047 §4). A dispatch
        without a PR-ID line names no PR and is not a row.
        """
        confirmed_dispatch_ids = load_receipt_dispatch_ids(self.receipts_file)
        projected = self._load_projection()

        pr_dispatches: Dict[str, List[DispatchRecord]] = {}
        for rec in scan_dispatch_dirs(self.dispatch_dir):
            if rec.pr_id:
                pr_dispatches.setdefault(rec.pr_id, []).append(rec)

        reconciled_prs: List[PRReconciled] = []
        for pr_id in sorted(pr_dispatches):
            dispatches = pr_dispatches[pr_id]
            state, provenance = self._derive_state(dispatches, confirmed_dispatch_ids)
            source = next(
                (d for d in dispatches if d.dispatch_id == provenance["dispatch_id"]),
                dispatches[0],
            )
            reconciled_prs.append(
                PRReconciled(pr_id=pr_id, state=state, provenance=provenance, metadata=dict(source.metadata))
            )

        drift_warnings = self._detect_drift(reconciled_prs, projected)
        has_blocking = any(w.severity == "blocking" for w in drift_warnings)

        return ReconcileResult(
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
        dispatches: List[DispatchRecord],
        confirmed_dispatch_ids: Set[str],
    ) -> tuple[str, Dict[str, Any]]:
        """Apply the contract's derivation steps to the dispatches naming one PR.

        There is no blocked step: a PR has no dependencies of its own
        (ADR-047 §5), so a PR that is neither active nor completed is pending.

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

        # Step 4: pending. A dispatch in pending/ or staging/ is the evidence;
        # a PR named only by a rejected dispatch is pending without one.
        inflight_dispatches = [
            d for d in dispatches if d.dir_state in ("pending", "staging")
        ]
        evidence_dispatch = inflight_dispatches[0] if inflight_dispatches else None
        return "pending", {
            "source": "dispatch_filesystem",
            "dir": evidence_dispatch.dir_state if evidence_dispatch else None,
            "evidence": str(evidence_dispatch.file_path) if evidence_dispatch else None,
            "dispatch_id": evidence_dispatch.dispatch_id if evidence_dispatch else None,
            "receipt_confirmed": False,
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
