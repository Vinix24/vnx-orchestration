#!/usr/bin/env python3
"""d3filter D2 (dlv-a82252463c87): `vnx learning review` shows terminal and agent scope."""
from __future__ import annotations

import json
import sys
from argparse import Namespace
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "scripts"))
sys.path.insert(0, str(REPO_ROOT / "scripts" / "lib"))

from vnx_cli.commands import learning  # noqa: E402


@pytest.fixture
def state_dir(tmp_path, monkeypatch):
    """Pin the data root to tmp_path: resolve_data_root must never read the real store."""
    monkeypatch.setenv("VNX_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")
    (tmp_path / "state").mkdir()
    return tmp_path / "state"


def _write(state_dir: Path, rules: list) -> None:
    (state_dir / "pending_rules.json").write_text(
        json.dumps({"pending_rules": rules}), encoding="utf-8"
    )


def _review(tmp_path) -> int:
    return learning._cmd_review(Namespace(project_dir=str(tmp_path), mode="rules"))


def test_review_prints_terminal_and_agent(state_dir, tmp_path, capsys):
    _write(state_dir, [{
        "id": "r1", "status": "pending", "pattern": "p", "prevention": "x",
        "terminal_constraint": "T1", "agent_constraint": "sonnet",
        "confidence": 0.7, "occurrence_count": 3,
    }])
    assert _review(tmp_path) == 0
    out = capsys.readouterr().out
    assert "Terminal: T1" in out
    assert "Agent: sonnet" in out


def test_review_agent_none_prints_alle(state_dir, tmp_path, capsys):
    _write(state_dir, [{
        "id": "r1", "status": "pending", "pattern": "p",
        "terminal_constraint": "any", "agent_constraint": None,
    }])
    assert _review(tmp_path) == 0
    out = capsys.readouterr().out
    assert "Terminal: any" in out
    assert "Agent: alle" in out


def test_review_record_without_scope_keys_does_not_fail(state_dir, tmp_path, capsys):
    _write(state_dir, [{"id": "r1", "status": "pending", "pattern": "p"}])
    assert _review(tmp_path) == 0
    out = capsys.readouterr().out
    assert "[r1]" in out
    assert "Agent: alle" in out
