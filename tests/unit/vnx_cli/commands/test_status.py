#!/usr/bin/env python3
"""Unit tests for vnx_cli/commands/status.py agent enumeration.

`vnx status --json` must report the FULL agent resolution chain (project
agents/, project examples/, engine agents/, engine examples/) — the same
chain dispatch_agent walks — not just the project-local agents/ folder.
"""

import json
from argparse import Namespace
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from vnx_cli.commands.status import vnx_status


def _make_agent(base: Path, name: str, rel: str = "agents") -> Path:
    agent_dir = base / rel / name
    agent_dir.mkdir(parents=True)
    (agent_dir / "CLAUDE.md").write_text(f"# {name}")
    return agent_dir


def _init_project(project_dir: Path) -> None:
    (project_dir / ".vnx").mkdir(parents=True, exist_ok=True)


def _run_status_json(project_dir: Path, engine_root: Path, data_root: Path) -> dict:
    args = Namespace(json=True, tracks=False, project_id=None, project_dir=str(project_dir))
    with patch("vnx_cli.commands.status._engine.resolve_data_root", return_value=data_root), \
         patch("vnx_cli.commands.status._engine.engine_root", return_value=engine_root):
        buf = StringIO()
        with patch("sys.stdout", buf):
            rc = vnx_status(args)
    assert rc == 0
    return json.loads(buf.getvalue())


class TestAgentCountFullChain:
    def test_project_local_only(self, tmp_path):
        project_dir = tmp_path / "project"
        _init_project(project_dir)
        _make_agent(project_dir, "local-agent")
        engine_root = tmp_path / "engine"
        engine_root.mkdir()
        data_root = tmp_path / "data"
        data_root.mkdir()

        out = _run_status_json(project_dir, engine_root, data_root)
        assert out["agent_count"] == 1
        assert out["agents"] == ["local-agent"]

    def test_engine_fleet_only_project_no_false_zero(self, tmp_path):
        """Project-local agents/ absent, engine populated — agent_count must not be 0.

        This is the exact defect from the dispatch: `vnx status --json` read
        only project_dir/agents and reported agent_count: 0 for an
        engine-fleet-only project even though dispatch_agent resolves fine
        via the engine fallback.
        """
        project_dir = tmp_path / "project"
        _init_project(project_dir)
        engine_root = tmp_path / "engine"
        _make_agent(engine_root, "backend-developer")
        data_root = tmp_path / "data"
        data_root.mkdir()

        out = _run_status_json(project_dir, engine_root, data_root)
        assert out["agent_count"] == 1
        assert out["agents"] == ["backend-developer"]

    def test_union_project_and_engine(self, tmp_path):
        project_dir = tmp_path / "project"
        _init_project(project_dir)
        _make_agent(project_dir, "local-agent")
        engine_root = tmp_path / "engine"
        _make_agent(engine_root, "backend-developer")
        data_root = tmp_path / "data"
        data_root.mkdir()

        out = _run_status_json(project_dir, engine_root, data_root)
        assert out["agent_count"] == 2
        assert out["agents"] == ["backend-developer", "local-agent"]

    def test_empty_everywhere(self, tmp_path):
        project_dir = tmp_path / "project"
        _init_project(project_dir)
        engine_root = tmp_path / "engine"
        engine_root.mkdir()
        data_root = tmp_path / "data"
        data_root.mkdir()

        out = _run_status_json(project_dir, engine_root, data_root)
        assert out["agent_count"] == 0
        assert out["agents"] == []
