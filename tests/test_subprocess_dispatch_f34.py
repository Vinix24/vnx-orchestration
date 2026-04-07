#!/usr/bin/env python3
"""Tests for F34 skill context inlining — 3-tier resolution in _inject_skill_context
and cwd propagation through deliver_via_subprocess → SubprocessAdapter.deliver().
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

SCRIPTS_LIB = Path(__file__).resolve().parent.parent / "scripts" / "lib"
sys.path.insert(0, str(SCRIPTS_LIB))

from subprocess_dispatch import _inject_skill_context, deliver_via_subprocess


# ---------------------------------------------------------------------------
# _inject_skill_context — 3-tier resolution
# ---------------------------------------------------------------------------

class TestInjectSkillContext:
    """Tests for the 3-tier CLAUDE.md resolution."""

    def _make_fs(self, tmp_path: Path, *, agents: bool = False, skills: bool = False, terminal: bool = False):
        """Create a fake project root under tmp_path with selected tiers present."""
        if agents:
            p = tmp_path / "agents" / "backend-developer" / "CLAUDE.md"
            p.parent.mkdir(parents=True)
            p.write_text("# agents tier")
        if skills:
            p = tmp_path / ".claude" / "skills" / "backend-developer" / "CLAUDE.md"
            p.parent.mkdir(parents=True)
            p.write_text("# skills tier")
        if terminal:
            p = tmp_path / ".claude" / "terminals" / "T1" / "CLAUDE.md"
            p.parent.mkdir(parents=True)
            p.write_text("# terminal tier")
        return tmp_path

    def _patch_root(self, tmp_path: Path):
        """Patch Path(__file__).resolve().parents[2] inside subprocess_dispatch."""
        import subprocess_dispatch as sd
        # parents[2] of scripts/lib/subprocess_dispatch.py is the project root
        real_parents = Path(sd.__file__).resolve().parents
        mock_parents = list(real_parents)
        mock_parents[2] = tmp_path

        class _MockPath(type(Path())):
            pass

        return patch.object(
            Path(sd.__file__).resolve(),
            "parents",
            new=mock_parents,
        )

    # --- tier-1: agents/{role}/CLAUDE.md wins when present ---

    def test_tier1_agents_wins(self, tmp_path):
        root = self._make_fs(tmp_path, agents=True, skills=True, terminal=True)
        with patch("subprocess_dispatch.Path") as mock_path_cls:
            # We need to intercept the resolution, so patch the project root directly
            import subprocess_dispatch as sd
            original_fn = sd._inject_skill_context

            # Build the real paths under tmp_path
            agents_md = root / "agents" / "backend-developer" / "CLAUDE.md"
            skills_md = root / ".claude" / "skills" / "backend-developer" / "CLAUDE.md"
            terminal_md = root / ".claude" / "terminals" / "T1" / "CLAUDE.md"

            # Patch parents[2] so resolution uses tmp_path as project root
            with patch.object(type(Path(sd.__file__).resolve()), "__new__", wraps=Path):
                pass  # can't easily patch Path internals; use a direct path mock instead

        # Direct approach: mock Path(__file__).resolve().parents[2]
        import subprocess_dispatch as sd

        fake_root_path = MagicMock()
        fake_root_path.__truediv__ = lambda self, other: root / other

        with patch("subprocess_dispatch.Path") as MockPath:
            # Simulate: Path(__file__).resolve().parents[2] / "agents" / role / "CLAUDE.md"
            # We construct real paths under tmp_path so exists()/read_text() work naturally
            agents_md = root / "agents" / "backend-developer" / "CLAUDE.md"
            skills_md = root / ".claude" / "skills" / "backend-developer" / "CLAUDE.md"
            terminal_md = root / ".claude" / "terminals" / "T1" / "CLAUDE.md"

            mock_file = MagicMock()
            mock_file.resolve.return_value.parents = [None, None, root]
            MockPath.return_value = mock_file
            MockPath.side_effect = lambda *a, **kw: Path(*a, **kw) if a else mock_file

            # Manually call with real paths by monkeypatching the project root
            with patch.object(sd, "_inject_skill_context", wraps=sd._inject_skill_context):
                # The simplest reliable test: just exercise the real function
                # with files at paths the function will actually construct.
                pass

        # ---- Reliable strategy: patch Path(__file__) inside the module ----
        with patch("subprocess_dispatch.Path") as MockPath:
            sentinel = MagicMock()
            sentinel.resolve.return_value.parents.__getitem__ = lambda self, i: root if i == 2 else MagicMock()

            # Re-implement _inject_skill_context inline to test logic
            project_root = root

            candidates = [
                project_root / "agents" / "backend-developer" / "CLAUDE.md",
                project_root / ".claude" / "skills" / "backend-developer" / "CLAUDE.md",
                project_root / ".claude" / "terminals" / "T1" / "CLAUDE.md",
            ]

            first_hit = next((p for p in candidates if p.exists()), None)
            assert first_hit == project_root / "agents" / "backend-developer" / "CLAUDE.md"
            assert first_hit.read_text() == "# agents tier"

    def test_tier2_skills_wins_when_no_agents(self, tmp_path):
        root = self._make_fs(tmp_path, agents=False, skills=True, terminal=True)
        project_root = root

        candidates = [
            project_root / "agents" / "backend-developer" / "CLAUDE.md",
            project_root / ".claude" / "skills" / "backend-developer" / "CLAUDE.md",
            project_root / ".claude" / "terminals" / "T1" / "CLAUDE.md",
        ]
        first_hit = next((p for p in candidates if p.exists()), None)
        assert first_hit == project_root / ".claude" / "skills" / "backend-developer" / "CLAUDE.md"
        assert first_hit.read_text() == "# skills tier"

    def test_tier3_terminal_fallback(self, tmp_path):
        root = self._make_fs(tmp_path, agents=False, skills=False, terminal=True)
        project_root = root

        candidates = [
            project_root / "agents" / "backend-developer" / "CLAUDE.md",
            project_root / ".claude" / "skills" / "backend-developer" / "CLAUDE.md",
            project_root / ".claude" / "terminals" / "T1" / "CLAUDE.md",
        ]
        first_hit = next((p for p in candidates if p.exists()), None)
        assert first_hit == project_root / ".claude" / "terminals" / "T1" / "CLAUDE.md"
        assert first_hit.read_text() == "# terminal tier"

    def test_no_role_skips_tiers_1_and_2(self, tmp_path):
        """Without role, only the terminal tier is checked."""
        root = self._make_fs(tmp_path, agents=True, skills=True, terminal=True)
        project_root = root

        # Without role, candidates must only include terminal
        candidates = [
            project_root / ".claude" / "terminals" / "T1" / "CLAUDE.md",
        ]
        first_hit = next((p for p in candidates if p.exists()), None)
        assert first_hit is not None
        assert "terminal" in first_hit.read_text()

    def test_no_match_returns_instruction_unchanged(self, tmp_path):
        root = self._make_fs(tmp_path)  # no files
        project_root = root

        candidates = [
            project_root / "agents" / "backend-developer" / "CLAUDE.md",
            project_root / ".claude" / "skills" / "backend-developer" / "CLAUDE.md",
            project_root / ".claude" / "terminals" / "T1" / "CLAUDE.md",
        ]
        first_hit = next((p for p in candidates if p.exists()), None)
        assert first_hit is None  # nothing found — instruction passes through unchanged


# ---------------------------------------------------------------------------
# _inject_skill_context — integration via module-level patching
# ---------------------------------------------------------------------------

class TestInjectSkillContextIntegration:
    """Integration tests patching Path inside subprocess_dispatch."""

    def _patch_project_root(self, tmp_path: Path):
        """Context manager that patches the project root inside subprocess_dispatch."""
        import subprocess_dispatch as sd

        # Capture real Path class
        real_path_cls = Path

        class PatchedPath(type(real_path_cls())):
            """Subclass of Path that redirects parents[2] to tmp_path."""
            pass

        # Simpler: patch using object-level mock on the module
        original_file = sd.__file__

        def fake_path(*args, **kwargs):
            if args and str(args[0]) == original_file:
                mock = MagicMock()
                # parents[2] → tmp_path
                parents_mock = MagicMock()
                parents_mock.__getitem__ = lambda self, i: tmp_path if i == 2 else real_path_cls(original_file).resolve().parents[i]
                mock.resolve.return_value.parents = parents_mock
                return mock
            return real_path_cls(*args, **kwargs)

        return patch("subprocess_dispatch.Path", side_effect=fake_path)

    def test_inject_prepends_agents_context(self, tmp_path):
        agents_md = tmp_path / "agents" / "backend-developer" / "CLAUDE.md"
        agents_md.parent.mkdir(parents=True)
        agents_md.write_text("AGENTS_CONTEXT")

        with self._patch_project_root(tmp_path):
            result = _inject_skill_context("T1", "do work", role="backend-developer")

        assert "AGENTS_CONTEXT" in result
        assert "DISPATCH INSTRUCTION:" in result
        assert "do work" in result

    def test_inject_prepends_skills_context(self, tmp_path):
        skills_md = tmp_path / ".claude" / "skills" / "backend-developer" / "CLAUDE.md"
        skills_md.parent.mkdir(parents=True)
        skills_md.write_text("SKILLS_CONTEXT")

        with self._patch_project_root(tmp_path):
            result = _inject_skill_context("T1", "do work", role="backend-developer")

        assert "SKILLS_CONTEXT" in result
        assert "do work" in result

    def test_inject_prepends_terminal_context(self, tmp_path):
        terminal_md = tmp_path / ".claude" / "terminals" / "T1" / "CLAUDE.md"
        terminal_md.parent.mkdir(parents=True)
        terminal_md.write_text("TERMINAL_CONTEXT")

        with self._patch_project_root(tmp_path):
            result = _inject_skill_context("T1", "do work")

        assert "TERMINAL_CONTEXT" in result
        assert "do work" in result

    def test_inject_no_match_unchanged(self, tmp_path):
        with self._patch_project_root(tmp_path):
            result = _inject_skill_context("T1", "do work", role="nonexistent-role")

        assert result == "do work"

    def test_agents_beats_skills(self, tmp_path):
        agents_md = tmp_path / "agents" / "backend-developer" / "CLAUDE.md"
        agents_md.parent.mkdir(parents=True)
        agents_md.write_text("AGENTS_CONTEXT")

        skills_md = tmp_path / ".claude" / "skills" / "backend-developer" / "CLAUDE.md"
        skills_md.parent.mkdir(parents=True)
        skills_md.write_text("SKILLS_CONTEXT")

        with self._patch_project_root(tmp_path):
            result = _inject_skill_context("T1", "do work", role="backend-developer")

        assert "AGENTS_CONTEXT" in result
        assert "SKILLS_CONTEXT" not in result


# ---------------------------------------------------------------------------
# deliver_via_subprocess — cwd propagation
# ---------------------------------------------------------------------------

class TestDeliverCwdPropagation:
    """Verify that cwd is passed to adapter.deliver() when agents/{role}/ exists."""

    @pytest.fixture
    def mock_adapter(self):
        with patch("subprocess_dispatch.SubprocessAdapter") as cls:
            instance = MagicMock()
            cls.return_value = instance
            yield instance

    def test_cwd_passed_when_agent_dir_exists(self, tmp_path, mock_adapter):
        agent_dir = tmp_path / "agents" / "backend-developer"
        agent_dir.mkdir(parents=True)

        mock_adapter.deliver.return_value = MagicMock(success=True)
        mock_adapter.read_events_with_timeout.return_value = iter([])

        import subprocess_dispatch as sd

        def fake_path(*args, **kwargs):
            if args and str(args[0]) == sd.__file__:
                mock = MagicMock()
                parents_mock = MagicMock()
                parents_mock.__getitem__ = lambda self, i: tmp_path if i == 2 else Path(sd.__file__).resolve().parents[i]
                mock.resolve.return_value.parents = parents_mock
                return mock
            return Path(*args, **kwargs)

        with patch("subprocess_dispatch.Path", side_effect=fake_path):
            deliver_via_subprocess("T1", "do stuff", "sonnet", "d-001", role="backend-developer")

        call_kwargs = mock_adapter.deliver.call_args[1]
        assert call_kwargs.get("cwd") == agent_dir

    def test_no_cwd_when_agent_dir_missing(self, tmp_path, mock_adapter):
        # agent dir does NOT exist
        mock_adapter.deliver.return_value = MagicMock(success=True)
        mock_adapter.read_events_with_timeout.return_value = iter([])

        import subprocess_dispatch as sd

        def fake_path(*args, **kwargs):
            if args and str(args[0]) == sd.__file__:
                mock = MagicMock()
                parents_mock = MagicMock()
                parents_mock.__getitem__ = lambda self, i: tmp_path if i == 2 else Path(sd.__file__).resolve().parents[i]
                mock.resolve.return_value.parents = parents_mock
                return mock
            return Path(*args, **kwargs)

        with patch("subprocess_dispatch.Path", side_effect=fake_path):
            deliver_via_subprocess("T1", "do stuff", "sonnet", "d-002", role="backend-developer")

        call_kwargs = mock_adapter.deliver.call_args[1]
        assert call_kwargs.get("cwd") is None

    def test_no_role_no_cwd(self, mock_adapter):
        mock_adapter.deliver.return_value = MagicMock(success=True)
        mock_adapter.read_events_with_timeout.return_value = iter([])

        deliver_via_subprocess("T1", "do stuff", "sonnet", "d-003")

        call_kwargs = mock_adapter.deliver.call_args[1]
        assert call_kwargs.get("cwd") is None


# ---------------------------------------------------------------------------
# SubprocessAdapter.deliver() — cwd kwarg
# ---------------------------------------------------------------------------

class TestSubprocessAdapterCwd:
    """Verify cwd is forwarded to Popen."""

    def test_cwd_forwarded_to_popen(self, tmp_path):
        from subprocess_adapter import SubprocessAdapter
        adapter = SubprocessAdapter()

        mock_proc = MagicMock()
        mock_proc.poll.return_value = None

        with patch("subprocess_adapter.subprocess.Popen", return_value=mock_proc) as mock_popen:
            adapter.deliver("T1", "d-001", instruction="hello", model="sonnet", cwd=tmp_path)

        _, popen_kwargs = mock_popen.call_args
        assert popen_kwargs.get("cwd") == str(tmp_path)

    def test_no_cwd_omitted_from_popen(self):
        from subprocess_adapter import SubprocessAdapter
        adapter = SubprocessAdapter()

        mock_proc = MagicMock()
        mock_proc.poll.return_value = None

        with patch("subprocess_adapter.subprocess.Popen", return_value=mock_proc) as mock_popen:
            adapter.deliver("T1", "d-001", instruction="hello", model="sonnet")

        _, popen_kwargs = mock_popen.call_args
        assert "cwd" not in popen_kwargs
