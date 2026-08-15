"""Tests for worker_permissions.py — per-terminal permission profiles."""

from __future__ import annotations

import json
import sys
import textwrap
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "lib"))

from worker_permissions import (
    DEFAULT_CODE_WORKER_TOOLS,
    EMPTY_MCP_CONFIG,
    MCP_NAMESPACE_DENY,
    PermissionProfile,
    build_claude_scope_args,
    classify_permission_posture,
    default_code_worker_profile,
    generate_claude_settings,
    generate_permission_preamble,
    load_permissions,
    match_bash_deny,
    match_file_write_scope,
    resolve_dispatch_write_scope,
    resolve_role_mcp_config,
    resolve_worker_profile,
    validate_dispatch_permissions,
)

# ---------------------------------------------------------------------------
# Canonical dispatched roles — the seven roles T0 actually dispatches, per the
# role-selection table in .claude/terminals/T0/role-orchestrator.md. Each MUST
# have a real profile in .vnx/worker_permissions.yaml (OI-1100). This list is the
# regression contract: if a role appears here but resolves to the code-worker
# fallback, the test goes red.
# ---------------------------------------------------------------------------
REPO_ROOT = Path(__file__).resolve().parents[1]
REPO_PERMISSIONS_YAML = REPO_ROOT / ".vnx" / "worker_permissions.yaml"
CANONICAL_DISPATCH_ROLES = [
    "backend-developer",
    "code-reviewer",
    "frontend-developer",
    "quality-engineer",
    "research-analyst",
    "security-engineer",
    "system-architect",
]

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

SAMPLE_YAML = textwrap.dedent("""\
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
        bash_deny_patterns:
          - "rm -rf*"
          - "git reset --hard*"
          - "git push --force*"
          - "git push -f*"
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

      frontend-developer:
        allowed_tools: [Read, Write, Edit, MultiEdit, Bash, Grep, Glob]
        denied_tools: [WebSearch, WebFetch]
        bash_allow_patterns:
          - "npm*"
          - "npx*"
          - "git add*"
          - "git commit*"
        bash_deny_patterns:
          - "rm -rf*"
          - "git push --force*"
        file_write_scope:
          - "dashboard/**"

    terminal_assignments:
      T1: backend-developer
      T2: test-engineer
      T3: frontend-developer
""")


@pytest.fixture
def yaml_file(tmp_path: Path) -> Path:
    p = tmp_path / "worker_permissions.yaml"
    p.write_text(SAMPLE_YAML)
    return p


# ---------------------------------------------------------------------------
# test_load_backend_profile
# ---------------------------------------------------------------------------

def test_load_backend_profile(yaml_file: Path) -> None:
    profile = load_permissions("backend-developer", yaml_path=yaml_file)

    assert profile.role == "backend-developer"
    assert "Read" in profile.allowed_tools
    assert "Bash" in profile.allowed_tools
    assert "WebSearch" in profile.denied_tools
    assert "WebFetch" in profile.denied_tools
    assert "rm -rf*" in profile.bash_deny_patterns
    assert "pytest*" in profile.bash_allow_patterns
    assert "scripts/**" in profile.file_write_scope
    assert profile.mcp_servers == ["notion"]


# ---------------------------------------------------------------------------
# test_load_test_engineer_profile
# ---------------------------------------------------------------------------

def test_load_test_engineer_profile(yaml_file: Path) -> None:
    profile = load_permissions("test-engineer", yaml_path=yaml_file)

    assert profile.role == "test-engineer"
    assert "MultiEdit" in profile.denied_tools
    assert "git push*" in profile.bash_deny_patterns
    assert "tests/**" in profile.file_write_scope
    # test-engineer cannot push
    assert any("git push" in p for p in profile.bash_deny_patterns)
    # no mcp_servers declared for this role -> empty allowlist, not fabricated
    assert profile.mcp_servers == []


# ---------------------------------------------------------------------------
# test_role_terminal_mismatch_warning
# ---------------------------------------------------------------------------

def test_role_terminal_mismatch_warning(yaml_file: Path) -> None:
    # T1 is assigned backend-developer; passing frontend-developer triggers warning
    warnings = validate_dispatch_permissions(
        {"terminal": "T1", "role": "frontend-developer"},
        yaml_path=yaml_file,
    )
    assert len(warnings) == 1
    assert "T1" in warnings[0]
    assert "backend-developer" in warnings[0]
    assert "frontend-developer" in warnings[0]


