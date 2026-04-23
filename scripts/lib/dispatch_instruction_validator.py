#!/usr/bin/env python3
"""Dispatch instruction template validator (W0 PR-6).

Validates dispatch instruction markdown files for scope-drift risk
before they are promoted from pending/ to the delivery queue.

Validation rules:
  D-1: Dispatch-ID must match canonical format YYYYMMDD-HHMMSS-<slug>-<track>
  D-2: Instruction body must contain a Description section (or Instruction: block)
  D-3: Scope section must not exceed item count threshold (warn ≥ 9, block ≥ 16)
  D-4: Unbounded-task language must not appear in scope/description
  D-5: Gate header must be accompanied by a Quality Gate section
  D-6: File paths spread across many top-level directories signal scope-drift
  D-7: Instruction body exceeding character threshold signals over-specification
  D-8: Gate-bearing dispatches must include a Success Criteria section
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Set


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DISPATCH_ID_RE = re.compile(r"^\d{8}-\d{6}-.+-[A-C]$")

# Scope item thresholds
SCOPE_WARN_THRESHOLD = 9     # ≥ this many bullets → warn
SCOPE_BLOCK_THRESHOLD = 16   # ≥ this many bullets → blocker

# Instruction body size threshold (characters)
INSTRUCTION_SIZE_WARN = 4000

# Max number of distinct top-level directories before scope-drift warning
DIR_BREADTH_WARN = 3

# Language patterns that imply unbounded / open-ended scope
UNBOUNDED_SCOPE_PATTERNS = [
    r"refactor\s+everything",
    r"fix\s+all\b",
    r"update\s+all\b",
    r"clean\s+up\s+all\b",
    r"while\s+you'?re?\s+at\s+it",
    r"while\s+(in|we'?re?\s+in)\s+(the\s+)?file",
    r"sweep\s+through",
    r"audit\s+everything",
    r"touch\s+every",
]


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

@dataclass
class DispatchFinding:
    """A single validation finding for a dispatch instruction."""
    rule: str        # D-1, D-2, …
    severity: str    # blocker | warn | info
    message: str
    line_hint: int = 0


@dataclass
class DispatchValidationResult:
    """Aggregated result of validating one dispatch instruction."""
    dispatch_id: str
    findings: List[DispatchFinding] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not any(f.severity == "blocker" for f in self.findings)

    @property
    def blocker_count(self) -> int:
        return sum(1 for f in self.findings if f.severity == "blocker")

    @property
    def warn_count(self) -> int:
        return sum(1 for f in self.findings if f.severity == "warn")


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def validate_dispatch_instruction(content: str) -> DispatchValidationResult:
    """Validate a dispatch instruction string and return findings.

    Orchestrates all per-rule helpers and returns a consolidated result.
    """
    dispatch_id = _extract_dispatch_id(content)
    result = DispatchValidationResult(dispatch_id=dispatch_id or "(unknown)")

    _check_dispatch_id(dispatch_id, result)
    _check_description_present(content, result)
    _check_scope_item_count(content, result)
    _check_unbounded_language(content, result)
    _check_gate_has_quality_section(content, result)
    _check_file_directory_breadth(content, result)
    _check_instruction_size(content, result)
    _check_gate_has_success_criteria(content, result)

    return result


# ---------------------------------------------------------------------------
# Per-rule helpers
# ---------------------------------------------------------------------------

def _check_dispatch_id(dispatch_id: Optional[str], result: DispatchValidationResult) -> None:
    """D-1: Dispatch-ID must match YYYYMMDD-HHMMSS-slug-track format."""
    if not dispatch_id:
        result.findings.append(DispatchFinding(
            rule="D-1", severity="blocker",
            message="No Dispatch-ID found. Must be present as 'Dispatch-ID: <id>' "
                    "matching YYYYMMDD-HHMMSS-<slug>-[A-C].",
        ))
        return
    if not DISPATCH_ID_RE.match(dispatch_id):
        result.findings.append(DispatchFinding(
            rule="D-1", severity="blocker",
            message=f"Dispatch-ID '{dispatch_id}' does not match required format "
                    "YYYYMMDD-HHMMSS-<slug>-[A-C] (e.g. 20260422-182004-my-task-A).",
        ))


def _check_description_present(content: str, result: DispatchValidationResult) -> None:
    """D-2: Instruction body must contain a description anchor."""
    has_description = bool(re.search(r"#{2,}\s+Description", content))
    has_instruction_block = bool(re.search(r"^Instruction:", content, re.MULTILINE))
    if not has_description and not has_instruction_block:
        result.findings.append(DispatchFinding(
            rule="D-2", severity="blocker",
            message="Dispatch must contain either a '### Description' section or "
                    "an 'Instruction:' block so the worker knows what to build.",
        ))


def _check_scope_item_count(content: str, result: DispatchValidationResult) -> None:
    """D-3: Scope section item count must not exceed thresholds."""
    scope_items = _extract_scope_items(content)
    n = len(scope_items)
    if n >= SCOPE_BLOCK_THRESHOLD:
        result.findings.append(DispatchFinding(
            rule="D-3", severity="blocker",
            message=f"Scope section has {n} items (≥ {SCOPE_BLOCK_THRESHOLD}). "
                    "Split into multiple dispatches to prevent scope-drift.",
        ))
    elif n >= SCOPE_WARN_THRESHOLD:
        result.findings.append(DispatchFinding(
            rule="D-3", severity="warn",
            message=f"Scope section has {n} items (≥ {SCOPE_WARN_THRESHOLD}). "
                    "Consider splitting if items span independent concerns.",
        ))


def _check_unbounded_language(content: str, result: DispatchValidationResult) -> None:
    """D-4: Detect open-ended / unbounded task language in Description/Context only."""
    scan_text = _extract_description_scope_section(content)
    if not scan_text:
        return
    for pattern in UNBOUNDED_SCOPE_PATTERNS:
        match = re.search(pattern, scan_text, re.IGNORECASE)
        if match:
            result.findings.append(DispatchFinding(
                rule="D-4", severity="warn",
                message=f"Unbounded-scope language detected: '{match.group()}'. "
                        "Replace with explicit, enumerable tasks.",
            ))


def _check_gate_has_quality_section(content: str, result: DispatchValidationResult) -> None:
    """D-5: Dispatches declaring a Gate header must have a Quality Gate section."""
    gate_value = _extract_field(content, "Gate")
    if gate_value and gate_value.strip():
        has_quality_gate = bool(re.search(r"#{2,}\s+Quality\s+Gate", content, re.IGNORECASE))
        if not has_quality_gate:
            result.findings.append(DispatchFinding(
                rule="D-5", severity="blocker",
                message=f"Gate '{gate_value}' declared but no '### Quality Gate' section found. "
                        "Workers cannot verify completion without acceptance criteria.",
            ))


def _check_file_directory_breadth(content: str, result: DispatchValidationResult) -> None:
    """D-6: File paths spread across many top-level directories indicate scope-drift."""
    top_dirs = _extract_top_level_dirs(content)
    if len(top_dirs) > DIR_BREADTH_WARN:
        result.findings.append(DispatchFinding(
            rule="D-6", severity="warn",
            message=f"File paths span {len(top_dirs)} top-level directories "
                    f"({', '.join(sorted(top_dirs))}). "
                    "Dispatches crossing many directories risk scope-drift; "
                    "consider splitting by directory.",
        ))


def _check_instruction_size(content: str, result: DispatchValidationResult) -> None:
    """D-7: Instruction body exceeding size threshold signals over-specification."""
    body = _extract_instruction_body(content)
    if len(body) > INSTRUCTION_SIZE_WARN:
        result.findings.append(DispatchFinding(
            rule="D-7", severity="warn",
            message=f"Instruction body is {len(body)} characters "
                    f"(> {INSTRUCTION_SIZE_WARN} threshold). "
                    "Over-specified dispatches increase misinterpretation risk; "
                    "prefer concise scope bullets over narrative prose.",
        ))


def _check_gate_has_success_criteria(content: str, result: DispatchValidationResult) -> None:
    """D-8: Gate-bearing dispatches must include a Success Criteria section."""
    gate_value = _extract_field(content, "Gate")
    if gate_value and gate_value.strip():
        has_criteria = bool(re.search(r"#{2,}\s+Success\s+Criteria", content, re.IGNORECASE))
        if not has_criteria:
            result.findings.append(DispatchFinding(
                rule="D-8", severity="blocker",
                message=f"Gate '{gate_value}' declared but no '### Success Criteria' section found. "
                        "Workers cannot self-assess completion without explicit acceptance criteria.",
            ))


# ---------------------------------------------------------------------------
# Extraction helpers
# ---------------------------------------------------------------------------

def _extract_dispatch_id(content: str) -> Optional[str]:
    """Extract the Dispatch-ID value from content."""
    match = re.search(r"(?:^|\s)Dispatch-ID:\s*(\S+)", content, re.MULTILINE)
    if match:
        return match.group(1).strip()
    return None


def _extract_field(content: str, field_name: str) -> Optional[str]:
    """Extract a metadata field value (header-style or bold-style)."""
    # Header style: "Gate: value"
    match = re.search(rf"^{re.escape(field_name)}:\s*(.+)", content, re.MULTILINE)
    if match:
        return match.group(1).strip()
    # Bold style: "**Gate**: value"
    match = re.search(rf"\*\*{re.escape(field_name)}\*\*:\s*(.+)", content)
    if match:
        return match.group(1).strip()
    return None


def _extract_scope_items(content: str) -> List[str]:
    """Extract bullet items from the ### Scope section (if present)."""
    scope_match = re.search(
        r"#{2,}\s+Scope\s*\n(.*?)(?=\n#{2,}|\Z)", content, re.DOTALL | re.IGNORECASE
    )
    if not scope_match:
        return []
    section = scope_match.group(1)
    return re.findall(r"^\s*[-*+]\s+.+", section, re.MULTILINE)


