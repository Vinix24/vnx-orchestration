#!/usr/bin/env python3
"""Tests for scripts/lib/agent_resolver.py (ADR-028 Phase 1)."""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT / "scripts" / "lib") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "scripts" / "lib"))

from agent_resolver import agent_folders_enabled, resolve_agent


def _make_agent(base: Path, name: str, rel: str = "agents", config: str | None = None) -> Path:
    agent_dir = base / rel / name
    agent_dir.mkdir(parents=True)
    (agent_dir / "CLAUDE.md").write_text(f"# {name}")
    if config is not None:
        (agent_dir / "config.yaml").write_text(config)
    return agent_dir


class TestResolveAgentPrecedence:
    def test_finds_agent_in_project_agents(self, tmp_path):
        agent_dir = _make_agent(tmp_path, "local-agent")
        cfg = resolve_agent("local-agent", tmp_path)
        assert cfg is not None and cfg.claude_md == agent_dir / "CLAUDE.md"

    def test_finds_agent_in_project_examples(self, tmp_path):
        agent_dir = _make_agent(tmp_path, "demo-agent", rel="examples")
        cfg = resolve_agent("demo-agent", tmp_path)
        assert cfg is not None and cfg.claude_md == agent_dir / "CLAUDE.md"

    def test_falls_back_to_engine_root_examples(self, tmp_path):
        engine_root = tmp_path / "engine"
        agent_dir = _make_agent(engine_root, "packaged-agent", rel="examples")
        project_dir = tmp_path / "empty-project"
        project_dir.mkdir()
        cfg = resolve_agent("packaged-agent", project_dir, engine_root=engine_root)
        assert cfg is not None and cfg.claude_md == agent_dir / "CLAUDE.md"

    def test_project_beats_engine_root(self, tmp_path):
        engine_root = tmp_path / "engine"
        _make_agent(engine_root, "shared", rel="examples", config="provider: engine\n")
        local_dir = _make_agent(tmp_path, "shared", rel="agents", config="provider: local\n")
        cfg = resolve_agent("shared", tmp_path, engine_root=engine_root)
        assert cfg is not None and cfg.claude_md == local_dir / "CLAUDE.md" and cfg.provider == "local"

    def test_unknown_agent_returns_none(self, tmp_path):
        assert resolve_agent("does-not-exist", tmp_path) is None


class TestBackwardCompatibleDefaults:
    def test_minimal_config_resolves_defaults(self, tmp_path):
        _make_agent(tmp_path, "minimal", config="governance_profile: minimal\ndefault_instruction: Hello\n")
        cfg = resolve_agent("minimal", tmp_path)
        assert cfg is not None
        assert cfg.provider == "claude" and cfg.model is None
        assert cfg.governance_profile == "minimal" and cfg.default_instruction == "Hello"
        assert cfg.isolation == {}
        assert cfg.skills == []
        assert cfg.permissions == {
            "allowed_tools": [],
            "denied_tools": [],
            "bash_allow_patterns": [],
            "bash_deny_patterns": [],
        }

    def test_missing_config_yaml_uses_defaults(self, tmp_path):
        _make_agent(tmp_path, "no-config", config=None)
        cfg = resolve_agent("no-config", tmp_path)
        assert cfg is not None and cfg.provider == "claude" and cfg.model is None


class TestExtendedConfigParsing:
    def test_resolves_provider_and_model(self, tmp_path):
        _make_agent(tmp_path, "kimi-agent", config="provider: kimi\nmodel: k2\n")
        cfg = resolve_agent("kimi-agent", tmp_path)
        assert cfg is not None and cfg.provider == "kimi" and cfg.model == "k2"

    def test_resolves_permissions(self, tmp_path):
        _make_agent(
            tmp_path,
            "guarded",
            config=(
                "permissions:\n"
                "  allowed_tools: [Read, Write]\n"
                "  denied_tools: [Bash]\n"
                "  bash_allow_patterns: ['^git ']\n"
                "  bash_deny_patterns: ['rm -rf']\n"
            ),
        )
        cfg = resolve_agent("guarded", tmp_path)
        assert cfg is not None
        assert cfg.permissions["allowed_tools"] == ["Read", "Write"]
        assert cfg.permissions["denied_tools"] == ["Bash"]
        assert cfg.permissions["bash_allow_patterns"] == ["^git "]
        assert cfg.permissions["bash_deny_patterns"] == ["rm -rf"]

    def test_resolves_skills(self, tmp_path):
        _make_agent(tmp_path, "skilled", config="skills:\n  - backend-developer\n  - reviewer\n")
        cfg = resolve_agent("skilled", tmp_path)
        assert cfg is not None and cfg.skills == ["backend-developer", "reviewer"]

    def test_isolation_preserved(self, tmp_path):
        _make_agent(tmp_path, "isolated", config="isolation:\n  scope_type: business_folder\n")
        cfg = resolve_agent("isolated", tmp_path)
        assert cfg is not None and cfg.isolation["scope_type"] == "business_folder"


class TestFeatureFlag:
    def test_defaults_to_enabled(self):
        assert agent_folders_enabled({}) is True
        assert agent_folders_enabled({"VNX_AGENT_FOLDERS": "1"}) is True

    def test_zero_disables(self):
        assert agent_folders_enabled({"VNX_AGENT_FOLDERS": "0"}) is False


class TestRobustness:
    def test_malformed_yaml_falls_back_to_defaults(self, tmp_path):
        agent_dir = _make_agent(tmp_path, "bad-yaml", config="not yaml: [")
        cfg = resolve_agent("bad-yaml", tmp_path)
        assert cfg is not None and cfg.claude_md == agent_dir / "CLAUDE.md" and cfg.provider == "claude"


def test_resolve_agent_rejects_path_traversal(tmp_path):
    """A traversal agent name must never resolve to a path outside the agent dirs."""
    import agent_resolver as ar
    # Plant a CLAUDE.md two levels up to prove traversal can't reach it.
    (tmp_path / "CLAUDE.md").write_text("outside", encoding="utf-8")
    proj = tmp_path / "proj"
    (proj / "agents").mkdir(parents=True)
    for bad in ["../../CLAUDE-dir", "..", "a/b", "../../../etc", "foo/../bar"]:
        assert ar._resolve_agent_claude_md(bad, proj) is None
        assert ar.resolve_agent(bad, proj) is None
    assert ar._is_safe_agent_name("backend-developer") is True
    assert ar._is_safe_agent_name("../x") is False
