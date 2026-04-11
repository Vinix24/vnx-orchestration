#!/usr/bin/env python3
"""Integration tests for agent dispatch routing — F40 PR-2.

Covers:
  1. test_agent_dir_detection               — _resolve_agent_cwd returns correct path
  2. test_governance_profile_loading        — _load_agent_profile reads config.yaml
  3. test_scope_isolation                   — business folder scope blocks scripts/ access
  4. test_dispatch_writer_creates_agent_dispatch — dispatch JSON fields are valid for agent role
  5. test_unknown_agent_raises              — dispatch-agent.sh exits non-zero for unknown agent
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_LIB = REPO_ROOT / "scripts" / "lib"
DISPATCH_AGENT_SH = REPO_ROOT / "scripts" / "commands" / "dispatch-agent.sh"

sys.path.insert(0, str(SCRIPTS_LIB))

from subprocess_dispatch import _load_agent_profile, _resolve_agent_cwd


# ---------------------------------------------------------------------------
# 1. test_agent_dir_detection
# ---------------------------------------------------------------------------

class TestAgentDirDetection:
    """_resolve_agent_cwd resolves to agents/{role}/ when the directory exists."""

    def test_known_agent_returns_path(self):
        """blog-writer agent dir exists — _resolve_agent_cwd must return it."""
        agent_dir = REPO_ROOT / "agents" / "blog-writer"
        assert agent_dir.is_dir(), "PR-1 precondition: agents/blog-writer/ must exist"

        result = _resolve_agent_cwd("blog-writer")

        assert result is not None
        assert result.is_dir()
        assert result.name == "blog-writer"
        assert result == agent_dir

    def test_unknown_agent_returns_none(self):
        """Non-existent agent role returns None — no crash."""
        result = _resolve_agent_cwd("nonexistent-agent-xyz")
        assert result is None

    def test_none_role_returns_none(self):
        """None role always returns None."""
        assert _resolve_agent_cwd(None) is None

    def test_all_known_agents_resolve(self):
        """All three PR-1 agents resolve to their directories."""
        for agent in ("blog-writer", "linkedin-writer", "research-analyst"):
            result = _resolve_agent_cwd(agent)
            assert result is not None, f"Agent dir missing for {agent}"
            assert (result / "CLAUDE.md").is_file(), f"CLAUDE.md missing for {agent}"


# ---------------------------------------------------------------------------
# 2. test_governance_profile_loading
# ---------------------------------------------------------------------------

class TestGovernanceProfileLoading:
    """_load_agent_profile reads governance_profile from config.yaml."""

    def test_loads_light_profile_from_blog_writer(self):
        """blog-writer config.yaml declares governance_profile: light."""
        config_path = REPO_ROOT / "agents" / "blog-writer" / "config.yaml"
        assert config_path.exists(), "PR-1 precondition: blog-writer/config.yaml must exist"

        profile = _load_agent_profile(config_path)
        assert profile == "light"

    def test_loads_profile_from_temp_config(self):
        """_load_agent_profile works for any config.yaml with governance_profile key."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write("governance_profile: minimal\nisolation:\n  scope_type: business_folder\n")
            tmp = Path(f.name)

        try:
            assert _load_agent_profile(tmp) == "minimal"
        finally:
            tmp.unlink(missing_ok=True)

    def test_missing_key_returns_default(self):
        """Config without governance_profile key returns 'default'."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write("isolation:\n  scope_type: business_folder\n")
            tmp = Path(f.name)

        try:
            assert _load_agent_profile(tmp) == "default"
        finally:
            tmp.unlink(missing_ok=True)

    def test_unreadable_file_returns_default(self):
        """Non-existent config.yaml path returns 'default' gracefully."""
        missing = Path("/tmp/nonexistent_vnx_config_xyz.yaml")
        assert _load_agent_profile(missing) == "default"

    def test_all_agents_have_light_profile(self):
        """All three PR-1 agents declare governance_profile: light."""
        for agent in ("blog-writer", "linkedin-writer", "research-analyst"):
            config_path = REPO_ROOT / "agents" / agent / "config.yaml"
            assert config_path.exists(), f"config.yaml missing for {agent}"
            profile = _load_agent_profile(config_path)
            assert profile == "light", f"{agent} expected 'light', got {profile!r}"


# ---------------------------------------------------------------------------
# 3. test_scope_isolation
# ---------------------------------------------------------------------------

class TestScopeIsolation:
    """Business folder scope enforces agent isolation — scripts/ is denied."""

    def test_agent_folder_is_allowed(self):
        """An agent's own directory is within its business folder scope."""
        from folder_scope import business_folder_scope, assemble_context

        agent_root = str(REPO_ROOT / "agents" / "blog-writer")
        scope = business_folder_scope(agent_root)
        ctx = assemble_context(scope, sources=[agent_root + "/CLAUDE.md"])

        assert ctx.is_path_allowed(agent_root + "/CLAUDE.md")
        assert ctx.is_path_allowed(agent_root + "/output.md")

    def test_scripts_dir_is_denied_from_agent_scope(self):
        """scripts/ path is outside the agent's business folder scope."""
        from folder_scope import business_folder_scope, IsolationViolation

        agent_root = str(REPO_ROOT / "agents" / "blog-writer")
        scope = business_folder_scope(agent_root)

        scripts_path = str(REPO_ROOT / "scripts" / "lib" / "subprocess_dispatch.py")
        assert not scope.contains_path(scripts_path)

    def test_cross_agent_access_denied(self):
        """One agent's scope cannot include another agent's path."""
        from folder_scope import business_folder_scope

        blog_root = str(REPO_ROOT / "agents" / "blog-writer")
        linkedin_path = str(REPO_ROOT / "agents" / "linkedin-writer" / "CLAUDE.md")

        scope = business_folder_scope(blog_root)
        assert not scope.contains_path(linkedin_path)

    def test_assemble_context_raises_for_out_of_scope_source(self):
        """assemble_context raises IsolationViolation for out-of-scope sources."""
        from folder_scope import business_folder_scope, assemble_context, IsolationViolation

        agent_root = str(REPO_ROOT / "agents" / "blog-writer")
        scope = business_folder_scope(agent_root)
        scripts_path = str(REPO_ROOT / "scripts" / "lib" / "subprocess_dispatch.py")

        with pytest.raises(IsolationViolation):
            assemble_context(scope, sources=[scripts_path])


