"""Regression tests for worker_permissions overlay merge.

Critical invariant: project-specific file_write_scope entries (e.g. src/**)
added to the backend-developer profile MUST survive a simulated regen/cutover
(re-running the merge with the VNX-shipped template).

Covers:
  - file_write_scope union: project paths survive after merge
  - VNX-managed fields (allowed_tools, denied_tools, bash_deny_patterns) union
  - Project-added roles not in template are preserved unchanged
  - terminal_assignments: project overrides win on collision
  - Idempotency: merging twice produces same result
  - First-time (no existing file) falls back to full from template
  - _vnx_meta is always replaced with template version
"""

from __future__ import annotations

import sys
import textwrap
from pathlib import Path
from typing import Any

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from vnx_worker_permissions_merge import (
    diff_summary,
    generate_full_permissions,
    merge_permissions,
    merge_role,
    validate_permissions,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

VNX_TEMPLATE: dict[str, Any] = yaml.safe_load(textwrap.dedent("""\
    version: 1

    _vnx_meta:
      managed_keys:
        - version
        - "profiles.<role>.allowed_tools (vnx_baseline)"
        - "profiles.<role>.denied_tools (vnx_baseline)"
        - "profiles.<role>.bash_allow_patterns (vnx_baseline)"
        - "profiles.<role>.bash_deny_patterns (vnx_baseline)"
        - "terminal_assignments (vnx_baseline; project overrides win)"
      project_owned_keys:
        - "profiles.<role>.file_write_scope"
        - "profiles.<project-role>  (roles not present in this template)"
      description: "VNX worker permission profiles."

    profiles:
      backend-developer:
        allowed_tools: [Read, Write, Edit, MultiEdit, Bash, Grep, Glob]
        denied_tools: [WebSearch, WebFetch]
        bash_allow_patterns:
          - "pytest*"
          - "python3*"
          - "git add*"
          - "git commit*"
          - "git push origin*"
          - "pip install*"
          - "bash -n*"
        bash_deny_patterns:
          - "rm -rf*"
          - "git reset --hard*"
          - "git push --force*"
          - "git push -f*"
          - "curl*anthropic*"
        file_write_scope:
          - "scripts/**"
          - "tests/**"
          - "dashboard/**"
        mcp_servers:
          - "notion"

      test-engineer:
        allowed_tools: [Read, Write, Edit, Bash, Grep, Glob]
        denied_tools: [WebSearch, WebFetch, MultiEdit]
        bash_allow_patterns:
          - "pytest*"
          - "python3 -m pytest*"
          - "git add*"
          - "git commit*"
        bash_deny_patterns:
          - "rm -rf*"
          - "git push*"
          - "git reset*"
        file_write_scope:
          - "tests/**"
          - "scripts/check_*"

    terminal_assignments:
      T1: backend-developer
      T2: test-engineer
      T3: frontend-developer
"""))

PROJECT_WITH_SRC_SCOPE: dict[str, Any] = yaml.safe_load(textwrap.dedent("""\
    version: 1

    profiles:
      backend-developer:
        allowed_tools: [Read, Write, Edit, MultiEdit, Bash, Grep, Glob]
        denied_tools: [WebSearch, WebFetch]
        bash_allow_patterns:
          - "pytest*"
          - "python3*"
          - "git add*"
          - "git commit*"
          - "git push origin*"
          - "pip install*"
          - "bash -n*"
        bash_deny_patterns:
          - "rm -rf*"
          - "git reset --hard*"
          - "git push --force*"
          - "git push -f*"
          - "curl*anthropic*"
        file_write_scope:
          - "scripts/**"
          - "tests/**"
          - "dashboard/**"
          - "src/**"

      test-engineer:
        allowed_tools: [Read, Write, Edit, Bash, Grep, Glob]
        denied_tools: [WebSearch, WebFetch, MultiEdit]
        bash_allow_patterns:
          - "pytest*"
          - "python3 -m pytest*"
          - "git add*"
          - "git commit*"
        bash_deny_patterns:
          - "rm -rf*"
          - "git push*"
          - "git reset*"
        file_write_scope:
          - "tests/**"
          - "scripts/check_*"

    terminal_assignments:
      T1: backend-developer
      T2: test-engineer
      T3: frontend-developer
"""))


# ---------------------------------------------------------------------------
# Core regression: project file_write_scope survives cutover
# ---------------------------------------------------------------------------

class TestFileWriteScopeSurvivesCutover:
    """The primary regression guard: src/** must survive after merge with template."""

    def test_project_src_scope_preserved_after_merge(self) -> None:
        """src/** added by project must survive merging with the VNX template."""
        result = merge_permissions(PROJECT_WITH_SRC_SCOPE, VNX_TEMPLATE)
        scope = result["profiles"]["backend-developer"]["file_write_scope"]
        assert "src/**" in scope, (
            "project-added src/** was lost during merge — template-leak regression"
        )

    def test_vnx_baseline_scope_paths_also_present(self) -> None:
        """VNX template paths must still be present after merge."""
        result = merge_permissions(PROJECT_WITH_SRC_SCOPE, VNX_TEMPLATE)
        scope = result["profiles"]["backend-developer"]["file_write_scope"]
        for expected in ("scripts/**", "tests/**", "dashboard/**"):
            assert expected in scope, f"VNX baseline path {expected!r} lost after merge"

    def test_idempotent_merge_preserves_src_scope(self) -> None:
        """Merging twice (simulating two consecutive cutovers) must be idempotent."""
        first = merge_permissions(PROJECT_WITH_SRC_SCOPE, VNX_TEMPLATE)
        second = merge_permissions(first, VNX_TEMPLATE)
        scope = second["profiles"]["backend-developer"]["file_write_scope"]
        assert "src/**" in scope, "src/** lost after second merge (idempotency failure)"

    def test_no_duplicates_in_scope_after_double_merge(self) -> None:
        """Union must deduplicate: no path appears twice after repeated merges."""
        first = merge_permissions(PROJECT_WITH_SRC_SCOPE, VNX_TEMPLATE)
        second = merge_permissions(first, VNX_TEMPLATE)
        scope = second["profiles"]["backend-developer"]["file_write_scope"]
        assert len(scope) == len(set(scope)), f"Duplicates in file_write_scope: {scope}"


# ---------------------------------------------------------------------------
# VNX-managed fields: union behaviour
# ---------------------------------------------------------------------------

class TestVnxManagedFieldsUnion:
    def test_allowed_tools_union(self) -> None:
        project = {
            "profiles": {"backend-developer": {"allowed_tools": ["Read", "ExtraCustomTool"]}},
            "terminal_assignments": {},
        }
        result = merge_permissions(project, VNX_TEMPLATE)
        tools = result["profiles"]["backend-developer"]["allowed_tools"]
        assert "Read" in tools
        assert "Bash" in tools          # from template
        assert "ExtraCustomTool" in tools  # project extra preserved

    def test_denied_tools_union(self) -> None:
        project = {
            "profiles": {"backend-developer": {"denied_tools": ["WebSearch", "ProjectDenied"]}},
            "terminal_assignments": {},
        }
        result = merge_permissions(project, VNX_TEMPLATE)
        denied = result["profiles"]["backend-developer"]["denied_tools"]
        assert "WebSearch" in denied
        assert "WebFetch" in denied     # from template
        assert "ProjectDenied" in denied  # project extra preserved

    def test_bash_deny_patterns_union(self) -> None:
        project = {
            "profiles": {"backend-developer": {
                "bash_deny_patterns": ["rm -rf*", "my-custom-deny*"]
            }},
            "terminal_assignments": {},
        }
        result = merge_permissions(project, VNX_TEMPLATE)
        deny = result["profiles"]["backend-developer"]["bash_deny_patterns"]
        assert "rm -rf*" in deny
        assert "git push --force*" in deny  # from template
        assert "my-custom-deny*" in deny    # project extra preserved

    def test_mcp_servers_union(self) -> None:
        project = {
            "profiles": {"backend-developer": {"mcp_servers": ["project-custom-mcp"]}},
            "terminal_assignments": {},
        }
        result = merge_permissions(project, VNX_TEMPLATE)
        servers = result["profiles"]["backend-developer"]["mcp_servers"]
        assert "notion" in servers          # from template
        assert "project-custom-mcp" in servers  # project extra preserved

    def test_mcp_servers_defaults_to_template_when_project_silent(self) -> None:
        # Project declares the role but says nothing about mcp_servers -> the
        # VNX template baseline still reaches the merged output (it is a
        # vnx_baseline key, not project-owned like file_write_scope).
        result = merge_permissions(PROJECT_WITH_SRC_SCOPE, VNX_TEMPLATE)
        assert result["profiles"]["backend-developer"]["mcp_servers"] == ["notion"]


# ---------------------------------------------------------------------------
# Project-added roles survive cutover
# ---------------------------------------------------------------------------

class TestProjectAddedRolesSurvive:
    def test_project_only_role_preserved(self) -> None:
        project = yaml.safe_load(textwrap.dedent("""\
            version: 1
            profiles:
              backend-developer:
                allowed_tools: [Read, Write, Edit, Bash]
                denied_tools: [WebSearch]
                file_write_scope:
                  - "scripts/**"
                  - "src/**"
              seocrawler-extractor:
                allowed_tools: [Read, Write, Edit, Bash]
                denied_tools: [WebSearch, WebFetch]
                bash_allow_patterns:
                  - "python3*"
                file_write_scope:
                  - "app/extractors/**"
                  - "tests/extractors/**"
            terminal_assignments:
              T1: seocrawler-extractor
        """))

        result = merge_permissions(project, VNX_TEMPLATE)

        # Project-only role must survive
        assert "seocrawler-extractor" in result["profiles"], (
            "project-added role 'seocrawler-extractor' was dropped during merge"
        )
        extractor = result["profiles"]["seocrawler-extractor"]
        assert "app/extractors/**" in extractor["file_write_scope"]
        assert "tests/extractors/**" in extractor["file_write_scope"]

    def test_project_only_role_scope_not_mutated(self) -> None:
        """Project-added roles must not get VNX baseline paths injected into them."""
        project = yaml.safe_load(textwrap.dedent("""\
            version: 1
            profiles:
              special-agent:
                allowed_tools: [Read]
                denied_tools: []
                file_write_scope:
                  - "special/**"
            terminal_assignments: {}
        """))

        result = merge_permissions(project, VNX_TEMPLATE)
        scope = result["profiles"]["special-agent"]["file_write_scope"]
        assert scope == ["special/**"], (
            f"project-only role got unexpected paths injected: {scope}"
        )


# ---------------------------------------------------------------------------
# terminal_assignments: project overrides win
# ---------------------------------------------------------------------------

class TestTerminalAssignments:
    def test_project_terminal_override_wins(self) -> None:
        project = {
            "profiles": {},
            "terminal_assignments": {"T1": "seocrawler-extractor", "T2": "test-engineer"},
        }
        result = merge_permissions(project, VNX_TEMPLATE)
        ta = result["terminal_assignments"]
        assert ta["T1"] == "seocrawler-extractor", (
            "project terminal override for T1 was overwritten by VNX template"
        )
        assert ta["T2"] == "test-engineer"   # same value — no conflict

    def test_vnx_baseline_terminals_present_when_project_does_not_override(self) -> None:
        project = {"profiles": {}, "terminal_assignments": {}}
        result = merge_permissions(project, VNX_TEMPLATE)
        ta = result["terminal_assignments"]
        assert ta.get("T3") == "frontend-developer"  # from VNX template


# ---------------------------------------------------------------------------
# _vnx_meta always comes from template
# ---------------------------------------------------------------------------

class TestVnxMeta:
    def test_vnx_meta_replaced_from_template(self) -> None:
        project = {
            "profiles": {},
            "terminal_assignments": {},
            "_vnx_meta": {"managed_keys": ["stale-key"], "description": "old"},
        }
        result = merge_permissions(project, VNX_TEMPLATE)
        meta = result.get("_vnx_meta", {})
        # The template _vnx_meta must be present and the stale project meta gone
        assert "stale-key" not in str(meta.get("managed_keys", []))
        assert "version" in str(meta.get("managed_keys", []))

    def test_version_always_from_template(self) -> None:
        project = {"version": 99, "profiles": {}, "terminal_assignments": {}}
        result = merge_permissions(project, VNX_TEMPLATE)
        assert result["version"] == VNX_TEMPLATE["version"]


# ---------------------------------------------------------------------------
# First-time (no existing file): full from template
# ---------------------------------------------------------------------------

class TestFirstTimeInit:
    def test_full_mode_generates_all_roles(self) -> None:
        result = generate_full_permissions(VNX_TEMPLATE)
        assert "backend-developer" in result["profiles"]
        assert "test-engineer" in result["profiles"]

    def test_full_mode_adds_vnx_meta(self) -> None:
        result = generate_full_permissions(VNX_TEMPLATE)
        assert "_vnx_meta" in result

    def test_merge_with_none_existing_equals_full(self) -> None:
        """merge_permissions with empty existing dict behaves like a full generate."""
        result = merge_permissions({}, VNX_TEMPLATE)
        full = generate_full_permissions(VNX_TEMPLATE)
        # Both must have same roles
        assert set(result["profiles"].keys()) == set(full["profiles"].keys())


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

class TestValidation:
    def test_valid_permissions_passes(self) -> None:
        result = merge_permissions(PROJECT_WITH_SRC_SCOPE, VNX_TEMPLATE)
        issues = validate_permissions(result)
        assert issues == []

    def test_invalid_profiles_not_dict(self) -> None:
        bad = {"profiles": ["not", "a", "dict"], "terminal_assignments": {}}
        issues = validate_permissions(bad)
        assert any("profiles" in i for i in issues)

    def test_invalid_file_write_scope_not_list(self) -> None:
        bad = {
            "profiles": {
                "backend-developer": {
                    "allowed_tools": [],
                    "file_write_scope": "should-be-a-list",
                }
            },
            "terminal_assignments": {},
        }
        issues = validate_permissions(bad)
        assert any("file_write_scope" in i for i in issues)

    def test_invalid_mcp_servers_not_list(self) -> None:
        bad = {
            "profiles": {
                "backend-developer": {
                    "allowed_tools": [],
                    "mcp_servers": "notion",
                }
            },
            "terminal_assignments": {},
        }
        issues = validate_permissions(bad)
        assert any("mcp_servers" in i for i in issues)


# ---------------------------------------------------------------------------
# diff_summary helper
# ---------------------------------------------------------------------------

class TestDiffSummary:
    def test_added_scope_path_appears_in_diff(self) -> None:
        before = merge_permissions({}, VNX_TEMPLATE)
        # Simulate a project adding src/**
        project_with_src = yaml.safe_load(yaml.dump(before))
        project_with_src["profiles"]["backend-developer"]["file_write_scope"].append("src/**")
        after = merge_permissions(project_with_src, VNX_TEMPLATE)
        summary = diff_summary(before, after)
        assert any("src/**" in line for line in summary), (
            f"diff_summary did not mention src/**:\n{summary}"
        )

    def test_no_changes_produces_empty_summary(self) -> None:
        merged = merge_permissions(PROJECT_WITH_SRC_SCOPE, VNX_TEMPLATE)
        # Second merge must show no changes
        second = merge_permissions(merged, VNX_TEMPLATE)
        summary = diff_summary(merged, second)
        assert summary == [], f"Expected no diff, got: {summary}"
