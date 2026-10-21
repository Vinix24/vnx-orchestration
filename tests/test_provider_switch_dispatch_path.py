"""Provider-schakelaar B: the dispatch path honours the operator's provider switch.

- The door refuses a switched-off provider with ``REJECT [provider-disabled]`` on the dry-run
  and on the fire path, before the reachability probe, and starts no worker.
- ``provider_dispatch`` refuses by name with the switch's own exit code, before the route
  constraints and the daily budget, and writes a ``provider_switched_off`` receipt.
- The router skips a switched-off lane, names the skip and writes no cooldown.
- The door, the launch function and the worker decide for one project: the launchers stamp
  ``VNX_PROJECT_ID`` and ``VNX_WORKER_DISPATCH_ID`` into every worker env.
- A refusal is never a reachability fact: after switching back on, every sink accepts the
  next call at once.

Every test writes a tmp switch file through ``VNX_PROVIDER_SWITCH_FILE``; the operator's real
``~/.vnx/providers.json`` is never read or written.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

import pytest

REPO = Path(__file__).resolve().parent.parent
for _p in (str(REPO / "scripts"), str(REPO / "scripts" / "lib")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import dispatch_cli  # noqa: E402
import failure_classification  # noqa: E402
import provider_dispatch  # noqa: E402
import provider_reachability  # noqa: E402
import provider_switch  # noqa: E402
from dispatch_plan import ModelPin, RuntimeSnapshot  # noqa: E402
from providers.smart_router import availability  # noqa: E402
from providers.smart_router.tier_routing import escalate_tier  # noqa: E402

PROJECT = "proj-door"
OTHER = "proj-other"
CLI = REPO / "scripts" / "providers_cli.py"
_K = "ki" + "mi"


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------

@pytest.fixture
def switch_file(tmp_path, monkeypatch) -> Path:
    path = tmp_path / "switch" / "providers.json"
    path.parent.mkdir()
    monkeypatch.setenv("VNX_PROVIDER_SWITCH_FILE", str(path))
    for key in [k for k in os.environ if k.startswith(provider_switch.OVERRIDE_ENV_PREFIX)]:
        monkeypatch.delenv(key)
    return path


@pytest.fixture
def store(tmp_path, monkeypatch) -> Path:
    """An explicit tmp store for the door and provider_dispatch; returns its state dir."""
    data_dir = tmp_path / "store"
    (data_dir / "state").mkdir(parents=True)
    monkeypatch.setenv("VNX_DATA_DIR", str(data_dir))
    monkeypatch.setenv("VNX_STATE_DIR", str(data_dir / "state"))
    monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")
    monkeypatch.setenv("VNX_PROJECT_ID", PROJECT)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-not-a-real-key")
    return data_dir / "state"


def write_switches(path: Path, *, global_: Optional[Dict[str, bool]] = None,
                   projects: Optional[Dict[str, Dict[str, bool]]] = None) -> None:
    def section(flags: Dict[str, bool]) -> Dict[str, Any]:
        return {f: {"enabled": on, "changed_at": "2026-10-21T08:00:00Z", "changed_by": "op",
                    "reason": "auth rotation, quota 401 test"} for f, on in flags.items()}
    path.write_text(json.dumps({
        "version": 1, "global": section(global_ or {}),
        "projects": {pid: section(flags) for pid, flags in (projects or {}).items()},
    }), encoding="utf-8")


def make_spec(tmp_path: Path, *, provider: str, model: Optional[str] = None,
              dispatch_id: str = "20261021-ps-b-test", project_id: str = PROJECT) -> Path:
    instruction = tmp_path / "instruction.md"
    instruction.write_text("# Fix a typo\n\nRole: backend-developer\n\nFix the typo in the README.\n",
                           encoding="utf-8")
    spec = {
        "schema_version": 1, "project_id": project_id, "dispatch_id": dispatch_id,
        "staging_id": "test-stage", "instruction_file": str(instruction), "role": "backend-developer",
        "target_slot": "T1", "gate": "codex_gate",
        "dispatch_paths": [{"path": "scripts/test.py", "access": "read_write", "materialize_at_cwd": False}],
        "provider": provider, "model": model, "deadline_seconds": 3600, "base_ref": "origin/main",
        "isolation": "worktree", "requires_mcp": False,
    }
    spec_file = tmp_path / "dispatch-spec.json"
    spec_file.write_text(json.dumps(spec), encoding="utf-8")
    return spec_file


def clean_snapshot() -> RuntimeSnapshot:
    return RuntimeSnapshot(
        constraint_verdicts=(), staging_promoted=True,
        target_health={"ephemeral": "healthy", "T1": "healthy"},
        target_capable={"ephemeral": True, "T1": True},
        model_pins={t: ModelPin(model="sonnet", semantics="floor") for t in ("T1", "T2", "T3")}
        | {"T0": ModelPin(model="opus", semantics="floor")},
    )


class Door:
    """The door with its snapshot, reachability probe and lane executors replaced."""

    def __init__(self) -> None:
        self.reachability = MagicMock(return_value=dispatch_cli.ReachabilityVerdict(block=False))
        self.envelope = MagicMock()
        self.envelope.return_value = MagicMock(returncode=0, status="success", error=None)
        self.headless = MagicMock(return_value=0)

    def run(self, spec_file: Path, *, dry_run: bool) -> int:
        with patch.dict(os.environ), \
                patch("dispatch_cli.build_runtime_snapshot", return_value=clean_snapshot()), \
                patch("dispatch_cli._check_reachability", self.reachability), \
                patch("dispatch_cli.run_envelope_plan", self.envelope), \
                patch("dispatch_cli._execute_claude_headless", self.headless):
            return dispatch_cli.run_dispatch(spec_file, dry_run=dry_run)

    @property
    def worker_started(self) -> bool:
        return self.envelope.called or self.headless.called


def pd_args(provider: str, dispatch_id: str = "20261021-ps-b-pd") -> argparse.Namespace:
    return provider_dispatch._build_parser().parse_args([
        "--provider", provider, "--terminal-id", "T1", "--dispatch-id", dispatch_id,
        "--instruction", "do it",
    ])


def receipts(state_dir: Path) -> List[Dict[str, Any]]:
    path = state_dir / "t0_receipts.ndjson"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


@pytest.fixture
def no_launch(monkeypatch) -> List[Any]:
    """Every process launch recorded and refused."""
    calls: List[Any] = []

    def refuse(*args, **kwargs):
        calls.append(args[0] if args else kwargs.get("args"))
        raise AssertionError(f"a process was launched: {calls[-1]!r}")

    for attr in ("Popen", "run"):
        monkeypatch.setattr(subprocess, attr, refuse)
    return calls


# ---------------------------------------------------------------------------
# The door
# ---------------------------------------------------------------------------

def test_door_dry_run_refuses_a_switched_off_provider_before_the_probe(switch_file, store, tmp_path, capsys):
    write_switches(switch_file, global_={"deepseek": False})
    door = Door()

    rc = door.run(make_spec(tmp_path, provider="deepseek-harness", model="deepseek-v4-pro"), dry_run=True)

    assert rc == 1
    err = capsys.readouterr().err
    assert "REJECT [provider-disabled]: provider_switched_off: deepseek-harness family=deepseek" in err
    assert f"scope={PROJECT}" in err and "consumer=dispatch_cli.door" in err
    assert "changed_by=op" in err and "reason=" in err
    assert not door.reachability.called and not door.worker_started


def test_door_dry_run_passes_with_a_project_override_on(switch_file, store, tmp_path):
    write_switches(switch_file, global_={"deepseek": False}, projects={PROJECT: {"deepseek": True}})
    door = Door()

    assert door.run(make_spec(tmp_path, provider="deepseek-harness", model="deepseek-v4-pro"), dry_run=True) == 0
    assert door.reachability.called and not door.worker_started


def test_door_fire_refuses_global_on_project_off(switch_file, store, tmp_path, capsys):
    """The reverse case: the project's own override wins over a global on."""
    write_switches(switch_file, global_={"deepseek": True}, projects={PROJECT: {"deepseek": False}})
    door = Door()

    rc = door.run(make_spec(tmp_path, provider="deepseek-harness", model="deepseek-v4-pro"), dry_run=False)

    assert rc == 1
    err = capsys.readouterr().err
    assert "REJECT [provider-disabled]" in err and f"source=project scope={PROJECT}" in err
    assert not door.worker_started and not door.reachability.called


