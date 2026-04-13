#!/usr/bin/env python3
"""auto_gate_trigger.py — Auto-trigger review gates when a feature's PRs are all committed.

Called from the headless orchestrator's decision loop after each receipt event.
When all checkboxes in FEATURE_PLAN.md are checked for a feature, this module:
  1. Checks whether a GitHub PR exists for the current branch.
  2. Creates one if it doesn't exist.
  3. Triggers each required review gate via review_gate_manager CLI.
  4. Logs the event to events/auto_gate.ndjson.

BILLING SAFETY: No Anthropic SDK. CLI-only.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[2]

# Required gate names from governance_enforcement.yaml (soft/hard mandatory checks)
_DEFAULT_GATE_STACK = ["codex_gate", "gemini_review"]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_required_gates() -> List[str]:
    """Load required gate names from governance_enforcement.yaml when available."""
    config_path = _REPO_ROOT / ".vnx" / "governance_enforcement.yaml"
    if not config_path.exists():
        return _DEFAULT_GATE_STACK

    try:
        import yaml  # type: ignore[import]
        with open(config_path, encoding="utf-8") as fh:
            cfg = yaml.safe_load(fh) or {}
        checks = cfg.get("checks", {})
        # Collect gate-style checks at level >= 2 (soft/hard mandatory)
        gates = []
        for name, spec in checks.items():
            level = int(spec.get("level", 0))
            if level >= 2 and name in {"codex_gate_required", "gemini_review_required"}:
                # Map check name → gate stack name
                gates.append(name.replace("_required", ""))
        return gates if gates else _DEFAULT_GATE_STACK
    except Exception as exc:
        logger.debug("Could not parse governance_enforcement.yaml: %s", exc)
        return _DEFAULT_GATE_STACK


def _find_feature_plan(state_dir: Path) -> Optional[Path]:
    """Walk up from state_dir to find FEATURE_PLAN.md (max 6 levels)."""
    candidate = state_dir
    for _ in range(6):
        candidate = candidate.parent
        fp = candidate / "FEATURE_PLAN.md"
        if fp.exists():
            return fp
    return None


def _extract_feature_id_from_plan(feature_plan: Path) -> Optional[str]:
    """Extract feature ID (e.g. 'F51') from FEATURE_PLAN.md H1 header."""
    try:
        text = feature_plan.read_text(encoding="utf-8")
        m = re.search(r"^#\s+(F\d+)", text, re.MULTILINE)
        return m.group(1) if m else None
    except OSError:
        return None


def _get_current_branch() -> str:
    """Return current git branch, or empty string on failure."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True, timeout=10,
            cwd=str(_REPO_ROOT),
        )
        return result.stdout.strip() if result.returncode == 0 else ""
    except Exception:
        return ""


def _find_open_pr(branch: str) -> Optional[int]:
    """Return open PR number for branch, or None if not found."""
    if not branch:
        return None
    try:
        proc = subprocess.run(
            ["gh", "pr", "list", "--head", branch, "--json", "number,state"],
            capture_output=True, text=True, timeout=20,
            cwd=str(_REPO_ROOT),
        )
        if proc.returncode != 0:
            logger.debug("gh pr list failed: %s", proc.stderr.strip()[:200])
            return None
        prs = json.loads(proc.stdout)
        open_prs = [p for p in prs if p.get("state", "").upper() == "OPEN"]
        return open_prs[0]["number"] if open_prs else None
    except Exception as exc:
        logger.debug("_find_open_pr failed: %s", exc)
        return None


def _create_pr(feature_id: str, branch: str) -> Optional[int]:
    """Create a GitHub PR for branch; return PR number or None on failure."""
    title = f"feat({feature_id.lower()}): auto-created by VNX governance trigger"
    body = (
        f"Auto-created by VNX auto_gate_trigger when all PRs for {feature_id} "
        "were detected as committed.\n\n"
        "This PR was opened automatically to satisfy the `pr_must_exist_before_next_dispatch` "
        "governance check. Please update the title and description before merging."
    )
    try:
        proc = subprocess.run(
            ["gh", "pr", "create",
             "--title", title,
             "--body", body,
             "--head", branch,
             "--draft"],
            capture_output=True, text=True, timeout=60,
            cwd=str(_REPO_ROOT),
        )
        if proc.returncode != 0:
            logger.warning("gh pr create failed: %s", proc.stderr.strip()[:400])
            return None
        # gh pr create outputs PR URL; parse the number from the last path segment
        url = proc.stdout.strip()
        m = re.search(r"/pull/(\d+)", url)
        if m:
            return int(m.group(1))
        logger.warning("Could not parse PR number from gh output: %s", url[:200])
        return None
    except Exception as exc:
        logger.warning("_create_pr exception: %s", exc)
        return None


def _trigger_gate(pr_number: int, branch: str, gate_name: str) -> bool:
    """Invoke review_gate_manager request-and-execute for a single gate.

    Returns True on exit-0, False otherwise.
    """
    script = _REPO_ROOT / "scripts" / "review_gate_manager.py"
    cmd = [
        sys.executable, str(script),
        "request-and-execute",
        "--pr", str(pr_number),
        "--branch", branch,
        "--review-stack", gate_name,
        "--mode", "final",
    ]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=300,
            cwd=str(_REPO_ROOT),
        )
        if proc.returncode != 0:
            logger.warning(
                "Gate %s trigger failed (exit %d): %s",
                gate_name, proc.returncode, proc.stderr.strip()[:400],
            )
            return False
        logger.info("Gate %s triggered successfully for PR #%d", gate_name, pr_number)
        return True
    except subprocess.TimeoutExpired:
        logger.warning("Gate %s trigger timed out (300s)", gate_name)
        return False
    except Exception as exc:
        logger.warning("Gate %s trigger exception: %s", gate_name, exc)
        return False


