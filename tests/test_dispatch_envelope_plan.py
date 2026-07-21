"""test_dispatch_envelope_plan.py — Tests for PR-3: ProviderAdapter + run_envelope_plan.

Covers:
1. require_permit backstop: forged/bare permit raises PermissionError; valid permit proceeds.
2. Non-provider lane rejection: claude_tmux_subscription → ValueError.
3. Provider routing: each provider routes to its spawn_* fn; no _dispatch_* wrapper invoked.
4. File-ref instruction delivery: instruction read from file, spawn receives raw text.
5. Governance emit: _govern produces receipt line + report (real governance_emit pipeline).
6. Legacy path unchanged: run_envelope(spec, "codex") still routes to CodexAdapter.
"""
from __future__ import annotations

import hashlib
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
from typing import Any, Dict, Optional
from unittest.mock import MagicMock, patch

import pytest

SCRIPTS_LIB = Path(__file__).resolve().parent.parent / "scripts" / "lib"
sys.path.insert(0, str(SCRIPTS_LIB))

import dispatch_envelope
from dispatch_envelope import (
    EnvelopeGovernError,
    EnvelopeSpec,
    LaneRouter,
    ProviderAdapter,
    _AdapterResult,
    run_envelope,
    run_envelope_plan,
)
from dispatch_internal import ExecutionPermit, issue_permit
from dispatch_plan import ExecutionPlan
from dispatch_spec import DispatchPath, Isolation, Provider


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_provider_plan(
    tmp_path: Path,
    *,
    provider: Provider = Provider.CODEX,
    model: str = "gpt-test",
    dispatch_id: str = "test-dispatch-pr3",
    target_id: str = "T1",
    instruction_file: Optional[Path] = None,
) -> ExecutionPlan:
    if instruction_file is None:
        instruction_file = tmp_path / f"instruction-{provider.value.replace(':', '-')}.md"
        instruction_file.write_text("# Test dispatch\nDo something.", encoding="utf-8")
    # PR-4c: executors now require a valid 64-hex plan hash before delivery, so a
    # well-formed fixture plan carries the sha256 of its instruction file content
    # (mirrors compile_plan, which always propagates it from the ValidatedSpec).
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
        target_id=target_id,
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
        route_reason="D1,D2,D3,D4,D5,D6,D7,D8,D9,D10,D11,D12",
        instruction_sha256=sha256,
    )


def _make_claude_plan(tmp_path: Path) -> ExecutionPlan:
    instruction_file = tmp_path / "claude_inst.md"
    instruction_file.write_text("# Claude dispatch", encoding="utf-8")
    # PR-4c: carry the instruction sha256 so the plan is well-formed under the
    # executors' mandatory 64-hex hash gate (see _make_provider_plan note).
    sha256 = hashlib.sha256(
        instruction_file.read_text(encoding="utf-8").encode("utf-8")
    ).hexdigest()
    return ExecutionPlan(
        dispatch_id="test-claude-dispatch",
        project_id="vnx-dev",
        provider=Provider.CLAUDE,
        model="sonnet",
        lane="claude_tmux_subscription",
        adapter="tmux_claude",
        target_id="ephemeral",
        billing="subscription",
        serialization_class="claude-tmux",
        isolation=Isolation.WORKTREE,
        require_worktree=True,
        seed_materialize=False,
        instruction_delivery="file_ref",
        report_contract="required",
        warmup="verify_strict",
        deadline_seconds=3600,
        base_ref="main",
        dispatch_paths=(),
        instruction_file=instruction_file,
        route_reason="D1,D2,D3",
        instruction_sha256=sha256,
    )


@dataclass
class _FakeSpawnResult:
    returncode: int = 0
    completion_text: str = "done"
    timed_out: bool = False
    stopped_early: bool = False
    error: Optional[str] = None
    event_writer_failures: int = 0
    token_usage: Dict[str, Any] = field(default_factory=dict)


def _fake_adapter_success() -> _AdapterResult:
    return _AdapterResult(returncode=0, completion_text="done", status="success")


