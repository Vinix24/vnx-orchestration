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

# File size thresholds. The blocking threshold is a HARD CEILING: over it a file is a
# monolith that must be split. Below it, the warning threshold is advisory only.
FILE_SIZE_WARNING_PYTHON = 500
FILE_SIZE_BLOCKING_PYTHON = 1200
FILE_SIZE_WARNING_SHELL = 300
FILE_SIZE_BLOCKING_SHELL = 600

# Files that ALREADY exceed the hard ceiling predate this gate. Blocking every PR that
# touches one would freeze legitimate work, so each is grandfathered here with a reason and
# surfaced as a standing advisory (refactor backlog) instead of a hard block. A NEW file
# over the ceiling is NOT on this list and therefore blocks — that is the point: no new
# monoliths; the existing ones get a refactor entry, not an eternal silent exempt.
# Refactor home: horizon tracks `quality-advisory-code-health` + `retire-redundant-architecture`.
# Keys are EXTENSION-LESS repo-relative paths, matched against the suffix-stripped file path.
# Extension-less on purpose: a lane-monolith path with its ".py" suffix would read as a
# lane-script delivery reference to dispatch_sidedoor_audit.py and false-flag this file as a
# side door. Dropping the suffix keeps that exhaustiveness audit pure without an allowlist exception.
FILE_SIZE_ALLOWLIST: Dict[str, str] = {
    "scripts/migrate_future_system": "grandfathered monolith; future-state migration",
    "scripts/pr_queue_manager": "grandfathered monolith; PR-queue manager",
    "scripts/lib/tmux_interactive_dispatch": "grandfathered monolith; tmux subscription lane",
    "scripts/build_t0_state": "grandfathered monolith; T0 state projection",
    "scripts/migrate_to_central_vnx": "grandfathered monolith; central-store migration",
    "scripts/planning_cli": "grandfathered monolith; planning/horizon CLI",
    "scripts/lib/provider_dispatch": "grandfathered monolith; provider dispatch lane",
    "scripts/llm_benchmark": "grandfathered monolith; model benchmark harness",
    "scripts/gather_intelligence": "grandfathered monolith; intelligence gather",
    "scripts/aggregator/t0_lifecycle": "grandfathered monolith; aggregator lifecycle",
    "scripts/quality_db_init": "grandfathered monolith; quality DB init",
    "scripts/lib/tenant_stamping": "grandfathered monolith; ADR-007 tenant stamping",
    "scripts/closure_verifier": "grandfathered monolith; closure verifier",
    "scripts/retroactive_backfill": "grandfathered monolith; retroactive backfill tool",
    "scripts/commands/dispatch": "grandfathered shell monolith; dispatch entrypoint",
    "scripts/commands/start": "grandfathered shell monolith; start command",
    "scripts/dispatcher_minimal": "grandfathered shell monolith; dispatcher",
    "scripts/generate_t0_brief": "grandfathered shell monolith; T0 brief generator",
    "scripts/smart_tap_json_translator": "grandfathered shell monolith; smart-tap translator",
    "dashboard/api_intelligence": "grandfathered monolith; dashboard intelligence API",
    "dashboard/api_operator": "grandfathered monolith; dashboard operator API",
}


def _is_test_file(file_path: Path) -> bool:
    """Test files are exempt from the file-size BLOCK: a thorough suite is legitimately large.
    They still get the advisory warning, never a hard block."""
    name = file_path.name
    return (
        "tests" in file_path.parts
        or name.startswith("test_")
        or name.endswith("_test.py")
        or name == "conftest.py"
    )


def _file_size_allowlist_reason(file_path: Path) -> Optional[str]:
    """Return the grandfather reason if ``file_path`` is on the size allowlist, else None.

    Keys are extension-less; the file path's own extension is stripped before matching, on
    exact key or a path-suffix (with a ``/`` boundary) so the absolute resolved path the
    diff scanner passes still resolves.
    """
    stem = str(file_path.with_suffix(""))
    for rel, reason in FILE_SIZE_ALLOWLIST.items():
        if stem == rel or stem.endswith("/" + rel):
            return reason
    return None

# Function size thresholds
FUNCTION_SIZE_WARNING_PYTHON = 40
FUNCTION_SIZE_BLOCKING_PYTHON = 70
FUNCTION_SIZE_WARNING_SHELL = 30
FUNCTION_SIZE_BLOCKING_SHELL = 60

# Risk score weights
RISK_WEIGHT_BLOCKING = 50
RISK_WEIGHT_WARNING = 10


@dataclass
class QualityCheck:
    check_id: str
    severity: str  # info|warning|blocking
    file: str
    symbol: Optional[str] = None
    message: str = ""
    evidence: str = ""
    action_required: bool = False
    tool: Optional[str] = None
    level: Optional[str] = None
    code: Optional[str] = None


@dataclass
class QualityAdvisory:
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


