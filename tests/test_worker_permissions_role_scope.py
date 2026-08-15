"""Role-scope parity tests for the 20260815-opsch-w2-rolescope-fix scope changes.

Pins the file_write_scope additions made in dispatch 20260815-opsch-w2-rolescope-fix
(track ``role-scope-parity``, points 11+12 of the OPSCHALING cluster). The additions
close the ``rol-te-smal`` gap measured by scripts/analysis/role_scope_outside_triage.py:
dispatches that did their OWN role's work on a path their scope did not cover.

These tests measure the BEHAVIOR of the scope resolver (resolve_worker_profile +
match_file_write_scope), never the YAML text. Each changed role has:

  * one case that fell OUTSIDE the scope before the change and now falls INSIDE;
  * one case that STILL falls outside (proof the scope was not blanket-opened).

security-engineer additionally pins that it did not gain a broad scripts/lib/**
(or docs/**) surface — the scope change is two narrow permission-doc file grants.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
REPO_PERMISSIONS_YAML = REPO_ROOT / ".vnx" / "worker_permissions.yaml"

sys.path.insert(0, str(REPO_ROOT / "scripts" / "lib"))

from worker_permissions import (
    match_file_write_scope,
    resolve_worker_profile,
)


def _profile(role: str):
    """Resolve *role* from the worktree-local YAML (bypasses env steering so the
    test reads exactly the file this PR edits, regardless of PROJECT_ROOT)."""
    profile = resolve_worker_profile(role, REPO_PERMISSIONS_YAML)
    assert not profile.is_fallback, f"{role} resolved to the code-worker fallback"
    return profile


# ---------------------------------------------------------------------------
# backend-developer — its own runtime work is now in scope
# ---------------------------------------------------------------------------

class TestBackendDeveloperRoleScope:
    def test_runtime_paths_now_in_scope(self):
        """vnx_cli / bin / hooks / configs are backend-developer's own runtime
        implementation — previously outside, now inside."""
        p = _profile("backend-developer")
        for path in (
            "vnx_cli/main.py",
            "vnx_cli/commands/doctor.py",
            "bin/vnx",
            "hooks/monitor_tripwire.sh",
            "configs/plan_gate_panel.yaml",
        ):
            assert match_file_write_scope(path, p, None) is True, path

    def test_own_docs_and_root_build_files_now_in_scope(self):
        p = _profile("backend-developer")
        for path in (
            "docs/applications/README.md",
            "docs/operations/OTEL_EXPORT.md",
            "docs/MIGRATION_GUIDE.md",
            "VERSION",
            "CHANGELOG.md",
            "pyproject.toml",
            "uv.lock",
            "requirements.txt",
        ):
            assert match_file_write_scope(path, p, None) is True, path

    def test_system_architect_territory_still_out_of_scope(self):
        """docs/core, docs/governance/decisions and docs/manifesto stay
        system-architect's — backend must NOT blanket-open docs (fnmatch `*`
        spans `/`, so this pins the fix against a naive docs/*.md glob)."""
        p = _profile("backend-developer")
        for path in (
            "docs/core/DISPATCH_RULES.md",
            "docs/core/technical/x.md",
            "docs/governance/decisions/ADR-001.md",
            "docs/manifesto/HEADLESS_TRANSITION.md",
            "schemas/quality_intelligence.sql",
            "templates/FEATURE_PLAN_TEMPLATE.md",
            ".vnx/worker_permissions.yaml",
        ):
            assert match_file_write_scope(path, p, None) is False, path


# ---------------------------------------------------------------------------
# quality-engineer — test-harness / CI / review-gate infra is now in scope
# ---------------------------------------------------------------------------

class TestQualityEngineerRoleScope:
    def test_gate_and_benchmark_infra_now_in_scope(self):
        """Review-gate, benchmark and refactor tooling is quality-engineer's own
        work — previously outside (scope was tests/**, scripts/check_*), now inside."""
        p = _profile("quality-engineer")
        for path in (
            "scripts/benchmark/field-tests/runners/run_field_tests.py",
            "scripts/refactor_equivalence.py",
            "scripts/lib/gate_executor.py",
            "scripts/commands/gate.sh",
            "scripts/review_gate_manager.py",
        ):
            assert match_file_write_scope(path, p, None) is True, path

    def test_backend_script_surface_still_out_of_scope(self):
        """quality-engineer did NOT gain broad scripts/lib — a backend runtime
        module stays outside its scope."""
        p = _profile("quality-engineer")
        for path in (
            "scripts/lib/worker_permissions.py",
            "scripts/lib/dispatch_govern.py",
            "vnx_cli/main.py",
            "dashboard/index.html",
            "docs/x.md",
        ):
            assert match_file_write_scope(path, p, None) is False, path


# ---------------------------------------------------------------------------
# security-engineer — permission docs now in scope, nothing else broadened
# ---------------------------------------------------------------------------

class TestSecurityEngineerRoleScope:
    def test_permission_docs_now_in_scope(self):
        """The permission-posture and key-provisioning docs are security-engineer's
        own work — previously outside, now inside."""
        p = _profile("security-engineer")
        assert match_file_write_scope("docs/operations/WORKER_PERMISSIONS.md", p, None) is True
        assert match_file_write_scope("docs/governance/KEY_PROVISIONING.md", p, None) is True

    def test_other_ops_docs_still_out_of_scope(self):
        """A narrow file grant, not docs/operations/** — a neighbouring backend ops
        doc stays outside security's scope."""
        p = _profile("security-engineer")
        assert match_file_write_scope("docs/operations/CONTEXT_ROTATION.md", p, None) is False
        assert match_file_write_scope("docs/operations/RECEIPT_PIPELINE.md", p, None) is False

    def test_scope_additions_are_narrow_doc_grants_not_broad_surface(self):
        """security-engineer must NOT have suddenly gained a broad scripts/lib/**
        (or docs/**) surface. The change is exactly the two permission-doc file
        grants on top of the pre-existing scripts/** + tests/** remediation
        surface — pinned both as a list delta and behaviourally."""
        p = _profile("security-engineer")
        new = [s for s in p.file_write_scope if s not in ("scripts/**", "tests/**")]
        assert new == [
            "docs/operations/WORKER_PERMISSIONS.md",
            "docs/governance/KEY_PROVISIONING.md",
        ]
        assert "scripts/lib/**" not in p.file_write_scope
        assert "docs/**" not in p.file_write_scope
        # Behavioural: backend/system-architect territory stays closed.
        for path in (
            "vnx_cli/main.py",
            "docs/core/DISPATCH_RULES.md",
            "docs/operations/CONTEXT_ROTATION.md",
        ):
            assert match_file_write_scope(path, p, None) is False, path