# ---------------------------------------------------------------------------
# Test 1: require_permit backstop
# ---------------------------------------------------------------------------


def test_run_envelope_plan_requires_permit(tmp_path):
    plan = _make_provider_plan(tmp_path)
    state_dir = tmp_path / "state"
    data_dir = tmp_path / "data"
    state_dir.mkdir()
    data_dir.mkdir()

    # Bare permit (default _sentinel=None) must be rejected
    bare_permit = ExecutionPermit(dispatch_id=plan.dispatch_id, plan_digest=plan.digest())
    with pytest.raises(PermissionError):
        run_envelope_plan(plan, bare_permit, state_dir=state_dir, data_dir=data_dir)

    # Wrong digest must be rejected even if sentinel were somehow set
    forged = ExecutionPermit(dispatch_id=plan.dispatch_id, plan_digest="bad-digest")
    with pytest.raises(PermissionError):
        run_envelope_plan(plan, forged, state_dir=state_dir, data_dir=data_dir)

    # Valid permit: proceed (mock spawn + govern to avoid real I/O)
    valid_permit = issue_permit(plan)
    fake_receipt = state_dir / "t0_receipts.ndjson"
    fake_receipt.touch()

    with patch.object(ProviderAdapter, "run", return_value=_fake_adapter_success()):
        with patch("dispatch_envelope._govern", return_value=(None, fake_receipt)):
            result = run_envelope_plan(plan, valid_permit, state_dir=state_dir, data_dir=data_dir)

    assert result.status == "success"
    assert result.returncode == 0


# ---------------------------------------------------------------------------
# Test 2: non-provider lane rejection
# ---------------------------------------------------------------------------


def test_rejects_non_provider_lane(tmp_path):
    plan = _make_claude_plan(tmp_path)
    permit = issue_permit(plan)

    with pytest.raises(ValueError, match="provider lane"):
        run_envelope_plan(plan, permit, state_dir=tmp_path / "state", data_dir=tmp_path / "data")


# ---------------------------------------------------------------------------
# Test 3: provider routing — each provider calls its spawn, no _dispatch_* wrapper
# ---------------------------------------------------------------------------


@pytest.fixture
def stubbed_provider_spawns(monkeypatch):
    """Stub every provider spawn_* and resolver helper (registry/env/CLI-free).

    Split out of the former single test_provider_adapter_routes_each_provider
    (OI-709, function-size gate) so each provider gets its own <70-line test
    function while sharing the same routing-mock setup.

    Returns a SimpleNamespace(calls, litellm_calls, dispatch_wrapper_called) —
    `calls`/`litellm_calls` record each spawn_* invocation; `dispatch_wrapper_called`
    must stay empty (the envelope must never call a _dispatch_* wrapper directly).
    """
    calls: Dict[str, Dict[str, Any]] = {}
    litellm_calls: list = []

    def _make_spawn(name):
        def fake(*args, **kwargs):
            calls[name] = {"args": args, "kwargs": kwargs}
            return _FakeSpawnResult()
        return fake

    def fake_litellm(*args, **kwargs):
        litellm_calls.append({"args": args, "kwargs": kwargs})
        return _FakeSpawnResult()

    monkeypatch.setattr("provider_spawns.codex_spawn.spawn_codex", _make_spawn("codex"))
    monkeypatch.setattr("provider_spawns.kimi_spawn.spawn_kimi", _make_spawn("kimi"))
    monkeypatch.setattr("provider_spawns.gemini_spawn.spawn_gemini", _make_spawn("gemini"))
    monkeypatch.setattr("provider_spawns.litellm_spawn.spawn_litellm", fake_litellm)
    monkeypatch.setattr(
        "provider_spawns.deepseek_harness_spawn.spawn_deepseek_harness", _make_spawn("deepseek_harness")
    )
    monkeypatch.setattr(
        "provider_spawns.local_gemma_spawn.spawn_local_gemma", _make_spawn("local_gemma")
    )

    # Stub resolution helpers and token extractor to avoid registry/env access
    monkeypatch.setattr("provider_dispatch._resolve_codex_model", lambda: "gpt-codex-test")
    monkeypatch.setattr("provider_dispatch._resolve_kimi_model_label", lambda: "kimi-test")
    monkeypatch.setattr("provider_dispatch._kimi_resolve_cli_model_arg", lambda k: "kimi-test-cli-arg")
    monkeypatch.setattr("provider_dispatch._resolve_deepseek_model", lambda: "deepseek/test")
    monkeypatch.setattr("provider_dispatch._resolve_zai_model", lambda m=None: "openrouter/glm-test")
    monkeypatch.setattr("provider_dispatch._resolve_moonshot_model", lambda m=None: "moonshot/kimi-test")
    monkeypatch.setattr("provider_dispatch._extract_token_usage", lambda r, p: {})
    monkeypatch.setattr("provider_dispatch._build_lane_key", lambda b, m: f"litellm:{b}:test")
    monkeypatch.setattr(
        "provider_spawns.deepseek_harness_spawn.resolve_harness_model", lambda m: "deepseek-v4-test"
    )
    monkeypatch.setattr(
        "provider_dispatch._MLX_MODEL_MAP", {"gemma-4b-local": "mlx-community/gemma-3-4b-it-4bit"}
    )

    # Track _dispatch_* calls — must all remain uncalled
    dispatch_wrapper_called: list = []
    for fn_name in [
        "_dispatch_codex", "_dispatch_kimi", "_dispatch_gemini",
        "_dispatch_litellm", "_dispatch_deepseek_harness", "_dispatch_local_gemma",
    ]:
        def _make_bad(n):
            def _bad(*a, **k):
                dispatch_wrapper_called.append(n)
            return _bad
        monkeypatch.setattr(f"provider_dispatch.{fn_name}", _make_bad(fn_name))

    return SimpleNamespace(
        calls=calls, litellm_calls=litellm_calls, dispatch_wrapper_called=dispatch_wrapper_called
    )