def _extract_instruction_body(content: str) -> str:
    """Return the textual body of the instruction (after any manager block headers)."""
    # Strip manager block (everything before the first blank line after headers)
    # Headers are key: value lines at the top.
    lines = content.splitlines()
    body_start = 0
    in_header = True
    for i, line in enumerate(lines):
        if in_header and re.match(r"^\[\[TARGET:", line):
            continue
        if in_header and re.match(r"^(Manager Block|Instruction:|Context:|\[\[DONE\]\])", line):
            body_start = i
            in_header = False
            continue
        if in_header and re.match(r"^\w[\w-]*:\s", line):
            continue
        if in_header and line.strip() == "":
            body_start = i
            in_header = False
            continue
        if not in_header:
            break
    return "\n".join(lines[body_start:])


def _extract_description_scope_section(content: str) -> str:
    """Extract Description/Context/Scope section text for D-4 scanning.

    Concatenates ALL matching sections (Description, Context, Scope) so
    unbounded language in Scope-only dispatches is still caught. Falls
    back to Task if none of the primary sections are present.
    Matches ## or ### headings to support both dispatch formats.
    """
    collected: list[str] = []
    for heading in ("Description", "Context", "Scope"):
        match = re.search(
            rf"#{{{2},}}\s+{heading}\s*\n(.*?)(?=\n#{{{2},}}|\Z)",
            content,
            re.DOTALL | re.IGNORECASE,
        )
        if match:
            collected.append(match.group(1))
    if collected:
        return "\n".join(collected)
    # Fallback to Task only when no primary section is present
    match = re.search(
        r"#{2,}\s+Task\s*\n(.*?)(?=\n#{2,}|\Z)",
        content,
        re.DOTALL | re.IGNORECASE,
    )
    return match.group(1) if match else ""


