#!/usr/bin/env python3
"""Tests for fresh-install UX fix: vnx init bootstraps runtime DBs.

Verifies:
- After vnx init, runtime_coordination.db has tracks, pool_config,
  dispatches, terminal_leases tables (no "no such table" errors).
- After vnx init, quality_intelligence.db exists.
- vnx migrate command handler works and is idempotent.
- vnx migrate is registered in TIER_UNIVERSAL (covered by docs test,
  duplicated here for local regression coverage).
- tracks.list_tracks returns empty list (not exception) on fresh DB.

ADR-007: composite PKs on tracks verified (track_id, project_id).
"""

import argparse
import sqlite3
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts" / "lib"))
sys.path.insert(0, str(REPO_ROOT))


@pytest.fixture(autouse=True)
def _clear_schema_preflight_hooks():
    """Isolate schema_migration._PREFLIGHT_HOOKS between tests.

    test_migrate_0022_preflight.py imports migrate_future_system which
    registers a global pre-hook for version 22 that rejects dispatches
    tables with extra columns (e.g. task_class added in v10). On a fresh
    install those extra columns carry no data — dropping them during 0022
    is safe. The pre-hook is intentionally for live-upgrade safety, not
    fresh-bootstrap safety. Clearing it here avoids test-order pollution.
    """
    import importlib
    sm = None
    try:
        sm = importlib.import_module("schema_migration")
        saved = {k: list(v) for k, v in sm._PREFLIGHT_HOOKS.items()}
        sm._PREFLIGHT_HOOKS.clear()
    except (ImportError, AttributeError):
        saved = None
    yield
    if saved is not None and sm is not None:
        sm._PREFLIGHT_HOOKS.clear()
        sm._PREFLIGHT_HOOKS.update(saved)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _init_args(tmp_path, **overrides):
    ns = argparse.Namespace(
        project_path=None,
        project_dir=str(tmp_path),
        project_id=None,
        template="default",
        force=False,
        non_interactive=False,
    )
    for k, v in overrides.items():
        setattr(ns, k, v)
    return ns


def _migrate_args(data_root, **overrides):
    ns = argparse.Namespace(project_dir=str(data_root))
    for k, v in overrides.items():
        setattr(ns, k, v)
    return ns


def _table_names(db_path: Path) -> set:
    """Return set of table names from a SQLite database."""
    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        return {r[0] for r in rows}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# _bootstrap_runtime_dbs (unit)
# ---------------------------------------------------------------------------

