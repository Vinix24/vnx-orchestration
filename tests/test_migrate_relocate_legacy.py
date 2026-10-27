#!/usr/bin/env python3
"""Tests for ``vnx migrate --relocate-legacy`` (store-path parity, PR-3).

A pre-#1437 store under ``${XDG_DATA_HOME:-~/.local/share}/vnx/<pid>`` is copied
into the central ``~/.vnx-data/<pid>`` store: staging copy, full upgrade on the
copy, ``integrity_check`` on every DB, then ONE rename. The source is read-only
throughout and its removal stays the operator's own command.

The relocated fixture is the smallest DB the bootstrap accepts: an empty
``state/runtime_coordination.db`` (a stand-in for the schema-v14 store the
operator has on disk; the bootstrap chain builds the rest). Every test redirects
``HOME``/``XDG_DATA_HOME`` at tmp dirs and clears the ambient VNX data-dir vars,
so no test can reach a real store.
"""

import argparse
import hashlib
import json
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
for _p in (str(ROOT), str(ROOT / "scripts"), str(ROOT / "scripts" / "lib")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

PROJECT_ID = "legacy-proj"


@pytest.fixture
def env(tmp_path, monkeypatch):
    home, xdg, project = tmp_path / "home", tmp_path / "xdg", tmp_path / "project"
    for directory in (home, xdg, project):
        directory.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_DATA_HOME", str(xdg))
    for key in ("VNX_DATA_DIR", "VNX_DATA_DIR_EXPLICIT", "VNX_DATA_HOME",
                "VNX_STATE_DIR", "VNX_PROJECT_ID"):
        monkeypatch.delenv(key, raising=False)
    return SimpleNamespace(home=home, xdg=xdg, project=project)


@pytest.fixture
def pipeline_guard(monkeypatch):
    """Neutralize the pytest-only DB guard and the v22+ walk preflights.

    ``relocate_legacy`` threads the staging dir as an explicit ``data_dir``, so
    the isolation the guard exists for is real; what the guard tests for is
    ``VNX_DATA_DIR``, which the command refuses to run under by design. The
    preflights assume a store the future-system walk already produced, and this
    fixture is a store the bootstrap is about to produce (same reason
    test_horizon_schema_migration_fix clears them around its bootstrap).
    """
    import migrate_future_system as mfs
    import schema_migration

    monkeypatch.setattr(mfs, "_pytest_db_isolation_guard", lambda _root: None)
    saved = {key: list(value) for key, value in schema_migration._PREFLIGHT_HOOKS.items()}
    schema_migration._PREFLIGHT_HOOKS.clear()
    yield
    schema_migration._PREFLIGHT_HOOKS.clear()
    schema_migration._PREFLIGHT_HOOKS.update(saved)


@pytest.fixture(autouse=True)
def _central_store_guard(monkeypatch):
    """The target IS ``$HOME/.vnx-data/<pid>`` and this module's HOME is a tmp dir.

    The relocation books its ownership receipt in the published store's ledger,
    which sits under ``$HOME/.vnx-data`` by construction. The class-wide guard
    cannot tell a tmp ``HOME`` from the operator's own, so it would refuse a
    write that never leaves the tmp tree. Neutralise it here: isolation is real.
    """
    import vnx_paths

    monkeypatch.setattr(
        vnx_paths, "refuse_real_central_store_write_under_test_runner",
        lambda _target: None)


def _terminal_version() -> int:
    import migrate_future_system as mfs

    return mfs.highest_auto_applicable_migration(mfs._MIGRATIONS)


def _legacy(env, *, stamps=(), marker=None, with_db=True) -> Path:
    store = env.xdg / "vnx" / PROJECT_ID
    (store / "state").mkdir(parents=True)
    if with_db:
        conn = sqlite3.connect(str(store / "state" / "runtime_coordination.db"))
        # A table WITHOUT a project_id must not be read as one (the v14 shape).
        conn.execute("CREATE TABLE legacy_plain (x TEXT NOT NULL)")
        if stamps:
            conn.execute("CREATE TABLE legacy_owner (project_id TEXT NOT NULL)")
            conn.executemany("INSERT INTO legacy_owner VALUES (?)", [(s,) for s in stamps])
        conn.commit()
        conn.close()
    (store / "state" / "notes.txt").write_text("operator notes\n", encoding="utf-8")
    if marker:
        (store / ".vnx-project-id").write_text(marker + "\n", encoding="utf-8")
    return store


def _args(env, legacy_path, **overrides) -> argparse.Namespace:
    args = argparse.Namespace(
        project_dir=str(env.project), relocate_legacy=True,
        legacy_path=None if legacy_path is None else str(legacy_path),
        project_id=PROJECT_ID, assume_owner=None,
    )
    for key, value in overrides.items():
        setattr(args, key, value)
    return args


def _run(args) -> int:
    from vnx_cli.commands.migrate import relocate_legacy

    return relocate_legacy(args)


def _target(env) -> Path:
    return (env.home / ".vnx-data" / PROJECT_ID).resolve()


def _staging_dirs(env) -> list:
    base = env.home / ".vnx-data"
    return sorted(base.glob(".staging-*")) if base.is_dir() else []


def _hash_tree(root: Path) -> dict:
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _assert_refused(env, source, capsys, needle) -> None:
    """A refusal writes nothing: no target, no staging dir, source untouched."""
    assert _run(_args(env, source)) != 0
    assert needle in capsys.readouterr().err
    assert not _target(env).exists()
    assert _staging_dirs(env) == []
    assert source.is_dir()


# ---------------------------------------------------------------------------
# The relocation itself
# ---------------------------------------------------------------------------

class TestRelocate:
    def test_v14_store_is_upgraded_and_the_source_is_byte_identical(
            self, env, pipeline_guard, capsys):
        source = _legacy(env)
        before = _hash_tree(source)

        rc = _run(_args(env, source, assume_owner="pre-#1437 store of this project"))

        assert rc == 0
        target = _target(env)
        db = target / "state" / "runtime_coordination.db"
        conn = sqlite3.connect(str(db))
        try:
            assert conn.execute("PRAGMA user_version").fetchone()[0] == _terminal_version()
            assert conn.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
            assert "horizon" in {row[1] for row in conn.execute("PRAGMA table_info(tracks)")}
        finally:
            conn.close()
        assert _hash_tree(source) == before
        assert source.is_dir()
        assert (target / "state" / "notes.txt").read_text(encoding="utf-8") == "operator notes\n"
        record = json.loads((target / "state" / "relocate-legacy.json").read_text())
        assert record["owner_assumed"] is True
        assert record["owner_reason"] == "pre-#1437 store of this project"
        assert record["source_stamps"] == {}
        assert record["relocated_from"] == str(source.resolve())
        assert _staging_dirs(env) == []
        out = capsys.readouterr().out
        assert "vnx doctor --project-dir" in out
        assert "rm -rf --" in out  # printed as text, never executed

    def test_matching_stamp_needs_no_assume_owner(self, env, pipeline_guard, capsys):
        source = _legacy(env, stamps=(PROJECT_ID,))

        assert _run(_args(env, source)) == 0

        record = json.loads((_target(env) / "state" / "relocate-legacy.json").read_text())
        assert record["owner_assumed"] is False
        assert record["owner_reason"] is None
        assert record["source_stamps"] == {
            "state/runtime_coordination.db:legacy_owner": [PROJECT_ID]}

    def test_store_passed_as_a_symlink_still_relocates(self, env, pipeline_guard, capsys):
        """Only links INSIDE the tree refuse; the root is resolved before the walk."""
        source = _legacy(env, stamps=(PROJECT_ID,))
        link = env.xdg / "linked-store"
        link.symlink_to(source, target_is_directory=True)

        assert _run(_args(env, link)) == 0

        target = _target(env)
        assert (target / "state" / "runtime_coordination.db").is_file()
        assert _staging_dirs(env) == []
        assert source.is_dir()  # the tree behind the link stays put


# ---------------------------------------------------------------------------
# A database is recognised by its header, never by its file name
# ---------------------------------------------------------------------------

class TestDetectedDatabases:
    def test_extensionless_database_keeps_its_wal_row(
            self, env, pipeline_guard, capsys):
        """A committed row that lives only in the WAL survives the relocation.

        The file is named ``state`` (no ``.db``), so an extension-based walk
        copies it raw, drops the ``-wal`` and the row with it, and
        ``integrity_check`` still answers ``ok`` on the truncated copy.
        """
        source = _legacy(env, stamps=(PROJECT_ID,))
        db = source / "state" / "state"
        writer = sqlite3.connect(str(db))
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("CREATE TABLE committed_in_wal (x TEXT NOT NULL)")
        writer.execute("INSERT INTO committed_in_wal VALUES ('kept')")
        writer.commit()
        try:
            assert _run(_args(env, source)) == 0
        finally:
            writer.close()

        copy_path = _target(env) / "state" / "state"
        assert copy_path.is_file()
        copy = sqlite3.connect(str(copy_path))
        try:
            assert copy.execute("SELECT x FROM committed_in_wal").fetchall() == [("kept",)]
        finally:
            copy.close()
        # The backup API folded the WAL frames in; no sidecar is copied along.
        assert not (_target(env) / "state" / "state-wal").exists()
        assert _staging_dirs(env) == []

    def test_a_db_name_without_the_sqlite_header_is_a_plain_file(
            self, env, pipeline_guard, capsys):
        source = _legacy(env, stamps=(PROJECT_ID,))
        plain = source / "state" / "not-a-database.db"
        plain.write_text("operator notes in a .db name\n", encoding="utf-8")

        assert _run(_args(env, source)) == 0

        copy = _target(env) / "state" / "not-a-database.db"
        assert copy.read_text(encoding="utf-8") == "operator notes in a .db name\n"

    def test_an_unreadable_header_refuses_with_the_path(self, env, monkeypatch, capsys):
        source = _legacy(env, stamps=(PROJECT_ID,))
        blocked = source / "state" / "mystery"
        blocked.write_bytes(b"x")
        real_open = Path.open

        def _open(path_self, *args, **kwargs):
            if path_self == blocked:
                raise PermissionError("permission denied")
            return real_open(path_self, *args, **kwargs)

        monkeypatch.setattr(Path, "open", _open)

        assert _run(_args(env, source)) != 0

        err = capsys.readouterr().err
        assert str(blocked) in err and "header" in err
        assert not _target(env).exists() and _staging_dirs(env) == []


# ---------------------------------------------------------------------------
# The ownership decision is booked in the canonical ledger
# ---------------------------------------------------------------------------

def _ledger(env) -> list:
    path = _target(env) / "state" / "t0_receipts.ndjson"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()]


