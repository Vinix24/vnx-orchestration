#!/usr/bin/env python3
"""Tests for --deadline-seconds passthrough on `vnx dispatch-agent` (20260716-deadline-passthrough).

`vnx dispatch-agent` (the consumer-door, vnx_cli) never exposed a deadline knob — every
consumer build got the tmux-lane's hardcoded 3600s default (tmux_interactive_dispatch.py),
even though the bundle layer already supported it (`dispatch_bridge.stage_spec_bundle
(deadline_seconds=...)`). Only the CLI passthrough was missing.

Covers:
  1. vnx_dispatch_agent: no --deadline-seconds -> deliver_via_door receives
     deadline_seconds=None (unset = exact current behavior, resolved downstream to 3600).
  2. vnx_dispatch_agent: --deadline-seconds 7200 -> deliver_via_door receives
     deadline_seconds=7200.
  3. vnx_dispatch_agent: out-of-range values (< 300 or > 14400) hard-error BEFORE dispatch
     with a clear message; deliver_via_door is never called.
  4. vnx_dispatch_agent: boundary values 300 and 14400 are accepted.
  5. dispatch_bridge.deliver_via_door: threads deadline_seconds through to bridge_dispatch,
     defaulting None -> 3600 (byte-identical to pre-change behavior).
  6. dispatch_bridge.deliver_via_door: an explicit deadline_seconds reaches the staged
     dispatch-spec.json (the actual lane-call payload), end-to-end through bridge_dispatch
     -> stage_spec_bundle.
"""

from __future__ import annotations

import argparse
import json
import sys
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

SCRIPTS_LIB = REPO_ROOT / "scripts" / "lib"
if str(SCRIPTS_LIB) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_LIB))

from vnx_cli.commands.dispatch_agent import vnx_dispatch_agent
from vnx_cli.main import _register_dispatch_agent_subparser

import dispatch_bridge  # real module, scripts/lib already on sys.path


# ---------------------------------------------------------------------------
# 0. Argument parsing — the --deadline-seconds flag itself
# ---------------------------------------------------------------------------

def _build_dispatch_agent_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="vnx")
    subparsers = parser.add_subparsers(dest="command")
    _register_dispatch_agent_subparser(subparsers)
    return parser


class TestDeadlineSecondsArgParsing:
    def test_unset_defaults_to_none(self):
        args = _build_dispatch_agent_parser().parse_args(["dispatch-agent", "--agent", "x"])
        assert args.deadline_seconds is None

    def test_explicit_value_parses_as_int(self):
        args = _build_dispatch_agent_parser().parse_args(
            ["dispatch-agent", "--agent", "x", "--deadline-seconds", "7200"]
        )
        assert args.deadline_seconds == 7200

    def test_non_integer_value_rejected_by_argparse(self):
        with pytest.raises(SystemExit):
            _build_dispatch_agent_parser().parse_args(
                ["dispatch-agent", "--agent", "x", "--deadline-seconds", "not-a-number"]
            )


# ---------------------------------------------------------------------------
# Shared dispatch harness (mirrors test_dispatch_agent_lane_coercion.py)
# ---------------------------------------------------------------------------

def _make_agent(base: Path, name: str = "hello-world") -> Path:
    agent_dir = base / "examples" / name
    agent_dir.mkdir(parents=True)
    (agent_dir / "CLAUDE.md").write_text(f"# {name} agent")
    (agent_dir / "config.yaml").write_text(
        'governance_profile: minimal\ndefault_instruction: "Say hi"\n'
    )
    return agent_dir


def _run_dispatch_capturing_door_kwargs(tmp_path: Path, *, deadline_seconds=None):
    """Invoke vnx_dispatch_agent with deliver_via_door replaced so no worker is ever
    staged/spawned — a plan-level assertion on the kwargs it receives."""
    _make_agent(tmp_path)

    captured = {}

    def fake_door(legacy_fn, **kwargs):
        captured.update(kwargs)
        return True

    from vnx_cli import _engine
    with patch.object(_engine, "engine_root", return_value=tmp_path), \
         patch.object(dispatch_bridge, "deliver_via_door", side_effect=fake_door):
        args = Namespace(
            agent="hello-world", instruction=None, model=None,
            project_dir=str(tmp_path), deadline_seconds=deadline_seconds,
        )
        rc = vnx_dispatch_agent(args)

    return rc, captured


# ---------------------------------------------------------------------------
# 1-2. deadline_seconds threading through vnx_dispatch_agent -> deliver_via_door
# ---------------------------------------------------------------------------

class TestDeadlineSecondsThreadedToDoor:
    def test_unset_deadline_passes_none(self, tmp_path):
        """Unset (no --deadline-seconds) must pass None through, not silently invent 3600
        at this layer — the 3600 default lives one layer down (deliver_via_door)."""
        rc, captured = _run_dispatch_capturing_door_kwargs(tmp_path, deadline_seconds=None)

        assert rc == 0
        assert captured.get("deadline_seconds") is None

    def test_explicit_deadline_reaches_door_kwargs(self, tmp_path):
        rc, captured = _run_dispatch_capturing_door_kwargs(tmp_path, deadline_seconds=7200)

        assert rc == 0
        assert captured.get("deadline_seconds") == 7200


