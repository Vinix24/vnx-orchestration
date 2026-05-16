"""test_vnx_pool_cli.py — Unit tests for vnx pool CLI subcommands.

Tests drive the CLI functions directly (cmd_* and main(argv=...)) with
PoolManager mocked so no real SQLite DB or subprocess is required.

Wave 6 PR-6.7 — vnx pool CLI.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import List
from unittest.mock import MagicMock, call, patch

import pytest

# Ensure vnx_cli and scripts/lib are importable.
_REPO_ROOT = Path(__file__).resolve().parent.parent
_LIB_DIR = _REPO_ROOT / "scripts" / "lib"
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))

from pool_decision_engine import Membership, PoolConfig, PoolDecision, PoolState  # noqa: E402
from pool_manager import ExecResult  # noqa: E402
from pool_reaper import ReapTarget  # noqa: E402
from vnx_cli.commands.pool import (  # noqa: E402
    cmd_config,
    cmd_reap,
    cmd_scale,
    cmd_status,
    main,
)


# ---------------------------------------------------------------------------
# Factories
# ---------------------------------------------------------------------------

def _make_config(
    pool_id: str = "default",
    min_workers: int = 1,
    max_workers: int = 6,
    policy: str = "queue_depth_v1",
) -> PoolConfig:
    return PoolConfig(
        pool_id=pool_id,
        min_workers=min_workers,
        max_workers=max_workers,
        scaling_policy=policy,
        provider_mix=["claude"],
        cooldown_seconds=60.0,
    )


def _make_state(queue_depth: int = 0) -> PoolState:
    return PoolState(queue_depth=queue_depth, last_scaled_at=None, now=1000.0)


def _make_member(tid: str = "T1", provider: str = "claude") -> Membership:
    return Membership(
        membership_id="m-001",
        terminal_id=tid,
        provider=provider,
        pool_role="backend-developer",
        status="active",
        joined_at=900.0,
        last_heartbeat=990.0,
    )


def _make_exec_result(
    spawned: List[str] = None,
    reaped: List[str] = None,
    errors: List[str] = None,
) -> ExecResult:
    decision = PoolDecision(action="noop", delta=0, reason="test")
    return ExecResult(
        decision=decision,
        spawned=spawned or [],
        reaped=reaped or [],
        errors=errors or [],
    )


# ---------------------------------------------------------------------------
# status tests
# ---------------------------------------------------------------------------

class TestCmdStatus:
    def _args(self, project: str = "test-proj", pool_id=None, json_out=False):
        args = MagicMock()
        args.project = project
        args.pool_id = pool_id
        args.json = json_out
        return args

    def test_status_outputs_pool_state(self, capsys):
        member = _make_member("WORKER-1")
        with patch("vnx_cli.commands.pool.PoolManager") as MockMgr:
            mgr = MockMgr.return_value
            mgr.load_state.return_value = (_make_config(), _make_state(queue_depth=3), [member])
            rc = cmd_status(self._args())
        assert rc == 0
        out = capsys.readouterr().out
        assert "Pool: default" in out
        assert "queue_depth_v1" in out
        assert "WORKER-1" in out
        assert "Queue depth: 3" in out

    def test_status_json_format(self, capsys):
        member = _make_member("WORKER-2", provider="litellm")
        with patch("vnx_cli.commands.pool.PoolManager") as MockMgr:
            mgr = MockMgr.return_value
            mgr.load_state.return_value = (_make_config(max_workers=4), _make_state(), [member])
            rc = cmd_status(self._args(json_out=True))
        assert rc == 0
        out = capsys.readouterr().out
        data = json.loads(out)
        assert data["pool_id"] == "default"
        assert data["max"] == 4
        assert data["current"] == 1
        assert data["members"][0]["terminal_id"] == "WORKER-2"
        assert data["members"][0]["provider"] == "litellm"

    def test_status_works_without_project(self, capsys):
        """--project defaults to 'default'; no error when omitted."""
        with patch("vnx_cli.commands.pool.PoolManager") as MockMgr:
            mgr = MockMgr.return_value
            mgr.load_state.return_value = (_make_config(), _make_state(), [])
            rc = main(argv=["status"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "Project: default" in out

    def test_status_with_pool_id_passed_to_manager(self):
        with patch("vnx_cli.commands.pool.PoolManager") as MockMgr:
            mgr = MockMgr.return_value
            mgr.load_state.return_value = (_make_config(pool_id="batch"), _make_state(), [])
            main(argv=["status", "--project", "proj-a", "--pool-id", "batch"])
        MockMgr.assert_called_once_with(project_id="proj-a", pool_id="batch")

    def test_status_empty_pool(self, capsys):
        with patch("vnx_cli.commands.pool.PoolManager") as MockMgr:
            mgr = MockMgr.return_value
            mgr.load_state.return_value = (_make_config(), _make_state(), [])
            rc = cmd_status(self._args())
        assert rc == 0
        out = capsys.readouterr().out
        assert "Current: 0" in out


# ---------------------------------------------------------------------------
# scale tests
# ---------------------------------------------------------------------------

class TestCmdScale:
    def _args(self, project="proj", pool_id=None, to=3):
        args = MagicMock()
        args.project = project
        args.pool_id = pool_id
        args.to = to
        return args

    def test_scale_invokes_execute_with_correct_decision(self, capsys):
        members = [_make_member("T1"), _make_member("T2")]
        with patch("vnx_cli.commands.pool.PoolManager") as MockMgr:
            mgr = MockMgr.return_value
            mgr.load_state.return_value = (_make_config(min_workers=1, max_workers=6), _make_state(), members)
            mgr.execute.return_value = _make_exec_result(spawned=["T3"])
            rc = cmd_scale(self._args(to=3))
        assert rc == 0
        call_args = mgr.execute.call_args[0][0]
        assert call_args.action == "scale_up"
        assert call_args.delta == 1
        assert "operator request" in call_args.reason

    def test_scale_rejects_target_outside_range(self, capsys):
        with patch("vnx_cli.commands.pool.PoolManager") as MockMgr:
            mgr = MockMgr.return_value
            mgr.load_state.return_value = (
                _make_config(min_workers=1, max_workers=4), _make_state(), []
            )
            rc = cmd_scale(self._args(to=10))
        assert rc == 1
        assert "outside" in capsys.readouterr().err

    def test_scale_below_min_rejected(self, capsys):
        with patch("vnx_cli.commands.pool.PoolManager") as MockMgr:
            mgr = MockMgr.return_value
            mgr.load_state.return_value = (
                _make_config(min_workers=2, max_workers=6), _make_state(), [_make_member()]
            )
            rc = cmd_scale(self._args(to=0))
        assert rc == 1

    def test_scale_zero_delta_skips(self, capsys):
        with patch("vnx_cli.commands.pool.PoolManager") as MockMgr:
            mgr = MockMgr.return_value
            mgr.load_state.return_value = (
                _make_config(), _make_state(), [_make_member("T1"), _make_member("T2")]
            )
            rc = cmd_scale(self._args(to=2))
        assert rc == 0
        mgr.execute.assert_not_called()
        assert "nothing to do" in capsys.readouterr().out

    def test_scale_down_uses_scale_down_action(self, capsys):
        members = [_make_member("T1"), _make_member("T2"), _make_member("T3")]
        with patch("vnx_cli.commands.pool.PoolManager") as MockMgr:
            mgr = MockMgr.return_value
            mgr.load_state.return_value = (_make_config(min_workers=1, max_workers=6), _make_state(), members)
            mgr.execute.return_value = _make_exec_result(reaped=["m-003"])
            rc = cmd_scale(self._args(to=2))
        assert rc == 0
        call_args = mgr.execute.call_args[0][0]
        assert call_args.action == "scale_down"
        assert call_args.delta == -1

    def test_scale_errors_return_nonzero(self, capsys):
        with patch("vnx_cli.commands.pool.PoolManager") as MockMgr:
            mgr = MockMgr.return_value
            mgr.load_state.return_value = (_make_config(), _make_state(), [])
            mgr.execute.return_value = _make_exec_result(spawned=[], errors=["spawn failed"])
            rc = cmd_scale(self._args(to=1))
        assert rc == 1


# ---------------------------------------------------------------------------
# config tests
# ---------------------------------------------------------------------------

class TestCmdConfig:
    def _args(self, project="proj", pool_id=None, min=None, max=None, policy=None, cooldown=None):
        args = MagicMock()
        args.project = project
        args.pool_id = pool_id
        args.min = min
        args.max = max
        args.policy = policy
        args.cooldown = cooldown
        return args

    def test_config_updates_min_max_policy_cooldown(self, capsys):
        with patch("vnx_cli.commands.pool.PoolManager") as MockMgr:
            mgr = MockMgr.return_value
            mgr.repo.get_config.return_value = _make_config()
            rc = cmd_config(self._args(min=2, max=8, policy="cost_aware_v1", cooldown=30.0))
        assert rc == 0
        mgr.repo.update_config.assert_called_once_with(
            "default",
            {"min_workers": 2, "max_workers": 8, "scaling_policy": "cost_aware_v1", "cooldown_seconds": 30.0},
        )

    def test_config_no_changes(self, capsys):
        with patch("vnx_cli.commands.pool.PoolManager") as MockMgr:
            mgr = MockMgr.return_value
            mgr.repo.get_config.return_value = _make_config()
            rc = cmd_config(self._args())
        assert rc == 0
        mgr.repo.update_config.assert_not_called()
        assert "No changes" in capsys.readouterr().out

    def test_config_unknown_pool_returns_error(self, capsys):
        with patch("vnx_cli.commands.pool.PoolManager") as MockMgr:
            mgr = MockMgr.return_value
            mgr.repo.get_config.return_value = None
            rc = cmd_config(self._args(max=8))
        assert rc == 1
        assert "not found" in capsys.readouterr().err

    def test_config_partial_update(self, capsys):
        with patch("vnx_cli.commands.pool.PoolManager") as MockMgr:
            mgr = MockMgr.return_value
            mgr.repo.get_config.return_value = _make_config()
            rc = cmd_config(self._args(max=10))
        assert rc == 0
        mgr.repo.update_config.assert_called_once_with("default", {"max_workers": 10})

    def test_config_rejects_negative_min(self, capsys):
        with patch("vnx_cli.commands.pool.PoolManager") as MockMgr:
            mgr = MockMgr.return_value
            mgr.repo.get_config.return_value = _make_config()
            rc = cmd_config(self._args(min=-1))
        assert rc == 1
        assert "--min must be >= 0" in capsys.readouterr().err
        mgr.repo.update_config.assert_not_called()

    def test_config_rejects_negative_max(self, capsys):
        with patch("vnx_cli.commands.pool.PoolManager") as MockMgr:
            mgr = MockMgr.return_value
            mgr.repo.get_config.return_value = _make_config()
            rc = cmd_config(self._args(max=-5))
        assert rc == 1
        assert "--max must be >= 0" in capsys.readouterr().err
        mgr.repo.update_config.assert_not_called()

    def test_config_rejects_invalid_policy(self, capsys):
        with patch("vnx_cli.commands.pool.PoolManager") as MockMgr:
            mgr = MockMgr.return_value
            mgr.repo.get_config.return_value = _make_config()
            rc = cmd_config(self._args(policy="nonexistent_policy"))
        assert rc == 1
        err = capsys.readouterr().err
        assert "invalid policy" in err
        assert "nonexistent_policy" in err
        mgr.repo.update_config.assert_not_called()

    def test_config_rejects_negative_cooldown(self, capsys):
        with patch("vnx_cli.commands.pool.PoolManager") as MockMgr:
            mgr = MockMgr.return_value
            mgr.repo.get_config.return_value = _make_config()
            rc = cmd_config(self._args(cooldown=-10.0))
        assert rc == 1
        assert "--cooldown must be >= 0" in capsys.readouterr().err
        mgr.repo.update_config.assert_not_called()

    def test_config_rejects_min_greater_than_max(self, capsys):
        with patch("vnx_cli.commands.pool.PoolManager") as MockMgr:
            mgr = MockMgr.return_value
            mgr.repo.get_config.return_value = _make_config(min_workers=2, max_workers=6)
            rc = cmd_config(self._args(min=8, max=4))
        assert rc == 1
        err = capsys.readouterr().err
        assert "min" in err and "max" in err and "invariant" in err
        mgr.repo.update_config.assert_not_called()


# ---------------------------------------------------------------------------
# reap tests
# ---------------------------------------------------------------------------

class TestCmdReap:
    def _args(self, project="proj", pool_id=None, force=False):
        args = MagicMock()
        args.project = project
        args.pool_id = pool_id
        args.force = force
        return args

    def test_reap_dry_run_default(self, capsys):
        member = _make_member("STUCK-1")
        member_stale = Membership(
            membership_id="m-stale",
            terminal_id="STUCK-1",
            provider="claude",
            pool_role="backend-developer",
            status="active",
            joined_at=0.0,
            last_heartbeat=0.0,
        )
        with patch("vnx_cli.commands.pool.PoolManager") as MockMgr:
            mgr = MockMgr.return_value
            mgr.load_state.return_value = (_make_config(), _make_state(), [member_stale])
            rc = cmd_reap(self._args(force=False))
        assert rc == 0
        mgr.reap_dead.assert_not_called()
        out = capsys.readouterr().out
        assert "dry-run" in out or "WARN" in out

    def test_reap_force_actually_reaps(self, capsys):
        targets = [
            ReapTarget(
                membership_id="m-001",
                terminal_id="T-DEAD",
                pid=None,
                reason="heartbeat_stale=250s>180s",
            )
        ]
        with patch("vnx_cli.commands.pool.PoolManager") as MockMgr:
            mgr = MockMgr.return_value
            mgr.reap_dead.return_value = targets
            rc = cmd_reap(self._args(force=True))
        assert rc == 0
        mgr.reap_dead.assert_called_once()
        out = capsys.readouterr().out
        assert "Reaped 1" in out
        assert "T-DEAD" in out

    def test_reap_dry_run_empty_when_no_candidates(self, capsys):
        with patch("vnx_cli.commands.pool.PoolManager") as MockMgr:
            mgr = MockMgr.return_value
            mgr.load_state.return_value = (_make_config(), _make_state(), [])
            rc = cmd_reap(self._args(force=False))
        assert rc == 0
        out = capsys.readouterr().out
        assert "no candidates" in out

    def test_reap_force_empty_result(self, capsys):
        with patch("vnx_cli.commands.pool.PoolManager") as MockMgr:
            mgr = MockMgr.return_value
            mgr.reap_dead.return_value = []
            rc = cmd_reap(self._args(force=True))
        assert rc == 0
        assert "Reaped 0" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# argparse / routing tests
# ---------------------------------------------------------------------------

class TestArgparseBehavior:
    def test_project_required_for_scale(self):
        with pytest.raises(SystemExit) as exc:
            main(argv=["scale", "--to", "3"])
        assert exc.value.code != 0

    def test_project_required_for_config(self):
        with pytest.raises(SystemExit) as exc:
            main(argv=["config", "--max", "5"])
        assert exc.value.code != 0

    def test_project_required_for_reap(self):
        with pytest.raises(SystemExit) as exc:
            main(argv=["reap"])
        assert exc.value.code != 0

    def test_to_required_for_scale(self):
        with pytest.raises(SystemExit) as exc:
            main(argv=["scale", "--project", "proj"])
        assert exc.value.code != 0

    def test_unknown_subcommand_exits_nonzero(self):
        with pytest.raises(SystemExit) as exc:
            main(argv=["bogus-cmd"])
        assert exc.value.code != 0