class CheckRecord(dict):
    """Dict check record with attribute access for object-style callers."""

    def __getattr__(self, name: str) -> Any:
        return self.get(name)


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
            allow_reason = _file_size_allowlist_reason(file_path)
            if allow_reason is None and not _is_test_file(file_path):
                # Over the hard ceiling and not grandfathered: a real block. A HOLD here
                # stops a new monolith from landing (split it, or add a reasoned allowlist entry).
                checks.append(QualityCheck(
                    check_id="file_size_blocking",
                    severity="blocking",
                    file=str(file_path),
                    message=(
                        f"File exceeds the hard size ceiling: {line_count} lines "
                        f"(max {blocking_threshold}). Split it, or add a reasoned "
                        f"FILE_SIZE_ALLOWLIST entry if the size is deliberate."
                    ),
                    evidence=f"lines={line_count},max={blocking_threshold}",
                    action_required=True,
                ))
            else:
                # Grandfathered monolith or a (large-but-legitimate) test file: surfaced as a
                # standing advisory (refactor backlog), not a block, so touching it never
                # freezes legitimate work.
                _reason = allow_reason or "test file (size gate is advisory for tests)"
                checks.append(QualityCheck(
                    check_id="file_size_grandfathered",
                    severity="warning",
                    file=str(file_path),
                    message=(
                        f"Large file (advisory): {line_count} lines "
                        f"(over {blocking_threshold}) — {_reason}"
                    ),
                    evidence=f"lines={line_count},max={blocking_threshold},allowlisted=1",
                    action_required=False,
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
    checks = []

    if file_path.suffix == ".py":
        checks.extend(_check_python_function_sizes(file_path))
    elif file_path.suffix == ".sh" or file_path.name.endswith(".bash"):
        checks.extend(_check_shell_function_sizes(file_path))

    return checks


def _check_python_function_sizes(file_path: Path) -> List[QualityCheck]:
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
                    severity="warning",
                    file=str(file_path),
                    symbol=node.name,
                    message=f"Function is large: {length} lines (soft max {FUNCTION_SIZE_BLOCKING_PYTHON})",
                    evidence=f"function={node.name},lines={length},max={FUNCTION_SIZE_BLOCKING_PYTHON}",
                    action_required=False,
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
                    severity="warning",
                    file=str(file_path),
                    symbol=function_name,
                    message=f"Function is large: {length} lines (soft max {FUNCTION_SIZE_BLOCKING_SHELL})",
                    evidence=f"function={function_name},lines={length},max={FUNCTION_SIZE_BLOCKING_SHELL}",
                    action_required=False,
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
    checks = []

    if file_path.suffix == ".py":
        checks.extend(_run_ruff_check(file_path))
    elif file_path.suffix == ".sh" or file_path.name.endswith(".bash"):
        checks.extend(_run_shellcheck(file_path))

    return checks


def _run_ruff_check(file_path: Path) -> List[QualityCheck]:
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
            if result.returncode != 0 and not findings:
                checks.append(_tool_unavailable_check("ruff", file_path, "lint check", result=result))
        elif result.returncode != 0:
            checks.append(_tool_unavailable_check("ruff", file_path, "lint check", result=result))
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, json.JSONDecodeError, FileNotFoundError) as exc:
        checks.append(_tool_unavailable_check("ruff", file_path, "lint check", exc=exc))

    return checks


def _run_shellcheck(file_path: Path) -> List[QualityCheck]:
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
            if result.returncode != 0 and not findings:
                checks.append(_tool_unavailable_check("shellcheck", file_path, "shell lint check", result=result))
        elif result.returncode != 0:
            checks.append(_tool_unavailable_check("shellcheck", file_path, "shell lint check", result=result))
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, json.JSONDecodeError, FileNotFoundError) as exc:
        checks.append(_tool_unavailable_check("shellcheck", file_path, "shell lint check", exc=exc))

    return checks


def check_dead_code(file_path: Path) -> List[QualityCheck]:
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
        if result.returncode != 0 and not result.stdout.strip():
            checks.append(_tool_unavailable_check("vulture", file_path, "dead-code check", result=result))
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError) as exc:
        checks.append(_tool_unavailable_check("vulture", file_path, "dead-code check", exc=exc))

    return checks


def _tool_unavailable_check(
    tool: str,
    file_path: Path,
    skipped_check: str,
    exc: Optional[BaseException] = None,
    result: Optional[subprocess.CompletedProcess[str]] = None,
) -> QualityCheck:
    error_class = "FileNotFoundError"
    if exc is not None:
        error_class = type(exc).__name__
    if result is not None:
        error_class = f"exit_{result.returncode}"

    if error_class == "FileNotFoundError":
        message = f"tool_unavailable: {tool} not installed; {skipped_check} skipped"
    else:
        message = f"tool_unavailable: {tool} failed ({error_class}); {skipped_check} skipped"

    return QualityCheck(
        check_id="tool_unavailable",
        severity="warning",
        file=str(file_path),
        symbol=tool,
        message=message,
        evidence=f"tool={tool},error_class={error_class}",
        action_required=False,
        tool=tool,
        level="warn",
        code="tool_unavailable",
    )


