#!/usr/bin/env python3
"""Handover and resume payload generation with structural validation.

Implements Sections 3 and 4 of the Context Injection Contract:
  - Handover payload: structured output on dispatch completion (HO-1..HO-5)
  - Resume payload: context injection on dispatch resumption (RS-1..RS-5)
  - Structural validation for both payload types

Handover invariants:
  HO-1  Every dispatch completion produces a handover
  HO-2  Status must honestly reflect outcome
  HO-3  next_action must have concrete recommendation (not "unknown")
  HO-4  residual_state always present (empty arrays allowed)
  HO-5  context_for_next must include critical_context

Resume invariants:
  RS-1  Must always include original task specification
  RS-2  rotation: work_completed must be specific
  RS-3  interruption: last_known_state must be specific
  RS-4  redispatch: findings_so_far must include prior findings
  RS-5  No raw conversation history or full transcripts
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from result_contract import Result, result_error, result_ok


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

HANDOVER_VERSION = "1.0"
RESUME_VERSION = "1.0"

VALID_STATUSES = frozenset({"success", "failed", "partial"})
VALID_ACTIONS = frozenset({"advance", "review", "fix", "block", "escalate"})
VALID_CHANGE_TYPES = frozenset({"created", "modified", "deleted"})
VALID_SEVERITIES = frozenset({"blocker", "warn", "info"})
VALID_RESUME_TYPES = frozenset({"rotation", "interruption", "redispatch"})
VALID_VERIFICATION_METHODS = frozenset({"local_tests", "ci_green", "manual_review", "none"})

DISPATCH_ID_PATTERN = re.compile(r"^\d{8}-\d{6}-.+$")
PR_ID_PATTERN = re.compile(r"^PR-\d+$")
VALID_TRACKS = frozenset({"A", "B", "C"})

# Vague terms that violate RS-2/RS-3 specificity requirements
VAGUE_PROGRESS_TERMS = frozenset({"in progress", "ongoing", "working on it", "started"})

# RS-5: Conversation transcript patterns that must not appear in resume fields
TRANSCRIPT_PATTERN = re.compile(r"^(User|Assistant|Human|Claude):", re.MULTILINE)


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------

def _validate_file_modified(entry: Dict[str, Any]) -> Optional[str]:
    """Return error message if file_modified entry is invalid, else None."""
    if not entry.get("path"):
        return "files_modified entry missing 'path'"
    if entry.get("change_type") not in VALID_CHANGE_TYPES:
        return f"invalid change_type: {entry.get('change_type')}"
    return None


def _validate_open_item(entry: Dict[str, Any]) -> Optional[str]:
    """Return error message if open_item entry is invalid, else None."""
    if not entry.get("id"):
        return "open_items_created entry missing 'id'"
    if entry.get("severity") not in VALID_SEVERITIES:
        return f"invalid severity: {entry.get('severity')}"
    return None


def _is_vague(text: str) -> bool:
    """Check if text is too vague for resume specificity requirements."""
    return text.strip().lower() in VAGUE_PROGRESS_TERMS


def _contains_transcript(text: str) -> bool:
    """Check if text contains raw conversation transcript patterns (RS-5)."""
    return bool(TRANSCRIPT_PATTERN.search(text))


def _safe_get_dict(payload: Any, key: str) -> Dict[str, Any]:
    """Safely get a dict value, returning empty dict for non-dict types."""
    val = payload.get(key) if isinstance(payload, dict) else None
    return val if isinstance(val, dict) else {}


def _safe_get_list(container: Any, key: str) -> List[Any]:
    """Safely get a list value, returning empty list for non-list types."""
    val = container.get(key) if isinstance(container, dict) else None
    return val if isinstance(val, list) else []


# ---------------------------------------------------------------------------
# Handover payload
# ---------------------------------------------------------------------------

def build_handover(
    *,
    dispatch_id: str,
    pr_id: str,
    track: str,
    gate: str,
    status: str,
    what_was_done: str,
    key_decisions: List[str],
    files_modified: List[Dict[str, str]],
    tests_run: str,
    tests_passed: str,
    tests_failed: str,
    commands_executed: List[str],
    verification_method: str,
    recommended_action: str,
    action_reason: str,
    blocking_conditions: List[str],
    open_items_created: Optional[List[Dict[str, Any]]] = None,
    findings: Optional[List[Dict[str, str]]] = None,
    residual_risks: Optional[List[Dict[str, str]]] = None,
    deferred_items: Optional[List[Dict[str, str]]] = None,
    critical_context: str = "",
    gotchas: Optional[List[str]] = None,
    relevant_file_paths: Optional[List[str]] = None,
) -> Result:
    """Build a validated handover payload per Section 3 of the contract.

    Returns Result with the handover dict on success, or validation error.
    """
    payload = {
        "handover_version": HANDOVER_VERSION,
        "dispatch_id": dispatch_id,
        "pr_id": pr_id,
        "track": track,
        "gate": gate,
        "status": status,
        "completion_summary": {
            "what_was_done": what_was_done,
            "key_decisions": key_decisions or [],
            "files_modified": files_modified or [],
        },
        "evidence": {
            "tests_run": tests_run,
            "tests_passed": tests_passed,
            "tests_failed": tests_failed,
            "commands_executed": commands_executed or [],
            "verification_method": verification_method,
        },
        "next_action": {
            "recommended_action": recommended_action,
            "reason": action_reason,
            "blocking_conditions": blocking_conditions or [],
        },
        "residual_state": {
            "open_items_created": open_items_created or [],
            "findings": findings or [],
            "residual_risks": residual_risks or [],
            "deferred_items": deferred_items or [],
        },
        "context_for_next": {
            "critical_context": critical_context,
            "gotchas": gotchas or [],
            "relevant_file_paths": relevant_file_paths or [],
        },
    }
    return validate_handover(payload)


def validate_handover(payload: Any) -> Result:
    """Validate a handover payload per Section 3.4 of the contract.

    Returns Result with the payload on success, or validation error.
    Never raises on malformed input — always returns a Result.
    """
    if not isinstance(payload, dict):
        return result_error("invalid_handover", "Payload must be a dict")
    try:
        return _validate_handover_fields(payload)
    except Exception as exc:
        return result_error("invalid_handover", f"Malformed payload: {exc}")


def _validate_handover_fields(payload: Dict[str, Any]) -> Result:
    errors: List[str] = []

    if not DISPATCH_ID_PATTERN.match(str(payload.get("dispatch_id", ""))):
        errors.append(f"Invalid dispatch_id: {payload.get('dispatch_id')}")
    if not PR_ID_PATTERN.match(str(payload.get("pr_id", ""))):
        errors.append(f"Invalid pr_id: {payload.get('pr_id')}")
    if payload.get("track") not in VALID_TRACKS:
        errors.append(f"Invalid track: {payload.get('track')}")
    if not payload.get("gate"):
        errors.append("gate must be non-empty")
    if payload.get("status") not in VALID_STATUSES:
        errors.append(f"Invalid status: {payload.get('status')}")

    next_action = _safe_get_dict(payload, "next_action")
    if next_action.get("recommended_action") not in VALID_ACTIONS:
        errors.append(f"Invalid recommended_action: {next_action.get('recommended_action')}")
    if not next_action.get("reason"):
        errors.append("next_action.reason must be non-empty")

    summary = _safe_get_dict(payload, "completion_summary")
    if not summary.get("what_was_done"):
        errors.append("completion_summary.what_was_done must be non-empty")
    for fm in _safe_get_list(summary, "files_modified"):
        if isinstance(fm, dict):
            err = _validate_file_modified(fm)
            if err:
                errors.append(err)

    evidence = _safe_get_dict(payload, "evidence")
    if evidence.get("verification_method") not in VALID_VERIFICATION_METHODS:
        errors.append(f"Invalid verification_method: {evidence.get('verification_method')}")

    if "residual_state" not in payload:
        errors.append("residual_state section is required (HO-4)")
    else:
        rs = _safe_get_dict(payload, "residual_state")
        for oi in _safe_get_list(rs, "open_items_created"):
            if isinstance(oi, dict):
                err = _validate_open_item(oi)
                if err:
                    errors.append(err)

    ctx = _safe_get_dict(payload, "context_for_next")
    if not ctx.get("critical_context"):
        errors.append("context_for_next.critical_context must be non-empty (HO-5)")

    if errors:
        return result_error("invalid_handover", "; ".join(errors))
    return result_ok(payload)


# ---------------------------------------------------------------------------
# Resume payload
# ---------------------------------------------------------------------------

def build_resume(
    *,
    resume_type: str,
    original_dispatch_id: str,
    original_session_id: str = "",
    work_completed: str,
    work_remaining: str,
    files_in_progress: List[str],
    last_known_state: str,
    key_decisions_made: Optional[List[str]] = None,
    findings_so_far: Optional[List[Dict[str, str]]] = None,
    blockers_encountered: Optional[List[str]] = None,
    task_specification: str = "",
    carry_forward_summary: str = "",
) -> Result:
    """Build a validated resume payload per Section 4 of the contract.

    Returns Result with the resume dict on success, or validation error.
    """
    payload = {
        "resume_version": RESUME_VERSION,
        "resume_type": resume_type,
        "original_dispatch_id": original_dispatch_id,
        "original_session_id": original_session_id,
        "prior_progress": {
            "work_completed": work_completed,
            "work_remaining": work_remaining,
            "files_in_progress": files_in_progress or [],
            "last_known_state": last_known_state,
        },
        "context_snapshot": {
            "key_decisions_made": key_decisions_made or [],
            "findings_so_far": findings_so_far or [],
            "blockers_encountered": blockers_encountered or [],
        },
        "dispatch_context": {
            "task_specification": task_specification,
            "carry_forward_summary": carry_forward_summary,
        },
    }
    return validate_resume(payload)


def validate_resume(payload: Any) -> Result:
    """Validate a resume payload per Section 4.4 of the contract.

    Returns Result with the payload on success, or validation error.
    Never raises on malformed input — always returns a Result.
    """
    if not isinstance(payload, dict):
        return result_error("invalid_resume", "Payload must be a dict")
    try:
        return _validate_resume_fields(payload)
    except Exception as exc:
        return result_error("invalid_resume", f"Malformed payload: {exc}")


def _validate_resume_fields(payload: Dict[str, Any]) -> Result:
    errors: List[str] = []

    resume_type = str(payload.get("resume_type", ""))
    if resume_type not in VALID_RESUME_TYPES:
        errors.append(f"Invalid resume_type: {resume_type}")

    if not DISPATCH_ID_PATTERN.match(str(payload.get("original_dispatch_id", ""))):
        errors.append(f"Invalid original_dispatch_id: {payload.get('original_dispatch_id')}")

    dispatch_ctx = _safe_get_dict(payload, "dispatch_context")
    task_spec = str(dispatch_ctx.get("task_specification", ""))
    if not task_spec:
        errors.append("dispatch_context.task_specification is required (RS-1)")

    prior = _safe_get_dict(payload, "prior_progress")

    if resume_type == "rotation":
        wc = str(prior.get("work_completed", ""))
        if not wc or _is_vague(wc):
            errors.append("rotation resume: work_completed must be specific (RS-2)")

    if resume_type == "interruption":
        lks = str(prior.get("last_known_state", ""))
        if not lks or _is_vague(lks):
            errors.append("interruption resume: last_known_state must be specific (RS-3)")

    if resume_type == "redispatch":
        snapshot = _safe_get_dict(payload, "context_snapshot")
        if not _safe_get_list(snapshot, "findings_so_far"):
            errors.append("redispatch resume: findings_so_far must include prior findings (RS-4)")

    # RS-5: No raw conversation history
    for field_name, value in [
        ("work_completed", str(prior.get("work_completed", ""))),
        ("work_remaining", str(prior.get("work_remaining", ""))),
        ("last_known_state", str(prior.get("last_known_state", ""))),
        ("task_specification", task_spec),
    ]:
        if _contains_transcript(value):
            errors.append(f"{field_name} contains conversation transcript (RS-5)")

    if not prior.get("work_remaining"):
        errors.append("prior_progress.work_remaining must be non-empty")

    if errors:
        return result_error("invalid_resume", "; ".join(errors))
    return result_ok(payload)
