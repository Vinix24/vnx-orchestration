"""t0_state has its own, shorter staleness limit (OI-3056); every artifact entry
carries the limit it was judged by."""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "lib"))

import session_state_freshness as ssf  # noqa: E402

NOW = datetime(2026, 11, 22, 12, 0, 0, tzinfo=timezone.utc)


def _write(state: Path, name: str, field: str, age_hours: float) -> None:
    ts = (NOW - timedelta(hours=age_hours)).isoformat()
    (state / name).write_text(json.dumps({field: ts}), encoding="utf-8")


def _populate(state: Path, age_hours: float) -> None:
    _write(state, "t0_state.json", "generated_at", age_hours)
    _write(state, "open_items.json", "last_updated", age_hours)
    _write(state, "dashboard_status.json", "timestamp", age_hours)
    _write(state, "t0_recommendations.json", "timestamp", age_hours)


def test_t0_state_limit_is_four_hours_and_the_rest_stay_24() -> None:
    assert ssf.T0_STATE_STALE_AFTER_HOURS == 4.0
    assert ssf.SESSION_STALE_AFTER_HOURS == 24.0


def test_t0_state_at_five_hours_is_stale_while_open_items_is_fresh(tmp_path: Path) -> None:
    _populate(tmp_path, 5.0)
    result = ssf.assess_artifact_freshness(tmp_path, now=NOW)
    assert result["artifacts"]["t0_state"]["status"] == "stale"
    assert result["artifacts"]["open_items"]["status"] == "fresh"
    assert result["any_stale"] is True


def test_t0_state_at_three_hours_is_fresh(tmp_path: Path) -> None:
    _populate(tmp_path, 3.0)
    result = ssf.assess_artifact_freshness(tmp_path, now=NOW)
    assert result["artifacts"]["t0_state"]["status"] == "fresh"
    assert result["any_stale"] is False


def test_every_entry_carries_its_own_threshold(tmp_path: Path) -> None:
    _write(tmp_path, "t0_state.json", "generated_at", 1.0)
    (tmp_path / "open_items.json").write_text("not json", encoding="utf-8")
    # dashboard_status.json, t0_recommendations.json absent
    result = ssf.assess_artifact_freshness(tmp_path, now=NOW)
    assert set(result["artifacts"]) == set(ssf.ARTIFACTS)
    for name, entry in result["artifacts"].items():
        assert isinstance(entry["threshold_hours"], float), (name, entry)
    assert result["artifacts"]["t0_state"]["threshold_hours"] == 4.0
    assert result["artifacts"]["open_items"]["threshold_hours"] == 24.0
    assert result["threshold_hours"] == 24.0


def test_cli_text_shows_each_limit(tmp_path: Path, capsys) -> None:
    _populate(tmp_path, 5.0)
    ssf.main(["--state-dir", str(tmp_path)])
    out = capsys.readouterr().out
    assert "limit 4h" in out and "limit 24h" in out


def test_terminal_state_is_not_an_artifact_and_never_blocks(tmp_path: Path) -> None:
    """OI-3058: no writer since T-G1; a 17-day-old file or none gives no entry and no block."""
    assert "terminal_state" not in ssf.ARTIFACTS
    _populate(tmp_path, 1.0)
    for old in (False, True):
        path = tmp_path / "terminal_state.json"
        if old:
            path.write_text("{}", encoding="utf-8")
            stamp = NOW.timestamp() - 17 * 86400
            os.utime(path, (stamp, stamp))
        result = ssf.assess_artifact_freshness(tmp_path, now=NOW)
        assert "terminal_state" not in result["artifacts"]
        assert result["any_stale"] is False