def test_door_never_refuses_claude_or_codex(switch_file, store, tmp_path):
    """Decision 2b: an unreadable switch file refuses every switchable family, never these."""
    switch_file.write_text("{broken", encoding="utf-8")
    for provider in ("claude", "codex"):
        assert Door().run(make_spec(tmp_path, provider=provider), dry_run=True) == 0


def test_fire_path_door_launch_function_and_worker_decide_for_one_project(
        switch_file, store, tmp_path, monkeypatch):
    """Global deepseek off, the dispatch's project on. The door passes; the provider lane builds
    the worker env through provider_dispatch, so the launch functions (the harness spawn and the
    host-keyed check in spawn_claude) decide for the dispatch's project too, although the door
    process itself carries no project; and a decision made inside the worker's own process,
    from that env alone, is for the same project and passes."""
    write_switches(switch_file, global_={"deepseek": False}, projects={PROJECT: {"deepseek": True}})
    door = Door()
    assert door.run(make_spec(tmp_path, provider="deepseek-harness", model="deepseek-v4-pro"), dry_run=False) == 0
    plan = door.envelope.call_args.args[0]
    assert plan.project_id == PROJECT and plan.provider.value == "deepseek-harness"

    # The door process's own project is another one: only the stamp can carry PROJECT.
    monkeypatch.setenv("VNX_PROJECT_ID", OTHER)
    child_envs: List[Dict[str, str]] = []
    real_run, real_popen = subprocess.run, subprocess.Popen

    class Started(Exception):
        pass

    def record_popen(cmd, **kwargs):
        if not cmd or cmd[0] != "claude":  # path resolution may run git; only the worker is held
            return real_popen(cmd, **kwargs)
        child_envs.append(dict(kwargs["env"]))
        raise Started(cmd[0])

    monkeypatch.setattr(subprocess, "Popen", record_popen)
    from envelope_adapters_provider import ProviderAdapter
    with pytest.raises(Started):
        ProviderAdapter().run(plan, "instruction", cwd=tmp_path)

    [env] = child_envs
    assert env["VNX_PROJECT_ID"] == PROJECT
    assert env["VNX_WORKER_DISPATCH_ID"] == plan.dispatch_id
    assert env["ANTHROPIC_BASE_URL"].startswith("https://api.deepseek.com")
    probe = textwrap.dedent(f"""
        import json, sys
        sys.path.insert(0, {str(REPO / 'scripts' / 'lib')!r})
        import provider_switch
        d = provider_switch.decide("deepseek-harness", consumer="worker")
        print(json.dumps({{"allowed": d.allowed, "scope": d.scope, "source": d.source}}))
    """)
    worker_env = {**env, "VNX_PROVIDER_SWITCH_FILE": str(switch_file)}
    out = real_run([sys.executable, "-c", probe], env=worker_env, capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout) == {"allowed": True, "scope": PROJECT, "source": "project"}


