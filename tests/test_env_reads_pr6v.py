"""config-een-oppervlak PR-6v-a: the claude slot reads through config_runtime, three names deleted.

A stored project_config row beats an exported env value for VNX_TMUX_MAX_CONCURRENT;
VNX_OVERRIDE_<NAME> still beats the row. The three deleted lock names have zero
string-literal reads left in scripts/, vnx_cli/ and dashboard/.
"""
from __future__ import annotations

import ast
import functools
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts" / "lib"))

import config_registry  # noqa: E402
import config_runtime  # noqa: E402
import config_store_db  # noqa: E402
import dispatch_serialization  # noqa: E402

DELETED = (
    "VNX_CLAUDE_LOCK_TIMEOUT_SECONDS",
    "VNX_CLAUDE_LOCK_WAIT_WARN_SECONDS",
    "VNX_LOCK_DIR",
)


@pytest.fixture
def store(tmp_path, monkeypatch):
    """A real store wired for project pr6v; returns a function that writes one row."""
    sd = tmp_path / "state"
    sd.mkdir()
    for key in ("VNX_TMUX_MAX_CONCURRENT",):
        monkeypatch.delenv(key, raising=False)
        monkeypatch.delenv("VNX_OVERRIDE_" + key[4:], raising=False)
    monkeypatch.setenv("VNX_STATE_DIR", str(sd))
    monkeypatch.setenv("VNX_PROJECT_ID", "pr6v")
    config_runtime._wired_for.clear()

    def put(key, value):
        conn = sqlite3.connect(sd / "runtime_coordination.db")
        config_store_db.set_config(conn, "pr6v", key, value, actor="op", approval_id="pr6v")
        conn.close()

    yield put
    config_registry.set_db_resolver(None)
    config_registry.set_default_project_id(None)
    config_runtime._wired_for.clear()


def test_claude_slot_stored_row_beats_env(store, monkeypatch):
    store("VNX_TMUX_MAX_CONCURRENT", "7")
    monkeypatch.setenv("VNX_TMUX_MAX_CONCURRENT", "4")
    assert dispatch_serialization._max_concurrent() == 7


def test_claude_slot_override_beats_stored_row(store, monkeypatch):
    store("VNX_TMUX_MAX_CONCURRENT", "7")
    monkeypatch.setenv("VNX_OVERRIDE_TMUX_MAX_CONCURRENT", "2")
    assert dispatch_serialization._max_concurrent() == 2


@functools.lru_cache(maxsize=1)
def _literal_reads():
    """{name: [file:line]} for every string literal (or bash $NAME) of a DELETED name."""
    hits = {name: [] for name in DELETED}
    for top in ("scripts", "vnx_cli", "dashboard"):
        for path in sorted((ROOT / top).rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for n in ast.walk(tree):
                if isinstance(n, ast.Constant) and n.value in hits:
                    hits[n.value].append(f"{path.relative_to(ROOT)}:{n.lineno}")
        for path in sorted((ROOT / top).rglob("*.sh")):
            text = path.read_text(encoding="utf-8")
            for name in DELETED:
                if "$" + name in text or "${" + name in text:
                    hits[name].append(str(path.relative_to(ROOT)))
    return hits


@pytest.mark.parametrize("name", DELETED)
def test_deleted_name_has_zero_reads(name):
    assert _literal_reads()[name] == []
