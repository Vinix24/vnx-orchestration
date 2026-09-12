"""test_tmux_lane_retirement.py — the tmux-interactive lane is retired (2026-09-12).

Operator directive dispatch-20260912-tmux-lane-uit-claude-altijd-headless: claude dispatches
headless only. A ``force_tmux=True`` spec is refused fail-loud at validate() Rule 12b with
reject-code ``tmux-lane-retired`` — never silently fallen back to headless. ``VNX_ALLOW_TMUX_LANE=1``
is the single emergency brake that restores the old opt-out.

Three tests, per the dispatch contract:
1. force_tmux + valid reason + provider=claude is refused (this is RED on the pre-fix code,
   which accepted it).
2. With VNX_ALLOW_TMUX_LANE=1 the same spec validates AND resolve_claude_lane still returns the
   tmux lane — the brake restores the old path, it does not merely pass validation.
3. A plain claude spec with no lane choice still resolves claude_headless — the default is
   untouched by the opt-out change.
"""

from __future__ import annotations

import sys
from pathlib import Path, PurePosixPath

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "lib"))

from dispatch_plan import resolve_claude_lane  # noqa: E402
from dispatch_spec import (  # noqa: E402
    DispatchPath,
    DispatchSpec,
    Provider,
    Reject,
    ValidatedSpec,
    validate,
)

_VALID_PROJECT_ID = "vnx-dev"


def _write_instruction(tmp_path: Path, text: str = "Do the work.") -> Path:
    p = tmp_path / "instruction.md"
    p.write_text(text, encoding="utf-8")
    return p


def _spec(instruction_file: Path, **overrides) -> DispatchSpec:
    defaults: dict = dict(
        schema_version=1,
        project_id=_VALID_PROJECT_ID,
        dispatch_id="20260912-tmux-retire",
        staging_id="20260912-tmux-retire-staging",
        instruction_file=instruction_file,
        role="backend-developer",
        target_slot="T1",
        gate="codex_gate",
        dispatch_paths=(DispatchPath(PurePosixPath("scripts/lib/foo.py")),),
    )
    defaults.update(overrides)
    return DispatchSpec(**defaults)


@pytest.fixture(autouse=True)
def _clear_tmux_brake(monkeypatch):
    # Never inherit an ambient VNX_ALLOW_TMUX_LANE (or its emergency override) — the
    # refusal test must see the flag OFF unless the test itself turns it on.
    monkeypatch.delenv("VNX_ALLOW_TMUX_LANE", raising=False)
    monkeypatch.delenv("VNX_OVERRIDE_ALLOW_TMUX_LANE", raising=False)


def test_force_tmux_is_refused_with_retired_code(tmp_path, monkeypatch):
    """force_tmux=True + valid reason + provider=claude -> Reject(tmux-lane-retired).

    Pre-fix this spec passed validate(); post-fix it is refused fail-loud rather than
    silently falling back to headless."""
    ifile = _write_instruction(tmp_path)
    spec = _spec(
        ifile,
        provider=Provider.CLAUDE,
        force_tmux=True,
        force_tmux_reason="operator wants a live pane for this run",
    )
    result = validate(spec, project_id=_VALID_PROJECT_ID, repo_root=Path("/fake/repo"))
    assert isinstance(result, Reject)
    assert result.code == "tmux-lane-retired"
    assert "VNX_ALLOW_TMUX_LANE=1" in result.reason


def test_allow_tmux_lane_restores_old_opt_out_and_lane_resolution(tmp_path, monkeypatch):
    """VNX_ALLOW_TMUX_LANE=1 -> the same spec validates, AND resolve_claude_lane still
    returns the tmux lane. The brake restores the real path, not just the validation."""
    monkeypatch.setenv("VNX_ALLOW_TMUX_LANE", "1")
    ifile = _write_instruction(tmp_path)
    spec = _spec(
        ifile,
        provider=Provider.CLAUDE,
        force_tmux=True,
        force_tmux_reason="operator wants a live pane for this run",
    )
    result = validate(spec, project_id=_VALID_PROJECT_ID, repo_root=Path("/fake/repo"))
    assert isinstance(result, ValidatedSpec)

    lane, adapter, warning = resolve_claude_lane(spec)
    assert lane == "claude_tmux_subscription"
    assert adapter == "tmux_claude"
    assert "TMUX lane opted-in" in (warning or "")


def test_plain_claude_spec_still_resolves_headless(tmp_path, monkeypatch):
    """A plain claude spec with no lane choice still resolves claude_headless — the
    default is untouched by the opt-out change (this must stay green)."""
    ifile = _write_instruction(tmp_path)
    spec = _spec(ifile, provider=Provider.CLAUDE)
    result = validate(spec, project_id=_VALID_PROJECT_ID, repo_root=Path("/fake/repo"))
    assert isinstance(result, ValidatedSpec)

    lane, adapter, warning = resolve_claude_lane(spec)
    assert lane == "claude_headless"
    assert adapter == "claude_subprocess"
    assert warning is None
