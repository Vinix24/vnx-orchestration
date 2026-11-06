"""Characterisation of the frontmatter ``_govern_impl`` stamps on its report (G2-14).

The govern step writes a schema-complete YAML frontmatter block in front of every
report. That block is a contract of its own: the report writer serialises it in
dict order (``yaml.dump(..., sort_keys=False)``), consumers read its keys, and
``schemas/unified_report_v1.json`` validates it. Moving the block into a helper
must not change a single key, a single value or the key order.

These tests were written against the pre-extraction code and pin, per input:

* the full frontmatter dict (every key and value), and
* ``list(frontmatter)`` — the serialisation order.

Scenarios: a worker receipt carrying model/exit code/token usage (with
``cache_hit`` instead of ``cache_read``)/cost, no receipt at all, the chain-link
keys from the spec, the same keys from the door's environment, and none of them
set.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

_SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(_SCRIPTS / "lib"))
sys.path.insert(0, str(_SCRIPTS))

import governance_emit  # noqa: E402
from dispatch_govern import (  # noqa: E402
    GovernRaw,
    GovernSpec,
    _split_yaml_frontmatter,
    govern,
)

_CHAIN_ENV_VARS = (
    "VNX_PARENT_DISPATCH",
    "VNX_TASK_CLASS",
    "VNX_TIER_FROM",
    "VNX_TIER_TO",
)


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

@pytest.fixture()
def tmp_data(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    return data_dir


@pytest.fixture()
def tmp_state(tmp_path):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    return state_dir


def _make_spec(data_dir: Path, state_dir: Path, **kwargs) -> GovernSpec:
    return GovernSpec(
        dispatch_id=kwargs.get("dispatch_id", "fm-test-001"),
        terminal_id=kwargs.get("terminal_id", "T1"),
        instruction=kwargs.get("instruction", "Do the thing."),
        data_dir=data_dir,
        state_dir=state_dir,
        pr_id=kwargs.get("pr_id"),
        base_sha=kwargs.get("base_sha"),
        worktree_path=kwargs.get("worktree_path"),
        role=kwargs.get("role", "backend-developer"),
        model=kwargs.get("model", "sonnet"),
        parent_dispatch=kwargs.get("parent_dispatch"),
        task_class=kwargs.get("task_class"),
        tier_from=kwargs.get("tier_from"),
        tier_to=kwargs.get("tier_to"),
        repo_root=kwargs.get("repo_root"),
    )


def _run_govern(
    tmp_data: Path,
    tmp_state: Path,
    monkeypatch,
    *,
    receipt=None,
    duration_seconds: float = 5.0,
    env: dict | None = None,
    **spec_kwargs,
):
    """Drive the real govern() and return its outcome (synthesized lane path)."""
    monkeypatch.setenv("VNX_SHARED_GOVERN", "1")
    monkeypatch.delenv("VNX_CONTRACT_VALIDATE", raising=False)
    for var in _CHAIN_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    for var, value in (env or {}).items():
        monkeypatch.setenv(var, value)

    spec = _make_spec(tmp_data, tmp_state, **spec_kwargs)
    raw = GovernRaw(receipt=receipt, duration_seconds=duration_seconds)
    with patch("dispatch_govern._git_summary", return_value="feat: synthesized report"), \
         patch("dispatch_govern._git_changes", return_value="scripts/lib/x.py | 1 +"):
        outcome = govern(spec, raw, lane="tmux_interactive")
    return outcome


def _frontmatter(outcome) -> dict:
    assert outcome.report_path is not None, "govern() wrote no report"
    content = outcome.report_path.read_text(encoding="utf-8")
    fm, _body = _split_yaml_frontmatter(content)
    assert isinstance(fm, dict) and fm, f"report has no parseable frontmatter: {content[:200]!r}"
    return fm


def _assert_key_order(fm: dict, expected: list) -> None:
    assert list(fm) == expected, (
        "frontmatter key order changed — the report writer serialises in dict "
        f"order. got={list(fm)!r} expected={expected!r}"
    )


def _route_decision(model: str, terminal_id: str = "T1") -> dict:
    return {
        "strategy": "synthesized",
        "selected_provider": "claude",
        "selected_model": model,
        "reason": (
            f"tmux interactive lane — govern synthesis (terminal={terminal_id})"
        ),
    }


def _expected(
    *,
    model: str,
    task_class: str,
    exit_code: int,
    token_usage: dict,
    cost_fields: dict,
    lane: str = "tmux_interactive",
    contract_status: str = "synthesized",
    permission_enforcement: str = "soft",
    dispatch_id: str = "fm-test-001",
    terminal_id: str = "T1",
    role: str = "backend-developer",
    duration_seconds: float = 5.0,
) -> dict:
    return {
        "schema_version": 1,
        "dispatch_id": dispatch_id,
        "provider": "claude",
        "sub_provider": "anthropic",
        "model": model,
        "terminal_id": terminal_id,
        "pool_id": "interactive",
        "role": role,
        "task_class": task_class,
        "pr_id": "none",
        "duration_seconds": duration_seconds,
        "exit_code": exit_code,
        "token_usage": token_usage,
        **cost_fields,
        "route_decision": _route_decision(model, terminal_id),
        "lane": lane,
        "contract_status": contract_status,
        "permission_enforcement": permission_enforcement,
    }


# ---------------------------------------------------------------------------
# (a) worker receipt: model, exit code, token usage (cache_hit), cost fields
# ---------------------------------------------------------------------------

def test_frontmatter_from_worker_receipt(tmp_data, tmp_state, monkeypatch):
    """A receipt's model/exit_code/token_usage (``cache_hit`` alias)/cost reach the frontmatter."""
    outcome = _run_govern(
        tmp_data, tmp_state, monkeypatch,
        receipt={
            "status": "done",
            "model": "deepseek-flash",
            "exit_code": 3,
            "token_usage": {"input": 120, "output": 45, "cache_hit": 900},
            "cost_usd": 0.42,
            "cost_source": "measured:litellm",
        },
        duration_seconds=12.5,
    )

    fm = _frontmatter(outcome)
    expected = _expected(
        model="deepseek-flash",
        task_class="implementation",
        exit_code=3,
        token_usage={"input": 120, "output": 45, "cache_read": 900},
        cost_fields={"cost_usd": 0.42, "cost_source": "measured:litellm"},
        duration_seconds=12.5,
    )
    assert fm == expected
    _assert_key_order(fm, list(expected))