def _log_auto_gate_event(data_dir: Path, record: Dict[str, Any]) -> None:
    events_dir = data_dir / "events"
    events_dir.mkdir(parents=True, exist_ok=True)
    log_path = events_dir / "auto_gate.ndjson"
    with open(log_path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record) + "\n")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def trigger_gates_if_feature_complete(feature_id: str, state_dir: Path) -> Dict[str, Any]:
    """Auto-trigger gates when all PRs for feature_id are committed.

    Checks FEATURE_PLAN.md completion state, finds/creates a GitHub PR,
    then calls review_gate_manager for each required gate.

    Args:
        feature_id: Feature identifier, e.g. "F51".
        state_dir:  VNX state directory (e.g. .vnx-data/state/).

    Returns:
        dict with keys:
          triggered (bool), pr_number (int|None), gates (List[str]),
          reason (str|None) — populated when triggered=False.
    """
    data_dir = state_dir.parent  # state_dir is <data_dir>/state/

    # 1. Locate FEATURE_PLAN.md
    feature_plan = _find_feature_plan(state_dir)
    if feature_plan is None:
        reason = "FEATURE_PLAN.md not found"
        logger.debug("auto_gate_trigger: %s", reason)
        return {"triggered": False, "reason": reason, "pr_number": None, "gates": []}

    # 2. Check that the plan's feature matches feature_id
    plan_feature = _extract_feature_id_from_plan(feature_plan)
    if plan_feature and plan_feature != feature_id:
        reason = f"FEATURE_PLAN.md is for {plan_feature}, not {feature_id}"
        logger.debug("auto_gate_trigger: %s", reason)
        return {"triggered": False, "reason": reason, "pr_number": None, "gates": []}

    # 3. Parse feature state
    sys.path.insert(0, str(_REPO_ROOT / "scripts" / "lib"))
    try:
        from feature_state_machine import parse_feature_plan  # noqa: PLC0415
    except ImportError as exc:
        reason = f"feature_state_machine import failed: {exc}"
        logger.warning("auto_gate_trigger: %s", reason)
        return {"triggered": False, "reason": reason, "pr_number": None, "gates": []}

    feature_state = parse_feature_plan(feature_plan)

    if feature_state.status != "completed":
        reason = (
            f"Feature {feature_id} not yet complete "
            f"({feature_state.completed_prs}/{feature_state.total_prs} PRs, "
            f"{feature_state.completion_pct}%)"
        )
        logger.debug("auto_gate_trigger: %s", reason)
        return {"triggered": False, "reason": reason, "pr_number": None, "gates": []}

    logger.info(
        "auto_gate_trigger: %s is complete (%d/%d PRs) — triggering gates",
        feature_id, feature_state.completed_prs, feature_state.total_prs,
    )

    # 4. Find or create GitHub PR
    branch = _get_current_branch()
    pr_number = _find_open_pr(branch)

    if pr_number is None:
        logger.info("No open PR found for branch '%s' — creating one", branch)
        pr_number = _create_pr(feature_id, branch)
        if pr_number is None:
            reason = f"Failed to create GitHub PR for branch '{branch}'"
            logger.warning("auto_gate_trigger: %s", reason)
            _log_auto_gate_event(data_dir, {
                "timestamp": _now_utc(),
                "feature_id": feature_id,
                "branch": branch,
                "triggered": False,
                "reason": reason,
            })
            return {"triggered": False, "reason": reason, "pr_number": None, "gates": []}

    logger.info("Using PR #%d for gate triggers", pr_number)

    # 5. Load required gate stack and trigger each
    gates = _load_required_gates()
    triggered_gates: List[str] = []
    failed_gates: List[str] = []

    try:
        from governance_audit import log_gate_result as _log_gate_result  # noqa: PLC0415
    except ImportError:
        _log_gate_result = None  # type: ignore[assignment]

    for gate_name in gates:
        ok = _trigger_gate(pr_number, branch, gate_name)
        if ok:
            triggered_gates.append(gate_name)
        else:
            failed_gates.append(gate_name)
        if _log_gate_result is not None:
            try:
                _log_gate_result(
                    gate=gate_name,
                    pr_number=pr_number,
                    status="triggered" if ok else "failed",
                    findings_count=0,
                )
            except Exception:
                pass  # audit must never block gate trigger flow

    # 6. Log event
    event: Dict[str, Any] = {
        "timestamp": _now_utc(),
        "feature_id": feature_id,
        "branch": branch,
        "pr_number": pr_number,
        "gates_triggered": triggered_gates,
        "gates_failed": failed_gates,
        "triggered": True,
    }
    _log_auto_gate_event(data_dir, event)

    logger.info(
        "auto_gate_trigger: triggered %d gate(s) for %s PR #%d (failed: %s)",
        len(triggered_gates), feature_id, pr_number, failed_gates,
    )

    return {
        "triggered": True,
        "pr_number": pr_number,
        "gates": triggered_gates,
        "gates_failed": failed_gates,
        "reason": None,
    }
