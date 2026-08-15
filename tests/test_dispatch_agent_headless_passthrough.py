#!/usr/bin/env python3
"""Tests for --allow-headless / --headless-reason passthrough on `vnx dispatch-agent` (OI-1174).

`vnx dispatch-agent` (the pip-CLI door, vnx_cli) never knew `allow_headless` /
`headless_reason` — grep returned empty, and `force_headless` was dead config
never read by any runtime gating decision. The bundle layer already supported the
fields (`dispatch_bridge.stage_spec_bundle(allow_headless=..., headless_reason=...)`)
and the door validated them (`dispatch_spec` Rule 12), but the pip-CLI could not
reach the claude_headless lane at all.

Covers:
  1. vnx_dispatch_agent: --allow-headless + --headless-reason -> deliver_via_door
     receives both kwargs (headless is reachable via the pip-CLI door).
  2. vnx_dispatch_agent: --allow-headless without --headless-reason -> rc=1,
     deliver_via_door never called (headless-reason-required, fail fast).
  3. vnx_dispatch_agent: --allow-headless with a non-claude provider -> rc=1
     (headless is claude-only, fail fast).
  4. vnx_dispatch_agent: --allow-headless with the door disabled -> rc=1
     (the legacy lane cannot honor a headless request; a half-wired flag is
     refused rather than silently dispatching a non-headless worker).
  5. dispatch_bridge.deliver_via_door: threads allow_headless/headless_reason
     through to bridge_dispatch, defaulting False/None (byte-identical to
     pre-change behavior).
  6. End-to-end: allow_headless/headless_reason reach the staged dispatch-spec.json.
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
# 0. Argument parsing — the --allow-headless / --headless-reason flags
# ---------------------------------------------------------------------------

def _build_dispatch_agent_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="vnx")
    subparsers = parser.add_subparsers(dest="command")
    _register_dispatch_agent_subparser(subparsers)
    return parser


class TestHeadlessArgParsing:
    def test_unset_defaults_to_false_and_none(self):
        args = _build_dispatch_agent_parser().parse_args(["dispatch-agent", "--agent", "x"])
        assert args.allow_headless is False
        assert args.headless_reason is None

    def test_allow_headless_flag_parses(self):
        args = _build_dispatch_agent_parser().parse_args(
            ["dispatch-agent", "--agent", "x", "--allow-headless"]
        )
        assert args.allow_headless is True

    def test_headless_reason_parses(self):
        args = _build_dispatch_agent_parser().parse_args(
            ["dispatch-agent", "--agent", "x", "--headless-reason", "burst benchmark"]
        )
        assert args.headless_reason == "burst benchmark"


# ---------------------------------------------------------------------------
# Shared dispatch harness (mirrors test_dispatch_agent_deadline_passthrough.py)
# ---------------------------------------------------------------------------

def _make_agent(base: Path, name: str = "hello-world") -> Path:
    agent_dir = base / "examples" / name
    agent_dir.mkdir(parents=True)
    (agent_dir / "CLAUDE.md").write_text(f"# {name} agent")
    (agent_dir / "config.yaml").write_text(
        'governance_profile: minimal\ndefault_instruction: "Say hi"\n'
    )
    return agent_dir


def _run_dispatch_capturing_door_kwargs(
    tmp_path: Path,
    *,
    model=None,
    allow_headless=False,
    headless_reason=None,
    monkeypatch: pytest.MonkeyPatch,
):
    """Invoke vnx_dispatch_agent with deliver_via_door replaced so no worker is ever
    staged/spawned — a plan-level assertion on the kwargs it receives. The door stays
    ENABLED (VNX_DISPATCH_LEGACY scrubbed) so the headless request reaches deliver_via_door."""
    _make_agent(tmp_path)

    monkeypatch.delenv("VNX_DISPATCH_LEGACY", raising=False)
    monkeypatch.delenv("VNX_SINGLE_ENTRY_DISPATCH", raising=False)

    captured = {}

    def fake_door(legacy_fn, **kwargs):
        captured.update(kwargs)
        return True

    from vnx_cli import _engine
    with patch.object(_engine, "engine_root", return_value=tmp_path), \
         patch.object(dispatch_bridge, "deliver_via_door", side_effect=fake_door):
        args = Namespace(
            agent="hello-world", instruction=None, model=model,
            project_dir=str(tmp_path), deadline_seconds=None,
            allow_headless=allow_headless, headless_reason=headless_reason,
        )
        rc = vnx_dispatch_agent(args)

    return rc, captured


# ---------------------------------------------------------------------------
# 1. allow_headless / headless_reason threading through vnx_dispatch_agent
# ---------------------------------------------------------------------------

class TestHeadlessThreadedToDoor:
    def test_allow_headless_and_reason_reach_door(self, tmp_path, monkeypatch):
        rc, captured = _run_dispatch_capturing_door_kwargs(
            tmp_path, allow_headless=True, headless_reason="burst benchmark",
            monkeypatch=monkeypatch,
        )

        assert rc == 0
        assert captured.get("allow_headless") is True
        assert captured.get("headless_reason") == "burst benchmark"

    def test_no_headless_passes_false_and_none(self, tmp_path, monkeypatch):
        """Unset (no --allow-headless) passes False/None through, byte-identical to
        pre-change behavior."""
        rc, captured = _run_dispatch_capturing_door_kwargs(tmp_path, monkeypatch=monkeypatch)

        assert rc == 0
        assert captured.get("allow_headless") is False
        assert captured.get("headless_reason") is None


# ---------------------------------------------------------------------------
# 2-4. Fail-fast guards: a half-wired flag is refused, never silently dropped
# ---------------------------------------------------------------------------

class TestHeadlessFailFast:
    def test_allow_headless_without_reason_errors(self, tmp_path, monkeypatch, capsys):
        rc, captured = _run_dispatch_capturing_door_kwargs(
            tmp_path, allow_headless=True, headless_reason=None,
            monkeypatch=monkeypatch,
        )

        assert rc == 1
        assert captured == {}, "deliver_via_door must never be called for a reasonless headless request"
        err = capsys.readouterr().err
        assert "--headless-reason" in err

    def test_allow_headless_with_non_claude_provider_errors(self, tmp_path, monkeypatch, capsys):
        rc, captured = _run_dispatch_capturing_door_kwargs(
            tmp_path, model="kimi", allow_headless=True, headless_reason="benchmark",
            monkeypatch=monkeypatch,
        )

        assert rc == 1
        assert captured == {}, "deliver_via_door must never be called for a non-claude headless request"
        err = capsys.readouterr().err
        assert "claude" in err
        assert "kimi" in err

    def test_allow_headless_with_door_disabled_errors(self, tmp_path, monkeypatch, capsys):
        _make_agent(tmp_path)
        monkeypatch.setenv("VNX_DISPATCH_LEGACY", "1")

        with patch.object(dispatch_bridge, "deliver_via_door") as mock_door:
            args = Namespace(
                agent="hello-world", instruction=None, model=None,
                project_dir=str(tmp_path), deadline_seconds=None,
                allow_headless=True, headless_reason="benchmark",
            )
            from vnx_cli import _engine
            with patch.object(_engine, "engine_root", return_value=tmp_path):
                rc = vnx_dispatch_agent(args)

        assert rc == 1
        mock_door.assert_not_called()
        err = capsys.readouterr().err
        assert "single-entry dispatch door" in err


# ---------------------------------------------------------------------------
# 5. dispatch_bridge.deliver_via_door: allow_headless/headless_reason passthrough
# ---------------------------------------------------------------------------

class TestDeliverViaDoorHeadlessDefault:
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

    def test_omitted_kwarg_defaults_false_none(self, monkeypatch):
        """Callers that never pass allow_headless/headless_reason (byte-identical to
        pre-change call sites) must reproduce the exact prior defaults."""
        captured = self._capture_bridge_kwargs(monkeypatch)
        assert captured.get("allow_headless") is False
        assert captured.get("headless_reason") is None

    def test_explicit_headless_passed_through(self, monkeypatch):
        captured = self._capture_bridge_kwargs(
            monkeypatch, allow_headless=True, headless_reason="burst benchmark"
        )
        assert captured.get("allow_headless") is True
        assert captured.get("headless_reason") == "burst benchmark"


# ---------------------------------------------------------------------------
# 6. End-to-end: allow_headless/headless_reason reaches the staged dispatch-spec.json
# ---------------------------------------------------------------------------

class TestHeadlessReachesStagedSpec:
    def _stage_via_door(self, tmp_path, monkeypatch, *, allow_headless, headless_reason):
        import dispatch_cli
        monkeypatch.setattr(dispatch_cli, "run_dispatch", lambda spec_file, dry_run=False: 0)
        monkeypatch.setenv("VNX_SINGLE_ENTRY_DISPATCH", "1")
        monkeypatch.delenv("VNX_DISPATCH_LEGACY", raising=False)
        monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")
        monkeypatch.setenv("VNX_DATA_DIR", str(tmp_path))

        dispatch_id = "20260101-120000-headless"
        ok = dispatch_bridge.deliver_via_door(
            lambda: (_ for _ in ()).throw(AssertionError("legacy must not run when door is on")),
            instruction_text="do the thing",
            dispatch_id=dispatch_id,
            target_slot="T1",
            role="backend-developer",
            provider="claude",
            model="sonnet",
            project_id="p1",
            allow_headless=allow_headless,
            headless_reason=headless_reason,
        )
        assert ok is True
        # OI-1072: bridge_dispatch MOVES the bundle out of pending/ once the door
        # has processed it — completed/ on rc == 0 (the mocked run_dispatch here).
        bundle = tmp_path / "dispatches" / "completed" / dispatch_id
        return json.loads((bundle / "dispatch-spec.json").read_text(encoding="utf-8"))

    def test_explicit_headless_written_into_spec(self, tmp_path, monkeypatch):
        payload = self._stage_via_door(
            tmp_path, monkeypatch, allow_headless=True, headless_reason="burst benchmark"
        )
        assert payload["allow_headless"] is True
        assert payload["headless_reason"] == "burst benchmark"

    def test_unset_headless_preserves_false_none_in_spec(self, tmp_path, monkeypatch):
        payload = self._stage_via_door(
            tmp_path, monkeypatch, allow_headless=False, headless_reason=None
        )
        assert payload["allow_headless"] is False
        assert payload["headless_reason"] is None
