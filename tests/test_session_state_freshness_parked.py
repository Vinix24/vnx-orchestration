"""tests/test_session_state_freshness_parked.py — absence-is-loud, punt 2 (B).

Measured on main 264e9460, 2026-09-23: ``t0_recommendations.json`` was 98 days
old and session_state_freshness reported it STALE, which put SessionStart on
BLOCKED. Its producer is ``generate_t0_recommendations.py``, phase 10 of the
nightly intelligence pipeline: the layer the operator parked on 2026-09-09. The
age is a decision, not a fault. The artifact must read ``parked`` with its
reason, its age must stay visible, and a REAL stale artifact (dashboard_status)
must keep blocking.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts" / "lib"))

import beacon_register  # noqa: E402
import session_state_freshness as ssf  # noqa: E402

def _reason() -> str:
    """The register's reason for t0_recommendations. Read inside the test, after
    the behaviour assertions, so a run against code without the register fails
    on the behaviour (status stale, not parked), not on a missing symbol."""
    return beacon_register.parked_artifact_reason("t0_recommendations")


def _iso(days_old: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_old)).isoformat().replace("+00:00", "Z")


def _write(state_dir: Path, filename: str, payload: dict) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / filename).write_text(json.dumps(payload), encoding="utf-8")


def _populate(state_dir: Path, *, recommendations_days=None, dashboard_days=0.01) -> None:
    """Every artifact fresh, except the ones the caller ages. ``None`` for
    ``recommendations_days`` leaves t0_recommendations.json absent. The real
    clock is used throughout because terminal_state.json has no content
    timestamp: its age is its mtime, which is "now"."""
    _write(state_dir, "t0_state.json", {"generated_at": _iso(0.01)})
    _write(state_dir, "open_items.json", {"last_updated": _iso(0.01)})
    _write(state_dir, "terminal_state.json", {"terminals": {}})
    _write(state_dir, "dashboard_status.json", {"timestamp": _iso(dashboard_days)})
    if recommendations_days is not None:
        _write(state_dir, "t0_recommendations.json", {"timestamp": _iso(recommendations_days)})


def _assess(state_dir: Path) -> dict:
    return ssf.assess_artifact_freshness(state_dir)


class TestParkedArtifactThatIsOld:
    def test_98_day_old_recommendations_read_parked_with_the_reason(self, tmp_path):
        _populate(tmp_path, recommendations_days=98)

        entry = _assess(tmp_path)["artifacts"]["t0_recommendations"]

        assert entry["status"] == "parked"
        assert entry["age_human"] == "98 days"
        assert entry["age_hours"] > 98 * 24 - 1  # the age stays visible, not blanked
        assert entry["source"] == "content"
        assert entry["reason"] == _reason()

    def test_it_does_not_count_toward_blocked(self, tmp_path):
        _populate(tmp_path, recommendations_days=98)

        result = _assess(tmp_path)

        assert result["any_stale"] is False
        assert result["any_parked"] is True

    def test_cli_exit_code_is_zero_when_only_the_parked_artifact_is_old(self, tmp_path):
        _populate(tmp_path, recommendations_days=98)

        assert ssf.main(["--state-dir", str(tmp_path)]) == 0

    def test_cli_line_carries_age_and_reason(self, tmp_path, capsys):
        _populate(tmp_path, recommendations_days=98)

        ssf.main(["--state-dir", str(tmp_path)])

        out = capsys.readouterr().out
        assert "[STALE] t0_recommendations" not in out
        assert "[PARKED] t0_recommendations: age 98 days — " in out
        assert f"[PARKED] t0_recommendations: age 98 days — {_reason()}" in out


class TestParkedArtifactThatIsMissing:
    def test_missing_recommendations_are_parked_not_missing(self, tmp_path):
        _populate(tmp_path, recommendations_days=None)

        result = _assess(tmp_path)
        entry = result["artifacts"]["t0_recommendations"]

        assert entry["status"] == "parked"
        assert entry["age_hours"] is None and entry["age_human"] is None
        assert result["any_missing"] is False
        assert result["any_parked"] is True
        assert entry["reason"] == _reason()

    def test_cli_line_says_not_found_with_the_reason(self, tmp_path, capsys):
        _populate(tmp_path, recommendations_days=None)

        ssf.main(["--state-dir", str(tmp_path)])

        out = capsys.readouterr().out
        assert "[PARKED] t0_recommendations: not found — " in out
        assert f"[PARKED] t0_recommendations: not found — {_reason()}" in out


class TestParkingCoversAbsenceOnly:
    def test_a_fresh_parked_artifact_reads_fresh(self, tmp_path):
        """If the producer is un-parked and the file is current again, the
        register must not keep calling it parked."""
        _populate(tmp_path, recommendations_days=0.01)

        result = _assess(tmp_path)
        entry = result["artifacts"]["t0_recommendations"]

        assert entry["status"] == "fresh"
        assert "reason" not in entry
        assert result["any_parked"] is False


class TestARealStaleArtifactStillBlocks:
    def test_88_day_old_dashboard_status_is_stale_and_blocks(self, tmp_path):
        _populate(tmp_path, recommendations_days=0.01, dashboard_days=88)

        result = _assess(tmp_path)
        entry = result["artifacts"]["dashboard_status"]

        assert entry["status"] == "stale"
        assert "reason" not in entry
        assert result["any_stale"] is True
        assert ssf.main(["--state-dir", str(tmp_path)]) == 1

    def test_parked_and_stale_together_still_block_on_the_stale_one(self, tmp_path):
        _populate(tmp_path, recommendations_days=98, dashboard_days=88)

        result = _assess(tmp_path)

        assert result["artifacts"]["t0_recommendations"]["status"] == "parked"
        assert result["artifacts"]["dashboard_status"]["status"] == "stale"
        assert result["any_stale"] is True
        assert result["any_parked"] is True

    def test_a_missing_artifact_that_is_not_parked_stays_missing(self, tmp_path):
        _populate(tmp_path, recommendations_days=0.01)
        (tmp_path / "dashboard_status.json").unlink()

        result = _assess(tmp_path)

        assert result["artifacts"]["dashboard_status"]["status"] == "missing"
        assert result["any_missing"] is True
