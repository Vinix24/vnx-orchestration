#!/usr/bin/env python3
"""Tests for scripts/lib/agent_resolver.py (ADR-028 Phase 1)."""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT / "scripts" / "lib") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "scripts" / "lib"))

from agent_resolver import agent_folders_enabled, list_available_agents, resolve_agent


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

    def test_falls_back_to_engine_root_agents_fleet_wide(self, tmp_path):
        """A fleet-wide dev-worker in engine/agents/ resolves from any project."""
        engine_root = tmp_path / "engine"
        agent_dir = _make_agent(engine_root, "backend-developer", rel="agents")
        project_dir = tmp_path / "empty-project"
        project_dir.mkdir()  # no local agents/ or examples/
        cfg = resolve_agent("backend-developer", project_dir, engine_root=engine_root)
        assert cfg is not None and cfg.claude_md == agent_dir / "CLAUDE.md"

    def test_engine_agents_beats_engine_examples(self, tmp_path):
        """The engine's agents/ (real fleet lib) wins over its examples/ (demo)."""
        engine_root = tmp_path / "engine"
        _make_agent(engine_root, "backend-developer", rel="examples", config="provider: demo\n")
        fleet_dir = _make_agent(engine_root, "backend-developer", rel="agents", config="provider: fleet\n")
        project_dir = tmp_path / "empty-project"
        project_dir.mkdir()
        cfg = resolve_agent("backend-developer", project_dir, engine_root=engine_root)
        assert cfg is not None and cfg.claude_md == fleet_dir / "CLAUDE.md" and cfg.provider == "fleet"

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


class TestListAvailableAgents:
    def test_empty_everywhere_returns_empty_list(self, tmp_path):
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        assert list_available_agents(project_dir) == []
        engine_root = tmp_path / "engine"
        engine_root.mkdir()
        assert list_available_agents(project_dir, engine_root=engine_root) == []

    def test_project_only(self, tmp_path):
        project_dir = tmp_path / "project"
        _make_agent(project_dir, "local-agent")
        agents = list_available_agents(project_dir)
        assert [a.name for a in agents] == ["local-agent"]
        assert agents[0].source == "project"

    def test_engine_only_agents_fleet_wide(self, tmp_path):
        """An engine-fleet-only project (no local agents/) still lists engine agents."""
        engine_root = tmp_path / "engine"
        _make_agent(engine_root, "backend-developer", rel="agents")
        project_dir = tmp_path / "empty-project"
        project_dir.mkdir()  # no local agents/ or examples/
        agents = list_available_agents(project_dir, engine_root=engine_root)
        assert [a.name for a in agents] == ["backend-developer"]
        assert agents[0].source == "engine"

    def test_examples_included_project_and_engine(self, tmp_path):
        project_dir = tmp_path / "project"
        _make_agent(project_dir, "demo-agent", rel="examples")
        engine_root = tmp_path / "engine"
        _make_agent(engine_root, "engine-demo", rel="examples")
        agents = list_available_agents(project_dir, engine_root=engine_root)
        names = {a.name: a.source for a in agents}
        assert names == {"demo-agent": "examples", "engine-demo": "examples"}

    def test_union_with_name_clash_project_wins(self, tmp_path):
        project_dir = tmp_path / "project"
        engine_root = tmp_path / "engine"
        _make_agent(project_dir, "shared", rel="agents")
        _make_agent(engine_root, "shared", rel="agents")
        agents = list_available_agents(project_dir, engine_root=engine_root)
        assert len(agents) == 1
        assert agents[0].name == "shared"
        assert agents[0].source == "project"
        assert agents[0].claude_md == project_dir / "agents" / "shared" / "CLAUDE.md"

    def test_union_across_all_four_tiers_dedup_and_count(self, tmp_path):
        project_dir = tmp_path / "project"
        engine_root = tmp_path / "engine"
        _make_agent(project_dir, "proj-agent", rel="agents")
        _make_agent(project_dir, "proj-example", rel="examples")
        _make_agent(engine_root, "engine-agent", rel="agents")
        _make_agent(engine_root, "engine-example", rel="examples")
        # a clash between project examples and engine agents — project tier wins
        _make_agent(project_dir, "clashed", rel="examples")
        _make_agent(engine_root, "clashed", rel="agents")

        agents = list_available_agents(project_dir, engine_root=engine_root)
        by_name = {a.name: a.source for a in agents}
        assert by_name == {
            "proj-agent": "project",
            "proj-example": "examples",
            "engine-agent": "engine",
            "engine-example": "examples",
            "clashed": "examples",
        }

    def test_ignores_dirs_without_claude_md(self, tmp_path):
        project_dir = tmp_path / "project"
        agents_dir = project_dir / "agents"
        agents_dir.mkdir(parents=True)
        (agents_dir / "not-an-agent").mkdir()
        assert list_available_agents(project_dir) == []

    def test_rejects_unsafe_names(self, tmp_path):
        project_dir = tmp_path / "project"
        _make_agent(project_dir, "valid-agent")
        unsafe_dir = project_dir / "agents" / ".hidden-agent"
        unsafe_dir.mkdir(parents=True)
        (unsafe_dir / "CLAUDE.md").write_text("# hidden")
        agents = list_available_agents(project_dir)
        assert [a.name for a in agents] == ["valid-agent"]


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
