"""test_envelope_claude_adapter_report_env.py — A-bis-2: the headless
(claude_headless) lane must export VNX_DATA_DIR / VNX_DATA_DIR_EXPLICIT=1 /
VNX_REPORT_PATH into the ``claude -p`` worker's own environment.

Measured root cause (T0, main 54943a2e, 2026-09-06): base_worker.md tells the
worker to write its report to ``$VNX_DATA_DIR/unified_reports/<dispatch_id>.md``,
but ``ClaudeSubprocessAdapter.run`` called ``spawn_claude`` with no ``extra_env``
at all (grep on VNX_DATA_DIR across envelope_adapters_claude.py / dispatch_envelope.py
/ headless_adapter.py: zero hits). The worker therefore guessed the directory —
sometimes landing in the wrong store — and GOVERN, which only reads the CENTRAL
``unified_reports/<dispatch_id>.md``, synthesized over a real, valid worker report.

These tests pin the fix at the adapter (mocking spawn_claude, no real subprocess):
RED on main (no extra_env kwarg at all), GREEN after the fix.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "lib"))

from envelope_adapters_claude import ClaudeSubprocessAdapter  # noqa: E402
from envelope_types import EnvelopeSpec  # noqa: E402


class _OkSpawnResult:
    """Minimal ClaudeSpawnResult-shaped stub with the attributes .run() reads."""

    returncode = 0
    error = None
    timed_out = False
    stopped_early = False
    completion_text = "done"
    session_id = "sess-1"
    token_usage = {"input_tokens": 1, "output_tokens": 1}
    model = None


def _spec(tmp_path: Path, **overrides) -> EnvelopeSpec:
    defaults = dict(
        dispatch_id="20260907-abis2-report-path-central",
        terminal_id="T1",
        provider="claude",
        model="sonnet",
        instruction="do the thing",
        role="backend-developer",
        pr_id=None,
        state_dir=tmp_path / "state",
        data_dir=tmp_path / "data",
        deadline_seconds=900,
    )
    defaults.update(overrides)
    return EnvelopeSpec(**defaults)


def test_spawn_receives_vnx_data_dir_env(tmp_path: Path) -> None:
    """extra_env must carry VNX_DATA_DIR set to the resolved data_dir."""
    spec = _spec(tmp_path)

    with patch(
        "provider_spawns.claude_spawn.spawn_claude", return_value=_OkSpawnResult()
    ) as mock_spawn:
        ClaudeSubprocessAdapter().run(spec)

    mock_spawn.assert_called_once()
    kwargs = mock_spawn.call_args.kwargs
    assert "extra_env" in kwargs, "spawn_claude was called without an extra_env kwarg"
    assert kwargs["extra_env"].get("VNX_DATA_DIR") == str(spec.data_dir)


def test_spawn_receives_vnx_data_dir_explicit_flag(tmp_path: Path) -> None:
    """extra_env must carry VNX_DATA_DIR_EXPLICIT=1 — the two-key contract
    other lanes already export (plan_gate_panel.py) so a bare VNX_DATA_DIR
    is never mistaken for pollution downstream."""
    spec = _spec(tmp_path)

    with patch(
        "provider_spawns.claude_spawn.spawn_claude", return_value=_OkSpawnResult()
    ) as mock_spawn:
        ClaudeSubprocessAdapter().run(spec)

    kwargs = mock_spawn.call_args.kwargs
    assert kwargs["extra_env"].get("VNX_DATA_DIR_EXPLICIT") == "1"


def test_spawn_receives_vnx_report_path_env(tmp_path: Path) -> None:
    """extra_env must carry the exact absolute report path — no guessing."""
    spec = _spec(tmp_path)

    with patch(
        "provider_spawns.claude_spawn.spawn_claude", return_value=_OkSpawnResult()
    ) as mock_spawn:
        ClaudeSubprocessAdapter().run(spec)

    kwargs = mock_spawn.call_args.kwargs
    expected = str(spec.data_dir / "unified_reports" / f"{spec.dispatch_id}.md")
    assert kwargs["extra_env"].get("VNX_REPORT_PATH") == expected


def test_report_path_env_uses_this_dispatch_id_not_another(tmp_path: Path) -> None:
    """Regression guard: the report path must be keyed on THIS spec's
    dispatch_id, not some other/cached value."""
    spec = _spec(tmp_path, dispatch_id="some-other-dispatch-id")

    with patch(
        "provider_spawns.claude_spawn.spawn_claude", return_value=_OkSpawnResult()
    ) as mock_spawn:
        ClaudeSubprocessAdapter().run(spec)

    kwargs = mock_spawn.call_args.kwargs
    assert kwargs["extra_env"]["VNX_REPORT_PATH"].endswith(
        "unified_reports/some-other-dispatch-id.md"
    )