def test_provider_adapter_routes_codex(tmp_path, stubbed_provider_spawns):
    """codex routes to spawn_codex; no _dispatch_* wrapper invoked."""
    adapter = ProviderAdapter()
    r = adapter.run(_make_provider_plan(tmp_path, provider=Provider.CODEX, model="default"), "prompt")
    assert r.status == "success", f"codex: {r}"
    assert "codex" in stubbed_provider_spawns.calls
    assert stubbed_provider_spawns.dispatch_wrapper_called == []


def test_provider_adapter_routes_kimi(tmp_path, stubbed_provider_spawns):
    """kimi routes to spawn_kimi (via ProviderAdapter._run_kimi); no _dispatch_* wrapper invoked."""
    adapter = ProviderAdapter()
    r = adapter.run(_make_provider_plan(tmp_path, provider=Provider.KIMI, model="default"), "prompt")
    assert r.status == "success", f"kimi: {r}"
    assert "kimi" in stubbed_provider_spawns.calls
    assert stubbed_provider_spawns.dispatch_wrapper_called == []


def test_provider_adapter_routes_gemini(tmp_path, stubbed_provider_spawns):
    """gemini routes to spawn_gemini; no _dispatch_* wrapper invoked."""
    adapter = ProviderAdapter()
    r = adapter.run(_make_provider_plan(tmp_path, provider=Provider.GEMINI, model="default"), "prompt")
    assert r.status == "success", f"gemini: {r}"
    assert "gemini" in stubbed_provider_spawns.calls
    assert stubbed_provider_spawns.dispatch_wrapper_called == []


def test_provider_adapter_routes_litellm_deepseek(tmp_path, stubbed_provider_spawns):
    """litellm:deepseek routes to spawn_litellm with the resolved deepseek model."""
    adapter = ProviderAdapter()
    r = adapter.run(
        _make_provider_plan(tmp_path, provider=Provider.LITELLM_DEEPSEEK, model="default"), "prompt"
    )
    assert r.status == "success", f"litellm:deepseek: {r}"
    litellm_calls = stubbed_provider_spawns.litellm_calls
    assert litellm_calls, "spawn_litellm not called for litellm:deepseek"
    assert litellm_calls[-1]["kwargs"].get("sub_provider") == "deepseek"
    assert litellm_calls[-1]["kwargs"].get("model") == "deepseek/test"
    assert stubbed_provider_spawns.dispatch_wrapper_called == []


