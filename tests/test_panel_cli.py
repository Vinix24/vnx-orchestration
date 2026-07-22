#!/usr/bin/env python3
"""Tests for scripts/panel.py CLI roster filtering."""

from __future__ import annotations

import importlib.util
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent


class _FakeResult:
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

    def fake_dispatcher_factory(data_dir, timeout):
        calls["dispatcher_factory"] = {"data_dir": data_dir, "timeout": timeout}
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


def test_unknown_seat_errors_without_dispatch(tmp_path, monkeypatch, capsys):
    panel = _load_panel_cli()

    def fail_dispatcher_factory(data_dir, timeout):
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

    def fake_dispatcher_factory(data_dir, timeout):
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
