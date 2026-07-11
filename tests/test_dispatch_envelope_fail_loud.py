"""test_dispatch_envelope_fail_loud.py — Regression tests for dispatch #20260710-211102.

Covers:
1. The happy path: run_envelope_plan propagates a real (non-empty) provider
   completion through PREPARE -> ROUTE -> EXECUTE -> GOVERN unchanged. This
   pins the ProviderAdapter.run(plan, instruction, ...) call signature so a
   future regression back to the single-positional adapter.run(spec) shape
   (the shape run_envelope() uses for the codex/claude-subprocess lanes) is
   caught immediately.
2. The fail-loud guard: a provider spawn that returns returncode=0 with a
   BLANK completion_text (the silent-empty-success vector — some spawn_*
   functions, unlike kimi_spawn, do not self-check for empty extraction) must
   be downgraded to status="failure" with a diagnosable error, never reported
   as an empty "success".
3. dispatch_cli surfaces the failure to stderr for the provider lane instead
   of returning a bare exit code with no visible cause.
"""
from __future__ import annotations

import hashlib
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional
from unittest.mock import patch

import pytest

SCRIPTS_LIB = Path(__file__).resolve().parent.parent / "scripts" / "lib"
sys.path.insert(0, str(SCRIPTS_LIB))

from dispatch_envelope import ProviderAdapter, run_envelope_plan
from dispatch_internal import issue_permit
from dispatch_plan import ExecutionPlan
from dispatch_spec import Isolation, Provider


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_provider_plan(
    tmp_path: Path,
    *,
    provider: Provider = Provider.KIMI,
    model: str = "default",
    dispatch_id: str = "test-fail-loud-dispatch",
) -> ExecutionPlan:
    instruction_file = tmp_path / f"instruction-{provider.value.replace(':', '-')}.md"
    instruction_file.write_text("Reply with exactly the word OK.", encoding="utf-8")
    sha256 = hashlib.sha256(
        instruction_file.read_text(encoding="utf-8").encode("utf-8")
    ).hexdigest()
    return ExecutionPlan(
        dispatch_id=dispatch_id,
        project_id="vnx-dev",
        provider=provider,
        model=model,
        lane="provider",
        adapter="provider",
        target_id="T1",
        billing="provider_metered",
        serialization_class=None,
        isolation=Isolation.WORKTREE,
        require_worktree=True,
        seed_materialize=False,
        instruction_delivery="file_ref",
        report_contract="required",
        warmup="n/a",
        deadline_seconds=3600,
        base_ref="main",
        dispatch_paths=(),
        instruction_file=instruction_file,
        route_reason="fail-loud-regression",
        instruction_sha256=sha256,
    )


@dataclass
class _FakeKimiResult:
    """Minimal stand-in for KimiSpawnResult — same attribute surface consumed
    by _map_generic_spawn_result / ProviderAdapter.run()."""

    returncode: int = 0
    completion_text: str = "OK"
    events_written: int = 1
    session_id: Optional[str] = None
    timed_out: bool = False
    stopped_early: bool = False
    token_usage: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    event_writer_failures: int = 0


# ---------------------------------------------------------------------------
# Test 1: happy path — a real completion propagates through the full envelope
# ---------------------------------------------------------------------------


def test_run_envelope_plan_propagates_successful_kimi_completion(tmp_path):
    """A provider plan whose spawn returns completion_text="OK", returncode=0
    must come back out of run_envelope_plan as EnvelopeResult(status="success",
    completion_text="OK") — this is the exact scenario dispatch
    20260710-211102-envelope-provider-lane-empty-completion asked to pin down.
    """
    plan = _make_provider_plan(tmp_path, dispatch_id="test-kimi-happy-path")
    permit = issue_permit(plan)

    state_dir = tmp_path / "state"
    data_dir = tmp_path / "data"
    state_dir.mkdir()
    data_dir.mkdir()

    fake_wt = tmp_path / "wt"
    fake_wt.mkdir()

    fake_result = _FakeKimiResult(returncode=0, completion_text="OK")

    with patch("provider_spawns.kimi_spawn.spawn_kimi", return_value=fake_result), \
         patch("dispatch_worktree_isolation.create_dispatch_worktree", return_value=fake_wt), \
         patch("dispatch_worktree_isolation.remove_dispatch_worktree"):
        result = run_envelope_plan(plan, permit, state_dir=state_dir, data_dir=data_dir)

    assert result.status == "success", f"expected success, got {result.status} (error={result.error})"
    assert result.returncode == 0
    assert result.completion_text == "OK"
    assert result.error is None