def test_the_door_project_reaches_a_launch_the_process_project_would_refuse(
        switch_file, store, tmp_path, monkeypatch):
    """The reverse of the stamp: global deepseek on, OTHER off. With the plan's project stamped
    the launch is for PROJECT and allowed, even when the process project is OTHER."""
    write_switches(switch_file, global_={"deepseek": True}, projects={OTHER: {"deepseek": False}})
    monkeypatch.setenv("VNX_PROJECT_ID", OTHER)
    env = provider_dispatch._worker_role_env(None, dispatch_id="d-1", project_id=PROJECT)
    assert env["VNX_PROJECT_ID"] == PROJECT and env["VNX_WORKER_DISPATCH_ID"] == "d-1"
    decision = provider_switch.require("deepseek-harness", consumer="t", project_id=env["VNX_PROJECT_ID"])
    assert decision.allowed and decision.scope == PROJECT
    own = provider_dispatch._worker_role_env(None, dispatch_id="d-1")
    assert own["VNX_PROJECT_ID"] == OTHER


# ---------------------------------------------------------------------------
# provider_dispatch
# ---------------------------------------------------------------------------

def test_provider_dispatch_refuses_by_name_before_constraints_and_budget(
        switch_file, store, monkeypatch, capsys, no_launch):
    write_switches(switch_file, global_={_K: False})
    monkeypatch.setattr(provider_dispatch, "_check_constraints",
                        lambda *a: pytest.fail("route constraints ran for a switched-off provider"))
    import paid_lane_budget
    monkeypatch.setattr(paid_lane_budget, "enforce_daily_budget",
                        lambda *a, **k: pytest.fail("the daily budget ran for a switched-off provider"))
    monkeypatch.setattr(provider_dispatch, "_dispatch_kimi",
                        lambda args: pytest.fail("the kimi handler ran"))

    rc = provider_dispatch.main(["--provider", _K, "--terminal-id", "T1",
                                 "--dispatch-id", "20261021-ps-b-pd", "--instruction", "do it"])

    assert rc == provider_switch.EXIT_PROVIDER_SWITCHED_OFF == 4
    err = capsys.readouterr().err
    assert f"provider_dispatch: REJECT [provider_switched_off]: provider_switched_off: {_K} family={_K}" in err
    assert no_launch == []
    [receipt] = receipts(store)
    assert receipt["failure_class"] == "provider_switched_off"
    assert receipt["delivery_state"] == "delivery_refused" and receipt["status"] == "blocked"
    assert receipt["provider"] == _K and receipt["failure_reason"].startswith("provider_switched_off:")