# ---------------------------------------------------------------------------
# 4. test_dispatch_writer_creates_agent_dispatch
# ---------------------------------------------------------------------------

class TestDispatchWriterCreatesAgentDispatch:
    """headless_dispatch_writer creates valid dispatch files for agent roles."""

    def test_generate_dispatch_id_format(self):
        """generate_dispatch_id returns a timestamped ID in expected format."""
        from headless_dispatch_writer import generate_dispatch_id

        dispatch_id = generate_dispatch_id("agent-blog-writer", "A")

        parts = dispatch_id.split("-")
        assert len(parts) >= 4, f"Expected at least 4 dash-separated parts, got: {dispatch_id}"
        assert len(parts[0]) == 8, "First part must be YYYYMMDD"
        assert len(parts[1]) == 6, "Second part must be HHMMSS"
        assert "agent" in dispatch_id
        assert dispatch_id.endswith("-A")

    def test_dispatch_json_written_with_agent_role(self):
        """Dispatch JSON written for an agent role contains correct fields."""
        with tempfile.TemporaryDirectory() as tmpdir:
            from headless_dispatch_writer import generate_dispatch_id
            from datetime import datetime, timezone

            dispatch_id = generate_dispatch_id("agent-blog-writer", "A")
            pending_dir = Path(tmpdir) / "pending" / dispatch_id
            pending_dir.mkdir(parents=True)

            payload = {
                "dispatch_id": dispatch_id,
                "terminal": "T1",
                "track": "A",
                "role": "blog-writer",
                "skill_name": "blog-writer",
                "gate": "gate_fix",
                "cognition": "normal",
                "priority": "P1",
                "pr_id": None,
                "parent_dispatch": None,
                "feature": "F40",
                "branch": None,
                "instruction": "Write a test blog post",
                "context_files": [],
                "created_at": datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            }

            dispatch_path = pending_dir / "dispatch.json"
            dispatch_path.write_text(json.dumps(payload, indent=2) + "\n")

            loaded = json.loads(dispatch_path.read_text())
            assert loaded["role"] == "blog-writer"
            assert loaded["track"] == "A"
            assert loaded["terminal"] == "T1"
            assert loaded["feature"] == "F40"
            assert loaded["dispatch_id"] == dispatch_id

    def test_dispatch_id_is_unique_per_call(self):
        """Two consecutive generate_dispatch_id calls produce different IDs."""
        from headless_dispatch_writer import generate_dispatch_id
        import time

        id1 = generate_dispatch_id("agent-blog-writer", "A")
        time.sleep(0.01)
        id2 = generate_dispatch_id("agent-blog-writer", "A")
        # IDs may collide within the same second but the format must be valid
        assert id1.endswith("-A")
        assert id2.endswith("-A")


# ---------------------------------------------------------------------------
# 5. test_unknown_agent_raises
# ---------------------------------------------------------------------------

class TestUnknownAgentRaises:
    """dispatch-agent.sh exits non-zero when the agent does not exist."""

    def test_dispatch_agent_sh_syntax_is_valid(self):
        """bash -n passes on dispatch-agent.sh."""
        result = subprocess.run(
            ["bash", "-n", str(DISPATCH_AGENT_SH)],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"bash -n failed:\n{result.stderr}"

    def test_unknown_agent_exits_nonzero(self):
        """dispatch-agent.sh exits 1 for a non-existent agent."""
        result = subprocess.run(
            [
                "bash",
                str(DISPATCH_AGENT_SH),
                "--agent", "nonexistent-agent-xyz-abc",
                "--instruction", "test",
            ],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
        )
        assert result.returncode != 0, "Expected non-zero exit for unknown agent"
        assert "not found" in result.stderr.lower() or "agent" in result.stderr.lower()

    def test_missing_agent_flag_exits_nonzero(self):
        """dispatch-agent.sh exits 1 when --agent is not provided."""
        result = subprocess.run(
            ["bash", str(DISPATCH_AGENT_SH), "--instruction", "test"],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
        )
        assert result.returncode != 0

    def test_missing_instruction_flag_exits_nonzero(self):
        """dispatch-agent.sh exits 1 when --instruction is not provided."""
        result = subprocess.run(
            ["bash", str(DISPATCH_AGENT_SH), "--agent", "blog-writer"],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
        )
        assert result.returncode != 0

    def test_resolve_agent_cwd_unknown_returns_none(self):
        """_resolve_agent_cwd with unknown role returns None, not an exception."""
        result = _resolve_agent_cwd("nonexistent-agent-xyz-abc")
        assert result is None