def test_provider_adapter_routes_litellm_zai(tmp_path, stubbed_provider_spawns):
    """litellm:zai routes to spawn_litellm with the resolved zai registry model."""
    adapter = ProviderAdapter()
    r = adapter.run(
        _make_provider_plan(tmp_path, provider=Provider.LITELLM_ZAI, model="default"), "prompt"
    )
    assert r.status == "success", f"litellm:zai: {r}"
    litellm_calls = stubbed_provider_spawns.litellm_calls
    assert litellm_calls, "spawn_litellm not called for litellm:zai"
    assert litellm_calls[-1]["kwargs"].get("sub_provider") == "zai"
    assert litellm_calls[-1]["kwargs"].get("model") == "openrouter/glm-test"
    assert stubbed_provider_spawns.dispatch_wrapper_called == []


def test_provider_adapter_routes_litellm_moonshot(tmp_path, stubbed_provider_spawns):
    """litellm:moonshot routes to spawn_litellm with sub_provider=moonshot."""
    adapter = ProviderAdapter()
    r = adapter.run(
        _make_provider_plan(tmp_path, provider=Provider.LITELLM_MOONSHOT, model="default"), "prompt"
    )
    assert r.status == "success", f"litellm:moonshot: {r}"
    litellm_calls = stubbed_provider_spawns.litellm_calls
    assert litellm_calls, "spawn_litellm not called for litellm:moonshot"
    assert litellm_calls[-1]["kwargs"].get("sub_provider") == "moonshot"
    assert stubbed_provider_spawns.dispatch_wrapper_called == []


def test_provider_adapter_routes_deepseek_harness(tmp_path, stubbed_provider_spawns):
    """deepseek-harness routes to spawn_deepseek_harness; no _dispatch_* wrapper invoked."""
    adapter = ProviderAdapter()
    r = adapter.run(
        _make_provider_plan(tmp_path, provider=Provider.DEEPSEEK_HARNESS, model="default"), "prompt"
    )
    assert r.status == "success", f"deepseek-harness: {r}"
    assert "deepseek_harness" in stubbed_provider_spawns.calls
    assert stubbed_provider_spawns.dispatch_wrapper_called == []


def test_provider_adapter_routes_local_gemma(tmp_path, stubbed_provider_spawns):
    """local-gemma routes to spawn_local_gemma; no _dispatch_* wrapper invoked."""
    adapter = ProviderAdapter()
    r = adapter.run(
        _make_provider_plan(tmp_path, provider=Provider.LOCAL_GEMMA, model="default"), "prompt"
    )
    assert r.status == "success", f"local-gemma: {r}"
    assert "local_gemma" in stubbed_provider_spawns.calls
    assert stubbed_provider_spawns.dispatch_wrapper_called == []


# ---------------------------------------------------------------------------
# Test 4: instruction read from file (file-ref neutralises injection text)
# ---------------------------------------------------------------------------


def test_instruction_read_from_file(tmp_path, monkeypatch):
    """Instruction file containing 'claude -p' is passed verbatim to spawn; no injection."""
    raw_text = "claude -p 'rm -rf /'\n"
    inst_file = tmp_path / "inst.md"
    inst_file.write_text(raw_text, encoding="utf-8")

    plan = _make_provider_plan(tmp_path, provider=Provider.CODEX, instruction_file=inst_file)
    permit = issue_permit(plan)

    state_dir = tmp_path / "state"
    data_dir = tmp_path / "data"
    state_dir.mkdir()
    data_dir.mkdir()

    received_prompts: list = []

    def fake_codex(prompt, model, dispatch_id, terminal_id, event_writer, cwd):
        received_prompts.append(prompt)
        return _FakeSpawnResult()

    monkeypatch.setattr("provider_spawns.codex_spawn.spawn_codex", fake_codex)
    monkeypatch.setattr("provider_dispatch._extract_token_usage", lambda r, p: {})
    # _prepare falls back silently (intelligence_injection not available in tests)

    fake_receipt = state_dir / "t0_receipts.ndjson"
    fake_receipt.touch()

    with patch("dispatch_envelope._govern", return_value=(None, fake_receipt)):
        result = run_envelope_plan(plan, permit, state_dir=state_dir, data_dir=data_dir)

    assert result.status == "success"
    assert received_prompts, "spawn_codex was not called"
    # The raw text (possibly enriched, but since intelligence_injection is absent,
    # it equals the file content) must reach the spawn unmodified in its base form
    assert "claude -p" in received_prompts[0]