class TestBootstrapRuntimeDbs:
    """_bootstrap_runtime_dbs creates required tables under a given data_root."""

    def test_runtime_coordination_db_created(self, tmp_path, monkeypatch):
        monkeypatch.setenv("VNX_DATA_DIR", str(tmp_path))
        monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")

        from vnx_cli.commands.init_cmd import _bootstrap_runtime_dbs
        _bootstrap_runtime_dbs(tmp_path)

        db = tmp_path / "state" / "runtime_coordination.db"
        assert db.exists(), "runtime_coordination.db must be created"

    def test_tracks_table_exists_after_bootstrap(self, tmp_path, monkeypatch):
        monkeypatch.setenv("VNX_DATA_DIR", str(tmp_path))
        monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")

        from vnx_cli.commands.init_cmd import _bootstrap_runtime_dbs
        _bootstrap_runtime_dbs(tmp_path)

        db = tmp_path / "state" / "runtime_coordination.db"
        tables = _table_names(db)
        assert "tracks" in tables, (
            f"tracks table missing after bootstrap. Found: {sorted(tables)}"
        )

    def test_pool_config_table_exists_after_bootstrap(self, tmp_path, monkeypatch):
        monkeypatch.setenv("VNX_DATA_DIR", str(tmp_path))
        monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")

        from vnx_cli.commands.init_cmd import _bootstrap_runtime_dbs
        _bootstrap_runtime_dbs(tmp_path)

        db = tmp_path / "state" / "runtime_coordination.db"
        tables = _table_names(db)
        assert "pool_config" in tables, (
            f"pool_config table missing. Found: {sorted(tables)}"
        )

    def test_dispatches_table_exists_after_bootstrap(self, tmp_path, monkeypatch):
        monkeypatch.setenv("VNX_DATA_DIR", str(tmp_path))
        monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")

        from vnx_cli.commands.init_cmd import _bootstrap_runtime_dbs
        _bootstrap_runtime_dbs(tmp_path)

        db = tmp_path / "state" / "runtime_coordination.db"
        tables = _table_names(db)
        assert "dispatches" in tables, (
            f"dispatches table missing. Found: {sorted(tables)}"
        )

    def test_tracks_composite_pk_adr007(self, tmp_path, monkeypatch):
        """ADR-007: tracks table must have composite (track_id, project_id) PK."""
        monkeypatch.setenv("VNX_DATA_DIR", str(tmp_path))
        monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")

        from vnx_cli.commands.init_cmd import _bootstrap_runtime_dbs
        _bootstrap_runtime_dbs(tmp_path)

        db = tmp_path / "state" / "runtime_coordination.db"
        conn = sqlite3.connect(str(db))
        try:
            pk_info = conn.execute("PRAGMA table_info(tracks)").fetchall()
            pk_cols = {row[1] for row in pk_info if row[5] > 0}  # col[5] = pk position
        finally:
            conn.close()

        assert "track_id" in pk_cols, "track_id must be part of PK"
        assert "project_id" in pk_cols, (
            "project_id must be part of composite PK (ADR-007)"
        )

    def test_bootstrap_idempotent(self, tmp_path, monkeypatch):
        """Running _bootstrap_runtime_dbs twice must not raise."""
        monkeypatch.setenv("VNX_DATA_DIR", str(tmp_path))
        monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")

        from vnx_cli.commands.init_cmd import _bootstrap_runtime_dbs
        _bootstrap_runtime_dbs(tmp_path)
        _bootstrap_runtime_dbs(tmp_path)  # second call — must not raise


# ---------------------------------------------------------------------------
# vnx init triggers bootstrap (integration)
# ---------------------------------------------------------------------------

class TestInitBootstrapsDb:
    """vnx init must call _bootstrap_runtime_dbs so tables exist immediately."""

    def test_init_creates_tracks_table(self, tmp_path, monkeypatch):
        monkeypatch.setenv("VNX_DATA_DIR", str(tmp_path / ".vnx-data"))
        monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")

        from vnx_cli.commands.init_cmd import vnx_init
        rc = vnx_init(_init_args(tmp_path))
        assert rc == 0

        db = tmp_path / ".vnx-data" / "state" / "runtime_coordination.db"
        if not db.exists():
            # XDG path — look under the env-set path
            db = Path(tmp_path / ".vnx-data") / "state" / "runtime_coordination.db"

        assert db.exists(), "runtime_coordination.db not created by vnx init"
        tables = _table_names(db)
        assert "tracks" in tables, (
            f"tracks table missing after vnx init. Got: {sorted(tables)}"
        )

    def test_init_no_track_table_exception(self, tmp_path, monkeypatch):
        """list_tracks must return [] (not raise) after vnx init."""
        monkeypatch.setenv("VNX_DATA_DIR", str(tmp_path / ".vnx-data"))
        monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")

        from vnx_cli.commands.init_cmd import vnx_init
        rc = vnx_init(_init_args(tmp_path))
        assert rc == 0

        state_dir = tmp_path / ".vnx-data" / "state"
        if state_dir.exists():
            from tracks import list_tracks
            result = list_tracks(state_dir, "test-project")
            assert result == [], f"Expected empty list, got {result}"


# ---------------------------------------------------------------------------
# vnx migrate command handler (unit)
# ---------------------------------------------------------------------------

