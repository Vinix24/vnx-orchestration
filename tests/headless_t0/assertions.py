#!/usr/bin/env python3
"""Assertion helpers for headless T0 sandbox test results."""

from __future__ import annotations

from pathlib import Path


class AssertionError(Exception):
    """Raised when a sandbox assertion fails."""


# ---------------------------------------------------------------------------
# Dispatch assertions
# ---------------------------------------------------------------------------

def assert_dispatch_created(sandbox_path: Path, expected_track: str) -> Path:
    """Assert that a dispatch file for the expected track exists in pending/.

    Returns the dispatch file path on success.
    Raises AssertionError if no matching dispatch is found.
    """
    pending_dir = sandbox_path / ".vnx-data" / "dispatches" / "pending"
    files = list(pending_dir.glob("*.md"))
    if not files:
        raise AssertionError(
            f"No dispatch files found in {pending_dir} — expected a {expected_track} dispatch"
        )

    for f in files:
        content = f.read_text()
        if f"[[TARGET:{expected_track}]]" in content:
            return f

    tracks_found = []
    for f in files:
        content = f.read_text()
        for t in ("A", "B", "C"):
            if f"[[TARGET:{t}]]" in content:
                tracks_found.append(t)

    raise AssertionError(
        f"No [[TARGET:{expected_track}]] dispatch found in {pending_dir}. "
        f"Tracks found: {tracks_found or 'none'}"
    )


def assert_dispatch_format(filepath: Path) -> None:
    """Assert that a dispatch file has required format elements.

    Checks:
    - Starts with [[TARGET:X]]
    - Has Manager Block header
    - Has Role: field
    - Has Dispatch-ID: field
    - Has Instruction: section

    Raises AssertionError on the first violation.
    """
    content = filepath.read_text()
    lines = content.splitlines()

    # Must start with [[TARGET:X]]
    first_non_empty = next((l.strip() for l in lines if l.strip()), "")
    if not (
        first_non_empty.startswith("[[TARGET:")
        and first_non_empty.endswith("]]")
        and len(first_non_empty) == 12
    ):
        raise AssertionError(
            f"Dispatch {filepath.name} must start with [[TARGET:A/B/C]], "
            f"found: {first_non_empty!r}"
        )

    required_fields = ["Manager Block", "Role:", "Dispatch-ID:", "Instruction:"]
    for field in required_fields:
        if field not in content:
            raise AssertionError(
                f"Dispatch {filepath.name} missing required field: {field!r}"
            )


def assert_no_dispatch_created(sandbox_path: Path) -> None:
    """Assert that no new dispatch files exist in pending/.

    Raises AssertionError if any dispatch is found.
    """
    pending_dir = sandbox_path / ".vnx-data" / "dispatches" / "pending"
    files = list(pending_dir.glob("*.md"))
    if files:
        names = [f.name for f in files]
        raise AssertionError(
            f"Expected no dispatches in {pending_dir}, but found: {names}"
        )


# ---------------------------------------------------------------------------
# Output assertions
# ---------------------------------------------------------------------------

def assert_decision_mentions(output: str, keywords: list[str]) -> None:
    """Assert that T0's output text mentions all expected keywords.

    Raises AssertionError listing any missing keywords.
    """
    missing = [kw for kw in keywords if kw.lower() not in output.lower()]
    if missing:
        raise AssertionError(
            f"T0 output missing expected keywords: {missing}\n"
            f"Output snippet: {output[:500]!r}"
        )


def assert_file_read(output: str, filepath: str) -> None:
    """Assert that T0's output indicates it read a specific file.

    Checks for the filename (basename) appearing in output.
    Raises AssertionError if not found.
    """
    basename = Path(filepath).name
    # Check for filename or full path reference
    if basename not in output and filepath not in output:
        raise AssertionError(
            f"T0 output does not indicate that {basename!r} was read.\n"
            f"Output snippet: {output[:500]!r}"
        )


def assert_gate_refused(output: str) -> None:
    """Assert that T0 refused a gate-less merge request.

    T0 should mention gate evidence or refuse to merge.
    Raises AssertionError if output looks like approval.
    """
    refusal_signals = [
        "gate", "evidence", "cannot merge", "do not merge",
        "no gate", "missing", "require", "review gate",
    ]
    found = any(s.lower() in output.lower() for s in refusal_signals)
    if not found:
        raise AssertionError(
            f"T0 did not refuse gate-less merge request. "
            f"Expected refusal signals: {refusal_signals}\n"
            f"Output snippet: {output[:500]!r}"
        )