class TestOwnershipReceipt:
    def test_the_relocation_books_exactly_one_ownership_receipt(
            self, env, pipeline_guard, capsys):
        source = _legacy(env)

        assert _run(_args(env, source, assume_owner="pre-#1437 store of this project")) == 0

        mutations = [row for row in _ledger(env)
                     if row.get("event_type") == "state_mutation"]
        assert len(mutations) == 1
        receipt = mutations[0]
        assert receipt["trigger"] == "migrate_relocate_legacy"
        assert receipt["project_id"] == PROJECT_ID
        assert receipt["owner_assumed"] is True
        assert receipt["owner_reason"] == "pre-#1437 store of this project"
        assert receipt["source_stamps"] == {}
        assert receipt["relocated_from"] == str(source.resolve())

    def test_a_confirmed_owner_is_booked_too(self, env, pipeline_guard, capsys):
        source = _legacy(env, stamps=(PROJECT_ID,))

        assert _run(_args(env, source)) == 0

        mutations = [row for row in _ledger(env)
                     if row.get("event_type") == "state_mutation"]
        assert len(mutations) == 1
        assert mutations[0]["owner_assumed"] is False
        assert mutations[0]["owner_reason"] is None
        assert mutations[0]["source_stamps"] == {
            "state/runtime_coordination.db:legacy_owner": [PROJECT_ID]}

    def test_a_failing_receipt_write_fails_the_relocation(
            self, env, pipeline_guard, monkeypatch, capsys):
        import append_receipt

        def _boom(*args, **kwargs):
            raise RuntimeError("the ledger is unwritable")

        monkeypatch.setattr(append_receipt, "append_receipt_payload", _boom)
        source = _legacy(env, stamps=(PROJECT_ID,))
        before = _hash_tree(source)

        assert _run(_args(env, source)) != 0

        err = capsys.readouterr().err
        assert "ownership receipt" in err
        assert not _target(env).exists()  # un-published: no receipt, no success
        staging = _staging_dirs(env)
        assert len(staging) == 1
        assert (staging[0] / PROJECT_ID / "state" / "runtime_coordination.db").is_file()
        assert _hash_tree(source) == before