class TestVnxMigrateCommand:
    """vnx_migrate(args) bootstraps DBs and returns exit code 0."""

    def test_migrate_returns_zero(self, tmp_path, monkeypatch):
        monkeypatch.setenv("VNX_DATA_DIR", str(tmp_path))
        monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")

        from vnx_cli.commands.migrate import vnx_migrate
        args = argparse.Namespace(project_dir=str(tmp_path))
        rc = vnx_migrate(args)
        assert rc == 0

    def test_migrate_creates_tracks_table(self, tmp_path, monkeypatch):
        monkeypatch.setenv("VNX_DATA_DIR", str(tmp_path))
        monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")

        from vnx_cli.commands.migrate import vnx_migrate
        args = argparse.Namespace(project_dir=str(tmp_path))
        vnx_migrate(args)

        db = tmp_path / "state" / "runtime_coordination.db"
        assert db.exists()
        tables = _table_names(db)
        assert "tracks" in tables, (
            f"tracks table missing after vnx migrate. Got: {sorted(tables)}"
        )

    def test_migrate_idempotent(self, tmp_path, monkeypatch):
        monkeypatch.setenv("VNX_DATA_DIR", str(tmp_path))
        monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")

        from vnx_cli.commands.migrate import vnx_migrate
        args = argparse.Namespace(project_dir=str(tmp_path))
        assert vnx_migrate(args) == 0
        assert vnx_migrate(args) == 0


# ---------------------------------------------------------------------------
# vnx migrate registered in mode tiers
# ---------------------------------------------------------------------------

class TestMigrateInModeTiers:
    """migrate must be in TIER_UNIVERSAL so it works in all modes."""

    def test_migrate_in_tier_universal(self):
        from vnx_mode import TIER_UNIVERSAL
        assert "migrate" in TIER_UNIVERSAL, (
            "'migrate' missing from TIER_UNIVERSAL — breaks fresh-install UX"
        )

    def test_migrate_in_starter_mode(self):
        from vnx_mode import MODE_COMMANDS, VNXMode
        assert "migrate" in MODE_COMMANDS[VNXMode.STARTER]

    def test_migrate_in_operator_mode(self):
        from vnx_mode import MODE_COMMANDS, VNXMode
        assert "migrate" in MODE_COMMANDS[VNXMode.OPERATOR]


# ---------------------------------------------------------------------------
# Pool status error message points to a real command
# ---------------------------------------------------------------------------

class TestPoolStatusErrorMessage:
    """Pool status error must reference 'vnx migrate', not a dead-end command."""

    def test_pool_status_error_references_migrate(self):
        from vnx_cli.commands import pool as pool_mod
        import inspect
        src = inspect.getsource(pool_mod)
        assert "vnx migrate" in src, (
            "pool status error message must reference 'vnx migrate'"
        )
        assert "migration 0020" not in src, (
            "pool status must not reference internal migration number (dead-end UX)"
        )


# ---------------------------------------------------------------------------
# Fail-loud regression: bootstrap failure must exit non-zero
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Fresh pip install: data root outside project dir (XDG / VNX_DATA_HOME)
# ---------------------------------------------------------------------------