def test_provider_dispatch_turns_a_launch_refusal_into_the_same_named_refusal(
        switch_file, store, monkeypatch, capsys):
    """Bare ``litellm`` is decided at launch on the resolved model: main books that refusal by
    name too, never as an uncaught spawn exception."""
    write_switches(switch_file, global_={"deepseek": False})
    monkeypatch.setattr(provider_dispatch, "_check_constraints", lambda *a: None)

    def launch_refused(args):
        provider_switch.require("litellm:deepseek/deepseek-v4-pro", consumer="litellm_spawn")

    monkeypatch.setattr(provider_dispatch, "_dispatch_litellm", launch_refused)
    rc = provider_dispatch.main(["--provider", "litellm", "--terminal-id", "T1",
                                 "--dispatch-id", "20261021-ps-b-lt", "--instruction", "x"])
    assert rc == provider_switch.EXIT_PROVIDER_SWITCHED_OFF
    assert "REJECT [provider_switched_off]" in capsys.readouterr().err
    assert receipts(store)[-1]["failure_class"] == "provider_switched_off"


def test_a_worker_started_by_provider_dispatch_cannot_switch(switch_file, store, tmp_path, monkeypatch):
    """The worker env provider_dispatch builds carries VNX_WORKER_DISPATCH_ID, so `vnx providers
    off` run inside the worker refuses with exit 5, from a directory outside any repo."""
    for marker in provider_switch.DISPATCH_MARKER_ENV:
        monkeypatch.delenv(marker, raising=False)
    outside = tmp_path / "outside-any-repo"
    outside.mkdir()
    result_file = tmp_path / "worker-result.json"
    runner = tmp_path / "worker_runner.py"
    runner.write_text(textwrap.dedent(f"""
        import json, os, subprocess, sys
        sys.stdin.read()
        r = subprocess.run([sys.executable, {str(CLI)!r}, "off", "deepseek", "--reason", "from a worker"],
                           cwd={str(outside)!r}, capture_output=True, text=True)
        with open({str(result_file)!r}, "w", encoding="utf-8") as fh:
            json.dump({{"rc": r.returncode, "stderr": r.stderr,
                        "marker": os.environ.get("VNX_WORKER_DISPATCH_ID"),
                        "project": os.environ.get("VNX_PROJECT_ID")}}, fh)
    """), encoding="utf-8")
    from provider_spawns.litellm_spawn import _start_litellm_subprocess

    extra_env = provider_dispatch._worker_role_env(None, dispatch_id="20261021-ps-b-worker")
    proc, err = _start_litellm_subprocess(
        str(runner), json.dumps({"model": "deepseek/deepseek-v4-pro", "messages": []}), extra_env, outside,
    )
    assert err is None
    proc.wait(timeout=120)

    result = json.loads(result_file.read_text(encoding="utf-8"))
    assert result["marker"] == "20261021-ps-b-worker" and result["project"] == PROJECT
    assert result["rc"] == 5
    assert "VNX_WORKER_DISPATCH_ID" in result["stderr"]
    assert not switch_file.exists()