def check_test_coverage_hygiene(changed_files: List[Path], repo_root: Path) -> List[QualityCheck]:
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
    changed_files: List[Path | str],
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
    changed_paths = [Path(f) for f in changed_files]
    advisory.scope = [str(f) for f in changed_paths]

    all_checks: List[QualityCheck] = []

    # Run checks on each changed file
    for file_path in changed_paths:
        all_checks.extend(check_file_size(file_path))
        all_checks.extend(check_function_sizes(file_path))
        all_checks.extend(run_linting(file_path))
        all_checks.extend(check_dead_code(file_path))

    # Test coverage hygiene check (across all files)
    all_checks.extend(check_test_coverage_hygiene(changed_paths, repo_root))

    # Convert checks to dict format
    advisory.checks = []
    for c in all_checks:
        record = CheckRecord({
            "check_id": c.check_id,
            "severity": c.severity,
            "file": c.file,
            "symbol": c.symbol,
            "message": c.message,
            "evidence": c.evidence,
            "action_required": c.action_required,
        })
        if c.tool is not None:
            record["tool"] = c.tool
        if c.level is not None:
            record["level"] = c.level
        if c.code is not None:
            record["code"] = c.code
        advisory.checks.append(record)

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


# Directories that are never source code for the whole-repo advisory scan.
_BACKLOG_SKIP_DIRS = {
    ".git",
    "__pycache__",
    ".venv",
    "venv",
    "node_modules",
    ".vnx-data",
    "dist",
    "build",
    ".tox",
    ".pytest_cache",
    ".mypy_cache",
    ".egg-info",
    ".claude",
}


def build_whole_repo_file_size_backlog(
    repo_root: Optional[Path] = None,
) -> Dict[str, Any]:
    """Scan the entire repository for the file-size refactor backlog.

    Unlike the diff-scoped blocking gate, this pass is purely ADVISORY. It walks
    every source file under ``repo_root`` and returns every file that exceeds the
    language warning threshold, sorted worst-first. Files over the blocking
    threshold are flagged as ``blocking`` unless they are on the size allowlist
    (then ``allowlisted`` with the grandfather reason) or are test files (then
    ``warning`` with ``is_test_file`` set). Nothing in this function fails or
    raises a gate hold.

    Args:
        repo_root: Repository root to scan. Defaults to the current working
            directory.

    Returns:
        A dict describing the backlog, including ``backlog`` (list of entries),
        counts, thresholds, and metadata.
    """
    if repo_root is None:
        repo_root = Path.cwd()
    repo_root = repo_root.resolve()

    backlog: List[Dict[str, Any]] = []

    for path in repo_root.rglob("*"):
        if not path.is_file() or path.is_symlink():
            continue
        if path.suffix not in (".py", ".sh") and not path.name.endswith(".bash"):
            continue
        rel_parts = path.relative_to(repo_root).parts
        if any(part in _BACKLOG_SKIP_DIRS for part in rel_parts):
            continue

        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError):
            continue

        line_count = len(lines)
        if path.suffix == ".py":
            warning_threshold = FILE_SIZE_WARNING_PYTHON
            blocking_threshold = FILE_SIZE_BLOCKING_PYTHON
        else:
            warning_threshold = FILE_SIZE_WARNING_SHELL
            blocking_threshold = FILE_SIZE_BLOCKING_SHELL

        if line_count <= warning_threshold:
            continue

        rel = path.relative_to(repo_root)
        allow_reason = _file_size_allowlist_reason(path)
        test_file = _is_test_file(path)

        entry: Dict[str, Any] = {
            "file": str(path),
            "repo_relative": str(rel),
            "line_count": line_count,
            "warning_threshold": warning_threshold,
            "blocking_threshold": blocking_threshold,
            "status": "warning",
            "severity": "warning",
        }
        if test_file:
            entry["is_test_file"] = True

        if line_count > blocking_threshold:
            if allow_reason is not None:
                entry["status"] = "allowlisted"
                entry["allowlist_reason"] = allow_reason
            elif test_file:
                entry["status"] = "warning"
            else:
                entry["status"] = "blocking"
                entry["severity"] = "blocking"

        backlog.append(entry)

    backlog.sort(key=lambda e: (-e["line_count"], e["repo_relative"]))

    return {
        "version": "1.0",
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "repo_root": str(repo_root),
        "warning_threshold_python": FILE_SIZE_WARNING_PYTHON,
        "blocking_threshold_python": FILE_SIZE_BLOCKING_PYTHON,
        "warning_threshold_shell": FILE_SIZE_WARNING_SHELL,
        "blocking_threshold_shell": FILE_SIZE_BLOCKING_SHELL,
        "allowlisted_count": sum(1 for e in backlog if e["status"] == "allowlisted"),
        "blocking_count": sum(1 for e in backlog if e["status"] == "blocking"),
        "warning_count": sum(
            1 for e in backlog if e["status"] not in ("allowlisted", "blocking")
        ),
        "total_backlog": len(backlog),
        "backlog": backlog,
    }
