"""Tests for per-worker .vnx-data/workers/<terminal_id>/ isolation (PR-6.5e)."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "lib"))

from vnx_paths import resolve_worker_state_dir


@pytest.fixture
def tmp_vnx_data(tmp_path):
    """Provide a temporary .vnx-data directory."""
    vnx_data = tmp_path / ".vnx-data"
    vnx_data.mkdir()
    return vnx_data


class TestResolveWorkerStateDir:
    def test_creates_directory_on_demand(self, tmp_vnx_data):
        result = resolve_worker_state_dir("T1", vnx_data_dir=tmp_vnx_data)
        assert result.exists()
        assert result.is_dir()
        assert result == (tmp_vnx_data / "workers" / "T1").resolve()

    def test_isolation_between_terminals(self, tmp_vnx_data):
        t1 = resolve_worker_state_dir("T1", vnx_data_dir=tmp_vnx_data)
        t2 = resolve_worker_state_dir("T2", vnx_data_dir=tmp_vnx_data)
        t3 = resolve_worker_state_dir("T3", vnx_data_dir=tmp_vnx_data)

        assert t1 != t2
        assert t2 != t3
        assert t1.name == "T1"
        assert t2.name == "T2"
        assert t3.name == "T3"

    def test_idempotent_on_existing_dir(self, tmp_vnx_data):
        first = resolve_worker_state_dir("T1", vnx_data_dir=tmp_vnx_data)
        marker = first / "state.json"
        marker.write_text("{}")
        second = resolve_worker_state_dir("T1", vnx_data_dir=tmp_vnx_data)
        assert second == first
        assert marker.exists()

    def test_worker_writes_isolated(self, tmp_vnx_data):
        t1_dir = resolve_worker_state_dir("T1", vnx_data_dir=tmp_vnx_data)
        t2_dir = resolve_worker_state_dir("T2", vnx_data_dir=tmp_vnx_data)

        (t1_dir / "dispatch.log").write_text("T1 log entry")
        (t2_dir / "dispatch.log").write_text("T2 log entry")

        assert (t1_dir / "dispatch.log").read_text() == "T1 log entry"
        assert (t2_dir / "dispatch.log").read_text() == "T2 log entry"

    def test_rejects_empty_terminal_id(self, tmp_vnx_data):
        with pytest.raises(ValueError, match="non-empty"):
            resolve_worker_state_dir("", vnx_data_dir=tmp_vnx_data)

    def test_rejects_whitespace_only_terminal_id(self, tmp_vnx_data):
        with pytest.raises(ValueError, match="non-empty"):
            resolve_worker_state_dir("   ", vnx_data_dir=tmp_vnx_data)

    def test_rejects_path_traversal(self, tmp_vnx_data):
        with pytest.raises(ValueError, match="path separators"):
            resolve_worker_state_dir("../etc", vnx_data_dir=tmp_vnx_data)

    def test_rejects_slash_in_terminal_id(self, tmp_vnx_data):
        with pytest.raises(ValueError, match="path separators"):
            resolve_worker_state_dir("T1/malicious", vnx_data_dir=tmp_vnx_data)

    def test_rejects_backslash_in_terminal_id(self, tmp_vnx_data):
        with pytest.raises(ValueError, match="path separators"):
            resolve_worker_state_dir("T1\\bad", vnx_data_dir=tmp_vnx_data)

    def test_uses_resolve_paths_when_no_vnx_data_dir(self, tmp_vnx_data):
        mock_paths = {"VNX_DATA_DIR": str(tmp_vnx_data)}
        with patch("vnx_paths.resolve_paths", return_value=mock_paths):
            result = resolve_worker_state_dir("T2")
        assert result.exists()
        assert result == (tmp_vnx_data / "workers" / "T2").resolve()

    def test_parent_workers_dir_created(self, tmp_vnx_data):
        resolve_worker_state_dir("T1", vnx_data_dir=tmp_vnx_data)
        workers_dir = tmp_vnx_data / "workers"
        assert workers_dir.exists()
        assert workers_dir.is_dir()


class TestDeliveryEnvInjection:
    """Verify that deliver_via_subprocess passes VNX_WORKER_STATE_DIR to spawn_claude."""

    def test_extra_env_contains_worker_state_dir(self, tmp_vnx_data):
        captured_env = {}

        def fake_spawn_claude(prompt, model, dispatch_id, terminal_id, **kwargs):
            extra_env = kwargs.get("extra_env")
            if extra_env:
                captured_env.update(extra_env)
            from provider_spawns.claude_spawn import ClaudeSpawnResult
            return ClaudeSpawnResult(
                returncode=0,
                completion={},
                events_written=0,
                session_id=None,
                timed_out=False,
                _adapter=None,
            )

        mock_paths = {"VNX_DATA_DIR": str(tmp_vnx_data)}

        with (
            patch("vnx_paths.resolve_paths", return_value=mock_paths),
            patch(
                "provider_spawns.claude_spawn.spawn_claude",
                side_effect=fake_spawn_claude,
            ),
            patch(
                "subprocess_dispatch_internals.delivery._build_worker_identity_env",
                return_value={},
            ),
            patch(
                "subprocess_dispatch_internals.delivery._apply_runtime_overrides",
                return_value=(300.0, 900.0),
            ),
            patch(
                "subprocess_dispatch_internals.delivery._prepare_dispatch",
                return_value=("instruction", None, None, "/tmp/manifest"),
            ),
            patch(
                "subprocess_dispatch_internals.delivery._load_resume_session",
                return_value=None,
            ),
            patch(
                "subprocess_dispatch_internals.delivery._start_heartbeat",
                return_value=(None, None),
            ),
        ):
            from subprocess_dispatch_internals.delivery import deliver_via_subprocess

            deliver_via_subprocess(
                terminal_id="T1",
                instruction="test",
                model="sonnet",
                dispatch_id="test-dispatch",
            )

        assert "VNX_WORKER_STATE_DIR" in captured_env
        expected = str((tmp_vnx_data / "workers" / "T1").resolve())
        assert captured_env["VNX_WORKER_STATE_DIR"] == expected