# ---------------------------------------------------------------------------
# The router
# ---------------------------------------------------------------------------

def test_lane_available_names_the_switch_and_writes_no_cooldown(switch_file, store, monkeypatch):
    write_switches(switch_file, global_={"deepseek": False})
    monkeypatch.setattr(availability, "record_lane_failure",
                        lambda *a, **k: pytest.fail("a switch refusal was booked as a lane failure"))
    env = {"DEEPSEEK_API_KEY": "sk-test", "VNX_PROJECT_ID": PROJECT}

    ok, reason = availability.lane_available("deepseek-harness", env=env, state_dir=store)

    assert ok is False
    assert reason.startswith("provider_switched_off: deepseek-harness family=deepseek")
    assert f"scope={PROJECT}" in reason
    assert not (store / "router_lane_cooldown").exists()
    assert availability.lane_available("claude", env=env, state_dir=store) == (True, "available")


def _route_auto(tmp_path: Path, monkeypatch, capsys) -> str:
    for key in ("VNX_SMART_ROUTER_TIER_ZERO", "VNX_SMART_ROUTER_TIER_LOW",
                "VNX_SMART_ROUTER_TIER_MID", "VNX_SMART_ROUTER_TIER_HIGH"):
        monkeypatch.setenv(key, "1")
    monkeypatch.setenv("VNX_SMART_ROUTER_CANARY_PCT", "100")
    monkeypatch.setattr(availability.shutil, "which", lambda name: f"/usr/local/bin/{name}")
    assert Door().run(make_spec(tmp_path, provider="auto"), dry_run=True) == 0
    return capsys.readouterr().out


def test_an_auto_spec_routes_past_a_switched_off_lane_and_names_the_skip(switch_file, store, tmp_path,
                                                                        monkeypatch, capsys):
    write_switches(switch_file, global_={"deepseek": False})
    out = _route_auto(tmp_path, monkeypatch, capsys)

    assert "provider:     deepseek-harness" not in out
    assert "provider:     kimi" in out
    assert (f"deepseek-harness unavailable (provider_switched_off: deepseek-harness family=deepseek "
            f"source=global scope={PROJECT} consumer=smart_router.lane_available") in out


