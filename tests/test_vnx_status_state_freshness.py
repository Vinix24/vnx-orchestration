"""tests/test_vnx_status_state_freshness.py — D2 (absence-is-loud), point 3.

"T0 weigert te orkestreren op een state ouder dan een sessie" -- a
`generated_at` older than the current session must yield a visible refusal,
not a silent briefing with stale numbers (the exact D1 failure mode:
t0_state.json stalled 23 days).

`dispatch_guard.sh` (skills/t0-orchestrator/scripts/dispatch_guard.sh) is the
actual pre-dispatch gate T0 consults, and it already refuses (WAIT, exit 2)
when `system_health.status` is degraded/failed -- see
tests/test_dispatch_guard_runtime_reader.py. That script sits outside this
worker's scripts/**-only write scope, so the refusal is wired through the
data it reads instead: `vnx status --json` (scripts/cli/vnx_status.py) folds
session-staleness into `system_health.status` before the guard ever sees it,
so the EXISTING gate refuses without any change to the guard itself.

Session start is approximated by `state_dir/panes.json`'s mtime (the same
file `_build_system_health` already reads for `uptime_seconds`).
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts" / "cli"))
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts" / "lib"))

import vnx_status as vs  # noqa: E402


def _write_state(state_dir: Path, generated_at: str, *, panes_mtime: float | None = None) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "t0_state.json").write_text(
        json.dumps({
            "schema_version": "2.2",
            "generated_at": generated_at,
            "terminals": {},
            "queues": {},
            "system_health": {"status": "healthy"},
        }),
        encoding="utf-8",
    )
    if panes_mtime is not None:
        panes = state_dir / "panes.json"
        panes.write_text("{}", encoding="utf-8")
        os.utime(panes, (panes_mtime, panes_mtime))


class TestStateFreshnessRefusal:
    def test_state_older_than_session_forces_non_healthy_status(self, tmp_path: Path) -> None:
        state_dir = tmp_path / "state"
        now = time.time()
        # generated_at: 23 days before "now"; panes.json (session start): now.
        old_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - 23 * 86400))
        _write_state(state_dir, old_iso, panes_mtime=now)

        t0 = vs._load_t0_state(state_dir)
        out = vs._build_json_output({}, t0, state_dir)

        assert out["state_freshness"]["applicable"] is True
        assert out["state_freshness"]["stale"] is True
        assert out["system_health"]["status"] != "healthy", (
            "a t0_state.json generated 23 days before this session started "
            "must not report system_health.status == healthy"
        )

    def test_fresh_state_within_session_stays_healthy(self, tmp_path: Path) -> None:
        state_dir = tmp_path / "state"
        now = time.time()
        # session started 5 minutes ago; state generated 1 minute ago (fresh).
        session_start = now - 300
        fresh_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - 60))
        _write_state(state_dir, fresh_iso, panes_mtime=session_start)

        t0 = vs._load_t0_state(state_dir)
        out = vs._build_json_output({}, t0, state_dir)

        assert out["state_freshness"]["applicable"] is True
        assert out["state_freshness"]["stale"] is False
        assert out["system_health"]["status"] == "healthy"

    def test_no_panes_json_not_applicable_no_penalty(self, tmp_path: Path) -> None:
        """No live session (e.g. a headless dispatch) can't prove staleness
        either way -- must not guess a refusal it cannot support."""
        state_dir = tmp_path / "state"
        old_iso = "2026-01-01T00:00:00Z"
        _write_state(state_dir, old_iso, panes_mtime=None)

        t0 = vs._load_t0_state(state_dir)
        out = vs._build_json_output({}, t0, state_dir)

        assert out["state_freshness"]["applicable"] is False
        assert out["system_health"]["status"] == "healthy"

    def test_degraded_status_stays_degraded_not_downgraded_by_freshness(self, tmp_path: Path) -> None:
        state_dir = tmp_path / "state"
        state_dir.mkdir(parents=True)
        now = time.time()
        old_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - 23 * 86400))
        (state_dir / "t0_state.json").write_text(
            json.dumps({
                "generated_at": old_iso,
                "system_health": {"status": "failed"},
            }),
            encoding="utf-8",
        )
        panes = state_dir / "panes.json"
        panes.write_text("{}", encoding="utf-8")
        os.utime(panes, (now, now))

        t0 = vs._load_t0_state(state_dir)
        out = vs._build_json_output({}, t0, state_dir)
        assert out["system_health"]["status"] == "failed"


class TestStateFreshnessSchema:
    def test_state_freshness_key_present_even_when_not_applicable(self, tmp_path: Path) -> None:
        state_dir = tmp_path / "state"
        state_dir.mkdir(parents=True)
        (state_dir / "t0_state.json").write_text(
            json.dumps({"generated_at": "2026-01-01T00:00:00Z", "system_health": {}}),
            encoding="utf-8",
        )
        t0 = vs._load_t0_state(state_dir)
        out = vs._build_json_output({}, t0, state_dir)
        assert "state_freshness" in out
        assert set(out["state_freshness"].keys()) >= {"applicable", "stale"}

    def test_missing_generated_at_not_applicable(self, tmp_path: Path) -> None:
        state_dir = tmp_path / "state"
        state_dir.mkdir(parents=True)
        (state_dir / "t0_state.json").write_text(
            json.dumps({"system_health": {}}), encoding="utf-8",
        )
        panes = state_dir / "panes.json"
        panes.write_text("{}", encoding="utf-8")
        t0 = vs._load_t0_state(state_dir)
        out = vs._build_json_output({}, t0, state_dir)
        assert out["state_freshness"]["applicable"] is False
