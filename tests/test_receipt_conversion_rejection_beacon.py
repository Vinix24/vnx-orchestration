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


def _iso(hours_ago: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - hours_ago * 3600))


def _entry(name: str, hours_ago: float) -> dict:
    return {
        "dispatch_id": name, "file": f"{name}.md", "reason": "missing model", "rejected_at": _iso(hours_ago),
    }


def _seed_converter_history(data_dir: Path, *entries: dict) -> Path:
    """Leave the converter beacon as a scan with these refusals wrote it."""
    from health_beacon import HealthBeacon

    HealthBeacon(data_dir, "report_to_receipt_converter", expected_interval_seconds=3600).heartbeat(
        status="fail", details={"rejected": list(entries)},
    )
    state_dir = data_dir / "state"
    state_dir.mkdir(exist_ok=True)
    return state_dir


class TestRecentRejections:
    def test_only_entries_inside_the_window_count(self) -> None:
        history = [_entry("old", 25), _entry("young", 23), _entry("fresh", 0)]
        recent = beacon_mod.recent_rejections(history)
        assert [e["dispatch_id"] for e in recent] == ["young", "fresh"]

    def test_window_is_one_day(self) -> None:
        assert beacon_mod.REJECTION_ALARM_WINDOW_SECONDS == 24 * 3600

    @pytest.mark.parametrize("rejected_at", [None, "", "yesterday", "2026-09-23", 1727000000, "2026-09-23T10:00:00+00:00"])
    def test_entry_without_a_parseable_timestamp_counts_as_old(self, rejected_at) -> None:
        """An age that cannot be shown must not hold the alarm: such an entry
        would pin the beacon at fail until 200 newer entries evicted it, which
        on a quiet store is never."""
        entry = {"dispatch_id": "x", "file": "x.md", "reason": "r", "rejected_at": rejected_at}
        assert beacon_mod.recent_rejections([entry]) == []

    def test_entry_without_the_key_at_all_counts_as_old(self) -> None:
        assert beacon_mod.recent_rejections([{"dispatch_id": "x"}]) == []


class TestLoadRejectedHistory:
    def test_missing_beacon_file_is_an_empty_history(self, tmp_path: Path) -> None:
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        assert beacon_mod.load_rejected_history(state_dir, beacon_mod.CONVERTER_COMPONENT) == []

    def test_corrupt_beacon_file_is_an_empty_history(self, tmp_path: Path) -> None:
        (tmp_path / "state").mkdir()
        (tmp_path / "health").mkdir()
        (tmp_path / "health" / f"{beacon_mod.CONVERTER_COMPONENT}.json").write_text("{not json", encoding="utf-8")
        assert beacon_mod.load_rejected_history(tmp_path / "state", beacon_mod.CONVERTER_COMPONENT) == []

    def test_non_dict_entries_are_dropped(self, tmp_path: Path) -> None:
        state_dir = _seed_converter_history(tmp_path, _entry("a", 1), "not-a-dict", 7)
        assert [e["dispatch_id"] for e in beacon_mod.load_rejected_history(state_dir, beacon_mod.CONVERTER_COMPONENT)] == ["a"]


class TestStatusFollowsTheConverterHistory:
    """The scan after a quarantine sees no report, so its stderr carries no
    REJECTED line. The status must still come from the refusal history the
    converter beacon keeps, or this beacon says ok while that one says fail."""

    def _payload(self, data_dir: Path) -> dict:
        return json.loads((data_dir / "health" / "receipt_conversion_rejections.json").read_text(encoding="utf-8"))

    def test_empty_scan_with_a_refusal_inside_the_window_is_fail(self, tmp_path: Path) -> None:
        state_dir = _seed_converter_history(tmp_path, _entry("young", 3))

        beacon_mod.record_rejections(state_dir, [])

        payload = self._payload(tmp_path)
        assert payload["status"] == "fail"
        assert payload["details"]["count"] == 0
        assert [e["dispatch_id"] for e in payload["details"]["recent_rejected"]] == ["young"]

    def test_empty_scan_with_only_old_refusals_is_ok_and_keeps_naming_nothing_as_recent(self, tmp_path: Path) -> None:
        state_dir = _seed_converter_history(tmp_path, _entry("old", 30))

        beacon_mod.record_rejections(state_dir, [])

        payload = self._payload(tmp_path)
        assert payload["status"] == "ok"
        assert payload["details"]["recent_rejected"] == []

    def test_a_rejection_in_this_scan_is_fail_even_when_the_converter_history_is_old_or_absent(self, tmp_path: Path) -> None:
        state_dir = _seed_converter_history(tmp_path, _entry("old", 30))

        beacon_mod.record_rejections(state_dir, [{"dispatch_id": "A", "file": "a.md", "reason": "missing model"}])

        assert self._payload(tmp_path)["status"] == "fail"

    def test_a_report_refused_on_every_scan_is_named_once_with_its_newest_timestamp(self, tmp_path: Path) -> None:
        state_dir = _seed_converter_history(
            tmp_path, _entry("again", 5), _entry("again", 3), _entry("again", 1), _entry("other", 2),
        )

        beacon_mod.record_rejections(state_dir, [])

        recent = self._payload(tmp_path)["details"]["recent_rejected"]
        # Ascending by rejected_at: "other" (2h ago), then "again" at its newest (1h ago).
        assert [(e["dispatch_id"], e["rejected_at"]) for e in recent] == [
            ("other", _entry("other", 2)["rejected_at"]),
            ("again", _entry("again", 1)["rejected_at"]),
        ]

    def test_window_is_reported_so_a_reader_can_see_why_it_is_fail(self, tmp_path: Path) -> None:
        state_dir = _seed_converter_history(tmp_path)
        beacon_mod.record_rejections(state_dir, [])
        assert self._payload(tmp_path)["details"]["alarm_window_seconds"] == beacon_mod.REJECTION_ALARM_WINDOW_SECONDS


