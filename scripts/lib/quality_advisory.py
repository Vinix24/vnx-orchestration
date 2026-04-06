#!/usr/bin/env python3
"""Quality advisory pipeline for VNX completion receipts.

Performs code quality checks on changed files and generates structured advisories
for T0 decision-making. Model-agnostic - runs from VNX scripts after receipt ingestion.
"""

from __future__ import annotations

import ast
import json
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

# File size thresholds
FILE_SIZE_WARNING_PYTHON = 500
FILE_SIZE_BLOCKING_PYTHON = 800
FILE_SIZE_WARNING_SHELL = 300
FILE_SIZE_BLOCKING_SHELL = 500

# Function size thresholds
FUNCTION_SIZE_WARNING_PYTHON = 40
FUNCTION_SIZE_BLOCKING_PYTHON = 70
FUNCTION_SIZE_WARNING_SHELL = 30
FUNCTION_SIZE_BLOCKING_SHELL = 50

# Risk score weights
RISK_WEIGHT_BLOCKING = 50
RISK_WEIGHT_WARNING = 10


@dataclass
class QualityCheck:
    """Single quality check result."""
    check_id: str
    severity: str  # info|warning|blocking
    file: str
    symbol: Optional[str] = None
    message: str = ""
    evidence: str = ""
    action_required: bool = False


@dataclass
class QualityAdvisory:
    """Complete quality advisory for a completion receipt."""
    version: str = "1.0"
    generated_at: str = field(default_factory=lambda: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    scope: List[str] = field(default_factory=list)
    checks: List[Dict[str, Any]] = field(default_factory=list)
    summary: Dict[str, Any] = field(default_factory=dict)
    t0_recommendation: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "version": self.version,
            "generated_at": self.generated_at,
            "scope": self.scope,
            "checks": self.checks,
            "summary": self.summary,
            "t0_recommendation": self.t0_recommendation,
        }