def test_an_auto_spec_with_deepseek_and_kimi_off_lands_on_codex(switch_file, store, tmp_path,
                                                                monkeypatch, capsys):
    """Risks, 'Fewer router candidates': with both cheap lanes off, a tier-zero spec walks the
    existing chain to codex."""
    write_switches(switch_file, global_={"deepseek": False, _K: False})
    out = _route_auto(tmp_path, monkeypatch, capsys)

    assert "provider:     codex" in out
    assert "deepseek-harness unavailable (provider_switched_off" in out
    assert f"{_K} unavailable (provider_switched_off: {_K} family={_K}" in out


# ---------------------------------------------------------------------------
# A refusal is never a reachability fact
# ---------------------------------------------------------------------------

def test_after_switching_back_on_every_sink_accepts_the_next_call(switch_file, store, tmp_path,
                                                                 monkeypatch, capsys):
    write_switches(switch_file, global_={_K: False})
    pd_dispatched: List[str] = []
    monkeypatch.setattr(provider_dispatch, "_check_constraints", lambda *a: None)
    monkeypatch.setattr(provider_dispatch, "_dispatch_kimi", lambda args: pd_dispatched.append(args.dispatch_id) or 0)
    monkeypatch.setattr(availability.shutil, "which", lambda name: f"/usr/local/bin/{name}")
    env = {"VNX_PROJECT_ID": PROJECT}
    spec = make_spec(tmp_path, provider=_K)

    # Off: every entry point refuses.
    assert Door().run(spec, dry_run=True) == 1
    assert provider_dispatch.main(["--provider", _K, "--terminal-id", "T1", "--dispatch-id", "d-off",
                                   "--instruction", "x"]) == provider_switch.EXIT_PROVIDER_SWITCHED_OFF
    assert availability.lane_available(_K, env=env, state_dir=store)[0] is False

    # The refusal left no fact behind in any sink.
    assert dispatch_cli._recent_blocking_rejection(_K, store) is None
    assert availability.lane_cooldown_remaining(_K, state_dir=store) == 0.0
    assert not provider_reachability.get(_K, state_dir=store).is_known_unreachable
    assert not (store / "provider_reachability").exists()

    # On again: the next call proceeds at once.
    write_switches(switch_file, global_={_K: True})
    door = Door()
    assert door.run(spec, dry_run=True) == 0 and door.reachability.called
    assert provider_dispatch.main(["--provider", _K, "--terminal-id", "T1", "--dispatch-id", "d-on",
                                   "--instruction", "x"]) == 0
    assert pd_dispatched == ["d-on"]
    assert availability.lane_available(_K, env=env, state_dir=store) == (True, "available")
    capsys.readouterr()


def test_a_refusal_detail_never_classifies_as_auth_or_credit():
    decision = provider_switch.SwitchDecision(
        identifier="kimi", allowed=False, family="kimi", source="global", scope=PROJECT,
        consumer="x", reason="auth rotation; insufficient balance; 401 quota",
    )
    detail = decision.detail()
    classified = failure_classification.classify_failure_safe(status="blocked", error=detail, provider="kimi")
    assert classified == {"failure_class": "provider_switched_off", "failure_reason": detail}
    assert "provider_switched_off" in failure_classification.FAILURE_CLASSES
    assert escalate_tier("tier-mid", "parent", failure_class="provider_switched_off").action == "no_climb"
    assert provider_reachability.classify_failure_text(detail) is None
    assert provider_reachability.classify_failure_text("HTTP 401 Unauthorized") is not None


def test_a_switch_refusal_never_cools_the_lane_or_marks_it_unreachable(store, monkeypatch):
    monkeypatch.setattr(availability, "record_lane_failure",
                        lambda *a, **k: pytest.fail("a switch refusal cooled the lane down"))
    detail = "provider_switched_off: kimi family=kimi source=global scope=x consumer=y reason='401'"
    fc = failure_classification.classify_failure_safe(status="failure", error=detail, provider="kimi")
    provider_dispatch._maybe_record_provider_lane_cooldown(
        lane=_K, failure_class=fc["failure_class"], reason=detail, state_dir=store)
    provider_dispatch._maybe_record_provider_reachability(
        provider=_K, status="failure", failure_class=fc["failure_class"], reason=detail, state_dir=store)
    assert not (store / "provider_reachability").exists()