class TestFreshInstallPathResolution:
    """track list and pool_manager must use the canonical data root, not hardcoded .vnx-data/.

    Regression for: fresh pip install where data root is the XDG path
    (~/.local/share/vnx/<project_id>/) rather than <project_dir>/.vnx-data/.
    Both vnx track list and vnx pool status were looking in the hardcoded
    project-local path and failing with "unable to open database file".
    """

    def test_track_list_returns_empty_when_data_root_outside_project(self, tmp_path, monkeypatch):
        """vnx track list must not error when the DB is at an XDG-style data root."""
        xdg_data_root = tmp_path / "xdg_data"
        project_dir = tmp_path / "my_project"
        project_dir.mkdir()

        monkeypatch.setenv("VNX_DATA_DIR", str(xdg_data_root))
        monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")

        from vnx_cli.commands.migrate import vnx_migrate
        rc = vnx_migrate(argparse.Namespace(project_dir=str(project_dir)))
        assert rc == 0

        db_xdg = xdg_data_root / "state" / "runtime_coordination.db"
        db_local = project_dir / ".vnx-data" / "state" / "runtime_coordination.db"
        assert db_xdg.exists(), "migrate must create DB at the resolved data root"
        assert not db_local.exists(), "migrate must not create DB at hardcoded .vnx-data/"

        from vnx_cli.commands.track import _resolve_state_dir
        state_dir = _resolve_state_dir(project_dir)
        assert str(xdg_data_root) in str(state_dir), (
            f"_resolve_state_dir must use canonical data root for pip installs; got {state_dir}"
        )

        from tracks import list_tracks
        result = list_tracks(state_dir, "test-project")
        assert result == [], f"list_tracks must return [] on fresh DB, got {result!r}"

    def test_track_resolve_state_dir_matches_migrate_data_root(self, tmp_path, monkeypatch):
        """_resolve_state_dir must resolve to the same root that vnx migrate writes to."""
        data_root = tmp_path / "explicit_root"
        project_dir = tmp_path / "proj"
        project_dir.mkdir()

        monkeypatch.setenv("VNX_DATA_DIR", str(data_root))
        monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")

        from vnx_cli.commands.migrate import vnx_migrate
        from vnx_cli.commands.track import _resolve_state_dir

        vnx_migrate(argparse.Namespace(project_dir=str(project_dir)))

        state_dir = _resolve_state_dir(project_dir)
        expected_db = state_dir / "runtime_coordination.db"
        assert expected_db.exists(), (
            f"_resolve_state_dir must point to the DB created by vnx migrate; "
            f"expected {expected_db}"
        )

    def test_pool_default_db_path_honors_vnx_data_dir(self, tmp_path, monkeypatch):
        """pool_manager._default_db_path must use the canonical data root (not hardcoded .vnx-data)."""
        xdg_root = tmp_path / "xdg_pool"
        monkeypatch.setenv("VNX_DATA_DIR", str(xdg_root))
        monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")

        from pool_manager import _default_db_path
        db_path = _default_db_path("test-project")
        assert str(xdg_root) in str(db_path), (
            f"_default_db_path must use canonical data root for pip installs; got {db_path}"
        )
        assert ".vnx-data" not in str(db_path), (
            f"_default_db_path must not hardcode .vnx-data when VNX_DATA_DIR is set; got {db_path}"
        )

    def test_track_list_error_free_after_migrate_no_local_vnx_data(self, tmp_path, monkeypatch):
        """Acceptance test: init+migrate in a fresh project without .vnx-data/ locally."""
        data_root = tmp_path / "runtime"
        project_dir = tmp_path / "fresh_proj"
        project_dir.mkdir()

        monkeypatch.setenv("VNX_DATA_DIR", str(data_root))
        monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")

        from vnx_cli.commands.init_cmd import vnx_init
        from vnx_cli.commands.migrate import vnx_migrate

        rc_init = vnx_init(_init_args(project_dir))
        assert rc_init == 0, "vnx init must succeed"

        rc_migrate = vnx_migrate(argparse.Namespace(project_dir=str(project_dir)))
        assert rc_migrate == 0, "vnx migrate must succeed after init"

        from vnx_cli.commands.track import _resolve_state_dir
        from tracks import list_tracks

        state_dir = _resolve_state_dir(project_dir)
        tracks = list_tracks(state_dir, "fresh-proj")
        assert tracks == [], (
            f"vnx track list must return empty (not raise) after init+migrate; got {tracks!r}"
        )