def get_changed_files(repo_root: Optional[Path] = None) -> List[Path]:
    """Get list of changed files from git diff.

    Returns files that are:
    - Modified (M)
    - Added (A)
    - Renamed (R)

    Excludes deleted files.
    """
    if repo_root is None:
        repo_root = Path.cwd()

    try:
        # Compare last commit against its parent (HEAD~1..HEAD).
        # Terminals commit their work before the receipt processor runs,
        # so `git diff HEAD` (uncommitted vs HEAD) would always be empty.
        result = subprocess.run(
            ["git", "diff", "--name-status", "HEAD~1", "HEAD"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=True,
        )

        changed_files = _parse_name_status(result.stdout, repo_root)

        if changed_files:
            return changed_files

        # Fallback: also check uncommitted changes (staged + unstaged).
        # Agents may still be working when the receipt fires.
        result2 = subprocess.run(
            ["git", "diff", "--name-status", "HEAD"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=True,
        )
        return _parse_name_status(result2.stdout, repo_root)
    except subprocess.CalledProcessError:
        # HEAD~1 fails when only 1 commit exists (e.g. fresh demo).
        # Fall back to listing all tracked files in the initial commit.
        # --root is required so diff-tree shows the initial commit's files.
        try:
            result = subprocess.run(
                ["git", "diff-tree", "--root", "--no-commit-id", "--name-status", "-r", "HEAD"],
                cwd=repo_root,
                capture_output=True,
                text=True,
                check=True,
            )
            return _parse_name_status(result.stdout, repo_root)
        except subprocess.CalledProcessError:
            return []


def _parse_name_status(output: str, repo_root: Path) -> "List[Path]":
    """Parse git diff --name-status output into resolved file paths."""
    changed_files = []
    for line in output.strip().split("\n"):
        if not line:
            continue
        parts = line.split(maxsplit=1)
        if len(parts) != 2:
            continue
        status, filepath = parts
        # Only include M (modified), A (added), R (renamed)
        if status in ("M", "A") or status.startswith("R"):
            file_path = repo_root / filepath
            if file_path.exists():
                changed_files.append(file_path.resolve())
    return changed_files


def check_file_size(file_path: Path) -> List[QualityCheck]:
    """Check file size against thresholds."""
    checks = []

    try:
        lines = file_path.read_text(encoding="utf-8").splitlines()
        line_count = len(lines)

        # Determine thresholds based on file type
        if file_path.suffix == ".py":
            warning_threshold = FILE_SIZE_WARNING_PYTHON
            blocking_threshold = FILE_SIZE_BLOCKING_PYTHON
        elif file_path.suffix == ".sh" or file_path.name.endswith(".bash"):
            warning_threshold = FILE_SIZE_WARNING_SHELL
            blocking_threshold = FILE_SIZE_BLOCKING_SHELL
        else:
            # Skip files we don't have thresholds for
            return checks

        if line_count > blocking_threshold:
            checks.append(QualityCheck(
                check_id="file_size_blocking",
                severity="blocking",
                file=str(file_path),
                message=f"File exceeds blocking threshold: {line_count} lines (max {blocking_threshold})",
                evidence=f"lines={line_count},max={blocking_threshold}",
                action_required=True,
            ))
        elif line_count > warning_threshold:
            checks.append(QualityCheck(
                check_id="file_size_warning",
                severity="warning",
                file=str(file_path),
                message=f"File exceeds warning threshold: {line_count} lines (max {warning_threshold})",
                evidence=f"lines={line_count},max={warning_threshold}",
                action_required=False,
            ))
    except (OSError, UnicodeDecodeError):
        pass  # Skip files we can't read

    return checks


def check_function_sizes(file_path: Path) -> List[QualityCheck]:
    """Check function sizes against thresholds."""
    checks = []

    if file_path.suffix == ".py":
        checks.extend(_check_python_function_sizes(file_path))
    elif file_path.suffix == ".sh" or file_path.name.endswith(".bash"):
        checks.extend(_check_shell_function_sizes(file_path))

    return checks


def _check_python_function_sizes(file_path: Path) -> List[QualityCheck]:
    """Check Python function sizes."""
    checks = []

    try:
        content = file_path.read_text(encoding="utf-8")
        tree = ast.parse(content, filename=str(file_path))

        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if node.end_lineno is None:
                continue

            length = node.end_lineno - node.lineno + 1

            if length > FUNCTION_SIZE_BLOCKING_PYTHON:
                checks.append(QualityCheck(
                    check_id="function_size_blocking",
                    severity="blocking",
                    file=str(file_path),
                    symbol=node.name,
                    message=f"Function exceeds blocking threshold: {length} lines (max {FUNCTION_SIZE_BLOCKING_PYTHON})",
                    evidence=f"function={node.name},lines={length},max={FUNCTION_SIZE_BLOCKING_PYTHON}",
                    action_required=True,
                ))
            elif length > FUNCTION_SIZE_WARNING_PYTHON:
                checks.append(QualityCheck(
                    check_id="function_size_warning",
                    severity="warning",
                    file=str(file_path),
                    symbol=node.name,
                    message=f"Function exceeds warning threshold: {length} lines (max {FUNCTION_SIZE_WARNING_PYTHON})",
                    evidence=f"function={node.name},lines={length},max={FUNCTION_SIZE_WARNING_PYTHON}",
                    action_required=False,
                ))
    except (OSError, SyntaxError, UnicodeDecodeError):
        pass  # Skip files we can't parse

    return checks


def _check_shell_function_sizes(file_path: Path) -> List[QualityCheck]:
    """Check shell function sizes."""
    checks = []
    pattern = re.compile(r"^\s*(?:function\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*(?:\(\))?\s*\{\s*$")

    try:
        lines = file_path.read_text(encoding="utf-8").splitlines()
        index = 0

        while index < len(lines):
            match = pattern.match(lines[index])
            if not match:
                index += 1
                continue

            function_name = match.group(1)
            start_line = index + 1
            depth = lines[index].count("{") - lines[index].count("}")
            cursor = index

            while cursor + 1 < len(lines) and depth > 0:
                cursor += 1
                depth += lines[cursor].count("{")
                depth -= lines[cursor].count("}")

            length = cursor - index + 1

            if length > FUNCTION_SIZE_BLOCKING_SHELL:
                checks.append(QualityCheck(
                    check_id="function_size_blocking",
                    severity="blocking",
                    file=str(file_path),
                    symbol=function_name,
                    message=f"Function exceeds blocking threshold: {length} lines (max {FUNCTION_SIZE_BLOCKING_SHELL})",
                    evidence=f"function={function_name},lines={length},max={FUNCTION_SIZE_BLOCKING_SHELL}",
                    action_required=True,
                ))
            elif length > FUNCTION_SIZE_WARNING_SHELL:
                checks.append(QualityCheck(
                    check_id="function_size_warning",
                    severity="warning",
                    file=str(file_path),
                    symbol=function_name,
                    message=f"Function exceeds warning threshold: {length} lines (max {FUNCTION_SIZE_WARNING_SHELL})",
                    evidence=f"function={function_name},lines={length},max={FUNCTION_SIZE_WARNING_SHELL}",
                    action_required=False,
                ))

            index = cursor + 1
    except (OSError, UnicodeDecodeError):
        pass  # Skip files we can't read

    return checks


def run_linting(file_path: Path) -> List[QualityCheck]:
    """Run linting checks on file."""
    checks = []

    if file_path.suffix == ".py":
        checks.extend(_run_ruff_check(file_path))
    elif file_path.suffix == ".sh" or file_path.name.endswith(".bash"):
        checks.extend(_run_shellcheck(file_path))

    return checks


def _run_ruff_check(file_path: Path) -> List[QualityCheck]:
    """Run ruff linter on Python file."""
    checks = []

    try:
        result = subprocess.run(
            ["ruff", "check", "--output-format=json", str(file_path)],
            capture_output=True,
            text=True,
            timeout=10,
        )

        if result.stdout:
            findings = json.loads(result.stdout)
            for finding in findings:
                # Map ruff severity to our severity levels
                # E/F series are errors, W series are warnings
                ruff_code = finding.get("code", "")
                severity = "warning"
                if ruff_code.startswith(("E", "F")):
                    severity = "warning"  # Most lint errors are warnings, not blocking

                checks.append(QualityCheck(
                    check_id=f"lint_{ruff_code.lower()}",
                    severity=severity,
                    file=str(file_path),
                    symbol=finding.get("code"),
                    message=finding.get("message", ""),
                    evidence=f"line={finding.get('location', {}).get('row')},code={ruff_code}",
                    action_required=False,
                ))
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, json.JSONDecodeError, FileNotFoundError):
        pass  # Linter not available or failed

    return checks


def _run_shellcheck(file_path: Path) -> List[QualityCheck]:
    """Run shellcheck on shell script."""
    checks = []

    try:
        result = subprocess.run(
            ["shellcheck", "-f", "json", str(file_path)],
            capture_output=True,
            text=True,
            timeout=10,
        )

        if result.stdout:
            findings = json.loads(result.stdout)
            for finding in findings:
                # Map shellcheck level to our severity
                level = finding.get("level", "info")
                severity = "warning"
                if level == "error":
                    severity = "warning"  # Still just warnings, not blocking

                checks.append(QualityCheck(
                    check_id=f"lint_sc{finding.get('code')}",
                    severity=severity,
                    file=str(file_path),
                    symbol=f"SC{finding.get('code')}",
                    message=finding.get("message", ""),
                    evidence=f"line={finding.get('line')},code=SC{finding.get('code')}",
                    action_required=False,
                ))
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, json.JSONDecodeError, FileNotFoundError):
        pass  # Shellcheck not available or failed

    return checks


