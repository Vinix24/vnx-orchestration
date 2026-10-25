#!/usr/bin/env python3
"""Codex final gate enforcement.

``enforce_codex_gate`` is the module's one production entry point: the closure
verifier imports it to decide whether a PR must pass a Codex final gate, from
the contract's risk class, review stack, governance/runtime path touches, and
mass deletion (file count and net line count).
"""

from __future__ import annotations

import subprocess
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR / "lib"))

from review_contract import ReviewContract
from auto_merge_policy import codex_final_gate_required, is_governance_path


# ---------------------------------------------------------------------------
# Gate enforcement
# ---------------------------------------------------------------------------

RUNTIME_PATH_MARKERS = (
    "scripts/lib/runtime_coordination",
    "scripts/lib/dispatch_broker",
    "scripts/commands/start.sh",
    "scripts/commands/stop.sh",
)

# Net-deletion thresholds: WARN matches pre_merge_gate (5); HOLD is higher here (20)
# because a full Codex review is expensive — only require it for large-scale deletions.
DELETION_FILE_WARN = 5
DELETION_FILE_HOLD = 20

# Net line deletion thresholds (lines_removed - lines_added across all changed files).
# Catches PRs that gut file content without fully deleting files.
NET_LINE_DELETION_WARN = 200
NET_LINE_DELETION_HOLD = 500


def _touches_governance_paths(changed_files: List[str]) -> bool:
    return is_governance_path(changed_files)


def _touches_runtime_paths(changed_files: List[str]) -> bool:
    for path in changed_files:
        p = str(path).strip()
        if any(marker in p for marker in RUNTIME_PATH_MARKERS):
            return True
    return False


def _get_deleted_files(project_root: Path) -> Optional[List[str]]:
    """Return list of files deleted in current PR vs origin/main. None on git failure."""
    for base_ref in ("origin/main", "origin/master"):
        try:
            result = subprocess.run(
                ["git", "diff", "--diff-filter=D", "--name-only", f"{base_ref}...HEAD"],
                cwd=str(project_root),
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode == 0:
                return [f for f in result.stdout.strip().splitlines() if f.strip()]
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass

    try:
        result = subprocess.run(
            ["git", "diff", "--diff-filter=D", "--name-only", "HEAD~1", "HEAD"],
            cwd=str(project_root),
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            return [f for f in result.stdout.strip().splitlines() if f.strip()]
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass

    return None


def _parse_numstat_net(numstat_output: str) -> int:
    """Parse git diff --numstat output, return net line deletion (removed - added).

    Binary files report '-' for both columns and are skipped.
    """
    total_added = 0
    total_removed = 0
    for line in numstat_output.strip().splitlines():
        parts = line.split("\t")
        if len(parts) >= 2 and parts[0] != "-" and parts[1] != "-":
            try:
                total_added += int(parts[0])
                total_removed += int(parts[1])
            except ValueError:
                pass
    return total_removed - total_added


def _get_net_line_deletion(project_root: Path) -> Optional[int]:
    """Return net lines deleted (removed - added) for current PR vs origin/main.

    Returns None on git failure. Complements _get_deleted_files: catches PRs that gut
    file content without fully deleting files.
    """
    for base_ref in ("origin/main", "origin/master"):
        try:
            result = subprocess.run(
                ["git", "diff", "--numstat", f"{base_ref}...HEAD"],
                cwd=str(project_root),
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode == 0:
                return _parse_numstat_net(result.stdout)
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass

    try:
        result = subprocess.run(
            ["git", "diff", "--numstat", "HEAD~1", "HEAD"],
            cwd=str(project_root),
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            return _parse_numstat_net(result.stdout)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass

    return None


@dataclass(frozen=True)
class CodexGateEnforcementResult:
    """Result of evaluating whether a Codex final gate is required."""

    required: bool
    reasons: List[str]
    risk_class: str
    merge_policy: str
    touches_governance: bool
    touches_runtime: bool
    high_risk_by_path: bool
    mass_deletion_count: int = 0
    mass_deletion_warn: bool = False
    net_line_deletion: int = 0
    net_line_deletion_warn: bool = False
    warnings: List[str] = field(default_factory=list)
    deleted_files: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def enforce_codex_gate(
    contract: ReviewContract,
    project_root: Optional[Path] = None,
) -> CodexGateEnforcementResult:
    """Determine whether a Codex final gate is required for this contract.

    Required when any of:
    - risk_class is "high"
    - review_stack includes "codex_gate" AND risk_class != "low"
    - changed files touch governance or runtime paths
    - auto_merge_policy says codex_final_gate_required
    - PR deletes >= DELETION_FILE_HOLD files (when project_root provided)

    Mass-deletion threshold semantics:
    - WARN level: `mass_deletion_warn=True`, added to `warnings` list. Does NOT promote
      required to True and does NOT add to `reasons`.
    - BLOCK level (>= DELETION_FILE_HOLD): adds "mass_file_deletion" to `reasons` and
      sets required=True.

    Operator visibility: WARN surfaces in receipt findings and PR comment but does not
    block merge. BLOCK level requires a full Codex review (required=True).
    Net line deletion (lines_removed - lines_added) is also checked with the same
    graduated semantics: NET_LINE_DELETION_WARN and NET_LINE_DELETION_HOLD.
    """
    reasons: List[str] = []
    risk = (contract.risk_class or "").strip().lower()
    touches_gov = _touches_governance_paths(contract.changed_files)
    touches_rt = _touches_runtime_paths(contract.changed_files)
    high_risk_path = codex_final_gate_required(contract.changed_files)

    if risk == "high":
        reasons.append("risk_class_high")
    if touches_gov:
        reasons.append("touches_governance_paths")
    if touches_rt:
        reasons.append("touches_runtime_paths")
    if high_risk_path and "high_risk_change_scope" not in reasons:
        reasons.append("high_risk_change_scope")
    if "codex_gate" in contract.review_stack and risk != "low":
        reasons.append("codex_gate_in_review_stack")

    deleted_files: List[str] = []
    deleted_count = 0
    mass_deletion_warn = False
    net_line_deletion = 0
    net_line_deletion_warn = False
    warnings_list: List[str] = []
    if project_root is not None:
        raw = _get_deleted_files(project_root)
        if raw is not None:
            deleted_files = raw
            deleted_count = len(raw)
        if deleted_count >= DELETION_FILE_HOLD:
            reasons.append("mass_file_deletion")
        elif deleted_count >= DELETION_FILE_WARN:
            mass_deletion_warn = True
            warnings_list.append("mass_deletion_warning")

        raw_net = _get_net_line_deletion(project_root)
        if raw_net is not None:
            net_line_deletion = raw_net
        if net_line_deletion >= NET_LINE_DELETION_HOLD:
            reasons.append("net_line_deletion")
        elif net_line_deletion >= NET_LINE_DELETION_WARN:
            net_line_deletion_warn = True
            warnings_list.append("net_line_deletion_warning")

    return CodexGateEnforcementResult(
        required=len(reasons) > 0,
        reasons=reasons,
        risk_class=risk,
        merge_policy=contract.merge_policy,
        touches_governance=touches_gov,
        touches_runtime=touches_rt,
        high_risk_by_path=high_risk_path,
        mass_deletion_count=deleted_count,
        mass_deletion_warn=mass_deletion_warn,
        net_line_deletion=net_line_deletion,
        net_line_deletion_warn=net_line_deletion_warn,
        warnings=warnings_list,
        deleted_files=deleted_files,
    )
