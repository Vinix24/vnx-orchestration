"""Tests for worker_permissions.py — per-terminal permission profiles."""

from __future__ import annotations

import sys
import textwrap
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "lib"))

from worker_permissions import (
    PermissionProfile,
    generate_claude_settings,
    generate_permission_preamble,
    load_permissions,
    match_bash_deny,
    match_file_write_scope,
    validate_dispatch_permissions,
)

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


# ---------------------------------------------------------------------------
# test_empty_metadata_produces_no_warnings
# ---------------------------------------------------------------------------

def test_empty_metadata_produces_no_warnings(yaml_file: Path) -> None:
    warnings = validate_dispatch_permissions({}, yaml_path=yaml_file)
    assert warnings == []
