#!/usr/bin/env python3
"""Tests for VNX Mode — detection, storage, and command gating (PR-2).

Validates mode persistence, command tier enforcement, feature flags,
and backward compatibility with pre-init state.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPT_DIR / "lib"))

from vnx_mode import (
    VNXMode,
    ModeGateError,
    TIER_UNIVERSAL,
    TIER_STARTER_OPERATOR,
    TIER_OPERATOR_ONLY,
    MODE_COMMANDS,
    read_mode,
    write_mode,
    read_mode_raw,
    check_command_allowed,
    get_available_commands,
    get_mode_description,
    is_feature_enabled,
    check_mode_feature_enabled,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def data_dir(tmp_path):
    """Create a temp .vnx-data directory and set VNX_DATA_DIR (+ explicit flag).

    VNX_DATA_DIR_EXPLICIT=1 is required by the two-key contract (OI-911): a bare
    VNX_DATA_DIR is inherited pollution and is ignored by ``_mode_file_path``.
    """
    d = tmp_path / ".vnx-data"
    d.mkdir()
    old = os.environ.get("VNX_DATA_DIR")
    old_explicit = os.environ.get("VNX_DATA_DIR_EXPLICIT")
    os.environ["VNX_DATA_DIR"] = str(d)
    os.environ["VNX_DATA_DIR_EXPLICIT"] = "1"
    yield d
    if old:
        os.environ["VNX_DATA_DIR"] = old
    else:
        os.environ.pop("VNX_DATA_DIR", None)
    if old_explicit:
        os.environ["VNX_DATA_DIR_EXPLICIT"] = old_explicit
    else:
        os.environ.pop("VNX_DATA_DIR_EXPLICIT", None)


# ---------------------------------------------------------------------------
# Mode read/write
# ---------------------------------------------------------------------------

class TestModeReadWrite:
    def test_read_mode_returns_none_when_no_file(self, data_dir):
        assert read_mode(str(data_dir)) is None

    def test_write_and_read_starter(self, data_dir):
        write_mode(VNXMode.STARTER, str(data_dir))
        assert read_mode(str(data_dir)) == VNXMode.STARTER

    def test_write_and_read_operator(self, data_dir):
        write_mode(VNXMode.OPERATOR, str(data_dir))
        assert read_mode(str(data_dir)) == VNXMode.OPERATOR

    def test_mode_file_is_valid_json(self, data_dir):
        write_mode(VNXMode.STARTER, str(data_dir))
        mode_file = data_dir / "mode.json"
        data = json.loads(mode_file.read_text())
        assert data["mode"] == "starter"
        assert data["schema_version"] == 1
        assert "set_at" in data

    def test_read_mode_raw(self, data_dir):
        write_mode(VNXMode.OPERATOR, str(data_dir))
        raw = read_mode_raw(str(data_dir))
        assert raw["mode"] == "operator"
        assert "set_at" in raw

    def test_read_mode_handles_corrupt_json(self, data_dir):
        (data_dir / "mode.json").write_text("not json")
        assert read_mode(str(data_dir)) is None

    def test_read_mode_handles_missing_key(self, data_dir):
        (data_dir / "mode.json").write_text('{"schema_version": 1}')
        assert read_mode(str(data_dir)) is None

    def test_write_mode_overwrites(self, data_dir):
        write_mode(VNXMode.STARTER, str(data_dir))
        assert read_mode(str(data_dir)) == VNXMode.STARTER
        write_mode(VNXMode.OPERATOR, str(data_dir))
        assert read_mode(str(data_dir)) == VNXMode.OPERATOR

    def test_read_mode_from_env(self, data_dir):
        """read_mode() without args uses VNX_DATA_DIR from env."""
        write_mode(VNXMode.STARTER, str(data_dir))
        assert read_mode() == VNXMode.STARTER


# ---------------------------------------------------------------------------
# OI-911 write guard — mode.json must never land in the wrong store
# ---------------------------------------------------------------------------

class TestModeWriteGuard:
    """mode.json writes resolve through the two-key contract and a write guard.

    OI-911: ``_mode_file_path`` used to read ``os.environ["VNX_DATA_DIR"]`` raw.
    A suite-wide test run pinned a scratch pad with ``VNX_DATA_DIR_EXPLICIT=1``;
    a cleaned-env subprocess lost the flag and its mode.json write silently fell
    back to the resolved central store. Every test here is RED on origin/main:
    the raw-env resolver wrote to the guard-identified target instead of
    refusing.
    """

    def _write_mode_script(self) -> str:
        """Resolve the data dir through the fabric resolver, then write mode.

        Mirrors ``vnx_setup.step_write_mode`` / ``vnx_starter.init_starter``:
        ``ensure_env()`` resolves ``VNX_DATA_DIR`` (ignoring a bare env value),
        then ``write_mode`` receives that resolved path as ``data_dir``.
        """
        return (
            "import sys; sys.path.insert(0, sys.argv[1]);\n"
            "from vnx_paths import ensure_env;\n"
            "from vnx_mode import VNXMode, write_mode;\n"
            "paths = ensure_env();\n"
            "write_mode(VNXMode.STARTER, paths['VNX_DATA_DIR']);\n"
        )

    def test_write_diverges_from_bare_vnx_data_dir_raises(self, tmp_path, monkeypatch):
        """A write target that differs from a bare inherited VNX_DATA_DIR fails loud."""
        scratch = tmp_path / "scratch"
        scratch.mkdir()
        other = tmp_path / "other"
        other.mkdir()
        monkeypatch.setenv("VNX_DATA_DIR", str(scratch))
        monkeypatch.delenv("VNX_DATA_DIR_EXPLICIT", raising=False)
        monkeypatch.setenv("VNX_DATA_DIR_GUARD", "off")
        with pytest.raises(RuntimeError, match="VNX_DATA_DIR_EXPLICIT"):
            write_mode(VNXMode.STARTER, str(other))

    def test_write_to_other_project_central_store_raises(self, tmp_path, monkeypatch):
        """A write into ~/.vnx-data/<other> while the active project is <pid> fails loud."""
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setenv("VNX_PROJECT_ID", "vnx-dev")
        monkeypatch.setenv("VNX_DATA_DIR_GUARD", "off")
        other = home / ".vnx-data" / "other-project"
        other.mkdir(parents=True)
        with pytest.raises(RuntimeError, match="another project"):
            write_mode(VNXMode.STARTER, str(other))

    def test_test_isolation_guard_fires_when_project_id_unresolvable(
        self, tmp_path, monkeypatch
    ):
        """w22/PR#1333: the test-isolation guard must fire even when
        resolve_project_id() cannot resolve a project_id at all — exactly the
        subprocess-with-a-cleaned-env scenario the guard exists for (no
        VNX_PROJECT_ID, no reachable .vnx-project-id marker, no git remote).

        Before the fix, ``_guard_mode_write_target`` returned on the
        ``except RuntimeError: return`` branch before ever calling
        ``refuse_real_central_store_write_under_pytest``, silently allowing
        the write to land under the (mocked) real central store. RED on
        43600f56.
        """
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        no_git_cwd = tmp_path / "no_git_cwd"
        no_git_cwd.mkdir()
        central = fake_home / ".vnx-data" / "vnx-dev"
        central.mkdir(parents=True)

        monkeypatch.setenv("HOME", str(fake_home))
        monkeypatch.delenv("VNX_PROJECT_ID", raising=False)
        monkeypatch.setenv("VNX_DATA_DIR_GUARD", "off")
        monkeypatch.chdir(no_git_cwd)

        # Sanity: this scenario really makes resolve_project_id() fail —
        # otherwise this test would pass for the wrong reason (hitting a
        # different guard branch instead of the RuntimeError-early-return gap).
        from project_root import resolve_project_id
        with pytest.raises(RuntimeError):
            resolve_project_id()

        with pytest.raises(RuntimeError, match="TEST ISOLATION GUARD"):
            write_mode(VNXMode.STARTER, str(central))

        assert not (central / "mode.json").exists()

    def test_cleaned_env_subprocess_does_not_write_real_store(self, tmp_path):
        """OI-911 regression: a subprocess that loses VNX_DATA_DIR_EXPLICIT must
        not write mode.json into the resolved central store.

        The parent test run pins a scratch pad with VNX_DATA_DIR_EXPLICIT=1 (the
        suite-wide pytest run does this). The subprocess keeps the scratch
        VNX_DATA_DIR but loses the EXPLICIT flag (a cleaned env), then resolves
        its data dir through the fabric resolver, which falls back to the central
        store. The mode.json write must NOT land there.
        """
        scratch = tmp_path / "scratch"
        scratch.mkdir()
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        pid = "vnx-dev"
        central = fake_home / ".vnx-data" / pid
        central.mkdir(parents=True)
        original = json.dumps({"mode": "operator", "schema_version": 1}, indent=2)
        (central / "mode.json").write_text(original, encoding="utf-8")

        env = {
            "HOME": str(fake_home),
            "VNX_PROJECT_ID": pid,
            "VNX_DATA_DIR": str(scratch),  # bare — EXPLICIT deliberately removed
            "VNX_DATA_DIR_GUARD": "off",
            "PATH": os.environ.get("PATH", ""),
        }
        result = subprocess.run(
            [sys.executable, "-c", self._write_mode_script(), str(SCRIPT_DIR / "lib")],
            env=env, capture_output=True, text=True, timeout=60,
        )
        # The write must not land in the (fake) central store.
        assert (central / "mode.json").read_text(encoding="utf-8") == original, (
            "cleaned-env subprocess wrote mode.json into the resolved central store "
            f"(rc={result.returncode})\nstdout: {result.stdout}\nstderr: {result.stderr}"
        )


# ---------------------------------------------------------------------------
# Command gating
# ---------------------------------------------------------------------------

class TestCommandGating:
    def test_universal_commands_allowed_in_all_modes(self, data_dir):
        for mode in VNXMode:
            for cmd in TIER_UNIVERSAL:
                check_command_allowed(cmd, mode)  # Should not raise

    def test_starter_allows_tier2(self, data_dir):
        for cmd in TIER_STARTER_OPERATOR:
            check_command_allowed(cmd, VNXMode.STARTER)  # Should not raise

    def test_starter_blocks_operator_commands(self, data_dir):
        for cmd in TIER_OPERATOR_ONLY:
            with pytest.raises(ModeGateError) as exc:
                check_command_allowed(cmd, VNXMode.STARTER)
            assert "starter" in str(exc.value)
            assert "vnx init --operator" in str(exc.value)

    def test_operator_allows_everything(self, data_dir):
        all_cmds = TIER_UNIVERSAL | TIER_STARTER_OPERATOR | TIER_OPERATOR_ONLY
        for cmd in all_cmds:
            check_command_allowed(cmd, VNXMode.OPERATOR)

    def test_pre_init_allows_everything(self, data_dir):
        """No mode.json = pre-init state, all commands allowed."""
        check_command_allowed("start")  # Should not raise
        check_command_allowed("dispatch")

    def test_gating_reads_from_file(self, data_dir):
        write_mode(VNXMode.STARTER, str(data_dir))
        with pytest.raises(ModeGateError):
            check_command_allowed("start")  # reads mode from file


# ---------------------------------------------------------------------------
# Available commands
# ---------------------------------------------------------------------------

class TestAvailableCommands:
    def test_starter_command_count(self):
        cmds = get_available_commands(VNXMode.STARTER)
        assert cmds == TIER_UNIVERSAL | TIER_STARTER_OPERATOR

    def test_operator_has_most_commands(self):
        starter = get_available_commands(VNXMode.STARTER)
        operator = get_available_commands(VNXMode.OPERATOR)
        assert len(operator) > len(starter)

    def test_pre_init_returns_all(self, data_dir):
        cmds = get_available_commands(None)
        assert TIER_UNIVERSAL <= cmds
        assert TIER_OPERATOR_ONLY <= cmds


# ---------------------------------------------------------------------------
# Feature flags
# ---------------------------------------------------------------------------

class TestFeatureFlags:
    def test_starter_enabled_by_default(self):
        os.environ.pop("VNX_STARTER_MODE_ENABLED", None)
        assert check_mode_feature_enabled(VNXMode.STARTER) is True

    def test_starter_disabled_by_flag(self):
        os.environ["VNX_STARTER_MODE_ENABLED"] = "0"
        assert check_mode_feature_enabled(VNXMode.STARTER) is False
        os.environ.pop("VNX_STARTER_MODE_ENABLED")

    def test_operator_always_enabled(self):
        assert check_mode_feature_enabled(VNXMode.OPERATOR) is True

    def test_mode_gating_flag(self):
        os.environ.pop("VNX_MODE_GATING_ENABLED", None)
        assert is_feature_enabled("VNX_MODE_GATING_ENABLED") is True
        os.environ["VNX_MODE_GATING_ENABLED"] = "0"
        assert is_feature_enabled("VNX_MODE_GATING_ENABLED") is False
        os.environ.pop("VNX_MODE_GATING_ENABLED")


# ---------------------------------------------------------------------------
# Mode descriptions
# ---------------------------------------------------------------------------

class TestModeDescriptions:
    def test_all_modes_have_descriptions(self):
        for mode in VNXMode:
            desc = get_mode_description(mode)
            assert len(desc) > 10

    def test_vnx_mode_str(self):
        assert str(VNXMode.STARTER) == "starter"
        assert str(VNXMode.OPERATOR) == "operator"


# ---------------------------------------------------------------------------
# Tier completeness
# ---------------------------------------------------------------------------

class TestTierCompleteness:
    def test_no_overlap_between_exclusive_tiers(self):
        assert TIER_UNIVERSAL & TIER_OPERATOR_ONLY == frozenset()
        assert TIER_STARTER_OPERATOR & TIER_OPERATOR_ONLY == frozenset()

    def test_operator_mode_includes_universal_and_starter(self):
        op = MODE_COMMANDS[VNXMode.OPERATOR]
        assert TIER_UNIVERSAL <= op
        assert TIER_STARTER_OPERATOR <= op
        assert TIER_OPERATOR_ONLY <= op
