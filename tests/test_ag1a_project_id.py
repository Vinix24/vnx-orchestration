"""AG-1a: real store and CLI boundaries refuse an unresolved tenant."""
import argparse
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
for directory in (ROOT, ROOT / "scripts", ROOT / "scripts/lib"):
    sys.path.insert(0, str(directory))
import project_scope
import quality_db_init
from coordination_db import init_schema
from migrations.apply_0015 import apply_migration
from project_id_migration import resolve_init_project_id
from vnx_cli.commands.init_cmd import vnx_init


def test_ag1a_apply_0015_refusal_and_retry(tmp_path, caplog):
    init_schema(tmp_path, ROOT / "schemas/runtime_coordination.sql")
    db = tmp_path / "runtime_coordination.db"
    migration = ROOT / "schemas/migrations/0015_complete_project_id.sql"
    with pytest.raises(RuntimeError, match="vnx init"):
        apply_migration(db, migration)
    assert "ERROR" in caplog.text
    (tmp_path / ".vnx-project-id").write_text("other\n")
    assert apply_migration(db, migration) is True
    with sqlite3.connect(db) as conn:
        assert next(r[4] for r in conn.execute("PRAGMA table_info(dispatches)")
                    if r[1] == "project_id") == "'other'"


@pytest.mark.parametrize("env", [None, "", "INVALID", "../other"])
def test_ag1a_qi_and_scoped_query_refuse_bad_sources(tmp_path, monkeypatch, capsys, env):
    if env is not None:
        monkeypatch.setenv("VNX_PROJECT_ID", env)
    db = tmp_path / "quality_intelligence.db"
    assert quality_db_init.bootstrap_qi_db(db) is False
    assert "ERROR" in capsys.readouterr().out and not db.exists()
    with pytest.raises(project_scope.TenantUnresolved):
        project_scope.scoped_query("SELECT * FROM x WHERE 1=1")


@pytest.mark.parametrize("qi_source", ["shared", "absent", "conflict"])
def test_ag1a_runtime_cli_both_resolvers(tmp_path, qi_source):
    state = tmp_path / "state"
    state.mkdir()
    qi = state / "quality_intelligence.db"
    with sqlite3.connect(qi) as conn:
        conn.executescript((ROOT / "schemas/quality_intelligence.sql").read_text())
    env = {k: v for k, v in os.environ.items() if not k.startswith("VNX_")}
    env.update(HOME=str(tmp_path), VNX_HOME=str(ROOT), VNX_DATA_DIR=str(tmp_path),
               VNX_DATA_DIR_EXPLICIT="1", VNX_STATE_DIR=str(state))
    command = [sys.executable, str(ROOT / "scripts/runtime_coordination_init.py"),
               "--state-dir", str(state)]
    refused = subprocess.run(command, env=env, capture_output=True, text=True)
    assert refused.returncode != 0 and "vnx init" in refused.stdout
    owner = tmp_path / ".vnx-data/other/state"
    owner.mkdir(parents=True)
    (state / "runtime_coordination.db").replace(owner / "runtime_coordination.db")
    (state / "runtime_coordination.db").symlink_to(owner / "runtime_coordination.db")
    if qi_source == "shared":
        (state / ".vnx-project-id").write_text("other\n")
    elif qi_source == "conflict":
        foreign = tmp_path / ".vnx-data/foreign/state"
        foreign.mkdir(parents=True)
        qi.replace(foreign / qi.name)
        qi.symlink_to(foreign / qi.name)
        env["VNX_PROJECT_ID"] = "other"
    result = subprocess.run(command, env=env, capture_output=True, text=True)
    assert result.returncode == (0 if qi_source == "shared" else 1), result.stdout
    if qi_source == "shared":
        with sqlite3.connect(qi) as conn:
            assert next(r[4] for r in conn.execute("PRAGMA table_info(success_patterns)")
                        if r[1] == "project_id") == "'other'"
    else:
        assert "ERROR" in result.stdout
        assert ("vnx init" if qi_source == "absent" else "conflict") in result.stdout


def test_ag1a_vnx_init_temporary_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    project = tmp_path / "ag1a-project"
    project.mkdir()
    data = tmp_path / ".vnx-data/ag1a-project"
    monkeypatch.setenv("VNX_DATA_DIR", str(data))
    args = argparse.Namespace(project_dir=str(project), project_id="ag1a-project")
    assert vnx_init(args) == 0
    assert resolve_init_project_id(data / "state/runtime_coordination.db") == "ag1a-project"
