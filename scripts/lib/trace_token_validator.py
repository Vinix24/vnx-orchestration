#!/usr/bin/env python3
"""CLI-agnostic trace token validation for VNX provenance enforcement.

Implements the trace token spec from docs/core/42_FPD_PROVENANCE_CONTRACT.md.
Used by git hooks, CI checks, and receipt validation.

Environment variables:
    VNX_PROVENANCE_ENFORCEMENT: "0" = shadow (warn), "1" = enforced (block)
    VNX_PROVENANCE_LEGACY_ACCEPTED: "1" = accept legacy formats (default), "0" = preferred only
    VNX_CURRENT_DISPATCH_ID: current dispatch context for prepare-commit-msg
"""

from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional

# ── Trace token regexes (from PR-0 contract Section 2.4) ────────────────

PREFERRED_RE = re.compile(r"^Dispatch-ID:\s+(\S+)$", re.MULTILINE)
LEGACY_DISPATCH_RE = re.compile(r"dispatch:(\S+)")
LEGACY_PR_RE = re.compile(r"\bPR-(\d+)\b")
LEGACY_FP_RE = re.compile(r"\bFP-([A-Z])\b")

# Dispatch ID format: YYYYMMDD-HHMMSS-<slug>-<track>
DISPATCH_ID_RE = re.compile(r"^\d{8}-\d{6}-.+-[A-Z]$")


class TokenFormat(Enum):
    PREFERRED = "preferred"
    LEGACY_DISPATCH = "legacy_dispatch"
    LEGACY_PR = "legacy_pr"
    LEGACY_FP = "legacy_fp"


class Severity(Enum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class EnforcementMode(Enum):
    SHADOW = "shadow"
    ENFORCED = "enforced"


@dataclass
class TraceTokens:
    """All trace tokens extracted from a commit message."""

    preferred: Optional[str] = None
    legacy_dispatch: Optional[str] = None
    legacy_pr: List[str] = field(default_factory=list)
    legacy_fp: List[str] = field(default_factory=list)

    @property
    def has_preferred(self) -> bool:
        return self.preferred is not None

    @property
    def has_any_legacy(self) -> bool:
        return (
            self.legacy_dispatch is not None
            or len(self.legacy_pr) > 0
            or len(self.legacy_fp) > 0
        )

    @property
    def has_any(self) -> bool:
        return self.has_preferred or self.has_any_legacy

    @property
    def primary_format(self) -> Optional[TokenFormat]:
        if self.has_preferred:
            return TokenFormat.PREFERRED
        if self.legacy_dispatch is not None:
            return TokenFormat.LEGACY_DISPATCH
        if self.legacy_pr:
            return TokenFormat.LEGACY_PR
        if self.legacy_fp:
            return TokenFormat.LEGACY_FP
        return None

    @property
    def primary_id(self) -> Optional[str]:
        if self.preferred:
            return self.preferred
        if self.legacy_dispatch:
            return self.legacy_dispatch
        if self.legacy_pr:
            return f"PR-{self.legacy_pr[0]}"
        if self.legacy_fp:
            return f"FP-{self.legacy_fp[0]}"
        return None


@dataclass
class ValidationResult:
    """Result of trace token validation."""

    valid: bool
    format: Optional[TokenFormat]
    severity: Severity
    enforcement_mode: EnforcementMode
    dispatch_id: Optional[str] = None
    tokens: Optional[TraceTokens] = None
    message: str = ""
    warnings: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "valid": self.valid,
            "format": self.format.value if self.format else None,
            "severity": self.severity.value,
            "enforcement_mode": self.enforcement_mode.value,
            "dispatch_id": self.dispatch_id,
            "message": self.message,
            "warnings": self.warnings,
        }

    def to_gap_event(self, entity_id: str, actor: str) -> dict:
        """Generate a provenance_gap coordination event (Section 5.2)."""
        return {
            "event_type": "provenance_gap",
            "entity_type": "commit",
            "entity_id": entity_id,
            "actor": actor,
            "reason": self.message,
            "metadata_json": {
                "gap_type": "missing_trace_token" if not self.valid else "legacy_format",
                "severity": self.severity.value,
                "enforcement_mode": self.enforcement_mode.value,
                "trace_token_found": self.dispatch_id,
                "expected_dispatch_id": None,
            },
        }