class TestBothBeaconsAgreeOnTheSameScan:
    """Requirement: no contradictory verdict between report_to_receipt_converter
    and receipt_conversion_rejections over the same scan. Drives the REAL
    converter scan, then feeds the beacon what receipt_processor.sh feeds it:
    the scan's stderr."""

    _REPORT = (
        "---\ndispatch_id: {name}\nprovider: claude\n---\n\n"
        "## Summary\n\nImplemented the feature per dispatch specification. "
        "All tests pass and coverage is at target.\n\n"
        "## Changes\n\n- scripts/lib/example.py: added X\n\n"
        "## Verification\n\npytest tests/ -x: 42 passed\n\n"
        "## Open Items\n\nNone\n"
    )

    def _scan_and_feed_the_beacon(self, reports_dir: Path, state_dir: Path, caplog) -> tuple[str, str, int]:
        """(converter status, rejections-beacon status, REJECTED lines this scan wrote to stderr)."""
        import logging

        import report_to_receipt_converter as rtc

        caplog.clear()
        with caplog.at_level(logging.WARNING, logger="report_to_receipt_converter"):
            rtc.scan_and_convert([reports_dir], state_dir)
        stderr = "\n".join(f"WARNING report_to_receipt_converter: {r.getMessage()}" for r in caplog.records)
        scan_rejections = beacon_mod.parse_rejections(stderr)
        beacon_mod.record_rejections(state_dir, scan_rejections)

        health = state_dir.parent / "health"
        verdicts = [
            json.loads((health / f"{component}.json").read_text(encoding="utf-8"))["status"]
            for component in ("report_to_receipt_converter", "receipt_conversion_rejections")
        ]
        return verdicts[0], verdicts[1], len(scan_rejections)

    def test_same_verdict_on_the_refusing_scan_and_the_scan_after_it(self, tmp_path: Path, caplog) -> None:
        reports_dir = tmp_path / "unified_reports"
        reports_dir.mkdir()
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        (reports_dir / "20260601-agree.md").write_text(self._REPORT.format(name="20260601-agree"), encoding="utf-8")

        assert self._scan_and_feed_the_beacon(reports_dir, state_dir, caplog) == ("fail", "fail", 1)
        # Scan 2 sees no report and its stderr carries no REJECTED line: the
        # rejections beacon can only say fail by reading the history.
        assert self._scan_and_feed_the_beacon(reports_dir, state_dir, caplog) == ("fail", "fail", 0)

    def test_same_verdict_once_the_refusal_is_older_than_the_window(self, tmp_path: Path, caplog) -> None:
        reports_dir = tmp_path / "unified_reports"
        reports_dir.mkdir()
        state_dir = _seed_converter_history(tmp_path, _entry("20260601-aged", 30))

        assert self._scan_and_feed_the_beacon(reports_dir, state_dir, caplog) == ("ok", "ok", 0)

    def test_same_verdict_for_a_refusal_next_to_a_booked_receipt(self, tmp_path: Path, caplog) -> None:
        reports_dir = tmp_path / "unified_reports"
        reports_dir.mkdir()
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        (reports_dir / "20260601-no-model.md").write_text(self._REPORT.format(name="20260601-no-model"), encoding="utf-8")
        (reports_dir / "20260601-with-model.md").write_text(
            self._REPORT.format(name="20260601-with-model").replace(
                "provider: claude\n", "provider: claude\nmodel: claude-sonnet-4-6\n"
            ),
            encoding="utf-8",
        )

        assert self._scan_and_feed_the_beacon(reports_dir, state_dir, caplog) == ("fail", "fail", 1)

    def test_same_verdict_for_a_model_name_in_the_wrong_shape(self, tmp_path: Path, caplog) -> None:
        """``invalid_model_shape`` is the other model refusal. Before it took
        the quarantine path the converter beacon said fail (error, retried) and
        this beacon said ok (it never saw a REJECTED line)."""
        reports_dir = tmp_path / "unified_reports"
        reports_dir.mkdir()
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        (reports_dir / "20260601-glued.md").write_text(
            "**Dispatch-ID:** 20260601-glued\n"
            "**Model:** glm-5.2 · **Provider:** claude\n\n"
            + self._REPORT.split("---\n\n", 1)[1],
            encoding="utf-8",
        )

        assert self._scan_and_feed_the_beacon(reports_dir, state_dir, caplog) == ("fail", "fail", 1)
        assert self._scan_and_feed_the_beacon(reports_dir, state_dir, caplog) == ("fail", "fail", 0)


def test_converter_component_name_matches_the_converter_beacon() -> None:
    """The reader in this module and the writer in the converter name the same
    file; a rename on one side alone would make this beacon read nothing."""
    import report_to_receipt_converter as rtc

    assert beacon_mod.CONVERTER_COMPONENT == rtc._HEALTH_COMPONENT


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
