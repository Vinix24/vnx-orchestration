#!/usr/bin/env python3
"""OI-1229 regression: codex_final_gate and auto_merge_policy classify paths identically.

Background: the two modules previously kept independent path-marker tuples.
codex_final_gate's GOVERNANCE_PATH_MARKERS was a strict superset of
auto_merge_policy's HIGH_RISK_PATH_MARKERS, so a PR touching e.g.
``scripts/lib/runtime_coordination.py`` would be flagged governance-sensitive
by codex_final_gate but still permitted to auto-merge by auto_merge_policy.

The shared ``is_governance_path()`` helper in auto_merge_policy must keep both
classifications in lock-step.
"""

from __future__ import annotations

import sys
from pathlib import Path

VNX_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(VNX_ROOT / "scripts" / "lib"))
sys.path.insert(0, str(VNX_ROOT / "scripts"))

import codex_final_gate as cfg  # noqa: E402
from auto_merge_policy import (  # noqa: E402
    GOVERNANCE_PATH_MARKERS,
    HIGH_RISK_PATH_MARKERS,
    codex_final_gate_required,
    is_governance_path,
)


GOVERNANCE_SAMPLES = [
    "scripts/dispatcher.py",
    "scripts/receipt_processor.py",
    "scripts/pr_queue_manager.py",
    "scripts/pre_merge_gate.py",
    "scripts/closure_verifier.py",
    "scripts/review_gate_manager.py",
    "scripts/roadmap_manager.py",
    "scripts/codex_final_gate.py",
    "scripts/lib/vnx_paths.py",
    "scripts/lib/runtime_coordination.py",
    "scripts/lib/dispatch_broker.py",
    "scripts/lib/review_contract.py",
    "scripts/commands/start.sh",
    "scripts/commands/stop.sh",
    "scripts/commands/doctor.sh",
    "schemas/runtime_coordination.sql",
    ".github/workflows/ci.yml",
    "migrations/2026_04_30_add_column.sql",
]

NON_GOVERNANCE_SAMPLES = [
    "docs/README.md",
    "tests/test_unrelated.py",
    "dashboard/app.py",
    "scripts/utils/format_helper.py",
]


def test_high_risk_alias_is_governance_set():
    """HIGH_RISK_PATH_MARKERS must be the same set as GOVERNANCE_PATH_MARKERS."""
    assert tuple(HIGH_RISK_PATH_MARKERS) == tuple(GOVERNANCE_PATH_MARKERS)


def test_governance_paths_classified_consistently():
    """For every governance-sensitive path, both modules must agree it is governance."""
    for path in GOVERNANCE_SAMPLES:
        cfg_view = cfg._touches_governance_paths([path])
        policy_view = is_governance_path([path])
        cfg_required = cfg.enforce_codex_gate.__wrapped__ if hasattr(
            cfg.enforce_codex_gate, "__wrapped__"
        ) else None  # not used; placeholder to keep imports tidy
        del cfg_required  # silence linter — we only need the boolean views
        assert cfg_view is True, f"codex_final_gate failed to flag {path}"
        assert policy_view is True, f"auto_merge_policy failed to flag {path}"
        assert codex_final_gate_required([path]) is True, (
            f"codex_final_gate_required disagrees on {path}"
        )


def test_non_governance_paths_classified_consistently():
    """For every benign path, both modules must agree it is NOT governance."""
    for path in NON_GOVERNANCE_SAMPLES:
        assert cfg._touches_governance_paths([path]) is False, (
            f"codex_final_gate falsely flagged {path}"
        )
        assert is_governance_path([path]) is False, (
            f"auto_merge_policy falsely flagged {path}"
        )
        assert codex_final_gate_required([path]) is False, (
            f"codex_final_gate_required falsely flagged {path}"
        )


def test_codex_final_gate_uses_shared_helper():
    """codex_final_gate must delegate to the shared helper, not its own list."""
    # The shared helper is imported into the codex_final_gate module namespace.
    assert cfg.is_governance_path is is_governance_path


def test_sql_files_classified_governance():
    """SQL files trigger governance classification in both modules."""
    sql_path = "migrations/0042_add_index.sql"
    assert is_governance_path([sql_path]) is True
    assert cfg._touches_governance_paths([sql_path]) is True


def test_empty_changed_files_not_governance():
    assert is_governance_path([]) is False
    assert cfg._touches_governance_paths([]) is False
