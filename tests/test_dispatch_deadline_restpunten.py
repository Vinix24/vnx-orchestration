"""Tests for the deadline-passthrough restpoints (dispatch 20260804-102003-deadline-restpunten).

PR #1180 merged as interim: the claude-tmux lane binds the deadline end-to-end. Three
restpoints stayed behind; these tests pin the fixes.

  1. Trust boundary range check: dispatch_bridge.stage_spec_bundle and its
     --deadline-seconds CLI argument previously accepted any value (the consumer door
     hard-errored at 300-14400, dispatch_spec.validate at 60-14400 — two ranges). The
     boundary now enforces [300, 14400] and validate() was unified to the SAME range,
     both from dispatch_spec.DEADLINE_SECONDS_MIN/MAX (single source of truth).
  2. Legacy silent no-op: VNX_DISPATCH_LEGACY=1 made --deadline-seconds a silent no-op on
     the claude dispatch-agent path. The claude path now warns (mirroring the non-claude
     guard's framing) when an explicit deadline is set with the door disabled.
  3. Doc repair (no test — doc-only): DISPATCH_RULES.md §deadline now names the raw .md
     legacy path exception. Line numbers scripts/commands/dispatch.sh:288/:315 were
     verified against origin/main.

Restpoint 1 is tested both at the unit boundary (stage_spec_bundle raises, main()
rejects) and at the door contract (validate() now rejects 60 and accepts 300).
"""

from __future__ import annotations

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

import dispatch_bridge  # noqa: E402
from dispatch_spec import (  # noqa: E402
    DEADLINE_SECONDS_MAX,
    DEADLINE_SECONDS_MIN,
    DispatchPath,
    DispatchSpec,
    PathAccess,
    Reject,
    ValidatedSpec,
    validate,
)
from vnx_cli.commands.dispatch_agent import vnx_dispatch_agent  # noqa: E402

_GOOD_ID = "20260101-120000-feat"


def _stage(tmp_path: Path, **over) -> Path:
    base = dict(
        instruction_text="do the thing",
        dispatch_id=_GOOD_ID,
        role="dev",
        target_slot="T1",
        project_id="p1",
        provider="claude",
        data_dir=tmp_path,
    )
    base.update(over)
    return dispatch_bridge.stage_spec_bundle(**base)


# ---------------------------------------------------------------------------
# Restpoint 1 — trust boundary rejects out-of-range deadlines at staging time
# ---------------------------------------------------------------------------

class TestStageSpecBundleDeadlineRange:
    @pytest.mark.parametrize("bad", [0, 60, 299, 14401, 99999, -1])
    def test_stage_rejects_deadline_outside_bounds(self, tmp_path, bad):
        """A staged bundle must never carry a deadline the door would reject — the
        trust boundary fails loud at staging instead of drifting silently downstream."""
        with pytest.raises(ValueError, match="deadline_seconds must be in"):
            _stage(tmp_path, deadline_seconds=bad)

    @pytest.mark.parametrize("boundary", [DEADLINE_SECONDS_MIN, DEADLINE_SECONDS_MAX])
    def test_stage_accepts_deadline_boundaries(self, tmp_path, boundary):
        spec_file = _stage(tmp_path, deadline_seconds=boundary)
        payload = json.loads(spec_file.read_text(encoding="utf-8"))
        assert payload["deadline_seconds"] == boundary

    def test_stage_accepts_default_3600(self, tmp_path):
        payload = json.loads(_stage(tmp_path).read_text(encoding="utf-8"))
        assert payload["deadline_seconds"] == 3600


class TestBridgeDispatchDeadlineRange:
    def test_bridge_dispatch_rejects_out_of_range_as_clean_exit(self, tmp_path, capsys):
        """An impossible deadline surfaces as bridge_dispatch exit 1 (staging-error)
        — never a side-door delivery, never a bundle on disk."""
        rc = dispatch_bridge.bridge_dispatch(
            instruction_text="x",
            dispatch_id=_GOOD_ID,
            role="dev",
            target_slot="T1",
            project_id="p1",
            data_dir=tmp_path,
            deadline_seconds=60,
            dry_run=True,
        )
        assert rc == 1
        err = capsys.readouterr().err
        assert "REJECT" in err and "staging-error" in err
        bundle = tmp_path / "dispatches" / "pending" / _GOOD_ID
        assert not bundle.exists(), "no bundle may be written for an out-of-range deadline"


