#!/usr/bin/env python3
"""Tests for vnx_cli/commands/subsystems.py (framework-status-audit-and-cockpit PR-3).

Covers: --json shape (subsystems + health keys), the --md round-trip against the
committed docs/core/SUBSYSTEMS.md deterministic columns, the --probe guarded
import of scripts/lib/subsystem_health.py (PR-5) — both the module-absent
fallback and the live-aggregator-result path — and the regression guard that
all_beacons() is always called with a resolved Path (never argless).
"""
from __future__ import annotations

import json
import sys
from argparse import Namespace
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from vnx_cli.commands.subsystems import (  # noqa: E402
    build_rows,
    _render_md,
    _parse_seed_health,
    vnx_subsystems,
)
from vnx_cli import _engine  # noqa: E402


def _args(project_dir, *, json_flag=False, md=False, probe=False, project_id=None):
    return Namespace(
        project_dir=str(project_dir),
        json=json_flag,
        md=md,
        probe=probe,
        project_id=project_id,
    )


# ---------------------------------------------------------------------------
# --json shape
# ---------------------------------------------------------------------------

def test_json_output_contains_expected_subsystems_and_health(tmp_path, capsys):
    rc = vnx_subsystems(_args(tmp_path, json_flag=True))
    assert rc == 0

    payload = json.loads(capsys.readouterr().out)
    assert "subsystems" in payload
    rows = payload["subsystems"]
    assert len(rows) >= 20

    names = {r["subsystem"] for r in rows}
    for expected in (
        "provider-routing",
        "phantom_guard",
        "intelligence-self-learning-loop",
        "governance-enforcement-stack",
        "plan-gate-panel",
        "plan-gate-task-class-scope",
    ):
        assert expected in names, f"missing subsystem row: {expected}"

    for row in rows:
        assert "health" in row
        assert row["health"]  # never empty — beacon, seed, or "unknown"

    # flag-backed row carries its registry effective value ...
    migration = next(r for r in rows if r["subsystem"] == "migration-mechanisms")
    assert migration["flag"] == "VNX_MIGRATION_SYSTEM"
    assert migration["effective_value"] == "manifest"

    # ... a flag-less (union-source-b) row carries neither
    phantom = next(r for r in rows if r["subsystem"] == "phantom_guard")
    assert phantom["flag"] is None
    assert phantom["effective_value"] is None


# ---------------------------------------------------------------------------
# --md round-trip against the committed ledger (deterministic columns only)
# ---------------------------------------------------------------------------

def test_md_deterministic_columns_match_committed_ledger():
    # Reuses the same parser make subsystems-check runs in CI, so this test and
    # the drift-check can never silently diverge on what "deterministic" means.
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    from check_subsystems_drift import _deterministic_rows

    engine_root = _engine.engine_root()
    committed = (engine_root / "docs" / "core" / "SUBSYSTEMS.md").read_text(encoding="utf-8")

    rows = build_rows()
    for row in rows:
        row["health"] = ""  # dynamic column excluded from the round-trip check
    generated = _render_md(rows)

    assert _deterministic_rows(generated) == _deterministic_rows(committed)


def test_md_health_falls_back_to_committed_seed_when_no_beacon(tmp_path, capsys):
    # tests/conftest.py points VNX_DATA_DIR at an empty per-session tmp dir, so
    # no beacon exists for any subsystem here — every row must fall back to the
    # seed value parsed out of the committed ledger.
    vnx_subsystems(_args(tmp_path, md=True))
    out = capsys.readouterr().out

    engine_root = _engine.engine_root()
    seed_health = _parse_seed_health(engine_root)

    for subsystem, expected_health in seed_health.items():
        line = next((l for l in out.splitlines() if l.startswith(f"| {subsystem} |")), None)
        assert line is not None, f"missing row for {subsystem}"
        assert line.rstrip("|").rsplit("|", 1)[-1].strip() == expected_health


# ---------------------------------------------------------------------------
# --probe: import of scripts/lib/subsystem_health.py (PR-5's owned module).
# A probe run that cannot happen (module missing) or that dies (aggregator
# raises) is a FAILED run, not a table of "unknown" rows: exit != 0, the reason
# on stderr, nothing on stdout. A launchd job running `vnx subsystems --probe`
# has no other way to show it failed (absence-is-loud, punt 1).
# ---------------------------------------------------------------------------

_OUTPUT_FLAGS = [
    pytest.param({"json_flag": True}, id="json"),
    pytest.param({"md": True}, id="md"),
    pytest.param({}, id="table"),
]


@pytest.mark.parametrize("flags", _OUTPUT_FLAGS)
def test_probe_fails_loud_when_aggregator_module_absent(tmp_path, monkeypatch, capsys, flags):
    # A None entry in sys.modules makes `from subsystem_health import ...` raise
    # ImportError, exercising the real import path in _run_registered_probes.
    monkeypatch.setitem(sys.modules, "subsystem_health", None)

    rc = vnx_subsystems(_args(tmp_path, probe=True, **flags))

    captured = capsys.readouterr()
    assert rc != 0
    assert "subsystem_health" in captured.err
    assert captured.out == ""


