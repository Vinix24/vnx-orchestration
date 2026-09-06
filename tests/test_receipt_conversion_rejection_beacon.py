"""Tests for scripts/lib/receipt_conversion_rejection_beacon.py (golf3b / F1-2).

report_to_receipt_converter.py's own beacon (health/report_to_receipt_converter.json,
out of scope for this dispatch — a different agent owns that file) already
carries a rejected_count integer, measured live at 27 on 2026-09-05 with zero
detail about WHICH report or WHY. This module is the Bash caller's
(receipt_processor.sh) own health writer: it parses the converter's captured
stderr for "REJECTED (fail-closed) dispatch=... file=... reason=..." lines and
writes each one as a {dispatch_id, file, reason} record into its own beacon,
reusing the exact same health_beacon.py mechanism/consumers (health_check.py,
hooks/sessionstart.sh's digest, vnx doctor, the dashboard) already used for
every other component under health/.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

SCRIPTS_LIB = Path(__file__).resolve().parent.parent / "scripts" / "lib"
if str(SCRIPTS_LIB) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_LIB))

from health_beacon import all_beacons  # noqa: E402
import receipt_conversion_rejection_beacon as beacon_mod  # noqa: E402

_REAL_REJECTED_LINE = (
    "WARNING report_to_receipt_converter: report_to_receipt_converter: "
    "REJECTED (fail-closed) dispatch=DISP-20260905-golf3b file="
    "20260905-golf3b-receipt-refusals-visible.md reason=missing model"
)


class TestParseRejections:
    def test_extracts_dispatch_file_and_reason_from_a_real_shaped_line(self) -> None:
        rejections = beacon_mod.parse_rejections(_REAL_REJECTED_LINE)
        assert rejections == [{
            "dispatch_id": "DISP-20260905-golf3b",
            "file": "20260905-golf3b-receipt-refusals-visible.md",
            "reason": "missing model",
        }]

    def test_multiple_rejections_in_one_scan_are_all_captured(self) -> None:
        raw = "\n".join([
            "WARNING report_to_receipt_converter: REJECTED (fail-closed) dispatch=A file=a.md reason=missing model",
            "INFO report_to_receipt_converter: 2 new receipt(s) emitted",
            "WARNING report_to_receipt_converter: REJECTED (fail-closed) dispatch=B file=b.md reason=missing model",
        ])
        rejections = beacon_mod.parse_rejections(raw)
        assert [r["dispatch_id"] for r in rejections] == ["A", "B"]

    def test_non_rejection_lines_are_ignored(self) -> None:
        raw = "\n".join([
            "INFO report_to_receipt_converter: 3 new receipt(s) emitted",
            "WARNING report_to_receipt_converter: append failed for x.md: boom",
            "WARNING report_to_receipt_converter: 1 rejected, 0 malformed, 0 error(s) this scan",
        ])
        assert beacon_mod.parse_rejections(raw) == []

    def test_empty_stderr_yields_no_rejections(self) -> None:
        assert beacon_mod.parse_rejections("") == []

    def test_malformed_garbage_never_raises(self) -> None:
        # Nul-is-eerst-een-meetfout: prove the parser tolerates arbitrary
        # noise rather than crashing the caller's non-fatal scan.
        garbage = "\x00\xff not even close to the pattern REJECTED (fail-closed"
        assert beacon_mod.parse_rejections(garbage) == []


class TestRecordRejections:
    def test_writes_fail_status_with_detail_when_rejections_present(self, tmp_path: Path) -> None:
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        rejections = [{"dispatch_id": "A", "file": "a.md", "reason": "missing model"}]

        beacon_mod.record_rejections(state_dir, rejections)

        beacon_path = tmp_path / "health" / "receipt_conversion_rejections.json"
        assert beacon_path.is_file(), "beacon must land at <state_dir.parent>/health/"
        payload = json.loads(beacon_path.read_text(encoding="utf-8"))
        assert payload["status"] == "fail"
        assert payload["details"]["count"] == 1
        assert payload["details"]["rejections"] == rejections
        assert payload["expected_interval_seconds"] == beacon_mod._EXPECTED_INTERVAL_SECONDS

    def test_writes_ok_status_when_no_rejections(self, tmp_path: Path) -> None:
        state_dir = tmp_path / "state"
        state_dir.mkdir()

        beacon_mod.record_rejections(state_dir, [])

        beacon_path = tmp_path / "health" / "receipt_conversion_rejections.json"
        payload = json.loads(beacon_path.read_text(encoding="utf-8"))
        assert payload["status"] == "ok"
        assert payload["details"]["count"] == 0

    def test_is_a_snapshot_not_an_accumulator(self, tmp_path: Path) -> None:
        """A report that gets re-rejected every cycle until fixed must not
        make details.rejections grow without bound — each heartbeat reflects
        only the scan that just ran."""
        state_dir = tmp_path / "state"
        state_dir.mkdir()

        beacon_mod.record_rejections(state_dir, [{"dispatch_id": "A", "file": "a.md", "reason": "x"}] * 5)
        beacon_mod.record_rejections(state_dir, [{"dispatch_id": "A", "file": "a.md", "reason": "x"}])

        beacon_path = tmp_path / "health" / "receipt_conversion_rejections.json"
        payload = json.loads(beacon_path.read_text(encoding="utf-8"))
        assert payload["details"]["count"] == 1

    def test_beacon_is_discoverable_by_beacon_register(self) -> None:
        """The component name is a module-level string constant so
        beacon_register.py's AST scan resolves it as an expected writer —
        the same 'absence-is-loud' contract every other beacon here has."""
        import beacon_register

        reg = beacon_register.read_beacon_register(SCRIPTS_LIB)
        names = {spec.name for spec in reg}
        assert beacon_mod._COMPONENT in names


class TestMainCli:
    def test_stdin_to_beacon_end_to_end(self, tmp_path: Path, capsys) -> None:
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        monkeypatch_stdin = _REAL_REJECTED_LINE

        import io
        old_stdin = sys.stdin
        try:
            sys.stdin = io.StringIO(monkeypatch_stdin)
            rc = beacon_mod.main(["--state-dir", str(state_dir)])
        finally:
            sys.stdin = old_stdin

        assert rc == 0
        beacon_path = tmp_path / "health" / "receipt_conversion_rejections.json"
        payload = json.loads(beacon_path.read_text(encoding="utf-8"))
        assert payload["details"]["rejections"][0]["dispatch_id"] == "DISP-20260905-golf3b"


class TestStalenessWatchdogFires:
    """golf3b requirement #3: prove the staleness watchdog that already
    exists (health_beacon.all_beacons()) actually classifies THIS
    component's beacon as stale once it stops running — not just some
    other component's beacon. Kapot-maken: manufacture a beacon older than
    its own expected_interval_seconds and confirm all_beacons() flags it,
    regardless of the status it self-reported at write time."""

    def test_beacon_older_than_its_own_interval_is_stale_even_if_self_reported_ok(
        self, tmp_path: Path,
    ) -> None:
        health_dir = tmp_path / "health"
        health_dir.mkdir()
        now = time.time()
        stale_ts = now - (beacon_mod._EXPECTED_INTERVAL_SECONDS * 3)
        payload = {
            "component": beacon_mod._COMPONENT,
            "last_run_ts": int(stale_ts),
            "last_run_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(stale_ts)),
            "status": "ok",
            "details": {"count": 0, "rejections": []},
            "expected_interval_seconds": beacon_mod._EXPECTED_INTERVAL_SECONDS,
        }
        (health_dir / f"{beacon_mod._COMPONENT}.json").write_text(
            json.dumps(payload), encoding="utf-8",
        )

        beacons = all_beacons(tmp_path)
        assert beacons[beacon_mod._COMPONENT]["health"] == "stale"

    def test_fresh_ok_beacon_is_not_flagged(self, tmp_path: Path) -> None:
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        beacon_mod.record_rejections(state_dir, [])

        beacons = all_beacons(tmp_path)
        assert beacons[beacon_mod._COMPONENT]["health"] == "ok"