# ---------------------------------------------------------------------------
# Test 5: _govern emits receipt line + report (real governance_emit pipeline)
# ---------------------------------------------------------------------------


def test_govern_emits_receipt_and_report(tmp_path, monkeypatch):
    """_govern with a tmp state_dir/data_dir produces a receipt line + report file."""
    state_dir = tmp_path / "state"
    data_dir = tmp_path / "data"
    state_dir.mkdir()
    data_dir.mkdir()

    dispatch_id = "test-govern-emit-pr3"
    spec = EnvelopeSpec(
        dispatch_id=dispatch_id,
        terminal_id="T1",
        provider="codex",
        model="gpt-test",
        instruction="do something",
        role=None,
        pr_id=None,
        state_dir=state_dir,
        data_dir=data_dir,
    )
    fake_result = _AdapterResult(returncode=0, completion_text="all good", status="success")
    start = end = datetime.now(timezone.utc)

    report_path, receipt_path = dispatch_envelope._govern(spec, fake_result, start, end)

    # Receipt line written
    assert receipt_path is not None
    assert receipt_path.exists()
    receipt_text = receipt_path.read_text(encoding="utf-8")
    assert dispatch_id in receipt_text

    # Report written
    assert report_path is not None
    assert report_path.exists()


# ---------------------------------------------------------------------------
# Test 6: legacy run_envelope unchanged — CodexAdapter still routes correctly
# ---------------------------------------------------------------------------


def test_legacy_run_envelope_unchanged(tmp_path, monkeypatch):
    """run_envelope(spec, 'codex') still routes to CodexAdapter (legacy path intact)."""
    state_dir = tmp_path / "state"
    data_dir = tmp_path / "data"
    state_dir.mkdir()
    data_dir.mkdir()

    spec = EnvelopeSpec(
        dispatch_id="test-legacy-codex",
        terminal_id="T1",
        provider="codex",
        model="gpt-test",
        instruction="legacy dispatch",
        role=None,
        pr_id=None,
        state_dir=state_dir,
        data_dir=data_dir,
    )

    codex_adapter_calls = []

    original_run = dispatch_envelope.CodexAdapter.run

    def spy_run(self, spec_arg, event_writer=None, cwd=None):
        codex_adapter_calls.append(spec_arg.dispatch_id)
        return _AdapterResult(returncode=0, completion_text="legacy done", status="success")

    monkeypatch.setattr(dispatch_envelope.CodexAdapter, "run", spy_run)

    fake_receipt = state_dir / "t0_receipts.ndjson"
    fake_receipt.touch()

    with patch("dispatch_envelope._govern", return_value=(None, fake_receipt)):
        result = run_envelope(spec, lane="codex")

    assert result.status == "success"
    assert codex_adapter_calls == ["test-legacy-codex"], (
        f"CodexAdapter.run was not called; got: {codex_adapter_calls}"
    )


# ---------------------------------------------------------------------------
# Test 7: PR-7 — envelope owns worktree; fail-loud on creation failure
# ---------------------------------------------------------------------------


_FAKE_WT_PATH = Path("/tmp/fake-worktrees/envelope-test")


