#!/usr/bin/env python3
"""tests/test_sessionstart_hook_producer_freshness.py — OI-1460 ("absence must
be loud"): hooks/sessionstart.sh must surface WHICH producer/gate went silent,
not just the coarse ok/stale verdict of the sweep's own heartbeat.

Defect this closes: scripts/lib/producer_freshness.py already detects the
exact failure OI-1460 names ("Geen enkele controle gaat af wanneer een poort
STOPT met produceren") — it groups activity per producer key and flags a key
whose newest record is older than its cadence. It runs on a schedule, writes
its findings to producer_freshness.ndjson, and writes a heartbeat on every
run (hooks/monitor_tripwire.sh watches that heartbeat's age). None of that
detection ever reaches a human or a T0 session: monitor_tripwire.sh checks
only the heartbeat FILE'S AGE, and hooks/sessionstart.sh's existing "Beacon
health" section (see test_sessionstart_hook_beacon_health.py) only prints the
beacon's derived health (ok/stale/fail) for `producer_freshness_monitor` as
ONE beacon among many — never the findings recorded INSIDE that run. A melder
exists; there was no consumer.

Measured live 2026-09-04 against this repo's own store (never trusted from a
handoff, per this project's own "een getal zonder herkomst is een claim"
rule): the persisted heartbeat at ~/.vnx-data/vnx-dev/health/
producer_freshness_monitor.json reads `"status": "stale"`,
`"details": {"findings_count": 10, ...}` — and the underlying
producer_freshness.ndjson for that same run_id (91dc22bbd480) carries, among
the ten, `review_gate_obligations/codex_gate` silent for 24.88 days. A T0
session at SessionStart sees "[stale] producer_freshness_monitor: last_run
..., age ...s" in the existing Beacon health block and nothing else — the
codex_gate finding itself is invisible without opening
producer_freshness.ndjson by hand.

Harness pattern (env isolation, `_run_hook`, `_make_terminal_project`) copied
from tests/test_sessionstart_hook_beacon_health.py, the sibling test for the
same hook's beacon-health section — same VNX_DATA_HOME redirection, same
"never touch this repo's own .vnx-data or the live ~/.vnx-data/<project>/
store" rule. Findings are written via the real
scripts/lib/producer_freshness.py `append_report()` (never a hand-rolled
NDJSON fixture) so a schema drift in the writer would break these tests too,
not just the reader.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
HOOK = REPO / "hooks" / "sessionstart.sh"
LIB = REPO / "scripts" / "lib"
sys.path.insert(0, str(LIB))

import vnx_paths  # noqa: E402
import producer_freshness as pf  # noqa: E402

_VNX_ENV_VARS = (
    "VNX_DATA_DIR",
    "VNX_STATE_DIR",
    "VNX_DATA_DIR_EXPLICIT",
    "VNX_DATA_HOME",
    "VNX_PROJECT_ID",
    "VNX_HOME",
    "VNX_BIN",
    "VNX_EXECUTABLE",
    "VNX_CANONICAL_ROOT",
    "VNX_PROJECT_ROOT",
)


def _clean_env(monkeypatch) -> None:
    for var in _VNX_ENV_VARS:
        monkeypatch.delenv(var, raising=False)


def _make_terminal_project(tmp_path: Path, name: str = "project") -> Path:
    root = tmp_path / name
    (root / ".vnx").mkdir(parents=True)
    t0_dir = root / ".claude" / "terminals" / "T0"
    t0_dir.mkdir(parents=True)
    return t0_dir


def _run_hook(cwd: Path, env: dict) -> dict:
    r = subprocess.run(
        ["bash", str(HOOK)],
        capture_output=True, text=True, cwd=str(cwd), env=env, timeout=10,
    )
    assert r.returncode == 0, f"hook must exit 0: rc={r.returncode}\nstdout={r.stdout}\nstderr={r.stderr}"
    return json.loads(r.stdout)


def _state_dir(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setenv("VNX_DATA_HOME", str(tmp_path / "vnx-data-home"))
    return Path(vnx_paths.resolve_paths()["VNX_STATE_DIR"])


class TestProducerFreshnessFindingsSurfaceAtSessionStart:
    """Klaar-wanneer: a real silent-producer finding is visible in the
    injected additionalContext, by producer, key and how long it has been
    silent — not just the coarse health of the sweep's own heartbeat."""

    def test_a_silent_gate_finding_surfaces_by_name_and_age(self, tmp_path, monkeypatch):
        """The exact OI-1460/OI-1460-evidence shape: a review-gate obligation
        (codex_gate) silent for 24.88 days. Must appear in the injected
        context naming the producer, the key and the silence duration —
        not merely 'producer_freshness_monitor: [stale]'."""
        _clean_env(monkeypatch)
        t0_dir = _make_terminal_project(tmp_path)
        state_dir = _state_dir(tmp_path, monkeypatch)
        env = dict(os.environ)

        report = {
            "run_id": "91dc22bbd480",
            "timestamp": "2026-09-04T16:00:05Z",
            "state_dir": str(state_dir),
            "producers_evaluated": 7,
            "keys_evaluated": 143,
            "findings_count": 1,
            "status": "stale",
            "producers": [],
            "findings": [
                {
                    "producer": "review_gate_obligations",
                    "key": "codex_gate",
                    "kind": "stale",
                    "last_seen": "2026-08-10T12:00:00Z",
                    "silence_seconds": 24.88 * 86400,
                    "silence_days": 24.88,
                    "cadence_seconds": 86400,
                }
            ],
        }
        pf.append_report(state_dir, report)
        pf.write_heartbeat(state_dir, report)

        out = _run_hook(t0_dir, env)
        ctx = out["hookSpecificOutput"]["additionalContext"]

        assert "codex_gate" in ctx, ctx
        assert "24.88" in ctx, ctx
        assert "review_gate_obligations" in ctx, ctx

    def test_a_clean_sweep_says_so_explicitly(self, tmp_path, monkeypatch):
        """A sweep that ran and found nothing must say so explicitly — silence
        here would be indistinguishable from 'nobody ever swept'."""
        _clean_env(monkeypatch)
        t0_dir = _make_terminal_project(tmp_path)
        state_dir = _state_dir(tmp_path, monkeypatch)
        env = dict(os.environ)

        report = {
            "run_id": "cleanrun0001",
            "timestamp": "2026-09-04T16:00:05Z",
            "state_dir": str(state_dir),
            "producers_evaluated": 7,
            "keys_evaluated": 143,
            "findings_count": 0,
            "status": "ok",
            "producers": [],
            "findings": [],
        }
        pf.append_report(state_dir, report)
        pf.write_heartbeat(state_dir, report)

        out = _run_hook(t0_dir, env)
        ctx = out["hookSpecificOutput"]["additionalContext"]

        assert "Producer freshness" in ctx, ctx
        assert "0 silent producer" in ctx, ctx

    def test_never_swept_is_a_third_state_not_silent_ok(self, tmp_path, monkeypatch):
        """No producer_freshness.ndjson at all (the monitor has never run in
        this store) must not read as a clean sweep — 'unmeasured' is its own
        branch, same convention as the beacon digest's D3b 'absence is loud'
        rule. The state dir is created (but left otherwise empty) so this
        assertion is not satisfied by an unrelated 'store not found' section
        that also happens to use the words UNAVAILABLE/UNMEASURED."""
        _clean_env(monkeypatch)
        t0_dir = _make_terminal_project(tmp_path)
        state_dir = _state_dir(tmp_path, monkeypatch)
        state_dir.mkdir(parents=True, exist_ok=True)
        env = dict(os.environ)

        out = _run_hook(t0_dir, env)
        ctx = out["hookSpecificOutput"]["additionalContext"]

        assert "PRODUCER FRESHNESS UNAVAILABLE" in ctx, ctx
        assert "UNMEASURED" in ctx, ctx

    def test_older_superseded_finding_does_not_leak_into_context(self, tmp_path, monkeypatch):
        """Only the LATEST sweep's findings may surface — a finding from a run
        that has since been re-swept clean must not linger forever in the
        session context just because the NDJSON file is append-only."""
        _clean_env(monkeypatch)
        t0_dir = _make_terminal_project(tmp_path)
        state_dir = _state_dir(tmp_path, monkeypatch)
        env = dict(os.environ)

        old_report = {
            "run_id": "oldrun0000001",
            "timestamp": "2026-09-01T10:00:00Z",
            "state_dir": str(state_dir),
            "producers_evaluated": 1,
            "keys_evaluated": 1,
            "findings_count": 1,
            "status": "stale",
            "producers": [],
            "findings": [
                {
                    "producer": "review_gate_obligations",
                    "key": "kimi_gate",
                    "kind": "stale",
                    "last_seen": "2026-08-01T00:00:00Z",
                    "silence_seconds": 30 * 86400,
                    "silence_days": 30.0,
                    "cadence_seconds": 86400,
                }
            ],
        }
        new_report = {
            "run_id": "newrun0000002",
            "timestamp": "2026-09-04T16:00:05Z",
            "state_dir": str(state_dir),
            "producers_evaluated": 1,
            "keys_evaluated": 1,
            "findings_count": 0,
            "status": "ok",
            "producers": [],
            "findings": [],
        }
        pf.append_report(state_dir, old_report)
        pf.write_heartbeat(state_dir, old_report)
        pf.append_report(state_dir, new_report)
        pf.write_heartbeat(state_dir, new_report)

        out = _run_hook(t0_dir, env)
        ctx = out["hookSpecificOutput"]["additionalContext"]

        assert "kimi_gate" not in ctx, ctx
        assert "0 silent producer" in ctx, ctx

    def test_hook_is_valid_bash(self):
        result = subprocess.run(["bash", "-n", str(HOOK)], capture_output=True, text=True)
        assert result.returncode == 0, result.stderr


if __name__ == "__main__":
    import pytest

    sys.exit(pytest.main([__file__, "-v"]))