def get_enforcement_mode() -> EnforcementMode:
    """Read enforcement mode from environment."""
    val = os.environ.get("VNX_PROVENANCE_ENFORCEMENT", "0")
    return EnforcementMode.ENFORCED if val == "1" else EnforcementMode.SHADOW


def is_legacy_accepted() -> bool:
    """Check if legacy trace token formats are accepted."""
    return os.environ.get("VNX_PROVENANCE_LEGACY_ACCEPTED", "1") == "1"


def get_current_dispatch_id() -> Optional[str]:
    """Read current dispatch ID from environment."""
    return os.environ.get("VNX_CURRENT_DISPATCH_ID") or None


def extract_trace_tokens(commit_message: str) -> TraceTokens:
    """Extract all trace tokens from a commit message."""
    tokens = TraceTokens()

    m = PREFERRED_RE.search(commit_message)
    if m:
        tokens.preferred = m.group(1)

    m = LEGACY_DISPATCH_RE.search(commit_message)
    if m:
        tokens.legacy_dispatch = m.group(1)

    tokens.legacy_pr = LEGACY_PR_RE.findall(commit_message)
    tokens.legacy_fp = LEGACY_FP_RE.findall(commit_message)

    return tokens


def validate_dispatch_id_format(dispatch_id: str) -> bool:
    """Check if a dispatch ID matches the expected format."""
    return bool(DISPATCH_ID_RE.match(dispatch_id))


def validate_trace_token(
    commit_message: str,
    enforcement_mode: Optional[EnforcementMode] = None,
    legacy_accepted: Optional[bool] = None,
) -> ValidationResult:
    """Validate a commit message for trace token presence.

    Args:
        commit_message: Full commit message text
        enforcement_mode: Override enforcement mode (defaults to env var)
        legacy_accepted: Override legacy acceptance (defaults to env var)

    Returns:
        ValidationResult with validity, format, severity, and diagnostics
    """
    if enforcement_mode is None:
        enforcement_mode = get_enforcement_mode()
    if legacy_accepted is None:
        legacy_accepted = is_legacy_accepted()

    tokens = extract_trace_tokens(commit_message)
    warnings: List[str] = []

    # Preferred format found
    if tokens.has_preferred:
        dispatch_id = tokens.preferred
        if not validate_dispatch_id_format(dispatch_id):
            warnings.append(
                f"Dispatch ID '{dispatch_id}' does not match expected format "
                f"(YYYYMMDD-HHMMSS-<slug>-<track>)"
            )
        return ValidationResult(
            valid=True,
            format=TokenFormat.PREFERRED,
            severity=Severity.INFO,
            enforcement_mode=enforcement_mode,
            dispatch_id=dispatch_id,
            tokens=tokens,
            message="Preferred trace token found",
            warnings=warnings,
        )

    # Legacy formats
    if tokens.has_any_legacy:
        if not legacy_accepted:
            severity = (
                Severity.ERROR
                if enforcement_mode == EnforcementMode.ENFORCED
                else Severity.WARNING
            )
            return ValidationResult(
                valid=False,
                format=tokens.primary_format,
                severity=severity,
                enforcement_mode=enforcement_mode,
                dispatch_id=tokens.primary_id,
                tokens=tokens,
                message=(
                    "Only preferred Dispatch-ID format accepted "
                    "(VNX_PROVENANCE_LEGACY_ACCEPTED=0)"
                ),
                warnings=["Legacy format found but legacy acceptance is disabled"],
            )

        warnings.append(
            "Legacy trace format detected; new commits should use "
            "'Dispatch-ID: <id>' in the commit body"
        )
        return ValidationResult(
            valid=True,
            format=tokens.primary_format,
            severity=Severity.WARNING,
            enforcement_mode=enforcement_mode,
            dispatch_id=tokens.primary_id,
            tokens=tokens,
            message="Legacy trace token accepted",
            warnings=warnings,
        )

    # No trace token found
    severity = (
        Severity.ERROR
        if enforcement_mode == EnforcementMode.ENFORCED
        else Severity.WARNING
    )
    return ValidationResult(
        valid=False,
        format=None,
        severity=severity,
        enforcement_mode=enforcement_mode,
        tokens=tokens,
        message="No trace token found in commit message",
        warnings=[],
    )


