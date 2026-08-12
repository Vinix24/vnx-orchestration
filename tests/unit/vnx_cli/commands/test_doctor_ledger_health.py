#!/usr/bin/env python3
"""Unit tests for vnx_cli/commands/doctor.py's ledger-health check.

Dispatch 20260812d-a-ledger-health: ``vnx doctor`` must go loud when the
ledger_health beacon (scripts/ledger_health.py) reports dispatches without a
receipt, a stale pull cursor, or a chain-status contradiction — but must
never fabricate a FAIL when the beacon simply hasn't been run yet.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

VNX_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(VNX_ROOT / "scripts"))
sys.path.insert(0, str(VNX_ROOT / "scripts" / "lib"))

import ledger_health as lh  # noqa: E402
from vnx_cli.commands.doctor import PASS, WARN, _check_ledger_health  # noqa: E402

_NO_DETAILS = object()


def _write_raw_beacon(tmp_path: Path, *, status: str, details=_NO_DETAILS, expected_interval_seconds=86400) -> None:
    """Write a ledger_health beacon file directly, bypassing compute_health/
    write_health_surface, so a test can force a ``details`` shape that the
    real writer would never produce (missing, empty, or malformed)."""
    health_dir = tmp_path / "health"
    health_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "component": lh.COMPONENT_NAME,
        "last_run_ts": time.time(),
        "last_run_iso": "2026-08-12T00:00:00Z",
        "status": status,
        "expected_interval_seconds": expected_interval_seconds,
    }
    if details is not _NO_DETAILS:
        payload["details"] = details
    (health_dir / f"{lh.COMPONENT_NAME}.json").write_text(json.dumps(payload), encoding="utf-8")


def _register_entry(dispatch_id: str) -> dict:
    return {"timestamp": "2026-08-11T10:00:00Z", "event": "dispatch_started", "dispatch_id": dispatch_id}


def _receipt(dispatch_id: str) -> dict:
    return {"timestamp": "2026-08-11T10:05:00Z", "event_type": "task_complete", "dispatch_id": dispatch_id}


def _write_ndjson(path: Path, *records: dict) -> None:
    lines = [__import__("json").dumps(r) for r in records]
    text = "\n".join(lines)
    if records:
        text += "\n"
    path.write_text(text, encoding="utf-8")


class TestCheckLedgerHealth:
    def test_no_beacon_yet_is_pass_not_fail(self, tmp_path):
        result = _check_ledger_health(tmp_path)
        assert result.status == PASS
        assert "ledger_health.py" in result.detail

    def test_all_healthy_beacon_is_pass(self, tmp_path):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        _write_ndjson(state_dir / lh.REGISTER_NAME, _register_entry("d-001"))
        _write_ndjson(state_dir / lh.LEDGER_NAME, _receipt("d-001"))
        ledger_size = (state_dir / lh.LEDGER_NAME).stat().st_size
        (state_dir / lh.CURSOR_NAME).write_text(
            __import__("json").dumps({"offset": ledger_size}), encoding="utf-8"
        )

        computed = lh.compute_health(tmp_path, state_dir)
        lh.write_health_surface(tmp_path, computed)

        result = _check_ledger_health(tmp_path)
        assert result.status == PASS

    def test_missing_receipt_finding_is_warn_with_detail(self, tmp_path):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        _write_ndjson(state_dir / lh.REGISTER_NAME, _register_entry("d-orphan"))
        _write_ndjson(state_dir / lh.LEDGER_NAME, _receipt("d-001"))
        ledger_size = (state_dir / lh.LEDGER_NAME).stat().st_size
        (state_dir / lh.CURSOR_NAME).write_text(
            __import__("json").dumps({"offset": ledger_size}), encoding="utf-8"
        )

        computed = lh.compute_health(tmp_path, state_dir)
        lh.write_health_surface(tmp_path, computed)

        result = _check_ledger_health(tmp_path)
        assert result.status == WARN
        assert "no matching receipt" in result.detail

    def test_unmeasurable_subcheck_is_warn_not_silently_ok(self, tmp_path):
        """No register/ledger at all -> ledger_health itself reports
        SKIPPED_UNVERIFIED. Doctor must surface that as WARN, never PASS —
        an unmeasurable state is never a pass (#1468 principle)."""
        state_dir = tmp_path / "state"
        state_dir.mkdir()

        computed = lh.compute_health(tmp_path, state_dir)
        lh.write_health_surface(tmp_path, computed)

        result = _check_ledger_health(tmp_path)
        assert result.status == WARN
        assert "unmeasurable" in result.detail

    def test_corrupt_beacon_is_warn(self, tmp_path):
        health_dir = tmp_path / "health"
        health_dir.mkdir()
        (health_dir / "ledger_health.json").write_text("{not json", encoding="utf-8")

        result = _check_ledger_health(tmp_path)
        assert result.status == WARN
        assert "corrupt" in result.detail.lower()

    def test_corrupt_register_line_is_warn_not_pass(self, tmp_path):
        """Regression (dispatch 20260812d-c): a corrupt line in
        dispatch_register.ndjson with zero missing receipts used to make
        ``check_receipt_coverage`` report STATUS_OK, so doctor would PASS on
        a register it never fully read. It must now surface as WARN via the
        SKIPPED_UNVERIFIED branch, the same as any other unmeasurable state.
        """
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        (state_dir / lh.REGISTER_NAME).write_text(
            __import__("json").dumps(_register_entry("d-001")) + "\n" + "{not json\n",
            encoding="utf-8",
        )
        _write_ndjson(state_dir / lh.LEDGER_NAME, _receipt("d-001"))
        ledger_size = (state_dir / lh.LEDGER_NAME).stat().st_size
        (state_dir / lh.CURSOR_NAME).write_text(
            __import__("json").dumps({"offset": ledger_size}), encoding="utf-8"
        )

        computed = lh.compute_health(tmp_path, state_dir)
        lh.write_health_surface(tmp_path, computed)

        result = _check_ledger_health(tmp_path)
        assert result.status == WARN
        assert "unmeasurable" in result.detail

    def test_stale_beacon_is_warn(self, tmp_path, monkeypatch):
        """A beacon older than its expected_interval_seconds is stale
        regardless of the status it recorded at write time (health_beacon
        convention) — simulate by writing directly with an old last_run_ts."""
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        _write_ndjson(state_dir / lh.REGISTER_NAME, _register_entry("d-001"))
        _write_ndjson(state_dir / lh.LEDGER_NAME, _receipt("d-001"))
        ledger_size = (state_dir / lh.LEDGER_NAME).stat().st_size
        (state_dir / lh.CURSOR_NAME).write_text(
            __import__("json").dumps({"offset": ledger_size}), encoding="utf-8"
        )
        computed = lh.compute_health(tmp_path, state_dir)
        lh.write_health_surface(tmp_path, computed)

        beacon_path = tmp_path / "health" / "ledger_health.json"
        import json as _json
        payload = _json.loads(beacon_path.read_text(encoding="utf-8"))
        payload["last_run_ts"] = 0  # 1970 — long past any interval
        beacon_path.write_text(_json.dumps(payload), encoding="utf-8")

        result = _check_ledger_health(tmp_path)
        assert result.status == WARN
        assert "stale" in result.detail.lower()


class TestFailBeaconWithUnreadableDetails:
    """Dispatch 20260812d-d: a beacon that says ``health: "fail"`` must never
    fall through to PASS just because ``details["checks"]`` couldn't be
    read — unknown is never healthy."""

    def test_fail_beacon_missing_details_is_warn_not_pass(self, tmp_path):
        _write_raw_beacon(tmp_path, status="fail")

        result = _check_ledger_health(tmp_path)
        assert result.status == WARN
        assert "fail" in result.detail.lower()

    def test_fail_beacon_empty_checks_is_warn_not_pass(self, tmp_path):
        _write_raw_beacon(tmp_path, status="fail", details={"checks": {}})

        result = _check_ledger_health(tmp_path)
        assert result.status == WARN
        assert "fail" in result.detail.lower()

    def test_fail_beacon_malformed_checks_is_warn_not_pass(self, tmp_path):
        _write_raw_beacon(tmp_path, status="fail", details={"checks": "not-a-dict"})

        result = _check_ledger_health(tmp_path)
        assert result.status == WARN
        assert "fail" in result.detail.lower()

    def test_ok_beacon_without_findings_is_still_pass(self, tmp_path):
        """Regression guard: a genuinely healthy beacon must not start
        WARNing just because the fail-path got stricter."""
        _write_raw_beacon(
            tmp_path,
            status="ok",
            details={
                "checks": {
                    "receipt_coverage": {"status": "ok"},
                    "pull_cursor": {"status": "ok"},
                    "chain_status": {"status": "ok"},
                }
            },
        )

        result = _check_ledger_health(tmp_path)
        assert result.status == PASS
