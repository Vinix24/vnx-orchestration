"""OI-3083: the envelope Codex lane must honor the dispatch deadline."""

from inspect import signature
from unittest.mock import patch

import pytest

from dispatch_envelope import EnvelopeSpec, LaneRouter
from provider_spawns.codex_spawn import CodexSpawnResult, coerce_chunk_stall, spawn_codex


@pytest.fixture
def spec(tmp_path, monkeypatch):
    monkeypatch.setenv("VNX_PROJECT_ID", "codex-deadline-test")
    return EnvelopeSpec(
        dispatch_id="codex-deadline-test",
        terminal_id="T1",
        provider="codex",
        model="gpt-6-astra",
        instruction="Verify the dispatch deadline.",
        role=None,
        pr_id=None,
        state_dir=tmp_path / "state",
        data_dir=tmp_path,
    )


@pytest.mark.parametrize("deadline", [3600, 5400, 900, 0])
def test_codex_lane_passes_spec_deadline_to_spawn(spec, deadline):
    spec.deadline_seconds = deadline
    spawn_result = CodexSpawnResult(
        returncode=0, completion_text="completed", events_written=0,
        session_id=None, timed_out=False,
    )
    with patch("provider_spawns.codex_spawn.spawn_codex", return_value=spawn_result) as spawn:
        result = LaneRouter().get("codex").run(spec)

    assert result.status == "success"
    spawn.assert_called_once()
    assert spawn.call_args.kwargs["total_deadline"] == float(deadline)
    assert isinstance(spawn.call_args.kwargs["total_deadline"], float)


@pytest.mark.parametrize("deadline, error", [(None, TypeError), ("invalid", ValueError)])
def test_codex_lane_invalid_deadline_never_spawns(spec, deadline, error):
    spec.deadline_seconds = deadline
    with patch("provider_spawns.codex_spawn.spawn_codex") as spawn:
        with pytest.raises(error):
            LaneRouter().get("codex").run(spec)
    spawn.assert_not_called()


@pytest.mark.parametrize("deadline, stall", [(3600.0, 1800.0), (5400.0, 2700.0)])
def test_codex_default_chunk_stall_scales_with_dispatch_deadline(deadline, stall):
    default_chunk = signature(spawn_codex).parameters["chunk_timeout"].default
    assert coerce_chunk_stall(default_chunk, deadline) == stall