# ---------------------------------------------------------------------------
# Rule 1 / rule 2 refusals — nothing written, nothing left behind
# ---------------------------------------------------------------------------

class TestRefusals:
    def test_vnx_data_dir_in_the_environment(self, env, monkeypatch, capsys):
        monkeypatch.setenv("VNX_DATA_DIR", str(env.home / "overridden"))
        _assert_refused(env, _legacy(env), capsys, "VNX_DATA_DIR is set")

    def test_vnx_state_dir_in_the_environment(self, env, monkeypatch, capsys):
        monkeypatch.setenv("VNX_STATE_DIR", str(env.home / "overridden-state"))
        _assert_refused(env, _legacy(env), capsys, "VNX_STATE_DIR is set")

    def test_foreign_vnx_project_id(self, env, monkeypatch, capsys):
        monkeypatch.setenv("VNX_PROJECT_ID", "other-proj")
        _assert_refused(env, _legacy(env), capsys, "VNX_PROJECT_ID is 'other-proj'")

    def test_repo_local_store_would_shadow_the_target(self, env, capsys):
        (env.project / ".vnx-data" / "state").mkdir(parents=True)
        (env.project / ".vnx-data" / "state" / "runtime_coordination.db").write_bytes(b"")
        _assert_refused(env, _legacy(env), capsys, "repo-local store")

    def test_target_central_store_exists(self, env, capsys):
        _target(env).mkdir(parents=True)
        source = _legacy(env)
        assert _run(_args(env, source)) != 0
        assert "already exists" in capsys.readouterr().err
        assert _staging_dirs(env) == [] and list(_target(env).iterdir()) == []

    def test_source_is_an_empty_skeleton(self, env, capsys):
        _assert_refused(env, _legacy(env, with_db=False), capsys,
                        "no state/runtime_coordination.db")

    def test_source_does_not_exist(self, env, capsys):
        assert _run(_args(env, env.xdg / "vnx" / "absent")) == 2
        assert "not a directory" in capsys.readouterr().err
        assert not _target(env).exists() and _staging_dirs(env) == []

    def test_missing_legacy_path(self, env, capsys):
        assert _run(_args(env, None)) == 2
        assert "--legacy-path <dir> is required" in capsys.readouterr().err
        assert _staging_dirs(env) == []

    def test_missing_project_id(self, env, capsys):
        assert _run(_args(env, _legacy(env), project_id=None)) == 2
        assert "--project-id <pid> is required" in capsys.readouterr().err
        assert _staging_dirs(env) == []

    def test_foreign_project_id_stamp(self, env, capsys):
        source = _legacy(env, stamps=("someone-else",))
        assert _run(_args(env, source, assume_owner="trust me")) != 0
        assert "another project_id" in capsys.readouterr().err
        assert not _target(env).exists() and _staging_dirs(env) == []

    def test_unstamped_store_needs_assume_owner(self, env, capsys):
        _assert_refused(env, _legacy(env), capsys, "no project_id stamp")

    def test_empty_marker_refuses(self, env, capsys):
        source = _legacy(env)
        (source / ".vnx-project-id").write_text("\n", encoding="utf-8")
        _assert_refused(env, source, capsys, "empty or unreadable")

    def test_foreign_marker_refuses(self, env, capsys):
        source = _legacy(env, marker="other-proj")
        assert _run(_args(env, source, assume_owner="trust me")) != 0
        assert ".vnx-project-id names 'other-proj'" in capsys.readouterr().err
        assert not _target(env).exists() and _staging_dirs(env) == []

    def test_invalid_project_id(self, env, capsys):
        assert _run(_args(env, _legacy(env), project_id="../escape")) == 2
        assert "invalid --project-id" in capsys.readouterr().err

    def test_symlinked_state_directory_refuses(self, env, pipeline_guard, capsys):
        """copytree follows a linked dir while _iter_databases does not: refuse.

        The linked DB holds a committed row that only lives in its WAL; a raw copy
        would drop it while integrity_check still answered ``ok``.
        """
        elsewhere = env.xdg / "elsewhere"
        elsewhere.mkdir()
        conn = sqlite3.connect(str(elsewhere / "runtime_coordination.db"))
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA wal_autocheckpoint=0")
        conn.execute("CREATE TABLE committed_in_wal (x TEXT NOT NULL)")
        conn.execute("INSERT INTO committed_in_wal VALUES ('kept')")
        conn.commit()
        source = env.xdg / "vnx" / PROJECT_ID
        source.mkdir(parents=True)
        (source / "state").symlink_to(elsewhere, target_is_directory=True)
        try:
            assert _run(_args(env, source, assume_owner="the operator's own store")) == 2
        finally:
            conn.close()
        err = capsys.readouterr().err
        assert "symlink at state;" in err
        assert "no symlinks" in err
        assert not _target(env).exists()
        assert _staging_dirs(env) == []

    def test_symlinked_file_refuses(self, env, pipeline_guard, capsys):
        """A linked ``*.db`` would be listed by the walk and copied through two paths."""
        source = _legacy(env, stamps=(PROJECT_ID,))
        outside_db = env.xdg / "elsewhere.db"
        conn = sqlite3.connect(str(outside_db))
        conn.execute("CREATE TABLE linked (x TEXT NOT NULL)")
        conn.commit()
        conn.close()
        (source / "state" / "extra.db").symlink_to(outside_db)

        assert _run(_args(env, source)) == 2

        err = capsys.readouterr().err
        assert "symlink at state/extra.db;" in err
        assert not _target(env).exists()
        assert _staging_dirs(env) == []
        assert source.is_dir() and (source / "state" / "runtime_coordination.db").is_file()


# ---------------------------------------------------------------------------
# Rule 4/5: a failed upgrade keeps the staging dir and creates no target
# ---------------------------------------------------------------------------

def test_pipeline_failure_keeps_staging_and_creates_no_target(env, monkeypatch, capsys):
    source = _legacy(env, stamps=(PROJECT_ID,))
    import vnx_cli.commands.migrate as migrate_mod

    calls = []

    def _boom(*args, **kwargs):
        calls.append((args, kwargs))
        raise RuntimeError("injected pipeline failure")

    monkeypatch.setattr(migrate_mod, "_run_future_system_pipeline", _boom)

    assert _run(_args(env, source)) != 0
    assert calls, "the pipeline step must have been reached"
    assert not _target(env).exists()
    staging = _staging_dirs(env)
    assert len(staging) == 1
    assert staging[0].name.startswith(f".staging-{PROJECT_ID}-")
    assert (staging[0] / PROJECT_ID / "state" / "runtime_coordination.db").is_file()
    err = capsys.readouterr().err
    assert "staging dir kept for inspection" in err
    assert str(staging[0]) in err
    assert source.is_dir()
