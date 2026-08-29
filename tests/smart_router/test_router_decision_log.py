"""Tests for smart_router.decision_log — the router writes down what it decided (OI-1494).

The smart router has been default-on since 2026-08-02: no kill-switch is set, yet every
tier flag and the canary sit at 0. ``staging_verdict`` therefore declines before
``resolve_tier_route`` is ever reached, so the route the router WOULD have chosen was
never computed, let alone recorded. A rollout with no observation layer has nothing to
ramp from — the operator cannot see what the router would have done, so no evidence ever
accumulates on which to switch a tier on.

These tests pin the fix: a dispatch whose tier is switched off still computes provider,
model and lane and appends a record, and its decline is exactly what it was before. The
router observes; it does not act.

Every test in this file fails on main 17de88de: ``decision_log`` does not exist there and
the decline path computes nothing.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "lib"))

from providers.smart_router import decision_log as dl
from providers.smart_router import door_routing as dr
from providers.smart_router import staging as st


@pytest.fixture(autouse=True)
def _reset_config_state(monkeypatch):
    """Neutralise config-registry global state (same contract as test_staging): operator
    config wiring from another test must not leak into the staging resolution here."""
    import config_registry as cr
    import config_runtime as crt

    cr.set_db_resolver(None)
    cr.set_default_project_id(None)
    crt._wired_for.clear()
    for key in list(st.TIER_FLAGS.values()) + [st.CANARY_PCT_KEY]:
        monkeypatch.delenv(key, raising=False)
        monkeypatch.delenv(f"VNX_OVERRIDE_{key[len('VNX_'):]}", raising=False)
    monkeypatch.delenv(dl.DISABLE_FLAG, raising=False)
    yield


def _records(state_dir: Path) -> list[dict]:
    path = dl.ledger_path(state_dir)
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _route_once(
    state_dir: Path,
    *,
    slot: str = "T1",
    instruction: str = "fix a typo",
    env: dict | None = None,
):
    """Route one dispatch. ``env`` defaults to an empty dict so the ambient process
    environment cannot decide the outcome of a test."""
    from dispatch_spec import Provider

    return dr.resolve_door_route(
        spec_provider=Provider.AUTO,
        spec_model=None,
        target_slot=slot,
        instruction_text=instruction,
        file_paths=["scripts/lib/example.py"],
        env={} if env is None else env,
        dispatch_id="20260829-beta-c1-observe",
        state_dir=state_dir,
    )


def _enable_every_tier(monkeypatch):
    for key in st.TIER_FLAGS.values():
        monkeypatch.setenv(key, "1")
    monkeypatch.setenv(st.CANARY_PCT_KEY, "100")


# ---------------------------------------------------------------------------
# The core of OI-1494: a switched-off tier still produces an observation
# ---------------------------------------------------------------------------

def test_disabled_tier_still_records_provider_model_and_lane(tmp_path):
    """The bug: every tier off => nothing computed, nothing written. The fix: the route
    is computed and recorded anyway, and NOT acted on."""
    result = _route_once(tmp_path)

    # unchanged behaviour: the dispatch is still declined
    assert result.route is None
    assert result.decline_reason.startswith(st.DECLINE_TIER_DISABLED)

    records = _records(tmp_path)
    assert len(records) == 1, "a declined decision must still leave exactly one record"
    rec = records[0]
    assert rec["event"] == "router_decision"
    assert rec["applied"] is False
    assert rec["decline_reason"] == result.decline_reason
    assert rec["tier"] == result.tier

    would = rec["would_route"]
    assert would["provider"], "provider must be computed even though the tier is off"
    assert would["model"], "model must be computed even though the tier is off"
    assert would["lane"], "lane must be computed even though the tier is off"


def test_would_route_is_also_exposed_on_the_result(tmp_path):
    """The computed-but-not-applied route is readable by the caller, not only by the
    ledger — the dry-run output must be able to show it."""
    result = _route_once(tmp_path)
    assert result.route is None
    assert result.would_route is not None
    assert result.would_route["provider"]
    assert result.would_route["model"]
    assert result.would_route["lane"]


def test_decline_reason_and_tier_are_byte_for_byte_unchanged(tmp_path):
    """Observation must not move the decision. The decline this returns is exactly the
    one staging_verdict produces for the same tier."""
    result = _route_once(tmp_path)
    expected = st.staging_verdict(
        result.tier,
        st.dispatch_group_key(
            "20260829-beta-c1-observe", "T1", "fix a typo", ["scripts/lib/example.py"]
        ),
        st.load_staging_config(),
    )
    assert result.decline_reason == expected
    assert result.route is None


# ---------------------------------------------------------------------------
# Applied routes are recorded too — one ledger, both halves of the canary
# ---------------------------------------------------------------------------

def test_applied_route_is_recorded_as_applied(tmp_path, monkeypatch):
    _enable_every_tier(monkeypatch)
    result = _route_once(tmp_path)

    assert result.route is not None, "every tier on + canary 100 must route"
    records = _records(tmp_path)
    assert len(records) == 1
    rec = records[0]
    assert rec["applied"] is True
    assert rec["decline_reason"] is None
    provider_enum, model, _reason = result.route
    assert rec["would_route"]["model"] == model
    assert rec["would_route"]["provider"] == provider_enum.value


def test_each_decision_appends_one_line(tmp_path):
    _route_once(tmp_path)
    _route_once(tmp_path)
    _route_once(tmp_path)
    assert len(_records(tmp_path)) == 3


def test_t0_decline_is_recorded_without_a_computed_route(tmp_path):
    """T0 declines before classification, so there is no tier and nothing to compute —
    but the decision itself is still written down."""
    result = _route_once(tmp_path, slot="T0")
    assert result.decline_reason == dr.DECLINE_T0_NEVER_ROUTES

    records = _records(tmp_path)
    assert len(records) == 1
    assert records[0]["applied"] is False
    assert records[0]["decline_reason"] == dr.DECLINE_T0_NEVER_ROUTES
    assert records[0]["tier"] is None
    assert records[0]["would_route"] is None


# ---------------------------------------------------------------------------
# Fail-open: the ledger must never be able to block a dispatch
# ---------------------------------------------------------------------------

def test_kill_switch_stops_recording_but_not_the_decision(tmp_path):
    """The switch is read from the env the door was handed, exactly like the router's
    own VNX_SMART_ROUTER_DISABLE — one env source for every router switch."""
    result = _route_once(tmp_path, env={dl.DISABLE_FLAG: "1"})
    assert result.decline_reason.startswith(st.DECLINE_TIER_DISABLED)
    assert _records(tmp_path) == []


def test_kill_switch_is_read_from_the_process_env_when_none_is_passed(tmp_path, monkeypatch):
    """The production call site passes no env, so the switch must also work from
    os.environ — otherwise the kill-switch would be unreachable in production."""
    from dispatch_spec import Provider

    monkeypatch.setenv(dl.DISABLE_FLAG, "1")
    monkeypatch.setenv("VNX_SMART_ROUTER_DISABLE", "1")  # keep the decision cheap

    result = dr.resolve_door_route(
        spec_provider=Provider.AUTO,
        spec_model=None,
        target_slot="T1",
        instruction_text="fix a typo",
        file_paths=["scripts/lib/example.py"],
        dispatch_id="20260829-beta-c1-observe",
        state_dir=tmp_path,
    )
    assert result.decline_reason == dr.DECLINE_ROUTER_DISABLED
    assert _records(tmp_path) == []


def test_recording_is_on_by_default(tmp_path):
    """The bug this closes is an observation layer nobody switched on. Recording is
    therefore ON unless explicitly disabled — the flag is a kill-switch, not an opt-in."""
    assert dl.recording_enabled({}) is True
    assert dl.recording_enabled({dl.DISABLE_FLAG: "1"}) is False


def test_unwritable_ledger_does_not_break_the_decision(tmp_path):
    """state_dir is a regular file, so mkdir/append cannot succeed. The dispatch must
    still get its decline."""
    blocked = tmp_path / "not-a-dir"
    blocked.write_text("i am a file", encoding="utf-8")

    result = _route_once(blocked)
    assert result.route is None
    assert result.decline_reason.startswith(st.DECLINE_TIER_DISABLED)


def test_record_router_decision_returns_false_instead_of_raising(tmp_path):
    blocked = tmp_path / "blocked"
    blocked.write_text("file", encoding="utf-8")
    assert (
        dl.record_router_decision(
            tier="tier-zero",
            applied=False,
            decline_reason="staging-tier-disabled:tier-zero",
            would_route={"provider": "deepseek", "model": "m", "lane": "l", "reason": None},
            target_slot="T1",
            dispatch_id="d",
            state_dir=blocked,
        )
        is False
    )


def test_shadow_computation_failure_still_records_and_still_declines(tmp_path, monkeypatch):
    """A broken tier-route computation is an observation problem, never a dispatch
    problem: the decline stands and the failure is visible in the record."""
    import providers.smart_router.tier_routing as tr

    def _boom(*_a, **_k):
        raise RuntimeError("registry drift in the shadow path")

    monkeypatch.setattr(tr, "resolve_tier_route", _boom)

    result = _route_once(tmp_path)
    assert result.route is None
    assert result.decline_reason.startswith(st.DECLINE_TIER_DISABLED)

    records = _records(tmp_path)
    assert len(records) == 1
    assert records[0]["would_route"] is None
    assert "registry drift in the shadow path" in records[0]["compute_error"]


# ---------------------------------------------------------------------------
# The third outcome: a refusal that raises is still a decision
# ---------------------------------------------------------------------------

def test_registry_drift_is_recorded_and_still_raises(tmp_path, monkeypatch):
    """ADR-036 §2 drift must keep failing loud — and must not be the one outcome the
    ledger cannot show. It is the only outcome that actually stops a dispatch."""
    import providers.smart_router.tier_routing as tr
    from providers.provider_registry import RegistryLookupError

    _enable_every_tier(monkeypatch)

    def _drifted(tier, env=None, **_kw):
        return tr.TierRoute(
            tier=tier,
            provider="a-provider-the-enum-never-heard-of",
            model="some-model",
            lane="some-lane",
        )

    monkeypatch.setattr(tr, "resolve_tier_route", _drifted)

    with pytest.raises(RegistryLookupError):
        _route_once(tmp_path)

    records = _records(tmp_path)
    assert len(records) == 1
    rec = records[0]
    assert rec["raised"] is True
    assert rec["applied"] is False
    assert rec["would_route"] is None
    assert "a-provider-the-enum-never-heard-of" in rec["compute_error"]


def test_a_normal_decline_is_not_marked_as_raised(tmp_path):
    """Guard against the raised flag becoming decorative."""
    _route_once(tmp_path)
    assert _records(tmp_path)[0]["raised"] is False