@pytest.mark.parametrize("flags", _OUTPUT_FLAGS)
def test_probe_fails_loud_when_the_aggregator_raises(tmp_path, monkeypatch, capsys, flags):
    _engine.ensure_engine_on_path()
    import subsystem_health

    def _boom(**kwargs):
        raise RuntimeError("coordination db is locked")

    monkeypatch.setattr(subsystem_health, "aggregate", _boom)

    rc = vnx_subsystems(_args(tmp_path, probe=True, **flags))

    captured = capsys.readouterr()
    assert rc != 0
    assert "RuntimeError" in captured.err
    assert "coordination db is locked" in captured.err
    assert captured.out == ""


def test_without_probe_a_broken_aggregator_is_never_reached(tmp_path, monkeypatch, capsys):
    _engine.ensure_engine_on_path()
    import subsystem_health

    calls = []

    def _boom(**kwargs):
        calls.append(kwargs)
        raise RuntimeError("must not run without --probe")

    monkeypatch.setattr(subsystem_health, "aggregate", _boom)

    rc = vnx_subsystems(_args(tmp_path, json_flag=True))

    assert rc == 0
    assert calls == []
    assert capsys.readouterr().err == ""


def test_dispatch_command_turns_a_failed_probe_run_into_a_nonzero_exit(tmp_path, monkeypatch, capsys):
    from vnx_cli import main as main_mod

    _engine.ensure_engine_on_path()
    import subsystem_health

    def _boom(**kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(subsystem_health, "aggregate", _boom)
    args = _args(tmp_path, probe=True)
    args.command = "subsystems"

    with pytest.raises(SystemExit) as excinfo:
        main_mod._dispatch_command(args, parser=None)

    assert excinfo.value.code not in (0, None)
    assert "boom" in capsys.readouterr().err


def test_probe_reads_the_data_dir_the_cli_resolved(tmp_path, monkeypatch, capsys):
    """The CLI resolves its own data_dir from --project-dir (the pip-installed
    CLI cannot rely on the ambient env). That dir must reach the probes: the
    plan-gate row has to reflect ``<data_dir>/state``, not whatever the probe
    would resolve on its own."""
    import sqlite3

    cli_data = tmp_path / "cli-data"
    (cli_data / "state").mkdir(parents=True)
    conn = sqlite3.connect(str(cli_data / "state" / "runtime_coordination.db"))
    conn.execute(
        "CREATE TABLE track_open_items (track_id TEXT, project_id TEXT, oi_id TEXT, "
        "link_type TEXT, linked_at TEXT, resolved_at TEXT)"
    )
    conn.executemany(
        "INSERT INTO track_open_items VALUES (?, 'p', ?, 'blocks', '2099-01-01T00:00:00+00:00', NULL)",
        [("t1", "OI-PLAN-t1"), ("t2", "OI-PLAN-t2")],
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(_engine, "resolve_data_root", lambda project_dir: cli_data)

    rc = vnx_subsystems(_args(tmp_path, json_flag=True, probe=True))

    assert rc == 0
    rows = json.loads(capsys.readouterr().out)["subsystems"]
    plan_gate = next(r for r in rows if r["subsystem"] == "plan-gate-panel")
    assert "2 unresolved OI-PLAN blocker(s)" in plan_gate["health"]


def test_probe_uses_live_aggregator_result_when_present(tmp_path, monkeypatch, capsys):
    import vnx_cli.commands.subsystems as subsystems_mod

    fake_results = {
        "provider-routing": {"status": "ok", "signal": "measured live", "detail": {}},
    }
    monkeypatch.setattr(subsystems_mod, "_run_registered_probes", lambda data_dir: fake_results)

    rc = vnx_subsystems(_args(tmp_path, json_flag=True, probe=True))
    assert rc == 0

    rows = json.loads(capsys.readouterr().out)["subsystems"]
    provider = next(r for r in rows if r["subsystem"] == "provider-routing")
    assert provider["health"] == "ok — measured live"
    assert provider["last_signal"] == "measured live"

    # any subsystem the fake aggregator didn't cover still degrades cleanly
    other = next(r for r in rows if r["subsystem"] == "phantom_guard")
    assert other["health"] == "unknown"
    assert other["last_signal"] == "no probe registered"


def test_probe_guarded_import_matches_current_subsystem_health_contract(tmp_path):
    """Regression guard: if scripts/lib/subsystem_health.py (PR-5) is present,
    _run_registered_probes must call its real aggregate() signature without
    raising — proves the guarded import isn't silently swallowing a real
    TypeError from an API drift between PR-3 and PR-5."""
    import vnx_cli.commands.subsystems as subsystems_mod

    _engine.ensure_engine_on_path()
    try:
        import subsystem_health  # noqa: F401
    except ImportError:
        pytest.skip("scripts/lib/subsystem_health.py (PR-5) not present yet")

    result = subsystems_mod._run_registered_probes(tmp_path)
    assert isinstance(result, dict)


# ---------------------------------------------------------------------------
# Regression guard: all_beacons() must never be called argless
# ---------------------------------------------------------------------------

def test_all_beacons_called_with_resolved_data_dir(tmp_path, monkeypatch, capsys):
    _engine.ensure_engine_on_path()
    import health_beacon

    calls = []
    real_all_beacons = health_beacon.all_beacons

    def _spy(state_dir):
        calls.append(state_dir)
        return real_all_beacons(state_dir)

    monkeypatch.setattr(health_beacon, "all_beacons", _spy)

    vnx_subsystems(_args(tmp_path, json_flag=True))
    capsys.readouterr()

    assert len(calls) == 1
    assert isinstance(calls[0], Path)
    assert calls[0].name == ".vnx-data" or str(calls[0])  # a real resolved path, not None
