"""tests/test_sonnet_5_5_registry_and_model_resolved.py

Dispatch 20260929-registry-sonnet-5-5.

Two facts pinned here:

1. The registry knows Sonnet 5.5. Since CLI 2.1.284 (2026-09-28 ~22:00) the alias
   ``sonnet`` in ``claude -p --model sonnet`` resolves to ``claude-sonnet-5-5``;
   the registry alias and the ``sonnet-5-5`` entry carry that id and the
   first-party price (2.00 / 10.00, cache hit 0.20). ``sonnet-5`` stays for the
   comparison of 5 against 5.5.

2. A receipt records the model that actually ran. The receipt's ``model`` keeps
   the REQUESTED value (``sonnet``); the init event of the claude stream carries
   the resolved id, and it lands on the receipt as ``model_resolved``, normalized
   through the same normalizer. No init event, no field.

No real claude process is started and nothing touches a real ~/.vnx-data store.
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

SCRIPTS_LIB = Path(__file__).resolve().parent.parent / "scripts" / "lib"
sys.path.insert(0, str(SCRIPTS_LIB))

import dispatch_envelope
from dispatch_envelope import (
    ClaudeSubprocessAdapter,
    _AdapterResult,
    run_envelope_headless_plan,
)
from dispatch_internal import issue_permit
from dispatch_plan import ExecutionPlan
from dispatch_spec import Isolation, Provider
from envelope_types import EnvelopeSpec
from providers import model_normalizer, provider_registry
from provider_dispatch import _load_pricing_from_registry
from provider_spawns import claude_spawn
from subprocess_adapter import StreamEvent


# ---------------------------------------------------------------------------
# (a) Registry
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def anthropic_models():
    return provider_registry.load()["anthropic"].models


@pytest.mark.parametrize("key", ["sonnet", "sonnet-5-5"])
def test_sonnet_alias_and_5_5_resolve_to_claude_sonnet_5_5(anthropic_models, key):
    entry = anthropic_models.get(key)
    assert entry is not None, f"registry has no anthropic/{key} entry"
    assert entry.litellm_name == "anthropic/claude-sonnet-5-5"
    assert (entry.cost_input_per_mtok, entry.cost_output_per_mtok) == (2.00, 10.00)
    assert entry.cost_cache_read_per_mtok == 0.20
    assert entry.max_tokens == 128000
    assert entry.context_window == 1000000
    assert entry.price_checked_at == "2026-09-29"
    assert entry.price_source.startswith("Anthropic first-party")
    assert "platform.claude.com/docs/en/about-claude/pricing" in entry.price_source


def test_sonnet_5_stays_claude_sonnet_5_for_the_comparison(anthropic_models):
    entry = anthropic_models["sonnet-5"]
    assert entry.litellm_name == "anthropic/claude-sonnet-5"
    assert (entry.cost_input_per_mtok, entry.cost_output_per_mtok) == (2.00, 10.00)


def test_cost_lookup_prices_the_alias_with_cache_hit_rate():
    pricing = _load_pricing_from_registry("claude", "sonnet")
    assert pricing == {"input": 2.00, "output": 10.00, "cache_read": 0.20}
    assert _load_pricing_from_registry("claude", "claude-sonnet-5-5") == pricing


def test_cost_loader_maps_claude_sonnet_5_5_to_its_own_key():
    from cost_loader import _ROUTING_MODEL_MAP

    assert _ROUTING_MODEL_MAP.get("claude-sonnet-5-5") == ("anthropic", "sonnet-5-5")
    assert _ROUTING_MODEL_MAP["claude-sonnet-5"] == ("anthropic", "sonnet-5")


# ---------------------------------------------------------------------------
# (b) Normalizer
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw, canonical",
    [
        ("claude-sonnet-5-5", "sonnet-5-5"),
        ("anthropic/claude-sonnet-5-5", "sonnet-5-5"),
        ("claude-sonnet-5", "sonnet-5"),
        ("sonnet", "sonnet"),
        ("sonnet-5-5", "sonnet-5-5"),
    ],
)
def test_normalizer_keeps_5_and_5_5_apart(raw, canonical):
    assert model_normalizer.normalize_model_name(raw) == canonical


# ---------------------------------------------------------------------------
# (c) model_resolved from the init event
# ---------------------------------------------------------------------------


class _FakeStreamAdapter:
    """SubprocessAdapter stand-in that replays a fixed event list."""

    def __init__(self, events):
        self._events = events
        self.event_store = None

    def deliver(self, *args, **kwargs):
        class _Ok:
            success = True
            failure_reason = None

        return _Ok()

    def read_events_with_timeout(self, terminal_id, **kwargs):
        yield from self._events

    def was_timed_out(self, terminal_id):
        return False

    def observe(self, terminal_id):
        class _Obs:
            transport_state = {"returncode": 0}

        return _Obs()

    def get_session_id(self, terminal_id):
        return "sess-test"

    def stop(self, terminal_id):
        return None


def _spawn(events):
    with patch.object(claude_spawn, "SubprocessAdapter", return_value=_FakeStreamAdapter(events)):
        return claude_spawn.spawn_claude(
            "do the thing",
            "sonnet",
            "20260929-model-resolved",
            "T1",
            scrub_env_keys=frozenset(),
        )


def test_spawn_reads_the_model_from_the_init_event():
    result = _spawn([
        StreamEvent(type="init", data={"session_id": "sess-test", "model": "claude-sonnet-5-5"}),
        StreamEvent(type="result", data={"usage": {"input_tokens": 1, "output_tokens": 1}}),
    ])
    assert getattr(result, "model_resolved", None) == "claude-sonnet-5-5"


def test_spawn_without_an_init_model_reports_nothing():
    result = _spawn([
        StreamEvent(type="init", data={"session_id": "sess-test", "model": None}),
        StreamEvent(type="result", data={}),
    ])
    assert getattr(result, "model_resolved", None) is None
    no_init = _spawn([StreamEvent(type="result", data={})])
    assert getattr(no_init, "model_resolved", None) is None


def test_adapter_carries_model_resolved_and_keeps_requested_model(tmp_path):
    class _Spawned:
        returncode = 0
        error = None
        timed_out = False
        stopped_early = False
        completion_text = "done"
        session_id = "sess-1"
        token_usage = {"input_tokens": 1, "output_tokens": 1}
        model = None
        model_resolved = "claude-sonnet-5-5"

    spec = EnvelopeSpec(
        dispatch_id="20260929-model-resolved",
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
    with patch("provider_spawns.claude_spawn.spawn_claude", return_value=_Spawned()):
        result = ClaudeSubprocessAdapter().run(spec)
    assert result.model == "sonnet"
    assert getattr(result, "model_resolved", None) == "claude-sonnet-5-5"


def _headless_plan(tmp_path: Path, dispatch_id: str) -> ExecutionPlan:
    instruction_file = tmp_path / "inst.md"
    instruction_file.write_text("# Claude dispatch\nDo the work.", encoding="utf-8")
    sha = hashlib.sha256(instruction_file.read_bytes()).hexdigest()
    return ExecutionPlan(
        dispatch_id=dispatch_id,
        project_id="vnx-dev",
        provider=Provider.CLAUDE,
        model="sonnet",
        lane="claude_headless",
        adapter="claude_subprocess",
        target_id="ephemeral",
        billing="subscription",
        serialization_class="claude-tmux",
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
        route_reason="D1,D2,D3",
        instruction_sha256=sha,
    )


def _worker(model_resolved=None) -> _AdapterResult:
    """A finished claude worker. model_resolved is set as an attribute after
    construction so the helper itself never depends on the field existing."""
    worker = _AdapterResult(returncode=0, completion_text="done", status="success", model="sonnet")
    if model_resolved is not None:
        worker.model_resolved = model_resolved
    return worker


def _own_receipt(tmp_path: Path, worker: _AdapterResult, dispatch_id: str) -> dict:
    plan = _headless_plan(tmp_path, dispatch_id)
    permit = issue_permit(plan)
    state_dir = tmp_path / "state"
    data_dir = tmp_path / "data"
    (data_dir / "unified_reports").mkdir(parents=True)
    state_dir.mkdir()

    with patch("dispatch_worktree_isolation.resolve_consumer_project_root",
               return_value=tmp_path / "consumer-root"), \
         patch("dispatch_worktree_isolation.create_dispatch_worktree",
               return_value=tmp_path / "fake-wt"), \
         patch("dispatch_worktree_isolation.remove_dispatch_worktree"), \
         patch.object(ClaudeSubprocessAdapter, "run", return_value=worker), \
         patch.object(
             dispatch_envelope, "_enforce_push_pr",
             side_effect=lambda **kw: kw["result"],
         ):
        result = run_envelope_headless_plan(plan, permit, state_dir=state_dir, data_dir=data_dir)

    assert result.receipt_path is not None and result.receipt_path.exists()
    lines = [json.loads(l) for l in result.receipt_path.read_text().splitlines() if l.strip()]
    own = [r for r in lines if r.get("dispatch_id") == dispatch_id]
    assert own, "the dispatch's own receipt must land on the ledger"
    return own[-1]


def test_receipt_keeps_requested_model_and_stamps_model_resolved(tmp_path):
    worker = _worker("claude-sonnet-5-5")
    receipt = _own_receipt(tmp_path, worker, "20260929-mr-with-init")
    assert receipt["model"] == "sonnet"
    assert receipt.get("model_resolved") == "sonnet-5-5"


def test_receipt_before_the_switch_resolves_to_sonnet_5(tmp_path):
    worker = _worker("claude-sonnet-5")
    receipt = _own_receipt(tmp_path, worker, "20260929-mr-before-switch")
    assert receipt["model"] == "sonnet"
    assert receipt.get("model_resolved") == "sonnet-5"


def test_receipt_without_init_event_has_no_model_resolved(tmp_path):
    worker = _worker()
    receipt = _own_receipt(tmp_path, worker, "20260929-mr-no-init")
    assert receipt["model"] == "sonnet"
    assert "model_resolved" not in receipt