# ---------------------------------------------------------------------------
# The worker stamps
# ---------------------------------------------------------------------------

def test_worker_env_stamps_resolution_order(tmp_path, monkeypatch):
    monkeypatch.setenv("VNX_PROJECT_ID", OTHER)
    assert provider_switch.worker_env_stamps("d-1", project_id=PROJECT) == {
        "VNX_PROJECT_ID": PROJECT, "VNX_WORKER_DISPATCH_ID": "d-1"}
    central = Path.home() / ".vnx-data" / "some-project" / "state"
    assert provider_switch.worker_env_stamps("d-2", state_dir=central)["VNX_PROJECT_ID"] == "some-project"
    assert provider_switch.worker_env_stamps(None)["VNX_PROJECT_ID"] == OTHER
    assert "VNX_WORKER_DISPATCH_ID" not in provider_switch.worker_env_stamps("  ")
    assert "VNX_PROJECT_ID" not in provider_switch.worker_env_stamps("d-3", project_id="global")
    assert "VNX_PROJECT_ID" not in provider_switch.worker_env_stamps("d-4", project_id="Not A Project!")


def test_the_headless_adapter_stamps_the_store_project_and_the_dispatch(tmp_path, monkeypatch):
    from envelope_adapters_claude import ClaudeSubprocessAdapter, CodexAdapter
    from envelope_types import EnvelopeSpec
    monkeypatch.setenv("VNX_PROJECT_ID", OTHER)
    state_dir = Path.home() / ".vnx-data" / PROJECT / "state"
    spec = EnvelopeSpec(dispatch_id="d-hl", terminal_id="ephemeral", provider="claude", model="sonnet",
                        instruction="x", role=None, pr_id=None, state_dir=state_dir,
                        data_dir=tmp_path / "data")
    seen: Dict[str, Dict[str, str]] = {}

    def fake_spawn(name):
        def spawn(**kwargs):
            seen[name] = kwargs["extra_env"]
            raise BrokenPipeError("stop here")
        return spawn

    monkeypatch.setattr("provider_spawns.claude_spawn.spawn_claude", fake_spawn("claude"))
    monkeypatch.setattr("provider_spawns.codex_spawn.spawn_codex", fake_spawn("codex"))
    ClaudeSubprocessAdapter().run(spec, cwd=tmp_path)
    CodexAdapter().run(spec, cwd=tmp_path)
    for name in ("claude", "codex"):
        assert seen[name]["VNX_PROJECT_ID"] == PROJECT, name
        assert seen[name]["VNX_WORKER_DISPATCH_ID"] == "d-hl", name


def test_subprocess_adapter_stamps_every_claude_child(tmp_path, monkeypatch):
    from subprocess_adapter import SubprocessAdapter
    monkeypatch.setenv("VNX_PROJECT_ID", OTHER)
    envs: List[Dict[str, str]] = []

    def record(cmd, **kwargs):
        envs.append(dict(kwargs["env"]))
        raise OSError("not started in a test")

    monkeypatch.setattr(subprocess, "Popen", record)
    adapter = SubprocessAdapter()
    adapter.deliver("T1", "d-sa", instruction="x", model="sonnet", cwd=tmp_path)
    adapter.deliver("T1", "d-sb", instruction="x", model="sonnet", cwd=tmp_path,
                    extra_env={"VNX_PROJECT_ID": PROJECT})
    assert envs[0]["VNX_PROJECT_ID"] == OTHER and envs[0]["VNX_WORKER_DISPATCH_ID"] == "d-sa"
    assert envs[1]["VNX_PROJECT_ID"] == PROJECT and envs[1]["VNX_WORKER_DISPATCH_ID"] == "d-sb"
    assert envs[1]["VNX_CURRENT_DISPATCH_ID"] == "d-sb"