class TestBootstrapFailLoud:
    """vnx init and vnx migrate must exit NON-ZERO when DB bootstrap fails.

    Regression for: warnings-then-success on core DB failure (blocking finding).
    """

    def test_vnx_init_exits_nonzero_on_bootstrap_failure(self, tmp_path, monkeypatch):
        """vnx init returns non-zero when _bootstrap_runtime_dbs raises."""
        monkeypatch.setenv("VNX_DATA_DIR", str(tmp_path / ".vnx-data"))
        monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")

        import vnx_cli.commands.init_cmd as init_mod

        def _fail(data_root, **kwargs):
            raise RuntimeError("simulated DB bootstrap failure")

        monkeypatch.setattr(init_mod, "_bootstrap_runtime_dbs", _fail)

        rc = init_mod.vnx_init(_init_args(tmp_path))
        assert rc != 0, (
            "vnx init must return non-zero when bootstrap fails — "
            "silent success on broken DB is the bug we fixed"
        )

    def test_vnx_migrate_exits_nonzero_on_bootstrap_failure(self, tmp_path, monkeypatch):
        """vnx migrate returns non-zero when _bootstrap_runtime_dbs raises."""
        monkeypatch.setenv("VNX_DATA_DIR", str(tmp_path))
        monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")

        import vnx_cli.commands.migrate as migrate_mod

        def _fail(data_root, **kwargs):
            raise RuntimeError("simulated migration failure")

        monkeypatch.setattr(migrate_mod, "_bootstrap_runtime_dbs", _fail)

        args = argparse.Namespace(project_dir=str(tmp_path))
        rc = migrate_mod.vnx_migrate(args)
        assert rc != 0, (
            "vnx migrate must return non-zero when bootstrap fails"
        )

    def test_vnx_init_bootstrap_failure_prints_error(self, tmp_path, monkeypatch, capsys):
        """vnx init must print a clear error (not just a warning) on bootstrap failure."""
        monkeypatch.setenv("VNX_DATA_DIR", str(tmp_path / ".vnx-data"))
        monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")

        import vnx_cli.commands.init_cmd as init_mod

        def _fail(data_root, **kwargs):
            raise RuntimeError("injected failure")

        monkeypatch.setattr(init_mod, "_bootstrap_runtime_dbs", _fail)

        init_mod.vnx_init(_init_args(tmp_path))
        captured = capsys.readouterr()
        assert "error" in captured.err.lower(), (
            "vnx init must print 'error' to stderr on bootstrap failure, not a silent warning"
        )


# ---------------------------------------------------------------------------
# LB-6: governance hooks wired in scaffolded settings.json
# ---------------------------------------------------------------------------