class TestEnvelopeWorktreeIsolation:
    def test_worktree_created_and_passed_as_cwd(self, tmp_path):
        """run_envelope_plan creates worktree and passes cwd to ProviderAdapter.run."""
        plan = _make_provider_plan(tmp_path, dispatch_id="wt-cwd-test")
        permit = issue_permit(plan)
        state_dir = tmp_path / "state"
        data_dir = tmp_path / "data"
        state_dir.mkdir()
        data_dir.mkdir()
        fake_receipt = state_dir / "t0_receipts.ndjson"
        fake_receipt.touch()

        adapter_calls: list = []

        def fake_adapter_run(self, plan_arg, instruction, *, event_writer=None, cwd=None):
            adapter_calls.append({"cwd": cwd})
            return _fake_adapter_success()

        with patch("dispatch_worktree_isolation.create_dispatch_worktree", return_value=_FAKE_WT_PATH) as mock_create, \
             patch("dispatch_worktree_isolation.remove_dispatch_worktree") as mock_remove, \
             patch.object(ProviderAdapter, "run", fake_adapter_run), \
             patch("dispatch_envelope._govern", return_value=(None, fake_receipt)):
            result = run_envelope_plan(plan, permit, state_dir=state_dir, data_dir=data_dir)

        assert result.status == "success"
        mock_create.assert_called_once_with("wt-cwd-test")
        assert adapter_calls, "ProviderAdapter.run was not called"
        assert adapter_calls[0]["cwd"] == _FAKE_WT_PATH, (
            f"expected cwd={_FAKE_WT_PATH}, got cwd={adapter_calls[0]['cwd']}"
        )
        mock_remove.assert_called_once_with("wt-cwd-test")

    def test_worktree_removed_on_spawn_failure(self, tmp_path):
        """remove_dispatch_worktree is called even when ProviderAdapter.run raises."""
        plan = _make_provider_plan(tmp_path, dispatch_id="wt-rm-fail-test")
        permit = issue_permit(plan)
        state_dir = tmp_path / "state"
        data_dir = tmp_path / "data"
        state_dir.mkdir()
        data_dir.mkdir()

        def bad_adapter_run(self, plan_arg, instruction, *, event_writer=None, cwd=None):
            raise RuntimeError("spawn exploded")

        with patch("dispatch_worktree_isolation.create_dispatch_worktree", return_value=_FAKE_WT_PATH), \
             patch("dispatch_worktree_isolation.remove_dispatch_worktree") as mock_remove, \
             patch.object(ProviderAdapter, "run", bad_adapter_run):
            with pytest.raises(RuntimeError, match="spawn exploded"):
                run_envelope_plan(plan, permit, state_dir=state_dir, data_dir=data_dir)

        mock_remove.assert_called_once_with("wt-rm-fail-test")

    def test_worktree_creation_failure_aborts_dispatch(self, tmp_path):
        """Worktree creation failure → dispatch aborts (status=failure, rc!=0), spawn NOT called."""
        plan = _make_provider_plan(tmp_path, dispatch_id="wt-create-fail-test")
        permit = issue_permit(plan)
        state_dir = tmp_path / "state"
        data_dir = tmp_path / "data"
        state_dir.mkdir()
        data_dir.mkdir()
        fake_receipt = state_dir / "t0_receipts.ndjson"
        fake_receipt.touch()

        adapter_calls: list = []

        def bad_create(dispatch_id):
            raise RuntimeError("no disk space")

        def spy_adapter_run(self, plan_arg, instruction, *, event_writer=None, cwd=None):
            adapter_calls.append({"cwd": cwd})
            return _fake_adapter_success()

        with patch("dispatch_worktree_isolation.create_dispatch_worktree", side_effect=bad_create), \
             patch("dispatch_worktree_isolation.remove_dispatch_worktree") as mock_remove, \
             patch.object(ProviderAdapter, "run", spy_adapter_run), \
             patch("dispatch_envelope._govern", return_value=(None, fake_receipt)) as mock_govern:
            result = run_envelope_plan(plan, permit, state_dir=state_dir, data_dir=data_dir)

        assert result.status == "failure"
        assert result.returncode == 1
        assert result.error is not None
        assert "no shared-checkout fallback" in result.error
        assert adapter_calls == [], "ProviderAdapter.run must NOT be called when worktree creation fails"
        mock_govern.assert_called_once()
        mock_remove.assert_not_called()
