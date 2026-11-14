"""Characterisation tests for golf-schoon R01 (extract-method refactor).

Pins the observable behaviour of ``lane_adapter.dispatch`` so the refactor
provably changes nothing. Only the dispatcher subprocesses are patched.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts" / "lib"))
sys.path.insert(0, str(REPO_ROOT / "scripts" / "benchmark" / "field-tests" / "runners"))

import lane_adapter  # noqa: E402


# --------------------------------------------------------------------------
# lane_adapter.dispatch
# --------------------------------------------------------------------------


@pytest.fixture
def adapter(tmp_path, monkeypatch):
    reports = tmp_path / "reports"
    reports.mkdir()
    monkeypatch.setattr(lane_adapter, "REPORT_DIR_CANDIDATES", (reports, tmp_path / "other"))
    monkeypatch.delenv("VNX_BENCH_CLAUDE_HEADLESS", raising=False)
    calls: list = []
    state = {"seed": ("", None), "ret": (0, "out", "err"), "wd": (None, "no cell")}

    monkeypatch.setattr(lane_adapter, "_main_seed_status", lambda p: state["seed"])

    def fake_provider(lane, did, instr, paths, deadline, role="backend-developer"):
        calls.append(("provider", did, instr, role))
        if isinstance(state["ret"], Exception):
            raise state["ret"]
        return state["ret"]

    def fake_claude(lane, did, instr, paths, deadline, role="backend-developer"):
        calls.append(("claude", did, instr, role))
        return state["ret"]

    monkeypatch.setattr(lane_adapter, "_provider_dispatch", fake_provider)
    monkeypatch.setattr(lane_adapter, "_claude_subprocess_headless", fake_claude)
    monkeypatch.setattr(lane_adapter, "_resolve_provider_workdir", lambda o, e: state["wd"])
    return reports, calls, state


KIMI = {"id": "kimi-x", "provider": "litellm:moonshot", "model_arg": "k"}
CLAUDE = {"id": "claude-x", "provider": "claude", "model_arg": "sonnet"}


def test_dispatch_seed_dirty_refused(adapter):
    _, calls, state = adapter
    state["seed"] = (" M seed/x", None)
    r = lane_adapter.dispatch(KIMI, "t1", 2, "do", "p", 5)
    assert not r.success and r.wallclock_seconds == 0.0 and calls == []
    assert r.dispatch_id.startswith("bench-kimi-x-t1-r2-")
    assert r.error == ("main-repo seed paths dirty before dispatch; refusing benchmark "
                       "cell: M seed/x".replace("M seed", " M seed", 1)) or \
        r.error.startswith("main-repo seed paths dirty before dispatch; refusing benchmark cell:")
    state["seed"] = ("", "boom")
    r = lane_adapter.dispatch(KIMI, "t1", 2, "do", "p", 5)
    assert r.error == "boom" and calls == []


def test_dispatch_claude_refused_without_headless_flag(adapter):
    _, calls, _ = adapter
    r = lane_adapter.dispatch(CLAUDE, "t1", 1, "do", "p", 5)
    assert not r.success and calls == []
    assert r.error.startswith("claude benchmark lane refused:")
    assert "VNX_BENCH_CLAUDE_HEADLESS=1" in r.error


def test_dispatch_claude_headless_flag_routes_provider(adapter, monkeypatch):
    reports, calls, state = adapter
    monkeypatch.setenv("VNX_BENCH_CLAUDE_HEADLESS", "1")
    state["wd"] = (Path("/tmp/wd"), None)
    r = lane_adapter.dispatch(CLAUDE, "t1", 1, "do", "p", 5)
    assert [c[0] for c in calls] == ["provider"]
    assert r.error == "no_report (worker rc=0 but never emitted unified_report)"
    assert r.workdir == Path("/tmp/wd")


def test_dispatch_forced_headless_routes_claude_subprocess(adapter, monkeypatch):
    _, calls, _ = adapter
    monkeypatch.setattr(lane_adapter, "HEADLESS_FORCED_MODELS", {"sonnet"})
    r = lane_adapter.dispatch(CLAUDE, "t1", 1, "do", "p", 5)
    assert [c[0] for c in calls] == ["claude"]
    assert r.workdir is None  # not via_provider
    assert r.error.startswith("no_report")


def test_dispatch_success_with_report_and_completion_protocol(adapter):
    reports, calls, state = adapter
    state["wd"] = (Path("/tmp/wd"), None)
    state["ret"] = (0, "o" * 3000, "e" * 3000)

    orig = lane_adapter._provider_dispatch

    def writer(lane, did, instr, paths, deadline, role="backend-developer"):
        (reports / f"{did}.md").write_text("r")
        return orig(lane, did, instr, paths, deadline, role)

    lane_adapter._provider_dispatch = writer
    try:
        r = lane_adapter.dispatch(KIMI, "t1", 1, "do the thing", "p", 5)
    finally:
        lane_adapter._provider_dispatch = orig
    assert r.success and r.error is None
    assert r.report_path == reports / f"{r.dispatch_id}.md"
    assert len(r.stdout) == 2000 and len(r.stderr) == 2000
    instr = calls[0][2]
    assert instr.startswith("do the thing\n\n# COMPLETION PROTOCOL (required)\n")
    assert f"  {reports / (r.dispatch_id + '.md')}\n" in instr
    assert calls[0][3] == "backend-developer"


def test_dispatch_alt_report_suffix_and_rc_nonzero_with_report(adapter):
    reports, calls, state = adapter
    state["ret"] = (3, "o", "e")
    state["wd"] = (None, "x")

    orig = lane_adapter._provider_dispatch

    def writer(lane, did, instr, paths, deadline, role="backend-developer"):
        (reports / f"{did}_report.md").write_text("r")
        return orig(lane, did, instr, paths, deadline, role)

    lane_adapter._provider_dispatch = writer
    try:
        r = lane_adapter.dispatch(KIMI, "t1", 1, "x", "p", 5)
    finally:
        lane_adapter._provider_dispatch = orig
    assert r.report_path.name.endswith("_report.md")
    assert r.error == "rc=3 (report present)"


def test_dispatch_failure_reasons_without_report(adapter, monkeypatch):
    _, _, state = adapter
    state["wd"] = (Path("/tmp/wd"), None)
    state["ret"] = (1, "o", "benchmark provider isolation required: nope")
    r = lane_adapter.dispatch(KIMI, "t", 1, "x", "p", 5)
    assert r.error.startswith("provider isolation failed; refusing shared main checkout: ")
    state["ret"] = (1, "o", "benchmark seed materialization failed")
    assert lane_adapter.dispatch(KIMI, "t", 1, "x", "p", 5).error.startswith("provider isolation")
    state["ret"] = (1, "o", "plain")
    r = lane_adapter.dispatch(KIMI, "t", 1, "x", "p", 5)
    assert r.error.startswith("immediate_exit (wall=") and ", rc=1)" in r.error
    monkeypatch.setattr(lane_adapter, "MIN_REAL_WALLCLOCK_SECONDS", -1.0)
    assert lane_adapter.dispatch(KIMI, "t", 1, "x", "p", 5).error == "rc=1 report_exists=False"


def test_dispatch_unscorable_when_no_workdir(adapter):
    _, _, state = adapter
    state["wd"] = (None, "cell missing")
    r = lane_adapter.dispatch(KIMI, "t", 1, "x", "p", 5)
    # no_report outranks unscorable; workdir stays None
    assert r.error.startswith("no_report") and r.workdir is None


def test_dispatch_unscorable_reason_when_report_present(adapter):
    reports, _, state = adapter
    state["wd"] = (None, "cell missing")
    orig = lane_adapter._provider_dispatch

    def writer(lane, did, instr, paths, deadline, role="backend-developer"):
        (reports / f"{did}.md").write_text("r")
        return orig(lane, did, instr, paths, deadline, role)

    lane_adapter._provider_dispatch = writer
    try:
        r = lane_adapter.dispatch(KIMI, "t", 1, "x", "p", 5)
    finally:
        lane_adapter._provider_dispatch = orig
    assert r.error == "unscorable: cell missing"


def test_dispatch_timeout_paths(adapter):
    _, _, state = adapter
    state["ret"] = subprocess.TimeoutExpired("cmd", 5)
    r = lane_adapter.dispatch(KIMI, "t", 1, "x", "p", 7)
    assert not r.success and r.error == "timeout after 7s"
    assert r.stdout == "" and r.stderr == str(state["ret"]) and r.report_path is None
    # seed contaminated after timeout
    calls = iter([("", None), (" M a", None)])
    lane_adapter._main_seed_status = lambda p: next(calls)
    r = lane_adapter.dispatch(KIMI, "t", 1, "x", "p", 7)
    assert r.error == "main-repo seed contamination after timeout:  M a"


def test_dispatch_seed_contaminated_after_dispatch_overrides(adapter):
    reports, _, state = adapter
    state["wd"] = (Path("/tmp/wd"), None)
    seq = iter([("", None), (" M s", None)])
    lane_adapter._main_seed_status = lambda p: next(seq)
    r = lane_adapter.dispatch(KIMI, "t", 1, "x", "p", 5)
    assert not r.success
    assert r.error == "main-repo seed contamination after dispatch:  M s"
    seq2 = iter([("", None), ("", "gone")])
    lane_adapter._main_seed_status = lambda p: next(seq2)
    assert lane_adapter.dispatch(KIMI, "t", 1, "x", "p", 5).error == "gone"


def test_dispatch_skill_names_role_and_structured_prompt(adapter, monkeypatch):
    _, calls, _ = adapter
    seen = {}

    def fake_build(names, instr, root):
        seen["a"] = (list(names), instr, root)
        return "WRAPPED"

    monkeypatch.setattr(lane_adapter, "build_structured_prompt", fake_build)
    lane_adapter.dispatch(KIMI, "t", 1, "raw", "p", 5, skill_names=["", "security-engineer"])
    assert seen["a"] == (["", "security-engineer"], "raw", lane_adapter.SKILLS_ROOT)
    assert calls[0][2].startswith("WRAPPED\n\n# COMPLETION PROTOCOL")
    assert calls[0][3] == "security-engineer"
