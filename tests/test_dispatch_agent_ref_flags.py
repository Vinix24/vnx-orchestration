#!/usr/bin/env python3
"""Tests for --base-ref / --work-ref / --pr-id passthrough on `vnx dispatch-agent` (OI-1390).

`vnx dispatch-agent` (the consumer-door, vnx_cli) never exposed a fix-forward knob — the
CLI flags at vnx_cli/main.py were exactly --agent/--instruction/--model/--project-dir/
--deadline-seconds/--allow-headless/--headless-reason. The bundle layer already supported
all three (`dispatch_bridge.stage_spec_bundle(base_ref=..., work_ref=..., pr_id=...)`) and
`scripts/lib/pr_enforcement.py` already honors a spec-declared work_ref (OI-1392) to avoid
opening a second PR — only the CLI passthrough (and dispatch_bridge.deliver_via_door's own
signature) was missing, so a consumer fix-forward onto an existing PR branch had no way to
reach any of it and lost its worktree on reap when pr_enforcement rejected the resulting
"no commits between main and dispatch/<id>" branch.

Covers:
  1. vnx_dispatch_agent: no --base-ref/--work-ref/--pr-id -> deliver_via_door receives
     None for all three (unset = exact current behavior).
  2. vnx_dispatch_agent: explicit --base-ref/--work-ref/--pr-id -> deliver_via_door
     receives all three.
  3. dispatch_bridge.deliver_via_door: threads base_ref/work_ref/pr_id through to
     bridge_dispatch, defaulting base_ref None -> "origin/main" (byte-identical to
     pre-change behavior since stage_spec_bundle's own default is "origin/main").
  4. End-to-end: an explicit --base-ref/--work-ref/--pr-id reaches the staged
     dispatch-spec.json (the actual lane-call payload).
  5. End-to-end: omitting all three produces a spec identical to before this change
     (base_ref="origin/main", pr_id=None, work_ref=None).
  6. Legacy-lane warning: with the single-entry door disabled, setting any of the three
     flags prints a loud warning instead of silently dropping it (mirrors the
     --deadline-seconds warning).
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
# 0. Argument parsing — the --base-ref / --work-ref / --pr-id flags
# ---------------------------------------------------------------------------

def _build_dispatch_agent_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="vnx")
    subparsers = parser.add_subparsers(dest="command")
    _register_dispatch_agent_subparser(subparsers)
    return parser


class TestRefFlagsArgParsing:
    def test_unset_defaults_to_none(self):
        args = _build_dispatch_agent_parser().parse_args(["dispatch-agent", "--agent", "x"])
        assert args.base_ref is None
        assert args.work_ref is None
        assert args.pr_id is None

    def test_explicit_values_parse(self):
        args = _build_dispatch_agent_parser().parse_args(
            [
                "dispatch-agent", "--agent", "x",
                "--base-ref", "origin/dispatch/D-abc123",
                "--work-ref", "dispatch/D-85e3c124",
                "--pr-id", "1642",
            ]
        )
        assert args.base_ref == "origin/dispatch/D-abc123"
        assert args.work_ref == "dispatch/D-85e3c124"
        assert args.pr_id == "1642"


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
    tmp_path: Path, *, base_ref=None, work_ref=None, pr_id=None, legacy: bool = False, monkeypatch=None,
):
    """Invoke vnx_dispatch_agent with deliver_via_door replaced so no worker is ever
    staged/spawned — a plan-level assertion on the kwargs it receives."""
    _make_agent(tmp_path)
    if legacy and monkeypatch is not None:
        monkeypatch.setenv("VNX_DISPATCH_LEGACY", "1")

    captured = {}

    def fake_door(legacy_fn, **kwargs):
        captured.update(kwargs)
        return True

    from vnx_cli import _engine
    with patch.object(_engine, "engine_root", return_value=tmp_path), \
         patch.object(dispatch_bridge, "deliver_via_door", side_effect=fake_door):
        args = Namespace(
            agent="hello-world", instruction=None, model="sonnet",
            project_dir=str(tmp_path), deadline_seconds=None,
            base_ref=base_ref, work_ref=work_ref, pr_id=pr_id,
        )
        rc = vnx_dispatch_agent(args)

    return rc, captured


# ---------------------------------------------------------------------------
# 1-2. base_ref/work_ref/pr_id threading through vnx_dispatch_agent -> deliver_via_door
# ---------------------------------------------------------------------------

class TestRefFlagsThreadedToDoor:
    def test_unset_flags_pass_none(self, tmp_path):
        """Unset (no --base-ref/--work-ref/--pr-id) must pass None through, not silently
        invent origin/main at this layer — that default lives one layer down."""
        rc, captured = _run_dispatch_capturing_door_kwargs(tmp_path)

        assert rc == 0
        assert captured.get("base_ref") is None
        assert captured.get("work_ref") is None
        assert captured.get("pr_id") is None

    def test_explicit_flags_reach_door_kwargs(self, tmp_path):
        rc, captured = _run_dispatch_capturing_door_kwargs(
            tmp_path,
            base_ref="origin/dispatch/D-abc123",
            work_ref="dispatch/D-85e3c124",
            pr_id="1642",
        )

        assert rc == 0
        assert captured.get("base_ref") == "origin/dispatch/D-abc123"
        assert captured.get("work_ref") == "dispatch/D-85e3c124"
        assert captured.get("pr_id") == "1642"


# ---------------------------------------------------------------------------
# 3. dispatch_bridge.deliver_via_door: base_ref/work_ref/pr_id passthrough to bridge_dispatch
# ---------------------------------------------------------------------------

class TestDeliverViaDoorRefFlagsDefault:
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

    def test_omitted_kwargs_default_to_prior_behavior(self, monkeypatch):
        """Callers that never pass base_ref/work_ref/pr_id at all (byte-identical to
        pre-change call sites) must reproduce the exact prior defaults."""
        captured = self._capture_bridge_kwargs(monkeypatch)
        assert captured.get("base_ref") == "origin/main"
        assert captured.get("pr_id") is None
        assert captured.get("work_ref") is None

    def test_explicit_none_base_ref_defaults_to_origin_main(self, monkeypatch):
        captured = self._capture_bridge_kwargs(monkeypatch, base_ref=None)
        assert captured.get("base_ref") == "origin/main"

    def test_explicit_values_passed_through(self, monkeypatch):
        captured = self._capture_bridge_kwargs(
            monkeypatch,
            base_ref="origin/dispatch/D-abc123",
            work_ref="dispatch/D-85e3c124",
            pr_id="1642",
        )
        assert captured.get("base_ref") == "origin/dispatch/D-abc123"
        assert captured.get("work_ref") == "dispatch/D-85e3c124"
        assert captured.get("pr_id") == "1642"


# ---------------------------------------------------------------------------
# 4-5. End-to-end: ref flags reach the staged dispatch-spec.json (the lane-call)
# ---------------------------------------------------------------------------

class TestRefFlagsReachStagedSpec:
    def _stage_via_door(self, tmp_path, monkeypatch, *, base_ref=None, work_ref=None, pr_id=None):
        import dispatch_cli
        monkeypatch.setattr(dispatch_cli, "run_dispatch", lambda spec_file, dry_run=False: 0)
        monkeypatch.setenv("VNX_SINGLE_ENTRY_DISPATCH", "1")
        monkeypatch.delenv("VNX_DISPATCH_LEGACY", raising=False)
        monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")
        monkeypatch.setenv("VNX_DATA_DIR", str(tmp_path))

        dispatch_id = "20260101-120000-fixfwd"
        ok = dispatch_bridge.deliver_via_door(
            lambda: (_ for _ in ()).throw(AssertionError("legacy must not run when door is on")),
            instruction_text="do the thing",
            dispatch_id=dispatch_id,
            target_slot="T1",
            role="backend-developer",
            provider="claude",
            model="sonnet",
            project_id="p1",
            base_ref=base_ref,
            work_ref=work_ref,
            pr_id=pr_id,
        )
        assert ok is True
        # OI-1072: bridge_dispatch MOVES the bundle out of pending/ once the door
        # has processed it — completed/ on rc == 0 (the mocked run_dispatch here).
        bundle = tmp_path / "dispatches" / "completed" / dispatch_id
        return json.loads((bundle / "dispatch-spec.json").read_text(encoding="utf-8"))

    def test_explicit_ref_flags_written_into_spec(self, tmp_path, monkeypatch):
        payload = self._stage_via_door(
            tmp_path, monkeypatch,
            base_ref="origin/dispatch/D-abc123",
            work_ref="dispatch/D-85e3c124",
            pr_id="1642",
        )
        assert payload["base_ref"] == "origin/dispatch/D-abc123"
        assert payload["work_ref"] == "dispatch/D-85e3c124"
        assert payload["pr_id"] == "1642"

    def test_unset_ref_flags_preserve_prior_defaults_in_spec(self, tmp_path, monkeypatch):
        """A dispatch staged with none of the three flags set must produce a spec
        identical to one staged before this change: base_ref="origin/main",
        pr_id=None, work_ref=None."""
        payload = self._stage_via_door(tmp_path, monkeypatch)
        assert payload["base_ref"] == "origin/main"
        assert payload["pr_id"] is None
        assert payload["work_ref"] is None


# ---------------------------------------------------------------------------
# 6. Legacy-lane warning — a ref flag on a path that cannot honor it must be loud,
#    never silently dropped (mirrors --deadline-seconds's warning pattern).
# ---------------------------------------------------------------------------

# The distinctive marker of the legacy no-op warning under test — the claude-not-found
# preflight emits its own "Warning: ..." line when the binary is absent (CI), so the
# assertions below must target THIS text, not the bare substring "Warning".
_LEGACY_REF_WARNING = "ignored because the single-entry dispatch door is disabled"


class TestLegacyRefFlagsWarning:
    def test_legacy_mode_warns_when_work_ref_set(self, tmp_path, monkeypatch, capsys):
        rc, captured = _run_dispatch_capturing_door_kwargs(
            tmp_path, work_ref="dispatch/D-85e3c124", legacy=True, monkeypatch=monkeypatch,
        )

        assert rc == 0, "a warning is not a hard-error — the dispatch still runs"
        assert captured.get("work_ref") == "dispatch/D-85e3c124"
        err = capsys.readouterr().err
        assert _LEGACY_REF_WARNING in err
        assert "--work-ref" in err
        assert "VNX_DISPATCH_LEGACY" in err

    def test_legacy_mode_warns_listing_all_flags_set(self, tmp_path, monkeypatch, capsys):
        rc, captured = _run_dispatch_capturing_door_kwargs(
            tmp_path,
            base_ref="origin/dispatch/D-abc123",
            work_ref="dispatch/D-85e3c124",
            pr_id="1642",
            legacy=True,
            monkeypatch=monkeypatch,
        )

        assert rc == 0
        err = capsys.readouterr().err
        assert _LEGACY_REF_WARNING in err
        assert "--base-ref" in err
        assert "--work-ref" in err
        assert "--pr-id" in err

    def test_legacy_mode_no_warning_when_flags_unset(self, tmp_path, monkeypatch, capsys):
        """No ref flags, no warning — the unset default is preserved byte-for-byte."""
        rc, captured = _run_dispatch_capturing_door_kwargs(tmp_path, legacy=True, monkeypatch=monkeypatch)

        assert rc == 0
        assert captured.get("base_ref") is None
        assert captured.get("work_ref") is None
        assert captured.get("pr_id") is None
        err = capsys.readouterr().err
        assert _LEGACY_REF_WARNING not in err

    def test_door_mode_no_warning_when_flags_set(self, tmp_path, monkeypatch, capsys):
        """Door on (default): the ref flags ARE honored, so no warning fires."""
        rc, captured = _run_dispatch_capturing_door_kwargs(
            tmp_path, base_ref="origin/dispatch/D-abc123", monkeypatch=monkeypatch,
        )

        assert rc == 0
        assert captured.get("base_ref") == "origin/dispatch/D-abc123"
        err = capsys.readouterr().err
        assert _LEGACY_REF_WARNING not in err