class TestBridgeCliDeadlineRange:
    def _run_cli(self, monkeypatch, deadline: int):
        captured = {}

        def fake_bridge_dispatch(*, dry_run=False, **kwargs):
            captured.update(kwargs)
            return 0

        monkeypatch.setattr(dispatch_bridge, "bridge_dispatch", fake_bridge_dispatch)
        argv = [
            "--dispatch-id", _GOOD_ID,
            "--terminal", "T1",
            "--deadline-seconds", str(deadline),
            "--instruction", "do it",
        ]
        rc = dispatch_bridge.main(argv)
        return rc, captured

    @pytest.mark.parametrize("bad", [0, 60, 299, 14401])
    def test_cli_rejects_out_of_range_before_staging(self, monkeypatch, capsys, bad):
        rc, captured = self._run_cli(monkeypatch, bad)
        assert rc == 2
        assert captured == {}, "bridge_dispatch must never be reached for an out-of-range deadline"
        err = capsys.readouterr().err
        assert "bad-deadline" in err
        assert str(bad) in err

    @pytest.mark.parametrize("boundary", [DEADLINE_SECONDS_MIN, DEADLINE_SECONDS_MAX])
    def test_cli_accepts_deadline_boundaries(self, monkeypatch, boundary):
        rc, captured = self._run_cli(monkeypatch, boundary)
        assert rc == 0
        assert captured.get("deadline_seconds") == boundary

    def test_cli_default_3600_reaches_bridge(self, monkeypatch):
        captured = {}

        def fake_bridge_dispatch(*, dry_run=False, **kwargs):
            captured.update(kwargs)
            return 0

        monkeypatch.setattr(dispatch_bridge, "bridge_dispatch", fake_bridge_dispatch)
        rc = dispatch_bridge.main([
            "--dispatch-id", _GOOD_ID,
            "--terminal", "T1",
            "--instruction", "do it",
        ])
        assert rc == 0
        assert captured.get("deadline_seconds") == 3600


# ---------------------------------------------------------------------------
# Restpoint 1 — the door contract is unified on [300, 14400]
# (previously validate() allowed [60, 14400]; two ranges was the drift source)
# ---------------------------------------------------------------------------

def _write_instruction(tmp_path: Path, text: str = "Do the work.") -> Path:
    p = tmp_path / "instruction.md"
    p.write_text(text, encoding="utf-8")
    return p


def _valid_spec(instruction_file: Path, **overrides) -> DispatchSpec:
    defaults: dict = dict(
        schema_version=1,
        project_id="vnx-dev",
        dispatch_id="20260615-test-dispatch",
        staging_id="20260615-test-staging",
        instruction_file=instruction_file,
        role="backend-developer",
        target_slot="T1",
        gate="human-promoted",
        dispatch_paths=(DispatchPath(Path("scripts/lib/foo.py"), PathAccess.READ_WRITE),),
    )
    defaults.update(overrides)
    return DispatchSpec(**defaults)


def _do_validate(spec: DispatchSpec, monkeypatch) -> ValidatedSpec | Reject:
    monkeypatch.setenv("VNX_PROJECT_ID", "vnx-dev")
    return validate(spec, project_id="vnx-dev", repo_root=Path("/fake/repo"))


class TestValidateDeadlineRangeUnified:
    @pytest.mark.parametrize("bad", [0, 59, 60, 299, 14401, 99999])
    def test_validate_rejects_every_value_below_300(self, tmp_path, monkeypatch, bad):
        """60 was previously accepted by validate() while the consumer door rejected it —
        the exact drift restpoint 1 removes. It must now fail loud."""
        ifile = _write_instruction(tmp_path)
        result = _do_validate(_valid_spec(ifile, deadline_seconds=bad), monkeypatch)
        assert isinstance(result, Reject)
        assert result.code == "bad-deadline"

    @pytest.mark.parametrize("good", [300, 3600, 14400])
    def test_validate_accepts_deadline_in_consumer_door_range(self, tmp_path, monkeypatch, good):
        ifile = _write_instruction(tmp_path)
        result = _do_validate(_valid_spec(ifile, deadline_seconds=good), monkeypatch)
        assert isinstance(result, ValidatedSpec)