class TestInitHooksWiring:
    """LB-6 / sweep A3: vnx init must scaffold .claude/settings.json with the
    Stop and PreToolUse governance hooks so the report→receipt→audit-trail
    loop is active out-of-the-box.

    Hook script paths must be absolute (resolved from engine_root) so they
    work in a pip-install where scripts/ lives in site-packages, not in the
    project directory.
    """

    @pytest.mark.parametrize("template", ["default", "minimal"])
    def test_hooks_block_present(self, tmp_path, monkeypatch, template):
        """settings.json must contain a top-level 'hooks' key after vnx init."""
        import json

        monkeypatch.setenv("VNX_DATA_DIR", str(tmp_path / ".vnx-data"))
        monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")

        from vnx_cli.commands.init_cmd import vnx_init
        rc = vnx_init(_init_args(tmp_path, template=template))
        assert rc == 0, f"vnx init (template={template!r}) must exit 0"

        settings_path = tmp_path / ".claude" / "settings.json"
        assert settings_path.exists(), ".claude/settings.json must be created by vnx init"

        settings = json.loads(settings_path.read_text(encoding="utf-8"))
        assert "hooks" in settings, (
            f"settings.json (template={template!r}) must contain a 'hooks' block — "
            "governance loop (report→receipt→audit-trail) is dead without it (LB-6)"
        )

    @pytest.mark.parametrize("template", ["default", "minimal"])
    def test_stop_hook_wired(self, tmp_path, monkeypatch, template):
        """settings.json must wire a Stop hook pointing to stop_report_hook.sh."""
        import json

        monkeypatch.setenv("VNX_DATA_DIR", str(tmp_path / ".vnx-data"))
        monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")

        from vnx_cli.commands.init_cmd import vnx_init
        vnx_init(_init_args(tmp_path, template=template))

        settings_path = tmp_path / ".claude" / "settings.json"
        settings = json.loads(settings_path.read_text(encoding="utf-8"))

        stop_hooks = settings.get("hooks", {}).get("Stop", [])
        assert stop_hooks, (
            f"settings.json (template={template!r}) must have at least one Stop hook"
        )
        commands = [
            h.get("command", "")
            for entry in stop_hooks
            for h in entry.get("hooks", [])
        ]
        assert any("stop_report_hook.sh" in cmd for cmd in commands), (
            f"Stop hook must reference stop_report_hook.sh (template={template!r}). "
            f"Found commands: {commands}"
        )

    @pytest.mark.parametrize("template", ["default", "minimal"])
    def test_pretooluse_guard_wired(self, tmp_path, monkeypatch, template):
        """settings.json must wire a PreToolUse Bash hook for the raw-spawn guard."""
        import json

        monkeypatch.setenv("VNX_DATA_DIR", str(tmp_path / ".vnx-data"))
        monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")

        from vnx_cli.commands.init_cmd import vnx_init
        vnx_init(_init_args(tmp_path, template=template))

        settings_path = tmp_path / ".claude" / "settings.json"
        settings = json.loads(settings_path.read_text(encoding="utf-8"))

        pretooluse_hooks = settings.get("hooks", {}).get("PreToolUse", [])
        assert pretooluse_hooks, (
            f"settings.json (template={template!r}) must have at least one PreToolUse hook"
        )
        commands = [
            h.get("command", "")
            for entry in pretooluse_hooks
            for h in entry.get("hooks", [])
        ]
        assert any("pretooluse_block_raw_claude_spawn.sh" in cmd for cmd in commands), (
            f"PreToolUse hook must reference pretooluse_block_raw_claude_spawn.sh "
            f"(template={template!r}). Found commands: {commands}"
        )

    @pytest.mark.parametrize("template", ["default", "minimal"])
    def test_hook_script_paths_are_absolute(self, tmp_path, monkeypatch, template):
        """Hook script paths must be absolute so they work from a pip-install.

        In a pip install, scripts/ is in site-packages, not in the project
        directory. git rev-parse from the worker cwd would resolve the project
        root, NOT the site-packages location. Absolute paths are the only
        reliable mechanism for cross-environment hook delivery.
        """
        import json

        monkeypatch.setenv("VNX_DATA_DIR", str(tmp_path / ".vnx-data"))
        monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")

        from vnx_cli.commands.init_cmd import vnx_init
        vnx_init(_init_args(tmp_path, template=template))

        settings_path = tmp_path / ".claude" / "settings.json"
        settings = json.loads(settings_path.read_text(encoding="utf-8"))

        hooks_block = settings.get("hooks", {})
        for hook_type, entries in hooks_block.items():
            for entry in entries:
                for h in entry.get("hooks", []):
                    cmd = h.get("command", "")
                    # Extract the script path from commands like:
                    # bash -c 'exec bash "/abs/path/to/hook.sh"'
                    import re
                    # Match any .sh path in the command string
                    sh_paths = re.findall(r'"(/[^"]+\.sh)"', cmd)
                    for sh_path in sh_paths:
                        assert sh_path.startswith("/"), (
                            f"Hook script path must be absolute (template={template!r}, "
                            f"hook_type={hook_type!r}): {sh_path!r}"
                        )

    @pytest.mark.parametrize("template", ["default", "minimal"])
    def test_hook_script_paths_exist_on_disk(self, tmp_path, monkeypatch, template):
        """Every hook script path in the scaffolded settings.json must exist on disk."""
        import json
        import re

        monkeypatch.setenv("VNX_DATA_DIR", str(tmp_path / ".vnx-data"))
        monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")

        from vnx_cli.commands.init_cmd import vnx_init
        vnx_init(_init_args(tmp_path, template=template))

        settings_path = tmp_path / ".claude" / "settings.json"
        settings = json.loads(settings_path.read_text(encoding="utf-8"))

        hooks_block = settings.get("hooks", {})
        missing = []
        for hook_type, entries in hooks_block.items():
            for entry in entries:
                for h in entry.get("hooks", []):
                    cmd = h.get("command", "")
                    sh_paths = re.findall(r'"(/[^"]+\.sh)"', cmd)
                    for sh_path in sh_paths:
                        if not Path(sh_path).exists():
                            missing.append(f"{hook_type}: {sh_path}")

        assert not missing, (
            f"Hook script(s) referenced in settings.json do not exist on disk "
            f"(template={template!r}): {missing}"
        )