# ---------------------------------------------------------------------------
# Test 2: fail-loud guard — returncode=0 + blank completion is never "success"
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "provider,spawn_target",
    [
        (Provider.KIMI, "provider_spawns.kimi_spawn.spawn_kimi"),
        (Provider.CODEX, "provider_spawns.codex_spawn.spawn_codex"),
        (Provider.GEMINI, "provider_spawns.gemini_spawn.spawn_gemini"),
        (Provider.DEEPSEEK_HARNESS, "provider_spawns.deepseek_harness_spawn.spawn_deepseek_harness"),
        (Provider.GLM_HARNESS, "provider_spawns.glm_harness_spawn.spawn_glm_harness"),
    ],
)
def test_provider_adapter_empty_completion_is_fail_loud(
    tmp_path, monkeypatch, provider, spawn_target
):
    """returncode=0 with a BLANK completion_text must downgrade to failure with
    a diagnosable error — never a silent empty "success" (the exact vector
    dispatch 20260710-211102 asked to close: "surface the raw spawn result +
    a clear error, never a silent empty report + failure receipt").
    """
    monkeypatch.setattr(
        "provider_dispatch._resolve_codex_model", lambda: "gpt-codex-test"
    )
    monkeypatch.setattr(
        "provider_spawns.deepseek_harness_spawn.resolve_harness_model",
        lambda m: "deepseek-v4-test",
    )
    monkeypatch.setattr(
        "provider_spawns.glm_harness_spawn.resolve_harness_model",
        lambda m: "glm-test",
    )

    empty_result = _FakeKimiResult(returncode=0, completion_text="", error=None)
    monkeypatch.setattr(spawn_target, lambda *a, **k: empty_result)

    plan = _make_provider_plan(tmp_path, provider=provider, model="default")
    adapter = ProviderAdapter()

    result = adapter.run(plan, "prompt")

    assert result.status == "failure", (
        f"{provider.value}: blank completion_text with returncode=0 must not "
        f"report success (got status={result.status})"
    )
    assert result.completion_text == ""
    assert result.error, f"{provider.value}: fail-loud guard must set a diagnosable error"
    assert provider.value in result.error
    assert "empty completion" in result.error


def test_provider_adapter_nonempty_completion_still_succeeds(tmp_path, monkeypatch):
    """Guard rail: the fail-loud guard must not false-positive on real output."""
    real_result = _FakeKimiResult(returncode=0, completion_text="a real answer")
    monkeypatch.setattr(
        "provider_spawns.kimi_spawn.spawn_kimi", lambda *a, **k: real_result
    )

    plan = _make_provider_plan(tmp_path, provider=Provider.KIMI, model="default")
    result = ProviderAdapter().run(plan, "prompt")

    assert result.status == "success"
    assert result.completion_text == "a real answer"
    assert result.error is None


# ---------------------------------------------------------------------------
# Test 3: dispatch_cli surfaces the failure to stderr for the provider lane
# ---------------------------------------------------------------------------


def test_dispatch_cli_prints_provider_lane_error_on_failure(tmp_path, capsys):
    """dispatch_cli.run_dispatch must not silently swallow a provider-lane
    failure into a bare exit code — it must print the raw error to stderr so
    the failure is visible to whoever ran `bin/vnx dispatch <id>`. Drives the
    REAL run_dispatch() (only run_envelope_plan + the snapshot are mocked),
    not a reimplementation of the print logic.
    """
    from test_dispatch_cli import _clean_snapshot, _make_spec_file  # noqa: PLC0415
    from dispatch_cli import run_dispatch
    from dispatch_envelope import EnvelopeResult

    failing_result = EnvelopeResult(
        status="failure",
        returncode=1,
        report_path=None,
        receipt_path=None,
        completion_text="",
        error="provider=kimi returned an empty completion with returncode=0 — "
              "refusing to report a silent empty success",
    )

    spec_file = _make_spec_file(tmp_path, provider="kimi", target_slot="T1")

    with patch("dispatch_cli.build_runtime_snapshot", return_value=_clean_snapshot()), \
         patch("dispatch_cli.run_envelope_plan", return_value=failing_result):
        rc = run_dispatch(spec_file)

    assert rc == 1
    captured = capsys.readouterr()
    assert "provider lane failure" in captured.err
    assert "empty completion" in captured.err
