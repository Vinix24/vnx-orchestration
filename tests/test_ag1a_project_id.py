"""AG-1a: real store and CLI boundaries refuse an unresolved tenant."""
import argparse
import os
import shutil
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


@pytest.mark.parametrize("name,marker,expected", [
    ("Fresh Source Project", None, "fresh-source-project"),
    ("renamed-project", "anchored-project", "anchored-project"),
    ("another-project", None, "another-project"),
])
def test_ag1a_source_init_temporary_home(tmp_path, name, marker, expected):
    project = tmp_path / name
    engine = project / ".vnx"
    for directory in ("scripts", "schemas"):
        shutil.copytree(ROOT / directory, engine / directory,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    home = tmp_path / "home"
    home.mkdir()
    data = project / ".vnx-data"
    if marker:
        data.mkdir()
        (data / ".vnx-project-id").write_text(marker + "\n")
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("VNX_") and k not in ("PROJECT_ROOT", "PYTHONPATH")}
    env["HOME"] = str(home)
    result = subprocess.run(
        [sys.executable, str(engine / "scripts/vnx_init.py"), "--step", "init-db"],
        cwd=project, env=env, capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert (data / ".vnx-project-id").read_text().strip() == expected
    assert expected != "vnx-dev"
    with sqlite3.connect(data / "state/quality_intelligence.db") as conn:
        default = next(r[4] for r in conn.execute("PRAGMA table_info(success_patterns)")
                       if r[1] == "project_id")
        assert default == repr(expected)
        assert "vnx-dev" not in "\n".join(conn.iterdump())


def test_ag1a_shared_derivation_path_hash_and_explicit(tmp_path):
    import hashlib
    from project_id_derivation import derive_project_id
    from vnx_cli import _engine

    project = tmp_path / "123"
    expected = "vnx-" + hashlib.sha1(str(project.resolve()).encode()).hexdigest()[:8]
    assert derive_project_id(project) == _engine.derive_project_id(project) == expected
    assert derive_project_id(tmp_path / "456") != expected
    assert _engine.slugify_project_id("123") is None
    assert _engine.slugify_project_id("Fresh Source Project") == "fresh-source-project"
    assert derive_project_id(project, explicit="chosen", marker="anchored") == "chosen"
    assert derive_project_id(project, marker="anchored") == "anchored"
    for invalid in ("", "../other", "/absolute", "INVALID"):
        with pytest.raises(ValueError):
            derive_project_id(project, explicit=invalid)
        with pytest.raises(ValueError):
            derive_project_id(project, marker=invalid)


@pytest.mark.parametrize("fault", [
    "empty", "invalid", "directory", "unreadable", "symlink", "layout", "project", "env",
])
def test_ag1a_source_marker_failure_is_visible(tmp_path, monkeypatch, fault):
    from vnx_init import FAIL, init_db

    project = tmp_path / "renamed-project"
    data = project / ".vnx-data"
    data.mkdir(parents=True)
    marker = data / ".vnx-project-id"
    marker.write_text("anchored-project\n")
    state = data / "state"
    if fault == "empty":
        marker.write_text("")
    elif fault == "invalid":
        marker.write_text("../other\n")
    elif fault == "directory":
        marker.unlink()
        marker.mkdir()
    elif fault == "unreadable":
        marker.chmod(0)
    elif fault == "symlink":
        target = tmp_path / "foreign-marker"
        marker.replace(target)
        marker.symlink_to(target)
    elif fault == "layout":
        state = tmp_path / ".vnx-data/foreign/state"
    elif fault == "project":
        (project / ".vnx-project-id").write_text("foreign\n")
    elif fault == "env":
        monkeypatch.setenv("VNX_PROJECT_ID", "foreign")
    paths = dict(PROJECT_ROOT=str(project), VNX_HOME=str(ROOT),
                 VNX_DATA_DIR=str(data), VNX_STATE_DIR=str(state))
    try:
        result = init_db(paths)
        assert result.status == FAIL and result.message
        assert not (state / "quality_intelligence.db").exists()
        assert "VNX_PROJECT_ID" not in paths
    finally:
        if fault == "unreadable":
            marker.chmod(0o600)
    if fault in ("layout", "project", "env", "symlink"):
        assert marker.read_text() == "anchored-project\n"


@pytest.mark.parametrize("source", ["project", "environment"])
def test_ag1a_source_init_preserves_existing_identity_source(tmp_path, monkeypatch, source):
    from vnx_init import anchor_init_project_id

    project = tmp_path / "renamed-project"
    project.mkdir()
    if source == "project":
        (project / ".vnx-project-id").write_text("anchored-project\nagent-line\n")
    else:
        monkeypatch.setenv("VNX_PROJECT_ID", "anchored-project")
    data = project / ".vnx-data"
    paths = dict(PROJECT_ROOT=str(project), VNX_DATA_DIR=str(data),
                 VNX_STATE_DIR=str(data / "state"))
    assert anchor_init_project_id(paths) == "anchored-project"
    assert (data / ".vnx-project-id").read_text() == "anchored-project\n"


def test_init_marker_conflict_leaves_store_unmarked_and_retry_succeeds(tmp_path, monkeypatch):
    from vnx_init import anchor_init_project_id

    project = tmp_path / "project-a"
    project.mkdir()
    data = tmp_path / ".vnx-data/project-a"
    state = data / "state"
    state.mkdir(parents=True)
    paths = dict(PROJECT_ROOT=str(project), VNX_DATA_DIR=str(data), VNX_STATE_DIR=str(state))
    marker = data / ".vnx-project-id"
    monkeypatch.setenv("VNX_PROJECT_ID", "project-b")

    with pytest.raises((RuntimeError, ValueError), match="project_id conflict"):
        anchor_init_project_id(paths)
    assert not marker.exists()
    assert not marker.with_name(marker.name + ".tmp").exists()
    assert "VNX_PROJECT_ID" not in paths

    monkeypatch.delenv("VNX_PROJECT_ID")
    assert anchor_init_project_id(paths) == "project-a"
    assert marker.read_text() == "project-a\n"
    assert paths["VNX_PROJECT_ID"] == "project-a"


@pytest.mark.parametrize("source", ["project", "explicit", "environment", "store"])
def test_init_marker_existing_identity_survives_refusal(tmp_path, monkeypatch, source):
    from vnx_init import FAIL, init_db

    project = tmp_path / "renamed-project"
    data = project / ".vnx-data"
    state = data / "state"
    state.mkdir(parents=True)
    marker = data / ".vnx-project-id"
    marker.write_text("project-a\nretained metadata\n")
    original = marker.stat()
    paths = dict(PROJECT_ROOT=str(project), VNX_HOME=str(ROOT),
                 VNX_DATA_DIR=str(data), VNX_STATE_DIR=str(state))
    if source == "project":
        (project / ".vnx-project-id").write_text("project-b\n")
    elif source == "explicit":
        paths["VNX_PROJECT_ID"] = "project-b"
    elif source == "environment":
        monkeypatch.setenv("VNX_PROJECT_ID", "project-b")
    else:
        paths["VNX_STATE_DIR"] = str(tmp_path / ".vnx-data/project-b/state")

    result = init_db(paths)
    assert result.status == FAIL and "project_id conflict" in result.message
    assert marker.read_text() == "project-a\nretained metadata\n"
    assert (marker.stat().st_ino, marker.stat().st_mtime_ns) == (original.st_ino, original.st_mtime_ns)
    assert not (Path(paths["VNX_STATE_DIR"]) / "quality_intelligence.db").exists()


@pytest.mark.parametrize("source", ["derived", "project", "explicit", "store-marker"])
def test_init_marker_unmarked_conflict_is_visible_without_publication(tmp_path, source):
    from vnx_init import FAIL, init_db

    project = tmp_path / ("project-b" if source == "derived" else "project-a")
    project.mkdir()
    data = tmp_path / ".vnx-data/project-a"
    state = data / "state"
    state.mkdir(parents=True)
    paths = dict(PROJECT_ROOT=str(project), VNX_HOME=str(ROOT),
                 VNX_DATA_DIR=str(data), VNX_STATE_DIR=str(state))
    if source == "project":
        (project / ".vnx-project-id").write_text("project-b\n")
    elif source == "explicit":
        paths["VNX_PROJECT_ID"] = "project-b"
    elif source == "store-marker":
        (state / ".vnx-project-id").write_text("project-b\n")

    result = init_db(paths)
    assert result.status == FAIL and "project_id conflict" in result.message
    assert not (data / ".vnx-project-id").exists()
    assert not (data / ".vnx-project-id.tmp").exists()
    assert not (state / "quality_intelligence.db").exists()


@pytest.mark.parametrize("candidate", ["", "../other", "/absolute", "INVALID"])
def test_init_marker_invalid_candidate_refuses_without_a_store_source(tmp_path, candidate):
    with pytest.raises(RuntimeError, match="not a valid VNX id"):
        resolve_init_project_id(tmp_path / "state/quality_intelligence.db",
                                candidate_project_id=candidate)


@pytest.mark.parametrize("layout", ["local", "central"])
def test_init_marker_agreeing_sources_publish_once(tmp_path, monkeypatch, layout):
    from vnx_init import anchor_init_project_id

    project = tmp_path / "renamed-project"
    project.mkdir()
    (project / ".vnx-project-id").write_text("project-a\n")
    data = project / ".vnx-data" if layout == "local" else tmp_path / ".vnx-data/project-a"
    (data / "state").mkdir(parents=True)
    paths = dict(PROJECT_ROOT=str(project), VNX_DATA_DIR=str(data),
                 VNX_STATE_DIR=str(data / "state"), VNX_PROJECT_ID="project-a")
    monkeypatch.setenv("VNX_PROJECT_ID", "project-a")
    assert anchor_init_project_id(paths) == "project-a"
    marker = data / ".vnx-project-id"
    original = marker.stat()
    assert marker.read_text() == "project-a\n"
    assert anchor_init_project_id(paths) == "project-a"
    assert (marker.stat().st_ino, marker.stat().st_mtime_ns) == (original.st_ino, original.st_mtime_ns)
