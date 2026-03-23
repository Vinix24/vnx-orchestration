#!/usr/bin/env python3
"""Contract block parser for VNX dispatch files.

Extracts structured contract claims from dispatch markdown files.
Contract blocks are optional sections that define verifiable assertions
about the expected outcome of a dispatch.

Phase 2a: Contracts are optional. Verification runs only when a contract
block is present in the dispatch.

Supported claim types:
  - file_exists:   Assert a file exists at a given path
  - file_changed:  Assert a file was modified (git diff check)
  - pattern_match: Assert a regex pattern appears in a file
  - no_pattern:    Assert a regex pattern does NOT appear in a file
  - bash_check:    Assert a shell command exits 0 (lightweight only)
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional


CLAIM_TYPES = frozenset({
    "file_exists",
    "file_changed",
    "pattern_match",
    "no_pattern",
    "bash_check",
})

# Regex patterns for each claim line format:
#   - file_exists: /path/to/file
#   - file_changed: /path/to/file
#   - pattern_match: "regex pattern" in /path/to/file
#   - no_pattern: "regex pattern" in /path/to/file
#   - bash_check: `command args`
_CLAIM_PATTERNS = {
    "file_exists": re.compile(
        r"^-\s*file_exists:\s*(?P<path>.+?)\s*$"
    ),
    "file_changed": re.compile(
        r"^-\s*file_changed:\s*(?P<path>.+?)\s*$"
    ),
    "pattern_match": re.compile(
        r'^-\s*pattern_match:\s*"(?P<pattern>.+?)"\s+in\s+(?P<path>.+?)\s*$'
    ),
    "no_pattern": re.compile(
        r'^-\s*no_pattern:\s*"(?P<pattern>.+?)"\s+in\s+(?P<path>.+?)\s*$'
    ),
    "bash_check": re.compile(
        r"^-\s*bash_check:\s*`(?P<command>.+?)`\s*$"
    ),
}


@dataclass(frozen=True)
class Claim:
    """A single verifiable contract claim."""

    claim_type: str
    path: Optional[str] = None
    pattern: Optional[str] = None
    command: Optional[str] = None
    line_number: int = 0
    raw_line: str = ""

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {"claim_type": self.claim_type}
        if self.path is not None:
            d["path"] = self.path
        if self.pattern is not None:
            d["pattern"] = self.pattern
        if self.command is not None:
            d["command"] = self.command
        d["line_number"] = self.line_number
        d["raw_line"] = self.raw_line
        return d


@dataclass
class ContractBlock:
    """Parsed contract block from a dispatch file."""

    dispatch_id: str = ""
    claims: List[Claim] = field(default_factory=list)
    raw_text: str = ""
    parse_errors: List[str] = field(default_factory=list)

    @property
    def has_claims(self) -> bool:
        return len(self.claims) > 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "dispatch_id": self.dispatch_id,
            "claim_count": len(self.claims),
            "claims": [c.to_dict() for c in self.claims],
            "parse_errors": self.parse_errors,
        }


def _extract_contract_section(content: str) -> Optional[str]:
    """Extract the contract section from dispatch markdown content.

    Looks for a section starting with '## Contract' (case-insensitive)
    and ending at the next '##' heading, '---' separator, or '[[DONE]]'.
    """
    pattern = re.compile(
        r"^##\s+Contract\b.*?\n(.*?)(?=^##\s|\n---|\[\[DONE\]\]|\Z)",
        re.MULTILINE | re.DOTALL | re.IGNORECASE,
    )
    match = pattern.search(content)
    if not match:
        return None
    return match.group(1).strip()


def _extract_dispatch_id(content: str) -> str:
    """Extract dispatch ID from the Manager Block."""
    match = re.search(r"^Dispatch-ID:\s*(.+?)\s*$", content, re.MULTILINE)
    return match.group(1).strip() if match else ""


def _parse_claim_line(line: str, line_number: int) -> Optional[Claim]:
    """Parse a single claim line into a Claim object."""
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return None

    for claim_type, pattern in _CLAIM_PATTERNS.items():
        match = pattern.match(stripped)
        if match:
            groups = match.groupdict()
            return Claim(
                claim_type=claim_type,
                path=groups.get("path"),
                pattern=groups.get("pattern"),
                command=groups.get("command"),
                line_number=line_number,
                raw_line=stripped,
            )
    return None


def parse_contract_from_text(content: str) -> ContractBlock:
    """Parse a contract block from full dispatch file content.

    Returns a ContractBlock with claims extracted. If no contract section
    exists, returns a ContractBlock with no claims (has_claims == False).
    """
    dispatch_id = _extract_dispatch_id(content)
    contract_text = _extract_contract_section(content)

    if contract_text is None:
        return ContractBlock(dispatch_id=dispatch_id)

    claims: List[Claim] = []
    parse_errors: List[str] = []

    for i, line in enumerate(contract_text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue

        # Lines that start with '- ' are claim lines
        if stripped.startswith("- "):
            claim = _parse_claim_line(stripped, line_number=i)
            if claim is not None:
                claims.append(claim)
            else:
                parse_errors.append(
                    f"line {i}: unrecognized claim format: {stripped}"
                )

    return ContractBlock(
        dispatch_id=dispatch_id,
        claims=claims,
        raw_text=contract_text,
        parse_errors=parse_errors,
    )


def parse_contract_from_file(dispatch_path: Path) -> ContractBlock:
    """Parse a contract block from a dispatch file on disk."""
    content = dispatch_path.read_text(encoding="utf-8")
    block = parse_contract_from_text(content)
    return block


def find_dispatch_for_receipt(
    dispatch_id: str, dispatch_dir: Path
) -> Optional[Path]:
    """Locate the dispatch file for a given dispatch ID.

    Searches active/ and completed/ subdirectories.
    """
    for subdir in ("active", "completed", "staging", "pending"):
        candidate_dir = dispatch_dir / subdir
        if not candidate_dir.is_dir():
            continue
        for md_file in candidate_dir.glob("*.md"):
            if dispatch_id in md_file.name:
                return md_file
    return None