def test_role_terminal_match_no_warning(yaml_file: Path) -> None:
    warnings = validate_dispatch_permissions(
        {"terminal": "T1", "role": "backend-developer"},
        yaml_path=yaml_file,
    )
    assert warnings == []


def test_unknown_terminal_produces_warning(yaml_file: Path) -> None:
    warnings = validate_dispatch_permissions(
        {"terminal": "T9", "role": "backend-developer"},
        yaml_path=yaml_file,
    )
    assert len(warnings) >= 1
    assert "T9" in warnings[0]


def test_unknown_role_produces_warning(yaml_file: Path) -> None:
    warnings = validate_dispatch_permissions(
        {"terminal": "T1", "role": "ghost-writer"},
        yaml_path=yaml_file,
    )
    # One warning for mismatch, one for missing profile
    assert len(warnings) == 2


# ---------------------------------------------------------------------------
# test_bash_deny_pattern_matched
# ---------------------------------------------------------------------------

def test_bash_deny_pattern_matched(yaml_file: Path) -> None:
    profile = load_permissions("backend-developer", yaml_path=yaml_file)

    assert match_bash_deny("rm -rf /tmp/something", profile) == "rm -rf*"
    assert match_bash_deny("git push --force origin main", profile) == "git push --force*"
    assert match_bash_deny("git push -f origin main", profile) == "git push -f*"
    assert match_bash_deny("git reset --hard HEAD~1", profile) == "git reset --hard*"


def test_bash_deny_pattern_not_matched(yaml_file: Path) -> None:
    profile = load_permissions("backend-developer", yaml_path=yaml_file)

    # Safe commands should not match any deny pattern
    assert match_bash_deny("pytest tests/", profile) is None
    assert match_bash_deny("python3 -m pytest", profile) is None
    assert match_bash_deny("git push origin feat/my-branch", profile) is None


# ---------------------------------------------------------------------------
# test_file_write_scope_enforced
# ---------------------------------------------------------------------------

def test_file_write_scope_enforced(yaml_file: Path) -> None:
    profile = load_permissions("test-engineer", yaml_path=yaml_file)

    assert match_file_write_scope("tests/test_foo.py", profile) is True
    assert match_file_write_scope("scripts/check_health.sh", profile) is True
    # Outside scope
    assert match_file_write_scope("dashboard/app.ts", profile) is False
    assert match_file_write_scope("scripts/dispatch.py", profile) is False


def test_file_write_scope_backend(yaml_file: Path) -> None:
    profile = load_permissions("backend-developer", yaml_path=yaml_file)

    assert match_file_write_scope("scripts/lib/foo.py", profile) is True
    assert match_file_write_scope("tests/test_bar.py", profile) is True
    assert match_file_write_scope("dashboard/token-dashboard/app.ts", profile) is True


# ---------------------------------------------------------------------------
# OI-1196: dispatch_paths -> file_write_scope narrowing
# ---------------------------------------------------------------------------

class TestResolveDispatchWriteScope:
    """resolve_dispatch_write_scope: raw --dispatch-paths entries -> write globs."""

    def test_none_when_no_paths_declared(self) -> None:
        assert resolve_dispatch_write_scope(None) is None
        assert resolve_dispatch_write_scope([]) is None

    def test_bare_path_defaults_to_read_write(self) -> None:
        assert resolve_dispatch_write_scope(["scripts/lib/foo.py"]) == [
            "scripts/lib/foo.py"
        ]

    def test_read_access_excluded_from_write_scope(self) -> None:
        # Declared paths, but every one is read-only -> empty list, NOT None.
        result = resolve_dispatch_write_scope(["docs/README.md:read"])
        assert result == []

    def test_write_and_create_and_read_write_all_grant_write(self) -> None:
        result = resolve_dispatch_write_scope(
            [
                "a.py:write",
                "b.py:read_write",
                "c.py:create",
                "d.py:read",
            ]
        )
        assert sorted(result) == ["a.py", "b.py", "c.py"]

    def test_unknown_access_suffix_treated_as_part_of_path(self) -> None:
        # "bogus" is not a legal PathAccess value, so the whole string is the
        # path (defaults to read_write) rather than silently misparsed.
        result = resolve_dispatch_write_scope(["weird:bogus"])
        assert result == ["weird:bogus"]