# ---------------------------------------------------------------------------
# 3. Out-of-range values hard-error before any dispatch is staged
# ---------------------------------------------------------------------------

class TestDeadlineSecondsClamp:
    @pytest.mark.parametrize("bad_value", [0, 60, 299, 14401, 100000, -1])
    def test_out_of_range_hard_errors_before_dispatch(self, tmp_path, capsys, bad_value):
        rc, captured = _run_dispatch_capturing_door_kwargs(tmp_path, deadline_seconds=bad_value)

        assert rc == 1
        assert captured == {}, "deliver_via_door must never be called for an out-of-range deadline"
        err = capsys.readouterr().err
        assert "out of range" in err
        assert "300" in err and "14400" in err

    @pytest.mark.parametrize("boundary_value", [300, 14400])
    def test_boundary_values_accepted(self, tmp_path, boundary_value):
        rc, captured = _run_dispatch_capturing_door_kwargs(tmp_path, deadline_seconds=boundary_value)

        assert rc == 0
        assert captured.get("deadline_seconds") == boundary_value


# ---------------------------------------------------------------------------
# 5. dispatch_bridge.deliver_via_door: deadline_seconds passthrough to bridge_dispatch
# ---------------------------------------------------------------------------

class TestDeliverViaDoorDeadlineDefault:
    def _capture_bridge_kwargs(self, monkeypatch, **deliver_kwargs):
        captured = {}

        def fake_bridge_dispatch(*, dry_run=False, **kwargs):
            captured.update(kwargs)
            return 0

        monkeypatch.setattr(dispatch_bridge, "bridge_dispatch", fake_bridge_dispatch)
        monkeypatch.setenv("VNX_SINGLE_ENTRY_DISPATCH", "1")
        monkeypatch.delenv("VNX_DISPATCH_LEGACY", raising=False)

        ok = dispatch_bridge.deliver_via_door(
            lambda: (_ for _ in ()).throw(AssertionError("legacy must not run when door is on")),
            instruction_text="do the thing",
            dispatch_id="20260101-120000-feat",
            target_slot="T1",
            role="backend-developer",
            provider="claude",
            model="sonnet",
            project_id="p1",
            **deliver_kwargs,
        )
        assert ok is True
        return captured

    def test_none_defaults_to_3600(self, monkeypatch):
        captured = self._capture_bridge_kwargs(monkeypatch, deadline_seconds=None)
        assert captured.get("deadline_seconds") == 3600

    def test_omitted_kwarg_defaults_to_3600(self, monkeypatch):
        """Callers that never pass deadline_seconds at all (byte-identical to pre-change
        call sites) must reproduce the exact prior default."""
        captured = self._capture_bridge_kwargs(monkeypatch)
        assert captured.get("deadline_seconds") == 3600

    def test_explicit_value_passed_through(self, monkeypatch):
        captured = self._capture_bridge_kwargs(monkeypatch, deadline_seconds=7200)
        assert captured.get("deadline_seconds") == 7200


# ---------------------------------------------------------------------------
# 6. End-to-end: deadline_seconds reaches the staged dispatch-spec.json (the lane-call)
# ---------------------------------------------------------------------------

class TestDeadlineSecondsReachesStagedSpec:
    def _stage_via_door(self, tmp_path, monkeypatch, *, deadline_seconds):
        import dispatch_cli
        monkeypatch.setattr(dispatch_cli, "run_dispatch", lambda spec_file, dry_run=False: 0)
        monkeypatch.setenv("VNX_SINGLE_ENTRY_DISPATCH", "1")
        monkeypatch.delenv("VNX_DISPATCH_LEGACY", raising=False)
        monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")
        monkeypatch.setenv("VNX_DATA_DIR", str(tmp_path))

        dispatch_id = "20260101-120000-deadline"
        ok = dispatch_bridge.deliver_via_door(
            lambda: (_ for _ in ()).throw(AssertionError("legacy must not run when door is on")),
            instruction_text="do the thing",
            dispatch_id=dispatch_id,
            target_slot="T1",
            role="backend-developer",
            provider="claude",
            model="sonnet",
            project_id="p1",
            deadline_seconds=deadline_seconds,
        )
        assert ok is True
        bundle = tmp_path / "dispatches" / "pending" / dispatch_id
        return json.loads((bundle / "dispatch-spec.json").read_text(encoding="utf-8"))

    def test_explicit_deadline_written_into_spec(self, tmp_path, monkeypatch):
        payload = self._stage_via_door(tmp_path, monkeypatch, deadline_seconds=7200)
        assert payload["deadline_seconds"] == 7200

    def test_unset_deadline_preserves_3600_in_spec(self, tmp_path, monkeypatch):
        payload = self._stage_via_door(tmp_path, monkeypatch, deadline_seconds=None)
        assert payload["deadline_seconds"] == 3600