def check_dead_code(file_path: Path) -> List[QualityCheck]:
    """Check for dead code (Python only)."""
    checks = []

    if file_path.suffix != ".py":
        return checks

    try:
        result = subprocess.run(
            ["vulture", "--min-confidence", "80", str(file_path)],
            capture_output=True,
            text=True,
            timeout=10,
        )

        # Vulture outputs findings to stdout, one per line
        for line in result.stdout.strip().split("\n"):
            if not line or "%" not in line:
                continue

            # Parse vulture output: "file.py:123: unused function 'foo' (80% confidence)"
            match = re.match(r"^(.+):(\d+):\s*(.+)\s*\((\d+)%", line)
            if match:
                checks.append(QualityCheck(
                    check_id="dead_code_detected",
                    severity="warning",
                    file=str(file_path),
                    message=match.group(3),
                    evidence=f"line={match.group(2)},confidence={match.group(4)}%",
                    action_required=False,
                ))
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
        pass  # Vulture not available or failed

    return checks


def check_test_coverage_hygiene(changed_files: List[Path], repo_root: Path) -> List[QualityCheck]:
    """Check if src changes have corresponding test changes."""
    checks = []

    # Find src files that changed
    src_changes = [f for f in changed_files if "/src/" in str(f) and f.suffix == ".py"]

    # Find test files that changed
    test_changes = [f for f in changed_files if "/test" in str(f) and f.suffix == ".py"]

    # If src changed but no tests changed, emit warning
    if src_changes and not test_changes:
        checks.append(QualityCheck(
            check_id="missing_test_delta",
            severity="warning",
            file=str(src_changes[0]),  # Reference first changed src file
            message=f"{len(src_changes)} src file(s) changed but no test files modified",
            evidence=f"src_changes={len(src_changes)},test_changes=0",
            action_required=False,
        ))

    return checks


def calculate_risk_score(checks: List[QualityCheck]) -> int:
    """Calculate risk score (0-100) based on checks."""
    score = 0

    for check in checks:
        if check.severity == "blocking":
            score += RISK_WEIGHT_BLOCKING
        elif check.severity == "warning":
            score += RISK_WEIGHT_WARNING

    return min(score, 100)


