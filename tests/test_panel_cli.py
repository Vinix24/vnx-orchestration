#!/usr/bin/env python3
"""Tests for scripts/panel.py CLI roster filtering."""

from __future__ import annotations

import importlib.util
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent


class _FakeResult:
    synthesis_refused_reason = ""

    def to_report(self) -> str:
        return "# fake panel report\n"


def _load_panel_cli():
    spec = importlib.util.spec_from_file_location("panel_cli_under_test", REPO_ROOT / "scripts" / "panel.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_seats_filters_to_requested_roster(tmp_path, monkeypatch):
    panel = _load_panel_cli()
    calls = {}

    def fake_dispatcher_factory(data_dir, timeout, role=None):
        calls["dispatcher_factory"] = {"data_dir": data_dir, "timeout": timeout, "role": role}
        return lambda provider, model, prompt, dispatch_id: "ok"

    def fake_run_deliberation(*args, **kwargs):
        calls["run_deliberation"] = {"args": args, "kwargs": kwargs}
        return _FakeResult()

    monkeypatch.setattr("plan_gate_panel._make_default_dispatcher", fake_dispatcher_factory)
    monkeypatch.setattr(panel, "run_deliberation", fake_run_deliberation)

    rc = panel.main([
        "sweep",
        "audit src/",
        "--seats",
        "codex,claude",
        "--out",
        str(tmp_path / "report.md"),
    ])

    assert rc == 0
    assert calls["run_deliberation"]["kwargs"]["roster"] == [
        ("codex", "gpt-5.5"),
        ("claude", "sonnet"),
    ]
    assert calls["dispatcher_factory"]["role"] == "research-analyst"


def test_unknown_seat_errors_without_dispatch(tmp_path, monkeypatch, capsys):
    panel = _load_panel_cli()

    def fail_dispatcher_factory(data_dir, timeout, role=None):
        raise AssertionError("dispatcher must not be built for invalid --seats")

    monkeypatch.setattr("plan_gate_panel._make_default_dispatcher", fail_dispatcher_factory)

    rc = panel.main([
        "sweep",
        "audit src/",
        "--seats",
        "codex,bogus",
        "--out",
        str(tmp_path / "report.md"),
    ])

    captured = capsys.readouterr()
    assert rc == 2
    assert "panel: unknown --seats value(s): bogus" in captured.err
    assert "known seats:" in captured.err


def test_default_omits_roster_kwarg_to_preserve_full_fleet_path(tmp_path, monkeypatch):
    panel = _load_panel_cli()
    calls = {}

    def fake_dispatcher_factory(data_dir, timeout, role=None):
        return lambda provider, model, prompt, dispatch_id: "ok"

    def fake_run_deliberation(*args, **kwargs):
        calls["run_deliberation"] = {"args": args, "kwargs": kwargs}
        return _FakeResult()

    monkeypatch.setattr("plan_gate_panel._make_default_dispatcher", fake_dispatcher_factory)
    monkeypatch.setattr(panel, "run_deliberation", fake_run_deliberation)

    rc = panel.main([
        "sweep",
        "audit src/",
        "--out",
        str(tmp_path / "report.md"),
    ])

    assert rc == 0
    assert "roster" not in calls["run_deliberation"]["kwargs"]


def test_dispatcher_factory_receives_non_plan_reviewer_role(tmp_path, monkeypatch):
    """OI-811: panel.py must not dispatch its stage prompts under the plan-reviewer role —
    that framing caused a plan-reviewer-role worker to reject a non-plan panel artifact.
    OI-1359: the OI-811 fix used role="deliberation-panelist", a role that exists in
    neither register — every seat failed fail-closed before producing content. The role
    is now "research-analyst" (see test_dispatcher_role_exists_in_both_registers below
    for the register-backed check that this string stays real)."""
    panel = _load_panel_cli()
    calls = {}

    def fake_dispatcher_factory(data_dir, timeout, role=None):
        calls["role"] = role
        return lambda provider, model, prompt, dispatch_id: "ok"

    def fake_run_deliberation(*args, **kwargs):
        return _FakeResult()

    monkeypatch.setattr("plan_gate_panel._make_default_dispatcher", fake_dispatcher_factory)
    monkeypatch.setattr(panel, "run_deliberation", fake_run_deliberation)

    rc = panel.main(["sweep", "audit src/", "--out", str(tmp_path / "report.md")])

    assert rc == 0
    assert calls["role"] == "research-analyst"
    assert calls["role"] != "plan-reviewer"


def test_panel_passes_config_min_seats_and_allow_degraded_to_run_deliberation(tmp_path, monkeypatch):
    """OI-1154: the synthesis coverage floor comes from the config loader (not a
    Python literal), and the --allow-degraded escape is forwarded to the panel."""
    panel = _load_panel_cli()
    calls = {}

    def fake_dispatcher_factory(data_dir, timeout, role=None):
        return lambda provider, model, prompt, dispatch_id: "ok"

    def fake_run_deliberation(*args, **kwargs):
        calls["kwargs"] = kwargs
        return _FakeResult()

    monkeypatch.setattr("plan_gate_panel._make_default_dispatcher", fake_dispatcher_factory)
    monkeypatch.setattr("plan_gate_panel.load_synthesis_min_seats", lambda config_path=None: 7)
    monkeypatch.setattr(panel, "run_deliberation", fake_run_deliberation)

    rc = panel.main([
        "sweep", "audit src/", "--seats", "codex,claude",
        "--out", str(tmp_path / "report.md"), "--allow-degraded",
    ])

    assert rc == 0
    assert calls["kwargs"]["min_seats"] == 7
    assert calls["kwargs"]["allow_degraded"] is True


def test_panel_refusal_returns_nonzero_and_loud(tmp_path, monkeypatch, capsys):
    """A refused synthesis must not look like success: the CLI exits non-zero and
    prints the refusal (with delivered/expected counts) to stderr."""
    panel = _load_panel_cli()

    def fake_dispatcher_factory(data_dir, timeout, role=None):
        return lambda provider, model, prompt, dispatch_id: "ok"

    class _RefusingResult:
        synthesis_refused_reason = "refusing synthesis: 1/5 lenses delivered (minimum 3)"

        def to_report(self) -> str:
            return "# fake panel report\n"

    def fake_run_deliberation(*args, **kwargs):
        return _RefusingResult()

    monkeypatch.setattr("plan_gate_panel._make_default_dispatcher", fake_dispatcher_factory)
    monkeypatch.setattr("plan_gate_panel.load_synthesis_min_seats", lambda config_path=None: 3)
    monkeypatch.setattr(panel, "run_deliberation", fake_run_deliberation)

    rc = panel.main(["sweep", "audit src/", "--out", str(tmp_path / "report.md")])

    captured = capsys.readouterr()
    assert rc == 1
    assert "SYNTHESIS REFUSED" in captured.err
    assert "1/5" in captured.err


def test_dispatcher_role_exists_in_both_registers(tmp_path, monkeypatch):
    """OI-1359: panel.py dispatched under role="deliberation-panelist", which is a key
    in NEITHER register (no agents/deliberation-panelist/ dir, no profile in
    worker_permissions.yaml). resolve_worker_profile fails closed on that: every seat
    refused before producing any content, providerneutrally (codex and kimi failed the
    same way).

    This test does not repeat a literal role string on both sides of an equality (that
    only proves the string matches itself). It captures the role panel.py actually
    passes to the dispatcher factory, then checks it against the two REAL registers:
    a non-empty profile in worker_permissions.yaml (via the same load_permissions()
    resolve_worker_profile uses), and an agents/<role>/ directory on disk.
    """
    import worker_permissions

    panel = _load_panel_cli()
    calls = {}

    def fake_dispatcher_factory(data_dir, timeout, role=None):
        calls["role"] = role
        return lambda provider, model, prompt, dispatch_id: "ok"

    def fake_run_deliberation(*args, **kwargs):
        return _FakeResult()

    monkeypatch.setattr("plan_gate_panel._make_default_dispatcher", fake_dispatcher_factory)
    monkeypatch.setattr(panel, "run_deliberation", fake_run_deliberation)

    rc = panel.main(["sweep", "audit src/", "--out", str(tmp_path / "report.md")])

    assert rc == 0
    role = calls["role"]
    assert role, "panel.py must pass a non-empty role to the dispatcher factory"

    profile = worker_permissions.load_permissions(role)
    assert profile.allowed_tools, (
        f"role {role!r} has no usable profile in worker_permissions.yaml — "
        "resolve_worker_profile() would fall back to the restrictive code-worker "
        "default (or raise UnknownRoleError if agents/<role>/ is also missing)"
    )

    agents_dir = REPO_ROOT / "agents" / role
    assert agents_dir.is_dir(), (
        f"agents/{role}/ does not exist — resolve_worker_profile() raises "
        "UnknownRoleError for a role absent from both registers"
    )