def inject_trace_token(commit_message: str, dispatch_id: str) -> str:
    """Inject a Dispatch-ID line into a commit message if not already present.

    Used by prepare-commit-msg hook. Appends after the commit body,
    preserving any existing content.
    """
    tokens = extract_trace_tokens(commit_message)
    if tokens.has_preferred:
        return commit_message

    line = f"\nDispatch-ID: {dispatch_id}\n"

    # Strip trailing whitespace, append token
    stripped = commit_message.rstrip()
    if not stripped:
        return line.lstrip("\n")

    return stripped + "\n" + line


def validate_commits(
    commit_messages: List[str],
    enforcement_mode: Optional[EnforcementMode] = None,
    legacy_accepted: Optional[bool] = None,
) -> dict:
    """Validate multiple commit messages. Used by CI checks.

    Returns a summary dict with per-commit results and overall status.
    """
    if enforcement_mode is None:
        enforcement_mode = get_enforcement_mode()
    if legacy_accepted is None:
        legacy_accepted = is_legacy_accepted()

    results = []
    valid_count = 0
    invalid_count = 0
    legacy_count = 0

    for msg in commit_messages:
        result = validate_trace_token(msg, enforcement_mode, legacy_accepted)
        results.append(result.to_dict())
        if result.valid:
            valid_count += 1
            if result.format and result.format != TokenFormat.PREFERRED:
                legacy_count += 1
        else:
            invalid_count += 1

    return {
        "total": len(commit_messages),
        "valid": valid_count,
        "invalid": invalid_count,
        "legacy": legacy_count,
        "enforcement_mode": enforcement_mode.value,
        "all_valid": invalid_count == 0,
        "results": results,
    }


# ── CLI entry point ─────────────────────────────────────────────────────

def _cli_validate(args: List[str]) -> int:
    """Validate a commit message from stdin or file."""
    if args and args[0] != "-":
        from pathlib import Path
        msg = Path(args[0]).read_text()
    else:
        msg = sys.stdin.read()

    result = validate_trace_token(msg)
    print(json.dumps(result.to_dict(), indent=2))
    return 0 if result.valid else 1


def _cli_inject(args: List[str]) -> int:
    """Inject trace token into a commit message file."""
    dispatch_id = get_current_dispatch_id()
    if not dispatch_id:
        return 0  # No dispatch context, nothing to inject

    if not args:
        print("Usage: trace_token_validator.py inject <commit-msg-file>", file=sys.stderr)
        return 1

    from pathlib import Path
    msg_file = Path(args[0])
    original = msg_file.read_text()
    updated = inject_trace_token(original, dispatch_id)
    if updated != original:
        msg_file.write_text(updated)
    return 0


def _cli_check_commits(args: List[str]) -> int:
    """CI mode: check commit messages from stdin (one per \\x00 separator) or git log."""
    messages = sys.stdin.read().strip()
    if not messages:
        print("No commit messages to check", file=sys.stderr)
        return 0

    commit_list = messages.split("\x00") if "\x00" in messages else [messages]
    commit_list = [m.strip() for m in commit_list if m.strip()]

    summary = validate_commits(commit_list)
    print(json.dumps(summary, indent=2))

    if not summary["all_valid"]:
        if get_enforcement_mode() == EnforcementMode.ENFORCED:
            return 1
    return 0


def main() -> int:
    if len(sys.argv) < 2:
        print(
            "Usage: trace_token_validator.py <validate|inject|check-commits> [args...]",
            file=sys.stderr,
        )
        return 1

    command = sys.argv[1]
    args = sys.argv[2:]

    if command == "validate":
        return _cli_validate(args)
    elif command == "inject":
        return _cli_inject(args)
    elif command == "check-commits":
        return _cli_check_commits(args)
    else:
        print(f"Unknown command: {command}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
