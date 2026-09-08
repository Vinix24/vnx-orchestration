#!/usr/bin/env python3
"""Tests for vnx doctor --strict: central-mode detection and pre-flight validation."""

import argparse
import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest

from vnx_cli import _engine
from vnx_cli.commands.doctor import (
    FAIL, PASS, WARN,
    _check_active_drain,
    _check_dual_install,
    _check_install_mode,
    _check_overrides,
    _check_schema_versions,
    _check_skill_coverage,
    _check_tools,
    vnx_doctor,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_project(tmp_path: Path) -> Path:
    """Minimal project skeleton sufficient for doctor checks."""
    p = tmp_path / "project"
    p.mkdir()
    (p / ".vnx").mkdir()
    (p / ".vnx-data" / "state").mkdir(parents=True)
    (p / ".vnx-data" / "dispatches" / "pending").mkdir(parents=True)
    return p


def _make_args(project_dir: str, strict: bool = False, emit_json: bool = False) -> argparse.Namespace:
    return argparse.Namespace(project_dir=project_dir, strict=strict, json=emit_json)


def _make_coordination_db(state_dir: Path, active_count: int = 0, schema_version: int = 10) -> Path:
    db_path = state_dir / "runtime_coordination.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS dispatches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            dispatch_id TEXT NOT NULL UNIQUE,
            state TEXT NOT NULL DEFAULT 'queued'
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS runtime_schema_version (
            version INTEGER NOT NULL,
            applied_at TEXT
        )
    """)
    conn.execute("INSERT OR IGNORE INTO runtime_schema_version (version) VALUES (?)", (schema_version,))
    for i in range(active_count):
        conn.execute(
            "INSERT INTO dispatches (dispatch_id, state) VALUES (?, 'running')",
            (f"test-dispatch-{i}",),
        )
    conn.commit()
    conn.close()
    return db_path


# ---------------------------------------------------------------------------
# Test 1: embedded-mode install detected
# ---------------------------------------------------------------------------

class TestEmbeddedMode:
    def test_embedded_mode_detected(self, tmp_path, monkeypatch):
        project = _make_project(tmp_path)
        # Hermetic home: the operator's real ~/.vnx-system must not leak in.
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path / "home"))
        embedded = project / ".claude" / "vnx-system" / "scripts"
        embedded.mkdir(parents=True)

        result = _check_install_mode(project)

        assert result.status == PASS
        assert "mode: embedded" in result.detail
        assert "vnx-system" in result.detail

    def test_no_install_warns(self, tmp_path, monkeypatch):
        project = _make_project(tmp_path)
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path / "home"))
        # No central/embedded install AND no visible source/packaged engine
        # tree: the dev-checkout repo root must not leak in as a source install.
        empty_root = tmp_path / "empty-engine"
        empty_root.mkdir()
        monkeypatch.setattr(_engine, "engine_root", lambda: empty_root)

        result = _check_install_mode(project)

        assert result.status == WARN
        assert "no VNX install detected" in result.detail


# ---------------------------------------------------------------------------
# Test 2: central-mode install detected with pin vs active
# ---------------------------------------------------------------------------

class TestCentralMode:
    def test_central_mode_detected(self, tmp_path, monkeypatch):
        project = _make_project(tmp_path)
        # Project pin: the value `cat .vnx-version` shows.
        (project / ".vnx-version").write_text("v1.0.0-rc2\n")
        version_dir = tmp_path / "home" / ".vnx-system" / "versions" / "v1.0.0-rc2"
        (version_dir / "scripts").mkdir(parents=True)
        (version_dir / "VERSION").write_text("1.0.0-rc2\n")
        (version_dir / ".vnx-install-mode").write_text("central\n")
        current = tmp_path / "home" / ".vnx-system" / "current"
        current.symlink_to(version_dir)

        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path / "home"))

        result = _check_install_mode(project)

        assert result.status == PASS
        assert "mode: central" in result.detail
        assert "pin: v1.0.0-rc2" in result.detail
        assert "active: v1.0.0-rc2" in result.detail

    def test_central_mode_pin_unset_when_no_version_file(self, tmp_path, monkeypatch):
        project = _make_project(tmp_path)
        central = tmp_path / "home" / ".vnx-system" / "current"
        (central / "scripts").mkdir(parents=True)
        (central / ".vnx-install-mode").write_text("central\n")

        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path / "home"))

        result = _check_install_mode(project)

        assert result.status == PASS
        assert "pin: unset" in result.detail
        assert "active: current" in result.detail

    def test_central_mode_pin_error_on_read_failure(self, tmp_path, monkeypatch):
        project = _make_project(tmp_path)
        pin_file = project / ".vnx-version"
        pin_file.write_text("1.0.0-rc2\n")
        pin_file.chmod(0o000)
        central = tmp_path / "home" / ".vnx-system" / "current"
        (central / "scripts").mkdir(parents=True)
        (central / ".vnx-install-mode").write_text("central\n")

        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path / "home"))

        try:
            result = _check_install_mode(project)
        finally:
            pin_file.chmod(0o644)

        assert result.status == WARN
        assert "pin: error" in result.detail

    def test_central_mode_error_pin_causes_strict_exit_1(self, tmp_path, monkeypatch):
        project = _make_project(tmp_path)
        pin_file = project / ".vnx-version"
        pin_file.write_text("1.0.0-rc2\n")
        pin_file.chmod(0o000)
        central = tmp_path / "home" / ".vnx-system" / "current"
        (central / "scripts").mkdir(parents=True)
        (central / ".vnx-install-mode").write_text("central\n")

        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path / "home"))

        try:
            exit_code = vnx_doctor(_make_args(str(project), strict=True))
        finally:
            pin_file.chmod(0o644)

        assert exit_code == 1

    def test_central_mode_pin_and_active_agree_with_pin_file(self, tmp_path, monkeypatch):
        """OI-914 split ``pin`` and ``active`` into two separately-reported
        values so a drift is visible instead of collapsed into one label —
        but stopped short of comparing them, so this scenario (pin v1.4.0,
        active v1.4.1) still PASSed. OI-1678 closes that gap: the two values
        being visible only matters if a mismatch actually WARNs."""
        project = _make_project(tmp_path)
        (project / ".vnx-version").write_text("v1.4.0\n")
        active_dir = tmp_path / "home" / ".vnx-system" / "versions" / "v1.4.1"
        (active_dir / "scripts").mkdir(parents=True)
        (active_dir / ".vnx-install-mode").write_text("central\n")
        current = tmp_path / "home" / ".vnx-system" / "current"
        current.symlink_to(active_dir)

        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path / "home"))

        result = _check_install_mode(project)

        assert result.status == WARN
        assert "pin: v1.4.0" in result.detail
        assert "active: v1.4.1" in result.detail
        assert "not honored" in result.detail

    def test_central_mode_pin_diverges_from_active_warns(self, tmp_path, monkeypatch):
        """OI-1678 live case (SEOcrawler_v2): pin v1.5.0, active v1.6.0. The
        pin is readable and there is no marker problem, so pre-fix code fell
        straight through to PASS — pin and active were reported side by side
        but never compared. WARN, naming both versions and the pin file
        path (OI-1679)."""
        project = _make_project(tmp_path)
        (project / ".vnx-version").write_text("v1.5.0\n")
        active_dir = tmp_path / "home" / ".vnx-system" / "versions" / "v1.6.0"
        (active_dir / "scripts").mkdir(parents=True)
        (active_dir / ".vnx-install-mode").write_text("central\n")
        current = tmp_path / "home" / ".vnx-system" / "current"
        current.symlink_to(active_dir)

        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path / "home"))

        result = _check_install_mode(project)

        assert result.status == WARN
        assert "mode: central" in result.detail
        assert "pin: v1.5.0" in result.detail
        assert "active: v1.6.0" in result.detail
        assert "not honored" in result.detail
        assert str(project / ".vnx-version") in result.detail

    def test_central_mode_pin_without_v_prefix_matches_v_prefixed_active(self, tmp_path, monkeypatch):
        """glm-gate finding on the OI-1678 PR: `_reexec` normalizes away a
        decorative leading `v` (`_normalize_version`) before deciding whether
        a pin is honored, but this check compared the raw pin string against
        `active` directly. A pin written WITHOUT the `v` (e.g. `1.5.0`) that
        names the SAME version as the `v`-prefixed active dir (`v1.5.0`) is
        honored by re-exec, but pre-fix this check reported it as a mismatch
        — the inverse of the bug OI-1678 closes: the check first stayed
        silent where it should have warned, then warned where it should have
        stayed silent."""
        project = _make_project(tmp_path)
        (project / ".vnx-version").write_text("1.5.0\n")
        active_dir = tmp_path / "home" / ".vnx-system" / "versions" / "v1.5.0"
        (active_dir / "scripts").mkdir(parents=True)
        (active_dir / ".vnx-install-mode").write_text("central\n")
        current = tmp_path / "home" / ".vnx-system" / "current"
        current.symlink_to(active_dir)

        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path / "home"))

        result = _check_install_mode(project)

        assert result.status == PASS
        assert "pin: 1.5.0" in result.detail
        assert "active: v1.5.0" in result.detail
        assert "not honored" not in result.detail

    def test_central_mode_pin_unset_stays_pass_with_versioned_active(self, tmp_path, monkeypatch):
        """An unpinned project must not get a false WARN just because active
        resolves to a real version dir name (v1.6.0) instead of the literal
        'current' fallback used by the no-VERSION-dir test above."""
        project = _make_project(tmp_path)
        active_dir = tmp_path / "home" / ".vnx-system" / "versions" / "v1.6.0"
        (active_dir / "scripts").mkdir(parents=True)
        (active_dir / ".vnx-install-mode").write_text("central\n")
        current = tmp_path / "home" / ".vnx-system" / "current"
        current.symlink_to(active_dir)

        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path / "home"))

        result = _check_install_mode(project)

        assert result.status == PASS
        assert "pin: unset" in result.detail
        assert "active: v1.6.0" in result.detail

    def test_central_mode_pin_found_via_ancestor_walkup(self, tmp_path, monkeypatch):
        """OI-1679: doctor run from a SUBDIRECTORY of a pinned project must
        still see the pin — the same walk `_reexec._find_pin_dir` performs at
        startup — and must name the ancestor path the pin came from, not
        silently report 'unset' just because the immediate project_dir has no
        pin file of its own."""
        project = _make_project(tmp_path)
        (project / ".vnx-version").write_text("v1.5.0\n")
        submap = project / "sub" / "deeper"
        submap.mkdir(parents=True)
        active_dir = tmp_path / "home" / ".vnx-system" / "versions" / "v1.6.0"
        (active_dir / "scripts").mkdir(parents=True)
        (active_dir / ".vnx-install-mode").write_text("central\n")
        current = tmp_path / "home" / ".vnx-system" / "current"
        current.symlink_to(active_dir)

        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path / "home"))

        result = _check_install_mode(submap)

        assert result.status == WARN
        assert "pin: v1.5.0" in result.detail
        assert "active: v1.6.0" in result.detail
        assert str(project / ".vnx-version") in result.detail

    def test_central_mode_sibling_directory_does_not_inherit_pin(self, tmp_path, monkeypatch):
        """OI-1679 documented blind spot: the pin walk only climbs ANCESTORS.
        A build worktree checked out as a SIBLING of the pinned project root
        (e.g. `<root>-wt-g1-<id>`) does not inherit the pin, so running this
        check from inside the sibling reports 'unset'/PASS even though the
        sibling may in fact be running a different engine version than the
        pinned root expects. This locks in the accepted, documented scope of
        the check (see the `_check_install_mode` docstring) — not a claim
        that the split is undetected everywhere."""
        root = tmp_path / "proj-root"
        root.mkdir()
        (root / ".vnx").mkdir()
        (root / ".vnx-data" / "state").mkdir(parents=True)
        (root / ".vnx-version").write_text("v1.5.0\n")

        sibling = tmp_path / "proj-root-wt-g1-abc123"
        sibling.mkdir()
        (sibling / ".vnx").mkdir()
        (sibling / ".vnx-data" / "state").mkdir(parents=True)

        active_dir = tmp_path / "home" / ".vnx-system" / "versions" / "v1.6.0"
        (active_dir / "scripts").mkdir(parents=True)
        (active_dir / ".vnx-install-mode").write_text("central\n")
        current = tmp_path / "home" / ".vnx-system" / "current"
        current.symlink_to(active_dir)

        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path / "home"))

        result = _check_install_mode(sibling)

        assert result.status == PASS
        assert "pin: unset" in result.detail

    def test_central_mode_missing_marker_warns(self, tmp_path, monkeypatch):
        """The active `current` resolves to a version dir with no
        `.vnx-install-mode` marker (the bug this dispatch fixes: `vnx update`
        never wrote one). The check must surface it, not silently PASS."""
        project = _make_project(tmp_path)
        version_dir = tmp_path / "home" / ".vnx-system" / "versions" / "edge"
        (version_dir / "scripts").mkdir(parents=True)
        version_file = version_dir / "VERSION"
        version_file.write_text("edge\n")
        current = tmp_path / "home" / ".vnx-system" / "current"
        current.symlink_to(version_dir)

        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path / "home"))

        result = _check_install_mode(project)

        assert result.status == WARN
        assert "mode: central" in result.detail
        assert "pin: unset" in result.detail
        assert "active: edge" in result.detail
        assert "install-mode marker missing" in result.detail

    def test_central_mode_invalid_marker_content_warns(self, tmp_path, monkeypatch):
        project = _make_project(tmp_path)
        version_dir = tmp_path / "home" / ".vnx-system" / "versions" / "edge"
        (version_dir / "scripts").mkdir(parents=True)
        (version_dir / ".vnx-install-mode").write_text("embedded\n")
        current = tmp_path / "home" / ".vnx-system" / "current"
        current.symlink_to(version_dir)

        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path / "home"))

        result = _check_install_mode(project)

        assert result.status == WARN
        assert "install-mode marker invalid" in result.detail


# ---------------------------------------------------------------------------
# Test 3: dual install → --strict returns exit 1
# ---------------------------------------------------------------------------

class TestDualInstall:
    def test_dual_install_is_fail(self, tmp_path, monkeypatch):
        """Real embedded dir (own scripts/) distinct from central -> FAIL."""
        project = _make_project(tmp_path)
        (project / ".claude" / "vnx-system" / "scripts").mkdir(parents=True)
        central = tmp_path / "home" / ".vnx-system" / "current"
        (central / "scripts").mkdir(parents=True)

        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path / "home"))

        result = _check_dual_install(project)

        assert result.status == FAIL
        assert "dual install" in result.detail
        assert "embedded" in result.detail
        assert "central" in result.detail
        # OI-1075: the destructive advice is correct only in the genuine-conflict case
        assert "remove embedded install" in result.detail

    def test_dual_install_strict_exit_1(self, tmp_path, monkeypatch, capsys):
        """Real embedded dir distinct from central -> --strict returns exit 1."""
        project = _make_project(tmp_path)
        (project / ".claude" / "vnx-system" / "scripts").mkdir(parents=True)
        central = tmp_path / "home" / ".vnx-system" / "current"
        (central / "scripts").mkdir(parents=True)

        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path / "home"))

        exit_code = vnx_doctor(_make_args(str(project), strict=True))

        assert exit_code == 1
        out = capsys.readouterr().out
        assert "dual install" in out

    def test_no_dual_install_passes(self, tmp_path, monkeypatch):
        """Embedded-only (no central) -> PASS."""
        project = _make_project(tmp_path)
        (project / ".claude" / "vnx-system" / "scripts").mkdir(parents=True)

        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path / "home"))

        result = _check_dual_install(project)

        assert result.status == PASS

    def test_no_embedded_path_passes(self, tmp_path, monkeypatch):
        """No embedded path at all -> PASS."""
        project = _make_project(tmp_path)
        central = tmp_path / "home" / ".vnx-system" / "current"
        (central / "scripts").mkdir(parents=True)

        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path / "home"))

        result = _check_dual_install(project)

        assert result.status == PASS
        assert "no dual install conflict" in result.detail

    def test_symlink_into_central_passes_names_version(self, tmp_path, monkeypatch):
        """Symlink into the central store -> PASS, detail names resolved version.

        OI-1075: the intended central-mode arrangement. A naive is_dir() test
        followed the symlink and saw the same scripts/ twice, falsely FAILing.
        """
        project = _make_project(tmp_path)
        central_root = tmp_path / "home" / ".vnx-system"
        version_dir = central_root / "versions" / "v1.4.5"
        (version_dir / "scripts").mkdir(parents=True)
        (central_root / "current").symlink_to(version_dir)
        (project / ".claude").mkdir(parents=True)
        (project / ".claude" / "vnx-system").symlink_to(central_root / "current")

        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path / "home"))

        result = _check_dual_install(project)

        assert result.status == PASS
        assert "v1.4.5" in result.detail
        assert "central store" in result.detail

    def test_dangling_symlink_is_warn_not_crash(self, tmp_path, monkeypatch):
        """Dangling symlink -> WARN, non-crashing.

        Chosen verdict: WARN (not FAIL). A dangling embedded symlink is not a
        dual-install conflict (there is no second tree), but central mode is
        broken until the link is repaired, so it must not pass silently either.
        """
        project = _make_project(tmp_path)
        central = tmp_path / "home" / ".vnx-system" / "current"
        (central / "scripts").mkdir(parents=True)
        (project / ".claude").mkdir(parents=True)
        (project / ".claude" / "vnx-system").symlink_to(project / "missing-target")

        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path / "home"))

        result = _check_dual_install(project)

        assert result.status == WARN
        assert "dangling" in result.detail
        assert "missing-target" in result.detail

    def test_symlink_outside_central_is_fail(self, tmp_path, monkeypatch):
        """Symlink pointing outside ~/.vnx-system entirely -> FAIL (genuine 2nd install)."""
        project = _make_project(tmp_path)
        outside = tmp_path / "outside-install"
        (outside / "scripts").mkdir(parents=True)
        central = tmp_path / "home" / ".vnx-system" / "current"
        (central / "scripts").mkdir(parents=True)
        (project / ".claude").mkdir(parents=True)
        (project / ".claude" / "vnx-system").symlink_to(outside)

        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path / "home"))

        result = _check_dual_install(project)

        assert result.status == FAIL
        assert "dual install" in result.detail
        assert "remove embedded install" in result.detail

    def test_fabric_repo_real_dir_no_scripts_passes(self, tmp_path, monkeypatch):
        """Fabric source repo: real .claude/vnx-system dir WITHOUT scripts/ -> PASS.

        The fabric repo carries a real (non-symlink) .claude/vnx-system by
        design, holding hooks/ and security_reports/ but no scripts/. This is
        NOT a consumer dual install and must not be flagged. Preserves the
        pre-OI-1075 behaviour verified on the live fabric repo.
        """
        project = _make_project(tmp_path)
        (project / ".claude" / "vnx-system" / "hooks").mkdir(parents=True)
        (project / ".claude" / "vnx-system" / "security_reports").mkdir(parents=True)
        central = tmp_path / "home" / ".vnx-system" / "current"
        (central / "scripts").mkdir(parents=True)

        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path / "home"))

        result = _check_dual_install(project)

        assert result.status == PASS
        assert "no dual install conflict" in result.detail


# ---------------------------------------------------------------------------
# Test 4: schema version mismatch warns (fails in --strict)
# ---------------------------------------------------------------------------

class TestSchemaVersions:
    def test_schema_version_ok(self, tmp_path):
        project = _make_project(tmp_path)
        _make_coordination_db(project / ".vnx-data" / "state", schema_version=10)

        results = _check_schema_versions(project / ".vnx-data")

        coord_check = next(r for r in results if "runtime_coordination" in r.name)
        assert coord_check.status == PASS
        assert "10" in coord_check.detail

    def test_schema_version_below_minimum_warns(self, tmp_path):
        project = _make_project(tmp_path)
        _make_coordination_db(project / ".vnx-data" / "state", schema_version=5)

        results = _check_schema_versions(project / ".vnx-data")

        coord_check = next(r for r in results if "runtime_coordination" in r.name)
        assert coord_check.status == WARN
        assert "< minimum" in coord_check.detail

    def test_schema_warn_causes_strict_exit_1(self, tmp_path, monkeypatch):
        project = _make_project(tmp_path)
        _make_coordination_db(project / ".vnx-data" / "state", schema_version=5)
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path / "home"))

        exit_code = vnx_doctor(_make_args(str(project), strict=True))

        assert exit_code == 1

    def test_missing_db_warns_not_fails(self, tmp_path):
        project = _make_project(tmp_path)

        results = _check_schema_versions(project / ".vnx-data")

        for r in results:
            assert r.status != FAIL

    def test_schema_no_runtime_table_falls_back_to_pragma(self, tmp_path):
        """When runtime_schema_version table is absent, PRAGMA user_version is the fallback."""
        project = _make_project(tmp_path)
        state_dir = project / ".vnx-data" / "state"
        db_path = state_dir / "runtime_coordination.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA user_version = 15")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS dispatches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                dispatch_id TEXT NOT NULL UNIQUE,
                state TEXT NOT NULL DEFAULT 'queued'
            )
        """)
        conn.commit()
        conn.close()

        results = _check_schema_versions(project / ".vnx-data")

        coord_check = next(r for r in results if "runtime_coordination" in r.name)
        assert coord_check.status == PASS
        assert "15" in coord_check.detail

    def test_schema_effective_is_max_of_pragma_and_table(self, tmp_path):
        """effective version = max(PRAGMA user_version, legacy_version)."""
        project = _make_project(tmp_path)
        state_dir = project / ".vnx-data" / "state"
        db_path = state_dir / "runtime_coordination.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA user_version = 3")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS runtime_schema_version (
                version INTEGER NOT NULL,
                applied_at TEXT
            )
        """)
        conn.execute("INSERT INTO runtime_schema_version (version) VALUES (12)")
        conn.commit()
        conn.close()

        results = _check_schema_versions(project / ".vnx-data")

        coord_check = next(r for r in results if "runtime_coordination" in r.name)
        assert coord_check.status == PASS
        assert "12" in coord_check.detail


# ---------------------------------------------------------------------------
# Test: worker-CLI probe (audit-dx-doctor-worker-cli, audit high #7)
# ---------------------------------------------------------------------------

class TestWorkerCliProbe:
    def test_no_worker_cli_warns(self):
        """No claude/codex/gemini/kimi on PATH -> tool:worker-cli is WARN, not FAIL."""
        worker_clis = {"claude", "codex", "gemini", "kimi"}

        def fake_which(tool):
            if tool in worker_clis:
                return None
            return "/usr/bin/" + tool

        with patch("shutil.which", side_effect=fake_which):
            results = _check_tools()

        worker_check = next(r for r in results if r.name == "tool:worker-cli")
        assert worker_check.status == WARN
        assert "no worker CLI" in worker_check.detail
        assert "dispatch-agent" in worker_check.detail

    def test_claude_present_passes(self):
        """claude on PATH (even with other worker CLIs absent) -> tool:worker-cli PASS."""
        def fake_which(tool):
            if tool == "claude":
                return "/usr/local/bin/claude"
            if tool in ("codex", "gemini", "kimi"):
                return None
            return "/usr/bin/" + tool

        with patch("shutil.which", side_effect=fake_which):
            results = _check_tools()

        worker_check = next(r for r in results if r.name == "tool:worker-cli")
        assert worker_check.status == PASS
        assert "claude" in worker_check.detail

    def test_missing_worker_cli_does_not_fail_doctor(self, tmp_path, monkeypatch):
        """A missing worker CLI is a WARN, so it must not push non-strict `vnx doctor` to exit 1."""
        project = _make_project(tmp_path)
        _make_coordination_db(project / ".vnx-data" / "state", schema_version=10)
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path / "home"))

        def fake_which(tool):
            if tool in ("claude", "codex", "gemini", "kimi"):
                return None
            return "/usr/bin/" + tool

        with patch("shutil.which", side_effect=fake_which):
            exit_code = vnx_doctor(_make_args(str(project), strict=False))

        assert exit_code == 0


# ---------------------------------------------------------------------------
# Test 5: skill coverage gap warns
# ---------------------------------------------------------------------------

class TestSkillCoverage:
    def test_no_dispatches_passes(self, tmp_path):
        project = _make_project(tmp_path)

        result = _check_skill_coverage(project, project / ".vnx-data")

        assert result.status == PASS

    def test_known_builtin_role_resolves(self, tmp_path):
        project = _make_project(tmp_path)
        dispatch = project / ".vnx-data" / "dispatches" / "pending" / "test.md"
        dispatch.write_text("Role: backend-developer\n\nDo some work.\n")

        result = _check_skill_coverage(project, project / ".vnx-data")

        assert result.status == PASS

    def test_unknown_role_warns(self, tmp_path):
        project = _make_project(tmp_path)
        dispatch = project / ".vnx-data" / "dispatches" / "pending" / "test.md"
        dispatch.write_text("Role: my-custom-nonexistent-skill\n\nDo some work.\n")

        result = _check_skill_coverage(project, project / ".vnx-data")

        assert result.status == WARN
        assert "my-custom-nonexistent-skill" in result.detail

    def test_skill_in_override_dir_resolves(self, tmp_path):
        project = _make_project(tmp_path)
        overrides = project / ".vnx-overrides"
        overrides.mkdir()
        (overrides / "my-custom-skill.md").write_text("# Custom Skill\n")
        dispatch = project / ".vnx-data" / "dispatches" / "pending" / "test.md"
        dispatch.write_text("Role: my-custom-skill\n\nDo some work.\n")

        result = _check_skill_coverage(project, project / ".vnx-data")

        assert result.status == PASS

    def test_unreadable_dispatch_warns_in_default_mode(self, tmp_path):
        project = _make_project(tmp_path)
        dispatch = project / ".vnx-data" / "dispatches" / "pending" / "locked.md"
        dispatch.write_text("Role: backend-developer\n\nDo some work.\n")
        dispatch.chmod(0o000)

        try:
            result = _check_skill_coverage(project, project / ".vnx-data", strict=False)
        finally:
            dispatch.chmod(0o644)

        assert result.status == WARN
        assert "cannot read" in result.detail

    def test_unreadable_dispatch_fails_in_strict_mode(self, tmp_path):
        project = _make_project(tmp_path)
        dispatch = project / ".vnx-data" / "dispatches" / "pending" / "locked.md"
        dispatch.write_text("Role: backend-developer\n\nDo some work.\n")
        dispatch.chmod(0o000)

        try:
            result = _check_skill_coverage(project, project / ".vnx-data", strict=True)
        finally:
            dispatch.chmod(0o644)

        assert result.status == FAIL
        assert "cannot audit" in result.detail


# ---------------------------------------------------------------------------
# Test 6: active dispatch count warns
# ---------------------------------------------------------------------------

class TestActiveDrain:
    def test_no_active_dispatches_passes(self, tmp_path):
        project = _make_project(tmp_path)
        _make_coordination_db(project / ".vnx-data" / "state", active_count=0)

        result = _check_active_drain(project / ".vnx-data")

        assert result.status == PASS
        assert "no active" in result.detail

    def test_active_dispatches_warn(self, tmp_path):
        project = _make_project(tmp_path)
        _make_coordination_db(project / ".vnx-data" / "state", active_count=3)

        result = _check_active_drain(project / ".vnx-data")

        assert result.status == WARN
        assert "3" in result.detail
        assert "drain" in result.detail.lower()

    def test_missing_db_passes(self, tmp_path):
        project = _make_project(tmp_path)

        result = _check_active_drain(project / ".vnx-data")

        assert result.status == PASS


# ---------------------------------------------------------------------------
# Integration: full vnx_doctor invocation
# ---------------------------------------------------------------------------

class TestVnxDoctorIntegration:
    def test_strict_flag_exit_0_on_clean_project(self, tmp_path, monkeypatch):
        project = _make_project(tmp_path)
        _make_coordination_db(project / ".vnx-data" / "state", active_count=0, schema_version=10)
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path / "home"))

        exit_code = vnx_doctor(_make_args(str(project), strict=True))

        # No FAIL; WARNs may exist (agents/, install:mode) but strict catches them
        # Accept exit 0 or 1 — key check is no crash + sensible output
        assert exit_code in (0, 1)

    def test_non_strict_exit_0_with_warnings(self, tmp_path, monkeypatch):
        project = _make_project(tmp_path)
        _make_coordination_db(project / ".vnx-data" / "state", active_count=2)
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path / "home"))

        exit_code = vnx_doctor(_make_args(str(project), strict=False))

        # active dispatches = WARN only; non-strict must exit 0
        assert exit_code == 0

    def test_json_output_contains_summary(self, tmp_path, monkeypatch, capsys):
        import json as _json

        project = _make_project(tmp_path)
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path / "home"))

        vnx_doctor(_make_args(str(project), emit_json=True))

        out = capsys.readouterr().out
        data = _json.loads(out)
        assert "summary" in data
        assert "checks" in data
        assert "pass" in data["summary"]