class TestMatchFileWriteScopeDispatchNarrowing:
    """match_file_write_scope's dispatch_write_scope param: intersection, never union."""

    def test_no_dispatch_scope_is_unchanged_behavior(self, yaml_file: Path) -> None:
        """dispatch_write_scope=None (not declared) must match pre-OI-1196 behavior."""
        profile = load_permissions("backend-developer", yaml_path=yaml_file)
        assert match_file_write_scope("scripts/lib/foo.py", profile) is True
        assert match_file_write_scope("scripts/lib/foo.py", profile, None) is True
        assert match_file_write_scope("docs/x.md", profile) is False
        assert match_file_write_scope("docs/x.md", profile, None) is False

    def test_dispatch_scope_narrower_than_role_is_enforced(self, yaml_file: Path) -> None:
        """A write inside role scope but outside the dispatch's declared paths is blocked."""
        profile = load_permissions("backend-developer", yaml_path=yaml_file)
        dispatch_scope = resolve_dispatch_write_scope(["scripts/lib/foo.py"])
        assert match_file_write_scope("scripts/lib/foo.py", profile, dispatch_scope) is True
        # Also within role scope (scripts/**) but not declared by this dispatch.
        assert match_file_write_scope("scripts/lib/bar.py", profile, dispatch_scope) is False

    def test_dispatch_scope_wider_than_role_never_widens(self, yaml_file: Path) -> None:
        """OI-1196 hard rule: a dispatch can never grant write outside the role's scope."""
        profile = load_permissions("backend-developer", yaml_path=yaml_file)
        # docs/** is outside backend-developer's file_write_scope entirely.
        dispatch_scope = resolve_dispatch_write_scope(["docs/README.md"])
        assert match_file_write_scope("docs/README.md", profile, dispatch_scope) is False

    def test_all_read_access_dispatch_scope_blocks_every_write(self, yaml_file: Path) -> None:
        """Declaring only read-access paths means the dispatch does no writing at all."""
        profile = load_permissions("backend-developer", yaml_path=yaml_file)
        dispatch_scope = resolve_dispatch_write_scope(["scripts/lib/foo.py:read"])
        assert dispatch_scope == []
        assert match_file_write_scope("scripts/lib/foo.py", profile, dispatch_scope) is False

    def test_composes_with_fallback_role_profile(self) -> None:
        """The fallback (depth-limited, non-glob) role scope still ANDs correctly."""
        profile = default_code_worker_profile()
        dispatch_scope = resolve_dispatch_write_scope(["src/app.py"])
        # Within the fallback depth limit AND the declared dispatch path.
        assert match_file_write_scope("src/app.py", profile, dispatch_scope) is True
        # Within the fallback depth limit but NOT declared by this dispatch.
        assert match_file_write_scope("src/other.py", profile, dispatch_scope) is False
        # Declared by the dispatch but exceeds the fallback depth limit (7 segments).
        deep_scope = resolve_dispatch_write_scope(["a/b/c/d/e/f/out.md"])
        assert match_file_write_scope("a/b/c/d/e/f/out.md", profile, deep_scope) is False


# ---------------------------------------------------------------------------
# test_generate_claude_settings
# ---------------------------------------------------------------------------

def test_generate_claude_settings(yaml_file: Path) -> None:
    profile = load_permissions("backend-developer", yaml_path=yaml_file)
    settings = generate_claude_settings(profile)

    assert "allowedTools" in settings
    allowed = settings["allowedTools"]
    assert "Read" in allowed
    assert "Bash" in allowed
    # Denied tools must not appear
    assert "WebSearch" not in allowed
    assert "WebFetch" not in allowed


# ---------------------------------------------------------------------------
# test_generate_permission_preamble
# ---------------------------------------------------------------------------

def test_generate_permission_preamble_contains_role(yaml_file: Path) -> None:
    profile = load_permissions("backend-developer", yaml_path=yaml_file)
    preamble = generate_permission_preamble(profile)

    assert "backend-developer" in preamble
    assert "Permission Profile" in preamble
    assert "rm -rf*" in preamble
    assert "WebSearch" in preamble


# ---------------------------------------------------------------------------
# test_missing_role_returns_empty_profile
# ---------------------------------------------------------------------------

def test_missing_role_returns_empty_profile(yaml_file: Path) -> None:
    profile = load_permissions("nonexistent-role", yaml_path=yaml_file)

    assert profile.role == "nonexistent-role"
    assert profile.allowed_tools == []
    assert profile.denied_tools == []
    assert profile.bash_deny_patterns == []
    assert profile.mcp_servers == []


