#!/usr/bin/env python3
"""Regression guard for OI-890: the Lint-Patterns marker syntax is codified.

Four PRs in one day (2026-07-31) went red on the same lint form error: a worker
suppressed a deliberate silent except with ``# noqa: BLE001``, which the Lint
Patterns gate (scripts/ci_lint_patterns.py) rejects — the gate needs a PLAIN
marker comment (``# vnx-silent-except: <reason>``). The gate's own error message
states the rule; the fix codifies the rule in the canonical Worker Dispatch
Standards footer so every dispatch carries it.

These tests assert the codification stays present in the canonical T0 role.
"""

from __future__ import annotations

from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_ROLE_PATH = _PROJECT_ROOT / ".claude" / "terminals" / "T0" / "role-orchestrator.md"


def _worker_dispatch_standards_section() -> str:
    """Return the Worker Dispatch Standards section of the canonical T0 role."""
    text = _ROLE_PATH.read_text(encoding="utf-8")
    marker = "## Worker Dispatch Standards"
    start = text.index(marker)
    # The section ends at "## Cluster naming convention" (the code-fenced
    # "## Critical rules" block inside the section is NOT a real heading).
    end_marker = "## Cluster naming convention"
    nxt = text.index(end_marker, start + len(marker))
    return text[start:nxt]


def test_role_file_exists():
    assert _ROLE_PATH.is_file(), f"canonical T0 role missing: {_ROLE_PATH}"


def test_marker_syntax_codified_in_worker_dispatch_standards():
    section = _worker_dispatch_standards_section()
    assert "# noqa:" in section, (
        "Worker Dispatch Standards must state the noqa rejection so a worker "
        "does not repeat the OI-890 form error."
    )
    assert "vnx-silent-except" in section and "vnx-atomic-write" in section, (
        "Worker Dispatch Standards must carry the plain marker syntax for both "
        "lint patterns (silent-except and atomic-write)."
    )


def test_critical_rules_footer_present():
    section = _worker_dispatch_standards_section()
    for required in ("DO NOT add TODO/FIXME", "DO NOT bypass tests with --no-verify"):
        assert required in section, f"Critical-rules footer lost: {required!r}"
