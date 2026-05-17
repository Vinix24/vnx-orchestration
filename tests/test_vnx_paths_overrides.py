"""Tests for _resolve_overrides_dir() and get_skill_path() added in PR-CENTRAL-5."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "lib"))

from vnx_paths import _resolve_overrides_dir, get_skill_path, resolve_paths


class TestResolveOverridesDir:
    def test_no_overrides_dir_returns_none(self, tmp_path):
        """No .vnx-overrides directory → returns None."""
        assert _resolve_overrides_dir(tmp_path) is None

    def test_overrides_dir_exists_returns_path(self, tmp_path):
        """Present .vnx-overrides directory → returns the Path."""
        overrides = tmp_path / ".vnx-overrides"
        overrides.mkdir()
        result = _resolve_overrides_dir(tmp_path)
        assert result == overrides

    def test_overrides_dir_is_file_returns_none(self, tmp_path):
        """File named .vnx-overrides is not a directory → returns None."""
        (tmp_path / ".vnx-overrides").write_text("not a dir")
        assert _resolve_overrides_dir(tmp_path) is None

    def test_schemas_accessible_under_overrides_dir(self, tmp_path):
        """Schemas reachable via the returned overrides dir."""
        overrides = tmp_path / ".vnx-overrides"
        schemas = overrides / "schemas"
        schemas.mkdir(parents=True)
        schema_file = schemas / "v10.sql"
        schema_file.write_text("-- v10")

        result = _resolve_overrides_dir(tmp_path)
        assert result is not None
        assert (result / "schemas" / "v10.sql").exists()


class TestGetSkillPath:
    def test_no_overrides_fallback_to_central(self, tmp_path, monkeypatch):
        """No .vnx-overrides → central VNX_HOME/skills/<name> is returned."""
        vnx_home = tmp_path / "vnx-home"
        central_skill = vnx_home / "skills" / "my-skill"
        central_skill.mkdir(parents=True)
        monkeypatch.setenv("VNX_HOME", str(vnx_home))

        project_root = tmp_path / "project"
        project_root.mkdir()

        result = get_skill_path("my-skill", project_root)
        assert result == central_skill.resolve()

    def test_override_skill_resolved_from_overrides(self, tmp_path, monkeypatch):
        """.vnx-overrides/skills/custom_skill/ → resolved from overrides."""
        vnx_home = tmp_path / "vnx-home"
        (vnx_home / "skills").mkdir(parents=True)
        monkeypatch.setenv("VNX_HOME", str(vnx_home))

        project_root = tmp_path / "project"
        override_skill = project_root / ".vnx-overrides" / "skills" / "custom-skill"
        override_skill.mkdir(parents=True)

        result = get_skill_path("custom-skill", project_root)
        assert result == override_skill.resolve()

    def test_overrides_wins_over_central(self, tmp_path, monkeypatch):
        """Skill in both locations → overrides version wins."""
        vnx_home = tmp_path / "vnx-home"
        central_skill = vnx_home / "skills" / "shared-skill"
        central_skill.mkdir(parents=True)
        monkeypatch.setenv("VNX_HOME", str(vnx_home))

        project_root = tmp_path / "project"
        override_skill = project_root / ".vnx-overrides" / "skills" / "shared-skill"
        override_skill.mkdir(parents=True)

        result = get_skill_path("shared-skill", project_root)
        assert result == override_skill.resolve()
        assert result != central_skill.resolve()

    def test_unknown_skill_raises_file_not_found(self, tmp_path, monkeypatch):
        """Skill not found anywhere → FileNotFoundError."""
        vnx_home = tmp_path / "vnx-home"
        (vnx_home / "skills").mkdir(parents=True)
        monkeypatch.setenv("VNX_HOME", str(vnx_home))

        project_root = tmp_path / "project"
        project_root.mkdir()

        with pytest.raises(FileNotFoundError, match="nonexistent"):
            get_skill_path("nonexistent", project_root)

    def test_no_project_root_falls_back_to_central(self, tmp_path, monkeypatch):
        """project_root=None → only central VNX_HOME is checked."""
        vnx_home = tmp_path / "vnx-home"
        central_skill = vnx_home / "skills" / "central-only"
        central_skill.mkdir(parents=True)
        monkeypatch.setenv("VNX_HOME", str(vnx_home))

        result = get_skill_path("central-only")
        assert result == central_skill.resolve()


class TestResolvePathsSkillsDir:
    """Tests for resolve_paths() VNX_SKILLS_DIR resolution.

    resolve_paths() derives project_root as vnx_home.parent (when no git root).
    So overrides/claude skills must be placed under tmp_path (vnx_home.parent),
    not a separate project subdir.
    """

    def test_overrides_skills_takes_priority_over_claude_skills(self, tmp_path, monkeypatch):
        """VNX_SKILLS_DIR resolution: .vnx-overrides/skills > .claude/skills."""
        vnx_home = tmp_path / "vnx-home"
        (vnx_home / "skills").mkdir(parents=True)
        monkeypatch.setenv("VNX_HOME", str(vnx_home))
        monkeypatch.delenv("VNX_SKILLS_DIR", raising=False)
        monkeypatch.delenv("VNX_DATA_DIR_EXPLICIT", raising=False)
        monkeypatch.delenv("VNX_DATA_DIR", raising=False)

        # project_root = vnx_home.parent = tmp_path (how resolve_paths() derives it)
        overrides_skills = tmp_path / ".vnx-overrides" / "skills"
        overrides_skills.mkdir(parents=True)
        claude_skills = tmp_path / ".claude" / "skills"
        claude_skills.mkdir(parents=True)

        paths = resolve_paths()
        assert paths["VNX_SKILLS_DIR"] == str(overrides_skills)

    def test_no_overrides_falls_back_to_claude_skills(self, tmp_path, monkeypatch):
        """Without overrides dir, .claude/skills is used when present."""
        vnx_home = tmp_path / "vnx-home"
        (vnx_home / "skills").mkdir(parents=True)
        monkeypatch.setenv("VNX_HOME", str(vnx_home))
        monkeypatch.delenv("VNX_SKILLS_DIR", raising=False)
        monkeypatch.delenv("VNX_DATA_DIR_EXPLICIT", raising=False)
        monkeypatch.delenv("VNX_DATA_DIR", raising=False)

        # project_root = vnx_home.parent = tmp_path
        claude_skills = tmp_path / ".claude" / "skills"
        claude_skills.mkdir(parents=True)

        paths = resolve_paths()
        assert paths["VNX_SKILLS_DIR"] == str(claude_skills)

    def test_env_var_overrides_all(self, tmp_path, monkeypatch):
        """VNX_SKILLS_DIR env var takes priority over everything."""
        vnx_home = tmp_path / "vnx-home"
        (vnx_home / "skills").mkdir(parents=True)
        monkeypatch.setenv("VNX_HOME", str(vnx_home))
        monkeypatch.delenv("VNX_DATA_DIR_EXPLICIT", raising=False)
        monkeypatch.delenv("VNX_DATA_DIR", raising=False)

        explicit_skills = tmp_path / "explicit-skills"
        explicit_skills.mkdir()
        monkeypatch.setenv("VNX_SKILLS_DIR", str(explicit_skills))

        # overrides dir exists but env var wins
        (tmp_path / ".vnx-overrides" / "skills").mkdir(parents=True)

        paths = resolve_paths()
        assert paths["VNX_SKILLS_DIR"] == str(explicit_skills)