def test_deadline_bounds_single_source_of_truth_matches_consumer_door():
    """The consumer door (dispatch-agent) and the spec/bridge must agree — the literal
    duplication is pinned by a test so a future edit to one range cannot drift."""
    from vnx_cli.commands.dispatch_agent import (
        _DEADLINE_SECONDS_MAX as agent_max,
        _DEADLINE_SECONDS_MIN as agent_min,
    )

    assert agent_min == DEADLINE_SECONDS_MIN
    assert agent_max == DEADLINE_SECONDS_MAX


# ---------------------------------------------------------------------------
# Restpoint 2 — VNX_DISPATCH_LEGACY=1 must not silently swallow --deadline-seconds
# ---------------------------------------------------------------------------

def _make_agent(base: Path, name: str = "hello-world") -> Path:
    agent_dir = base / "examples" / name
    agent_dir.mkdir(parents=True)
    (agent_dir / "CLAUDE.md").write_text(f"# {name} agent")
    (agent_dir / "config.yaml").write_text(
        'governance_profile: minimal\ndefault_instruction: "Say hi"\n'
    )
    return agent_dir


def _run_dispatch_agent(tmp_path: Path, *, deadline_seconds=None, legacy: bool = False, monkeypatch=None):
    """Invoke vnx_dispatch_agent with deliver_via_door replaced so no worker is ever
    staged/spawned — the warning under test fires inside vnx_dispatch_agent itself,
    before the door call."""
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
            project_dir=str(tmp_path), deadline_seconds=deadline_seconds,
        )
        rc = vnx_dispatch_agent(args)

    return rc, captured


# The distinctive marker of the legacy no-op warning under test — the claude-not-found
# preflight emits its own "Warning: ..." line when the binary is absent (CI), so the
# assertions below must target THIS text, not the bare substring "Warning".
_LEGACY_DEADLINE_WARNING = "is ignored because the single-entry dispatch door is disabled"


class TestLegacyDeadlineWarning:
    def test_legacy_mode_warns_when_deadline_set(self, tmp_path, monkeypatch, capsys):
        """VNX_DISPATCH_LEGACY=1 + explicit --deadline-seconds on the claude path: the
        flag is a no-op on the legacy lane and must report itself, not vanish."""
        rc, captured = _run_dispatch_agent(
            tmp_path, deadline_seconds=7200, legacy=True, monkeypatch=monkeypatch,
        )

        assert rc == 0, "a warning is not a hard-error — the dispatch still runs"
        assert captured.get("deadline_seconds") == 7200
        err = capsys.readouterr().err
        assert _LEGACY_DEADLINE_WARNING in err
        assert "--deadline-seconds" in err
        assert "VNX_DISPATCH_LEGACY" in err

    def test_legacy_mode_no_warning_when_deadline_unset(self, tmp_path, monkeypatch, capsys):
        """No deadline flag, no warning — the unset default is preserved byte-for-byte."""
        rc, captured = _run_dispatch_agent(tmp_path, legacy=True, monkeypatch=monkeypatch)

        assert rc == 0
        assert captured.get("deadline_seconds") is None
        err = capsys.readouterr().err
        assert _LEGACY_DEADLINE_WARNING not in err
        assert "--deadline-seconds" not in err

    def test_door_mode_no_warning_when_deadline_set(self, tmp_path, monkeypatch, capsys):
        """Door on (default): the deadline IS honored, so no warning fires."""
        rc, captured = _run_dispatch_agent(tmp_path, deadline_seconds=7200, monkeypatch=monkeypatch)

        assert rc == 0
        assert captured.get("deadline_seconds") == 7200
        err = capsys.readouterr().err
        assert _LEGACY_DEADLINE_WARNING not in err