# ---------------------------------------------------------------------------
# (b) no raw receipt: model from spec, zero usage, timeout status
# ---------------------------------------------------------------------------

def test_frontmatter_without_receipt_falls_back_to_spec(tmp_data, tmp_state, monkeypatch):
    """No receipt: model from the spec, zeroed usage, status ``timeout`` in the body."""
    outcome = _run_govern(
        tmp_data, tmp_state, monkeypatch,
        receipt=None,
        duration_seconds=7.0,
        model="opus",
        terminal_id="T2",
    )

    fm = _frontmatter(outcome)
    expected = _expected(
        model="opus",
        task_class="implementation",
        exit_code=0,
        token_usage={"input": 0, "output": 0, "cache_read": 0},
        cost_fields={"cost_usd": 0.0},
        terminal_id="T2",
        duration_seconds=7.0,
    )
    assert fm == expected
    _assert_key_order(fm, list(expected))
    # The declared status the lane synthesises from, with no receipt, is "timeout".
    assert "- Status: timeout" in outcome.report_path.read_text(encoding="utf-8")


def test_frontmatter_receipt_without_token_usage_uses_raw_usage(tmp_data, tmp_state, monkeypatch):
    """A receipt without ``token_usage`` falls back to ``raw.token_usage``."""
    monkeypatch.setenv("VNX_SHARED_GOVERN", "1")
    spec = _make_spec(tmp_data, tmp_state)
    raw = GovernRaw(
        receipt={"status": "done", "model": "sonnet", "exit_code": 0},
        duration_seconds=5.0,
        token_usage={"input": 11, "output": 22, "cache_read": 33},
    )
    with patch("dispatch_govern._git_summary", return_value="feat: x"), \
         patch("dispatch_govern._git_changes", return_value="x | 1 +"):
        outcome = govern(spec, raw, lane="tmux_interactive")

    fm = _frontmatter(outcome)
    assert fm["token_usage"] == {"input": 11, "output": 22, "cache_read": 33}


# ---------------------------------------------------------------------------
# (c) chain-link keys from the spec (env present but must lose)
# ---------------------------------------------------------------------------