def _extract_top_level_dirs(content: str) -> Set[str]:
    """Extract distinct top-level directory names from file path mentions in content."""
    _EXT_RE = r"(?:py|sh|yml|yaml|md|json|toml|bash|ts|tsx|js|jsx|rs|go)"
    path_pattern = re.compile(
        r"`([.a-zA-Z_][\w/.-]*/[\w/.-]+)`"
        r"|(?<!\w)([\w][\w-]*/[\w/.-]+\.(?:" + _EXT_RE + r")\b)"
    )
    dirs: Set[str] = set()
    for match in path_pattern.finditer(content):
        raw = match.group(1) or match.group(2)
        if raw:
            parts = Path(raw).parts
            # Skip leading ./ or ../ so ./scripts/foo.py → scripts
            idx = 0
            while idx < len(parts) and parts[idx] in (".", ".."):
                idx += 1
            if idx >= len(parts):
                continue
            top = parts[idx]
            if top not in ("http:", "https:"):
                dirs.add(top)
    return dirs


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _format_result(result: DispatchValidationResult) -> str:
    lines = ["=" * 60, "DISPATCH INSTRUCTION VALIDATION", "=" * 60]
    lines.append(f"Dispatch-ID: {result.dispatch_id}")
    lines.append(f"Blockers: {result.blocker_count} | Warnings: {result.warn_count}")
    lines.append("")
    for finding in result.findings:
        tag = "BLOCK" if finding.severity == "blocker" else "WARN "
        lines.append(f"  [{tag}] {finding.rule}: {finding.message}")
    lines.append("")
    lines.append("=" * 60)
    lines.append("RESULT: PASSED" if result.passed else "RESULT: FAILED")
    lines.append("=" * 60)
    return "\n".join(lines)


def main() -> None:
    import sys
    if len(sys.argv) != 2:
        print("Usage: dispatch_instruction_validator.py <dispatch.md>")
        sys.exit(1)
    path = Path(sys.argv[1])
    if not path.exists():
        print(f"File not found: {path}")
        sys.exit(2)
    content = path.read_text(encoding="utf-8")
    result = validate_dispatch_instruction(content)
    print(_format_result(result))
    sys.exit(0 if result.passed else 1)


if __name__ == "__main__":
    main()