# ---------------------------------------------------------------------------
# test_empty_metadata_produces_no_warnings
# ---------------------------------------------------------------------------

def test_empty_metadata_produces_no_warnings(yaml_file: Path) -> None:
    warnings = validate_dispatch_permissions({}, yaml_path=yaml_file)
    assert warnings == []


# ---------------------------------------------------------------------------
# resolve_role_mcp_config / build_claude_scope_args — mcp_servers allowlist
# ---------------------------------------------------------------------------

@pytest.fixture
def global_mcp_config(tmp_path: Path) -> Path:
    """A fake ambient global config (~/.claude.json shape) with two servers."""
    p = tmp_path / "global.claude.json"
    p.write_text(json.dumps({
        "mcpServers": {
            "notion": {"command": "npx", "args": ["-y", "notion-mcp"]},
            "gmail": {"command": "npx", "args": ["-y", "gmail-mcp"]},
        }
    }))
    return p


class TestResolveRoleMcpConfig:
    def test_empty_allowlist_yields_empty_mcp_servers(self, global_mcp_config: Path) -> None:
        profile = PermissionProfile(role="r")
        assert resolve_role_mcp_config(profile, global_mcp_config) == {"mcpServers": {}}

    def test_allowlisted_server_included_with_its_definition(self, global_mcp_config: Path) -> None:
        profile = PermissionProfile(role="r", mcp_servers=["notion"])
        result = resolve_role_mcp_config(profile, global_mcp_config)
        assert result == {"mcpServers": {"notion": {"command": "npx", "args": ["-y", "notion-mcp"]}}}
        # gmail exists in the ambient config but was not allowlisted -> excluded
        assert "gmail" not in result["mcpServers"]

    def test_allowlisted_name_not_in_ambient_config_is_skipped_not_fabricated(
        self, global_mcp_config: Path
    ) -> None:
        profile = PermissionProfile(role="r", mcp_servers=["notion", "does-not-exist"])
        result = resolve_role_mcp_config(profile, global_mcp_config)
        assert set(result["mcpServers"].keys()) == {"notion"}

    def test_missing_global_config_file_yields_empty_scoped_set(self, tmp_path: Path) -> None:
        profile = PermissionProfile(role="r", mcp_servers=["notion"])
        result = resolve_role_mcp_config(profile, tmp_path / "does-not-exist.json")
        assert result == {"mcpServers": {}}

    def test_malformed_global_config_does_not_crash(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad.json"
        bad.write_text("{not valid json")
        profile = PermissionProfile(role="r", mcp_servers=["notion"])
        result = resolve_role_mcp_config(profile, bad)
        assert result == {"mcpServers": {}}


class TestBuildClaudeScopeArgsMcpServers:
    def test_no_allowlist_keeps_empty_mcp_config(self, global_mcp_config: Path) -> None:
        # Backward compat: a profile without mcp_servers is byte-identical to
        # the pre-existing EMPTY_MCP_CONFIG behavior.
        profile = PermissionProfile(role="r", allowed_tools=["Read"])
        args = build_claude_scope_args(profile, global_mcp_config_path=global_mcp_config)
        idx = args.index("--mcp-config")
        assert args[idx + 1] == EMPTY_MCP_CONFIG

    def test_allowlist_narrows_mcp_config_to_named_servers(self, global_mcp_config: Path) -> None:
        profile = PermissionProfile(role="r", allowed_tools=["Read"], mcp_servers=["notion"])
        args = build_claude_scope_args(profile, global_mcp_config_path=global_mcp_config)
        assert "--strict-mcp-config" in args
        idx = args.index("--mcp-config")
        assert json.loads(args[idx + 1]) == {
            "mcpServers": {"notion": {"command": "npx", "args": ["-y", "notion-mcp"]}}
        }

    def test_requires_mcp_true_ignores_allowlist_entirely(self, global_mcp_config: Path) -> None:
        # requires_mcp=True keeps the dispatch's full ambient config — the
        # allowlist reconciliation is explicit follow-up work (Open Items).
        profile = PermissionProfile(role="r", allowed_tools=["Read"], mcp_servers=["notion"])
        args = build_claude_scope_args(
            profile, requires_mcp=True, global_mcp_config_path=global_mcp_config
        )
        assert "--strict-mcp-config" not in args
        assert "--mcp-config" not in args


class TestMcpNamespaceDeny:
    """The mcp__ namespace is denied via --disallowedTools, not only the empty
    --mcp-config (dispatch 20260815-mcp-namespace-leak).

    The empty --mcp-config only reaches the mcpServers group; extension bridges
    (claude-in-chrome) surface mcp__ tools without appearing in `claude mcp
    list`. The `mcp__*` glob closes the gap for the whole namespace. Verified
    against claude 2.1.233 (see scripts/analysis/mcp_namespace_probe.py).
    """

    def test_mcp_namespace_denied_by_default(self) -> None:
        args = build_claude_scope_args(default_code_worker_profile())
        denied = args[args.index("--disallowedTools") + 1].split(",")
        assert MCP_NAMESPACE_DENY in denied

    def test_mcp_namespace_deny_alongside_websearch_deny(self) -> None:
        # The existing WebSearch/WebFetch deny must remain alongside mcp__*.
        args = build_claude_scope_args(default_code_worker_profile())
        denied = args[args.index("--disallowedTools") + 1].split(",")
        assert "WebSearch" in denied
        assert "WebFetch" in denied
        assert MCP_NAMESPACE_DENY in denied

    def test_requires_mcp_true_keeps_mcp_namespace(self) -> None:
        # requires_mcp=True keeps the worker's MCP tools — the namespace deny
        # must NOT be added (only WebSearch/WebFetch from the profile remain).
        args = build_claude_scope_args(default_code_worker_profile(), requires_mcp=True)
        denied = args[args.index("--disallowedTools") + 1].split(",")
        assert MCP_NAMESPACE_DENY not in denied

    def test_mcp_namespace_deny_combined_with_working_tree_only_git_deny(self) -> None:
        args = build_claude_scope_args(default_code_worker_profile(), working_tree_only=True)
        denied = args[args.index("--disallowedTools") + 1].split(",")
        assert MCP_NAMESPACE_DENY in denied
        assert "Bash(git commit)" in denied
        assert "Bash(git push)" in denied

    def test_deny_not_duplicated_when_profile_declares_it(self) -> None:
        # A role that already declares mcp__* in denied_tools must not get it twice.
        profile = PermissionProfile(
            role="r", allowed_tools=["Read"], denied_tools=[MCP_NAMESPACE_DENY, "WebSearch"]
        )
        args = build_claude_scope_args(profile)
        denied = args[args.index("--disallowedTools") + 1].split(",")
        assert denied.count(MCP_NAMESPACE_DENY) == 1


# ---------------------------------------------------------------------------
# classify_permission_posture (OI-864)
# ---------------------------------------------------------------------------

class TestClassifyPermissionPosture:
    """Posture must come from the ACTUAL assembled argv, never from re-reading
    VNX_ENFORCE_WORKER_PERMISSIONS/VNX_WORKER_SCOPED — two independent reads of
    that OR-condition can diverge from the flags a given spawn actually used
    (e.g. VNX_WORKER_SCOPED=1 alone with the newer flag unset)."""

    # An unknown role deterministically falls back to default_code_worker_profile()
    # (9 allowed tools, role="code-worker") regardless of this repo's real
    # .vnx/worker_permissions.yaml content — keeps these tests independent of
    # that file's contents.
    UNKNOWN_ROLE = "nonexistent-role-xyz-oi864"

    def test_blanket_skip_argv(self) -> None:
        argv = ["claude", "--model", "sonnet", "--dangerously-skip-permissions"]
        result = classify_permission_posture(argv, self.UNKNOWN_ROLE)
        assert result == {"permission_posture": "blanket-skip"}

    def test_scoped_allowlist_argv_includes_profile_and_count(self) -> None:
        argv = [
            "claude", "--model", "sonnet",
            "--permission-mode", "acceptEdits",
            "--strict-mcp-config", "--mcp-config", EMPTY_MCP_CONFIG,
            "--allowedTools", "Read,Write,Edit,MultiEdit,Bash,Grep,Glob,Bash(git:*),Bash(gh:*)",
        ]
        result = classify_permission_posture(argv, self.UNKNOWN_ROLE)
        assert result["permission_posture"] == "scoped-allowlist"
        assert result["permission_profile"] == "code-worker"
        assert result["permission_allow_pattern_count"] == 9

    def test_attached_interactive_argv(self) -> None:
        """Neither flag present — a human-attended session (attach=True in the
        tmux lane), answering real permission prompts."""
        argv = ["claude", "--model", "sonnet"]
        result = classify_permission_posture(argv, self.UNKNOWN_ROLE)
        assert result == {"permission_posture": "attached-interactive"}

    def test_scoped_with_empty_allowlist_counts_zero(self) -> None:
        argv = ["claude", "--model", "sonnet", "--permission-mode", "acceptEdits"]
        result = classify_permission_posture(argv, self.UNKNOWN_ROLE)
        assert result["permission_posture"] == "scoped-allowlist"
        assert result["permission_allow_pattern_count"] == 0

    def test_blanket_skip_wins_even_if_scoped_flags_also_present(self) -> None:
        # --dangerously-skip-permissions is checked first: if a caller somehow
        # assembled both (should never happen), the actually-effective claude
        # behavior is the skip flag, so posture must report that, not scoped.
        argv = [
            "claude", "--model", "sonnet",
            "--dangerously-skip-permissions",
            "--permission-mode", "acceptEdits", "--allowedTools", "Read",
        ]
        result = classify_permission_posture(argv, self.UNKNOWN_ROLE)
        assert result["permission_posture"] == "blanket-skip"

    def test_posture_diverges_between_scoped_off_and_on(self) -> None:
        """The required OI-864 negative test: same role, only the argv differs
        (mirroring VNX_WORKER_SCOPED/VNX_ENFORCE_WORKER_PERMISSIONS off vs on)
        — the classified posture must differ. Asserting only that the field
        EXISTS would still pass if the classifier always returned a constant;
        asserting the VALUE differs is what actually proves the wiring works.
        """
        off_argv = ["claude", "--model", "sonnet", "--dangerously-skip-permissions"]
        on_argv = [
            "claude", "--model", "sonnet",
            "--permission-mode", "acceptEdits",
            "--allowedTools", "Read,Write,Edit",
        ]
        posture_off = classify_permission_posture(off_argv, "backend-developer")
        posture_on = classify_permission_posture(on_argv, "backend-developer")

        assert posture_off["permission_posture"] == "blanket-skip"
        assert posture_on["permission_posture"] == "scoped-allowlist"
        assert posture_off["permission_posture"] != posture_on["permission_posture"]
        assert "permission_profile" not in posture_off
        assert posture_on["permission_profile"] == "backend-developer"
        assert posture_on["permission_allow_pattern_count"] == 3


# ---------------------------------------------------------------------------
# OI-1100 — every canonical dispatched role resolves to a REAL profile, not the
# code-worker fallback. This test is RED on main (four of the seven roles have no
# profile and silently inherit the 15-tool permissive fallback) and GREEN on the
# branch that adds profiles for quality-engineer / system-architect /
# research-analyst / code-reviewer.
# ---------------------------------------------------------------------------

@pytest.mark.skipif(
    not REPO_PERMISSIONS_YAML.exists(),
    reason="repo .vnx/worker_permissions.yaml not present in this checkout",
)
class TestCanonicalRolesHaveProfiles:
    """Pin that every dispatched role resolves to a real YAML profile."""

    def test_every_canonical_role_resolves_to_a_real_profile(self) -> None:
        missing = []
        fallback = []
        for role in CANONICAL_DISPATCH_ROLES:
            profile = resolve_worker_profile(role, REPO_PERMISSIONS_YAML)
            if profile.is_fallback:
                fallback.append(role)
            elif profile.role != role:
                missing.append((role, profile.role))
        assert not fallback, (
            f"canonical roles resolved to the code-worker fallback (no real profile): "
            f"{fallback}. Each dispatched role MUST have a profile in "
            f".vnx/worker_permissions.yaml (OI-1100)."
        )
        assert not missing, (
            f"canonical roles resolved to an unexpected profile role: {missing}"
        )

    def test_canonical_roles_match_agents_registry(self) -> None:
        """The canonical role names must exist as agents/<role>/ directories —
        the registry is the SSOT for role names, and the yaml must follow it."""
        agents_dir = REPO_ROOT / "agents"
        missing_dirs = [
            role for role in CANONICAL_DISPATCH_ROLES
            if not (agents_dir / role).is_dir()
        ]
        assert not missing_dirs, (
            f"canonical roles with no agents/<role>/ directory in the registry: "
            f"{missing_dirs}. Role names must match the agents/ registry."
        )

    def test_build_roles_may_commit_and_push(self) -> None:
        """OI-1100 deny-vs-duty: build roles (default_instruction says
        'commit and push') must NOT deny git add/commit/push, or the
        push-mandatory footer cannot be satisfied."""
        build_roles = [
            "backend-developer", "frontend-developer", "security-engineer",
            "system-architect", "quality-engineer",
        ]
        offenders = []
        for role in build_roles:
            profile = load_permissions(role, REPO_PERMISSIONS_YAML)
            for pat in profile.bash_deny_patterns:
                if pat in ("git add*", "git commit*", "git push*"):
                    offenders.append((role, pat))
        assert not offenders, (
            f"build roles deny a git op required by the push-mandatory footer: "
            f"{offenders}. A role dispatched with the standard footer must be "
            f"able to commit and push (OI-1100)."
        )

    def test_read_roles_deny_code_mutation(self) -> None:
        """OI-1100: read roles (code-reviewer, research-analyst) deny Edit/
        MultiEdit and git history mutation — they are not dispatched with the
        push-mandatory footer, so the deny and the duty must agree."""
        read_roles = ["code-reviewer", "research-analyst"]
        for role in read_roles:
            profile = load_permissions(role, REPO_PERMISSIONS_YAML)
            assert "Edit" in profile.denied_tools, (
                f"read role {role} must deny Edit (does not modify code)"
            )
            assert "MultiEdit" in profile.denied_tools, (
                f"read role {role} must deny MultiEdit"
            )
            deny_set = set(profile.bash_deny_patterns)
            assert {"git add*", "git commit*", "git push*"} <= deny_set, (
                f"read role {role} must deny git add/commit/push"
            )

    def test_legacy_role_names_not_present(self) -> None:
        """The old names (test-engineer, architect, database-engineer,
        intelligence-engineer) must NOT carry profiles — they are not dispatched
        and would otherwise shadow the canonical names via the merge."""
        from yaml import safe_load
        data = safe_load(REPO_PERMISSIONS_YAML.read_text()) or {}
        profiles = data.get("profiles", {})
        stale = [
            r for r in ("test-engineer", "architect",
                        "database-engineer", "intelligence-engineer")
            if r in profiles
        ]
        assert not stale, (
            f"legacy non-canonical role names still carry profiles: {stale}. "
            f"Use the canonical registry name instead (OI-1100)."
        )


# ---------------------------------------------------------------------------
# OI-1100 — unknown role gets the EXPLICIT fallback, not a silent permissive
# default. The fallback is restrictive (DEFAULT_CODE_WORKER_TOOLS, no MCP,
# WebSearch/WebFetch denied) AND marked is_fallback=True AND logged.
# ---------------------------------------------------------------------------

class TestUnknownRoleExplicitFallback:
    def test_unknown_role_is_marked_fallback(self, yaml_file: Path) -> None:
        profile = resolve_worker_profile("totally-unknown-role-xyz", yaml_file)
        assert profile.is_fallback is True, (
            "unknown role must resolve to the fallback marked is_fallback=True "
            "(OI-1100), not a silent permissive default"
        )
        assert profile.role == "code-worker"

    def test_unknown_role_fallback_is_restrictive(self, yaml_file: Path) -> None:
        """The fallback carries the code-worker toolset (no MCP, WebSearch/WebFetch
        denied) — restrictive vs blanket-skip, never silently permissive."""
        profile = resolve_worker_profile("totally-unknown-role-xyz", yaml_file)
        assert set(profile.allowed_tools) == set(DEFAULT_CODE_WORKER_TOOLS)
        assert "WebSearch" in profile.denied_tools
        assert "WebFetch" in profile.denied_tools
        assert profile.mcp_servers == []

    def test_unknown_role_logs_warning(self, yaml_file: Path, caplog) -> None:
        import logging
        with caplog.at_level(logging.WARNING, logger="worker_permissions"):
            resolve_worker_profile("totally-unknown-role-xyz", yaml_file)
        assert any(
            "code-worker fallback" in rec.getMessage()
            for rec in caplog.records
        ), "unknown role must log an explicit warning naming the fallback (OI-1100)"

    def test_none_role_is_marked_fallback(self) -> None:
        profile = resolve_worker_profile(None)
        assert profile.is_fallback is True

    def test_known_role_not_marked_fallback(self, yaml_file: Path) -> None:
        profile = resolve_worker_profile("backend-developer", yaml_file)
        assert profile.is_fallback is False