def test_frontmatter_chain_link_from_spec_beats_env(tmp_data, tmp_state, monkeypatch):
    """Spec values win over the door's env for the chain-link keys."""
    outcome = _run_govern(
        tmp_data, tmp_state, monkeypatch,
        receipt={"status": "done"},
        parent_dispatch="parent-from-spec",
        task_class="03_refactoring",
        tier_from="tier-1",
        tier_to="tier-2",
        env={
            "VNX_PARENT_DISPATCH": "parent-from-env",
            "VNX_TASK_CLASS": "task-class-from-env",
            "VNX_TIER_FROM": "tier-from-env",
            "VNX_TIER_TO": "tier-to-env",
        },
    )

    fm = _frontmatter(outcome)
    expected = _expected(
        model="sonnet",
        task_class="03_refactoring",
        exit_code=0,
        token_usage={"input": 0, "output": 0, "cache_read": 0},
        cost_fields={"cost_usd": 0.0},
    )
    expected["parent_dispatch"] = "parent-from-spec"
    expected["tier_from"] = "tier-1"
    expected["tier_to"] = "tier-2"
    assert fm == expected
    _assert_key_order(fm, list(expected))


# ---------------------------------------------------------------------------
# (d) chain-link keys only from the environment
# ---------------------------------------------------------------------------

def test_frontmatter_chain_link_from_env(tmp_data, tmp_state, monkeypatch):
    """With nothing in the spec, the chain-link keys come from the door's env."""
    outcome = _run_govern(
        tmp_data, tmp_state, monkeypatch,
        receipt={"status": "done"},
        env={
            "VNX_PARENT_DISPATCH": "parent-from-env",
            "VNX_TASK_CLASS": "task-class-from-env",
            "VNX_TIER_FROM": "tier-from-env",
            "VNX_TIER_TO": "tier-to-env",
        },
    )

    fm = _frontmatter(outcome)
    expected = _expected(
        model="sonnet",
        task_class="task-class-from-env",
        exit_code=0,
        token_usage={"input": 0, "output": 0, "cache_read": 0},
        cost_fields={"cost_usd": 0.0},
    )
    expected["parent_dispatch"] = "parent-from-env"
    expected["tier_from"] = "tier-from-env"
    expected["tier_to"] = "tier-to-env"
    assert fm == expected
    _assert_key_order(fm, list(expected))


# ---------------------------------------------------------------------------
# (e) chain-link keys set nowhere — absent, not null
# ---------------------------------------------------------------------------

def test_frontmatter_chain_link_absent(tmp_data, tmp_state, monkeypatch):
    """No spec value and no env value: the three keys are absent, task_class defaults."""
    outcome = _run_govern(tmp_data, tmp_state, monkeypatch, receipt={"status": "done"})

    fm = _frontmatter(outcome)
    expected = _expected(
        model="sonnet",
        task_class="implementation",
        exit_code=0,
        token_usage={"input": 0, "output": 0, "cache_read": 0},
        cost_fields={"cost_usd": 0.0},
    )
    assert fm == expected
    _assert_key_order(fm, list(expected))
    for key in ("parent_dispatch", "tier_from", "tier_to"):
        assert key not in fm, f"{key} must be absent when unset, not null"


# ---------------------------------------------------------------------------
# Negative control: the key-order assertion is load-bearing
# ---------------------------------------------------------------------------

def test_key_order_assertion_detects_reordering(tmp_data, tmp_state, monkeypatch):
    """A reordered frontmatter (injected at emit time) must fail the order check.

    This is the negative control for every ``_assert_key_order`` above: if the
    assertion could not tell order from content, the characterisation would
    prove nothing about the serialisation order the report writer depends on.
    """
    real_emit = governance_emit.emit_unified_report

    def _reordering_emit(**kwargs):
        fm = kwargs.get("frontmatter")
        if isinstance(fm, dict) and len(fm) > 2:
            keys = list(fm)
            keys[1], keys[2] = keys[2], keys[1]
            kwargs["frontmatter"] = {k: fm[k] for k in keys}
        return real_emit(**kwargs)

    monkeypatch.setattr(governance_emit, "emit_unified_report", _reordering_emit)

    outcome = _run_govern(tmp_data, tmp_state, monkeypatch, receipt={"status": "done"})
    fm = _frontmatter(outcome)
    assert list(fm)[1] == "provider" and list(fm)[2] == "dispatch_id"

    expected = _expected(
        model="sonnet",
        task_class="implementation",
        exit_code=0,
        token_usage={"input": 0, "output": 0, "cache_read": 0},
        cost_fields={"cost_usd": 0.0},
    )
    with pytest.raises(AssertionError, match="key order changed") as excinfo:
        _assert_key_order(fm, list(expected))
    # The failure names both the wrong and the expected order.
    message = str(excinfo.value)
    assert "['schema_version', 'provider', 'dispatch_id'" in message
    assert "['schema_version', 'dispatch_id', 'provider'" in message
