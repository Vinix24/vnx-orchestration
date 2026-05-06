"""Integration tests for build_current_state.py.

Verifies:
- SessionEnd hook in .claude/settings.json invokes projector successfully.
- Projector creates/updates current_state.md in the correct location.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts" / "lib"))

import build_current_state as bcs


SETTINGS_PATH = Path(__file__).parent.parent / ".claude" / "settings.json"
PROJECT_ROOT = Path(__file__).parent.parent


class TestSessionEndHookWiring:
    def test_settings_json_has_session_end_key(self) -> None:
        """settings.json must contain a SessionEnd hook entry."""
        assert SETTINGS_PATH.exists(), ".claude/settings.json not found"
        settings = json.loads(SETTINGS_PATH.read_text())
        assert "SessionEnd" in settings.get("hooks", {}), (
            "SessionEnd key missing from hooks in .claude/settings.json"
        )

    def test_session_end_hook_references_projector(self) -> None:
        """The SessionEnd hook command must reference build_current_state.py."""
        settings = json.loads(SETTINGS_PATH.read_text())
        hooks = settings["hooks"]["SessionEnd"]
        commands = []
        for entry in hooks:
            for h in entry.get("hooks", []):
                if h.get("type") == "command":
                    commands.append(h["command"])
        assert any("build_current_state.py" in cmd for cmd in commands), (
            f"No hook references build_current_state.py. Commands: {commands}"
        )

    def test_session_end_hook_exits_zero_on_fail(self) -> None:
        """Hook command must end with exit 0 so it never blocks Claude sessions."""
        settings = json.loads(SETTINGS_PATH.read_text())
        hooks = settings["hooks"]["SessionEnd"]
        for entry in hooks:
            for h in entry.get("hooks", []):
                if h.get("type") == "command" and "build_current_state" in h.get("command", ""):
                    assert "exit 0" in h["command"], (
                        "SessionEnd hook missing 'exit 0' safety guard. "
                        f"Command: {h['command']!r}"
                    )


class TestProjectorInvocationViaSubprocess:
    def test_projector_exits_zero(self, tmp_path: Path) -> None:
        """Running the projector via subprocess must exit 0 (no crashes)."""
        strategy_dir = tmp_path / "strategy"
        state_dir = tmp_path / "state"
        strategy_dir.mkdir(parents=True)
        state_dir.mkdir(parents=True)

        projector = PROJECT_ROOT / "scripts" / "build_current_state.py"
        env = {
            "VNX_DATA_DIR": str(tmp_path),
            "VNX_DATA_DIR_EXPLICIT": "1",
            "PATH": "/usr/bin:/bin:/usr/local/bin",
            "HOME": str(Path.home()),
        }
        result = subprocess.run(
            [sys.executable, str(projector)],
            capture_output=True,
            text=True,
            env=env,
            timeout=15,
        )
        assert result.returncode == 0, (
            f"Projector exited with {result.returncode}.\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )

    def test_projector_creates_output_file(self, tmp_path: Path) -> None:
        """Projector must write current_state.md to strategy/."""
        strategy_dir = tmp_path / "strategy"
        state_dir = tmp_path / "state"
        strategy_dir.mkdir(parents=True)
        state_dir.mkdir(parents=True)

        projector = PROJECT_ROOT / "scripts" / "build_current_state.py"
        env = {
            "VNX_DATA_DIR": str(tmp_path),
            "VNX_DATA_DIR_EXPLICIT": "1",
            "PATH": "/usr/bin:/bin:/usr/local/bin",
            "HOME": str(Path.home()),
        }
        subprocess.run(
            [sys.executable, str(projector)],
            capture_output=True, text=True, env=env, timeout=15,
        )
        out = strategy_dir / "current_state.md"
        assert out.exists(), f"current_state.md not created at {out}"
        content = out.read_text()
        assert "# Mission" in content

    def test_projector_idempotent_via_subprocess(self, tmp_path: Path) -> None:
        """Two subprocess calls produce byte-identical current_state.md."""
        strategy_dir = tmp_path / "strategy"
        state_dir = tmp_path / "state"
        strategy_dir.mkdir(parents=True)
        state_dir.mkdir(parents=True)

        projector = PROJECT_ROOT / "scripts" / "build_current_state.py"
        env = {
            "VNX_DATA_DIR": str(tmp_path),
            "VNX_DATA_DIR_EXPLICIT": "1",
            "PATH": "/usr/bin:/bin:/usr/local/bin",
            "HOME": str(Path.home()),
        }
        out = strategy_dir / "current_state.md"
        subprocess.run([sys.executable, str(projector)],
                       capture_output=True, text=True, env=env, timeout=15)
        content_run1 = out.read_text()

        subprocess.run([sys.executable, str(projector)],
                       capture_output=True, text=True, env=env, timeout=15)
        content_run2 = out.read_text()

        assert content_run1 == content_run2, (
            "current_state.md content changed between two consecutive runs "
            "(idempotency violated)"
        )

    def test_projector_under_200_lines_via_subprocess(self, tmp_path: Path) -> None:
        """Output must be ≤200 lines when run as subprocess."""
        strategy_dir = tmp_path / "strategy"
        state_dir = tmp_path / "state"
        strategy_dir.mkdir(parents=True)
        state_dir.mkdir(parents=True)

        projector = PROJECT_ROOT / "scripts" / "build_current_state.py"
        env = {
            "VNX_DATA_DIR": str(tmp_path),
            "VNX_DATA_DIR_EXPLICIT": "1",
            "PATH": "/usr/bin:/bin:/usr/local/bin",
            "HOME": str(Path.home()),
        }
        subprocess.run([sys.executable, str(projector)],
                       capture_output=True, text=True, env=env, timeout=15)
        line_count = len((strategy_dir / "current_state.md").read_text().splitlines())
        assert line_count <= 200, f"Output has {line_count} lines (max 200)"