def make_t0_decision(checks: List[QualityCheck], risk_score: int) -> Dict[str, Any]:
    """Generate T0 recommendation based on checks and risk score."""
    blocking_count = sum(1 for c in checks if c.severity == "blocking")
    warning_count = sum(1 for c in checks if c.severity == "warning")

    if blocking_count > 0:
        return {
            "decision": "hold",
            "reason": f"{blocking_count} blocking issue(s) detected",
            "suggested_dispatches": _generate_followup_tasks(checks, blocking_only=True),
            "open_items": _generate_open_items(checks, blocking_only=True),
        }

    if warning_count >= 2 or risk_score >= 50:
        return {
            "decision": "approve_with_followup",
            "reason": f"{warning_count} warning(s) detected, risk_score={risk_score}",
            "suggested_dispatches": _generate_followup_tasks(checks, blocking_only=False),
            "open_items": _generate_open_items(checks, blocking_only=False),
        }

    return {
        "decision": "approve",
        "reason": "No significant quality issues detected",
        "suggested_dispatches": [],
        "open_items": [],
    }


def _generate_followup_tasks(checks: List[QualityCheck], blocking_only: bool) -> List[Dict[str, str]]:
    """Generate suggested follow-up dispatch tasks."""
    tasks = []

    relevant_checks = [c for c in checks if c.severity == "blocking"] if blocking_only else checks

    # Group by check type
    file_size_issues = [c for c in relevant_checks if "file_size" in c.check_id]
    function_size_issues = [c for c in relevant_checks if "function_size" in c.check_id]
    lint_issues = [c for c in relevant_checks if c.check_id.startswith("lint_")]
    dead_code_issues = [c for c in relevant_checks if c.check_id == "dead_code_detected"]
    test_issues = [c for c in relevant_checks if c.check_id == "missing_test_delta"]

    if file_size_issues:
        tasks.append({
            "type": "refactoring",
            "description": f"Split {len(file_size_issues)} oversized file(s)",
            "files": list({c.file for c in file_size_issues}),
        })

    if function_size_issues:
        tasks.append({
            "type": "refactoring",
            "description": f"Refactor {len(function_size_issues)} oversized function(s)",
            "files": list({c.file for c in function_size_issues}),
        })

    if lint_issues:
        tasks.append({
            "type": "cleanup",
            "description": f"Fix {len(lint_issues)} linting issue(s)",
            "files": list({c.file for c in lint_issues}),
        })

    if dead_code_issues:
        tasks.append({
            "type": "cleanup",
            "description": f"Remove {len(dead_code_issues)} dead code finding(s)",
            "files": list({c.file for c in dead_code_issues}),
        })

    if test_issues:
        tasks.append({
            "type": "testing",
            "description": "Add tests for src/ changes",
            "files": [],
        })

    return tasks


def _generate_open_items(checks: List[QualityCheck], blocking_only: bool) -> List[Dict[str, Any]]:
    """Generate open items list suitable for feature-plan format."""
    items = []

    relevant_checks = [c for c in checks if c.severity == "blocking"] if blocking_only else checks

    for check in relevant_checks:
        items.append({
            "item": check.message,
            "file": check.file,
            "severity": check.severity,
            "check_id": check.check_id,
            "symbol": check.symbol,
        })

    return items


def generate_quality_advisory(
    changed_files: List[Path],
    repo_root: Optional[Path] = None,
) -> QualityAdvisory:
    """Generate complete quality advisory for changed files.

    Args:
        changed_files: List of changed file paths
        repo_root: Repository root path

    Returns:
        QualityAdvisory object with all checks and recommendations
    """
    if repo_root is None:
        repo_root = Path.cwd()

    advisory = QualityAdvisory()
    advisory.scope = [str(f) for f in changed_files]

    all_checks: List[QualityCheck] = []

    # Run checks on each changed file
    for file_path in changed_files:
        all_checks.extend(check_file_size(file_path))
        all_checks.extend(check_function_sizes(file_path))
        all_checks.extend(run_linting(file_path))
        all_checks.extend(check_dead_code(file_path))

    # Test coverage hygiene check (across all files)
    all_checks.extend(check_test_coverage_hygiene(changed_files, repo_root))

    # Convert checks to dict format
    advisory.checks = [
        {
            "check_id": c.check_id,
            "severity": c.severity,
            "file": c.file,
            "symbol": c.symbol,
            "message": c.message,
            "evidence": c.evidence,
            "action_required": c.action_required,
        }
        for c in all_checks
    ]

    # Calculate summary
    warning_count = sum(1 for c in all_checks if c.severity == "warning")
    blocking_count = sum(1 for c in all_checks if c.severity == "blocking")
    risk_score = calculate_risk_score(all_checks)

    advisory.summary = {
        "warning_count": warning_count,
        "blocking_count": blocking_count,
        "risk_score": risk_score,
    }

    # Generate T0 recommendation
    advisory.t0_recommendation = make_t0_decision(all_checks, risk_score)

    return advisory
