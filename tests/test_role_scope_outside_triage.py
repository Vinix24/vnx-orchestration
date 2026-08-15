"""Tests for scripts/analysis/role_scope_outside_triage.py.

The triage script splits the ``role_scope_only__outside`` dispatches into three
buckets (rol-te-smal / verkeerd-gerouteerd / onbeslisbaar) using the ownership
map in ``OWNERSHIP_RULES``. These tests pin the pure classification logic and
the population-selection rules with fabricated specs, so nothing here reads the
real pending dir or git history.

Covered:
  * the sum check — the three buckets must partition the measured population
    exactly (verify_partition), and group_by_bucket never drops/duplicates a
    dispatch;
  * one fabricated case per bucket (classify + build_triage);
  * a dispatch with no linked commit falls OUTSIDE the measurement (unlinked)
    and is never placed in a bucket.

Dispatch-ID: 20260815-opsch-w1-rolescope-triage
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
_LIB = str(REPO_ROOT / "scripts" / "lib")
_ANALYSIS = str(REPO_ROOT / "scripts" / "analysis")
for _p in (_LIB, _ANALYSIS):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import role_scope_outside_triage as triage  # noqa: E402
from role_scope_outside_triage import (  # noqa: E402
    BUCKET_MISROUTED,
    BUCKET_TOO_NARROW,
    BUCKET_UNDECIDABLE,
    BUCKETS,
    build_triage,
    classify,
    group_by_bucket,
    owner,
    verify_partition,
)


# ---------------------------------------------------------------------------
# classify() — pure bucket rules, one fabricated case per bucket
# ---------------------------------------------------------------------------

class TestClassify:
    def test_too_narrow__own_work_missing_from_scope(self):
        # The dispatch instruction's own example: a system-architect writing
        # docs/core/** is their work, so the SCOPE is too narrow, not the routing.
        c = classify("system-architect", ["docs/core/DISPATCH_RULES.md"])
        assert c.bucket == BUCKET_TOO_NARROW
        assert owner("docs/core/DISPATCH_RULES.md") == "system-architect"
        assert c.own_paths == ("docs/core/DISPATCH_RULES.md",)

    def test_too_narrow__backend_runtime_paths(self):
        # backend-developer writing vnx_cli/bin (runtime impl) is its own work.
        c = classify("backend-developer", ["vnx_cli/main.py", "bin/vnx"])
        assert c.bucket == BUCKET_TOO_NARROW
        assert c.other_roles == ()

    def test_misrouted__single_other_role(self):
        # backend-developer writing test infrastructure belongs to
        # quality-engineer — routed to the wrong role.
        c = classify("backend-developer", ["tests/test_harness.py"])
        assert c.bucket == BUCKET_MISROUTED
        assert c.other_roles == ("quality-engineer",)
        assert c.own_paths == ()

    def test_misrouted__permission_path_belongs_to_security(self):
        # backend-developer writing the permission config belongs to
        # security-engineer.
        c = classify("backend-developer", [".vnx/worker_permissions.yaml"])
        assert c.bucket == BUCKET_MISROUTED
        assert c.other_roles == ("security-engineer",)

    def test_undecidable__unknown_owner(self):
        # A path with no ownership rule forces undecidable, never a silent claim.
        c = classify("backend-developer", ["CLAUDE.md"])
        assert c.bucket == BUCKET_UNDECIDABLE
        assert c.unknown_paths == ("CLAUDE.md",)

    def test_undecidable__mixed_own_and_other(self):
        # A dispatch straddling its own territory and another role's.
        c = classify(
            "backend-developer",
            ["vnx_cli/main.py", "docs/core/SUBSYSTEMS.md"],
        )
        assert c.bucket == BUCKET_UNDECIDABLE
        assert c.own_paths == ("vnx_cli/main.py",)
        assert c.other_roles == ("system-architect",)

    def test_undecidable__multiple_other_roles(self):
        # No own work, but the paths span two different roles — no single routing.
        c = classify(
            "backend-developer",
            ["tests/test_x.py", "docs/governance/decisions/ADR-001.md"],
        )
        assert c.bucket == BUCKET_UNDECIDABLE
        assert c.other_roles == ("quality-engineer", "system-architect")

    def test_bucket_is_always_one_of_the_three(self):
        samples = [
            ("backend-developer", ["vnx_cli/main.py"]),
            ("backend-developer", ["tests/test_x.py"]),
            ("backend-developer", ["CLAUDE.md"]),
            ("backend-developer", ["vnx_cli/main.py", "docs/core/SUBSYSTEMS.md"]),
            ("system-architect", ["docs/core/DISPATCH_RULES.md"]),
            ("security-engineer", [".vnx/worker_permissions.yaml"]),
            ("quality-engineer", ["scripts/lib/gate_executor.py"]),
        ]
        for role, paths in samples:
            assert classify(role, paths).bucket in BUCKETS


# ---------------------------------------------------------------------------
# build_triage() — population selection and the sum check
# ---------------------------------------------------------------------------

def _specs():
    return {
        "D1-too-narrow": {"role": "backend-developer"},
        "D2-misrouted": {"role": "backend-developer"},
        "D3-mixed": {"role": "backend-developer"},
        "D4-unknown": {"role": "backend-developer"},
        "D5-in-scope": {"role": "backend-developer"},
        "D6-unlinked": {"role": "backend-developer"},
    }


def _did2files():
    return {
        # vnx_cli / bin are outside backend-developer's scope and are its own work.
        "D1-too-narrow": {"vnx_cli/main.py", "bin/vnx"},
        # docs/core belongs to system-architect — single other role.
        "D2-misrouted": {"docs/core/SUBSYSTEMS.md"},
        # own work + another role's work — undecidable.
        "D3-mixed": {"vnx_cli/main.py", "docs/core/SUBSYSTEMS.md"},
        # no ownership rule — undecidable.
        "D4-unknown": {"CLAUDE.md"},
        # scripts/** is inside backend-developer's scope — not outside.
        "D5-in-scope": {"scripts/lib/foo.py"},
        # D6-unlinked is absent from did2files — no linked commit.
    }


class TestBuildTriage:
    def test_population_counts(self):
        result = build_triage(_specs(), _did2files())
        assert result.unlinked == 1  # D6-unlinked excluded from the measurement
        assert result.in_scope == 1  # D5-in-scope
        assert result.outside == 4  # D1..D4
        ids = {t.dispatch_id for t in result.triages}
        assert ids == {"D1-too-narrow", "D2-misrouted", "D3-mixed", "D4-unknown"}

    def test_unlinked_never_lands_in_a_bucket(self):
        result = build_triage(_specs(), _did2files())
        grouped = group_by_bucket(result.triages)
        all_bucketed = {
            t.dispatch_id
            for bucket in BUCKETS
            for t in grouped[bucket]
        }
        assert "D6-unlinked" not in all_bucketed
        # and the unlinked dispatch is reported as such, not swallowed
        assert result.unlinked == 1

    def test_one_case_per_bucket_via_build_triage(self):
        result = build_triage(_specs(), _did2files())
        by_id = {t.dispatch_id: t.classification.bucket for t in result.triages}
        assert by_id["D1-too-narrow"] == BUCKET_TOO_NARROW
        assert by_id["D2-misrouted"] == BUCKET_MISROUTED
        assert by_id["D3-mixed"] == BUCKET_UNDECIDABLE
        assert by_id["D4-unknown"] == BUCKET_UNDECIDABLE

    def test_sum_check__three_buckets_equal_total(self):
        result = build_triage(_specs(), _did2files())
        grouped = group_by_bucket(result.triages)
        assert verify_partition(grouped, result.outside) is True
        # the hard check the script enforces: sum of buckets == outside total
        assert sum(len(v) for v in grouped.values()) == result.outside

    def test_sum_check__fails_when_a_dispatch_is_lost(self):
        result = build_triage(_specs(), _did2files())
        grouped = group_by_bucket(result.triages)
        # drop one triage and the partition no longer sums to the population
        grouped[BUCKET_UNDECIDABLE].pop()
        assert verify_partition(grouped, result.outside) is False

    def test_no_linked_commit_is_excluded_not_bucketed(self):
        # A spec that appears in specs but has no entry in did2files (no linked
        # commit) must fall outside the measurement entirely.
        specs = {"D-unlinked": {"role": "backend-developer"}}
        result = build_triage(specs, {})
        assert result.triages == []
        assert result.unlinked == 1
        assert result.outside == 0


# ---------------------------------------------------------------------------
# owner() — the ownership map is the classification rule in code
# ---------------------------------------------------------------------------

class TestOwner:
    def test_unmapped_path_returns_none(self):
        assert owner("CLAUDE.md") is None
        assert owner("CODEOWNERS") is None
        assert owner("FEATURE_PLAN.md") is None

    def test_known_owners(self):
        assert owner("docs/governance/decisions/ADR-001.md") == "system-architect"
        assert owner("docs/core/DISPATCH_RULES.md") == "system-architect"
        assert owner("tests/test_x.py") == "quality-engineer"
        assert owner(".github/workflows/ci.yml") == "quality-engineer"
        assert owner("vnx_cli/main.py") == "backend-developer"
        assert owner("dashboard/ui.py") == "frontend-developer"
        assert owner(".vnx/worker_permissions.yaml") == "security-engineer"
