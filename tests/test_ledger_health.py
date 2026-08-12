#!/usr/bin/env python3
"""Tests for scripts/ledger_health.py (dispatch 20260812d-a-ledger-health).

Covers the three read-only checks (receipt coverage, pull-cursor freshness,
chain status), the atomic health-surface write/read round-trip, and the CLI.

The headline regression this file exists to prove: a substring search over
the raw receipts ledger gives a FALSE POSITIVE for dispatch coverage when a
receipt's ``branch`` field happens to contain another dispatch's slug (the
exact shape measured in the real ledger for PR #1454/#1455's gate receipts —
``dispatch_id: "20260811c-gate-pr1454"``, ``branch:
"dispatch/20260811c-a-freshinstall-hookpins"``). ``check_receipt_coverage``
must report that dispatch as missing a receipt; a naive substring check would
not.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).resolve().parent
VNX_ROOT = TESTS_DIR.parent
SCRIPTS_DIR = VNX_ROOT / "scripts"
SCRIPTS_LIB = SCRIPTS_DIR / "lib"
sys.path.insert(0, str(SCRIPTS_LIB))
sys.path.insert(0, str(SCRIPTS_DIR))

import ledger_health as lh  # noqa: E402
from ndjson_hash_chain import append_chained_entry  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _write_ndjson(path: Path, *records: dict) -> None:
    lines = [json.dumps(r) for r in records]
    text = "\n".join(lines)
    if records:
        text += "\n"
    path.write_text(text, encoding="utf-8")


def _register_entry(dispatch_id: str, event: str = "dispatch_started") -> dict:
    return {"timestamp": "2026-08-11T10:00:00Z", "event": event, "dispatch_id": dispatch_id}


def _receipt(dispatch_id: str, **overrides) -> dict:
    rec = {
        "timestamp": "2026-08-11T10:05:00Z",
        "event_type": "task_complete",
        "dispatch_id": dispatch_id,
        "status": "success",
    }
    rec.update(overrides)
    return rec


@pytest.fixture
def state_dir(tmp_path):
    d = tmp_path / "state"
    d.mkdir()
    return d


# ---------------------------------------------------------------------------
# receipt_coverage
# ---------------------------------------------------------------------------


class TestReceiptCoverage:
    def test_all_covered_is_ok(self, state_dir):
        _write_ndjson(state_dir / lh.REGISTER_NAME, _register_entry("d-001"), _register_entry("d-002"))
        _write_ndjson(state_dir / lh.LEDGER_NAME, _receipt("d-001"), _receipt("d-002"))

        result = lh.check_receipt_coverage(state_dir)

        assert result["status"] == lh.STATUS_OK
        assert result["missing_receipt_count"] == 0
        assert result["missing_receipt_dispatch_ids"] == []

    def test_missing_receipt_is_a_finding(self, state_dir):
        _write_ndjson(state_dir / lh.REGISTER_NAME, _register_entry("d-001"), _register_entry("d-orphan"))
        _write_ndjson(state_dir / lh.LEDGER_NAME, _receipt("d-001"))

        result = lh.check_receipt_coverage(state_dir)

        assert result["status"] == lh.STATUS_FINDING
        assert result["missing_receipt_dispatch_ids"] == ["d-orphan"]

    def test_substring_trap_branch_field_is_not_a_false_match(self, state_dir):
        """The exact shape measured in the real ledger (#1454/#1455): a gate
        receipt's dispatch_id is its OWN id, but its ``branch`` field embeds
        the slug of the dispatch that was actually fired and merged. Field
        match must call that dispatch missing; substring match would not.
        """
        target_dispatch_id = "20260811c-a-freshinstall-hookpins"
        gate_receipt = _receipt(
            "20260811c-gate-pr1454",
            event_type="review_gate_request",
            branch=f"dispatch/{target_dispatch_id}",
        )
        _write_ndjson(state_dir / lh.REGISTER_NAME, _register_entry(target_dispatch_id))
        _write_ndjson(state_dir / lh.LEDGER_NAME, gate_receipt)

        # Prove the substring trap is real: a naive text search over the raw
        # ledger DOES find a "hit" for the target dispatch id.
        raw_ledger_text = (state_dir / lh.LEDGER_NAME).read_text(encoding="utf-8")
        assert target_dispatch_id in raw_ledger_text, (
            "fixture is wrong — the substring trap this test defends against isn't present"
        )

        # The field-matching tool must NOT be fooled by it.
        result = lh.check_receipt_coverage(state_dir)
        assert result["status"] == lh.STATUS_FINDING
        assert target_dispatch_id in result["missing_receipt_dispatch_ids"]

    def test_legacy_cmd_id_fallback_counts_as_covered(self, state_dir):
        """Mirrors receipt_provenance.find_receipts_by_dispatch's cmd_id fallback."""
        _write_ndjson(state_dir / lh.REGISTER_NAME, _register_entry("d-legacy"))
        _write_ndjson(
            state_dir / lh.LEDGER_NAME,
            {"timestamp": "2026-08-11T10:05:00Z", "cmd_id": "d-legacy", "status": "success"},
        )

        result = lh.check_receipt_coverage(state_dir)
        assert result["status"] == lh.STATUS_OK

    def test_malformed_lines_do_not_crash_the_check(self, state_dir):
        """A malformed line must never raise — but (regression, dispatch
        20260812d-c) it must also never let the check report STATUS_OK: a
        corrupt register line could be hiding a dispatch_id that has no
        receipt at all, which this check would then never see."""
        register_path = state_dir / lh.REGISTER_NAME
        register_path.write_text(
            json.dumps(_register_entry("d-001")) + "\n" + "{not json\n", encoding="utf-8"
        )
        _write_ndjson(state_dir / lh.LEDGER_NAME, _receipt("d-001"))

        result = lh.check_receipt_coverage(state_dir)
        assert result["register_parse_errors"] == 1
        assert result["status"] != lh.STATUS_OK

    def test_corrupt_register_line_with_no_missing_receipts_is_not_ok(self, state_dir):
        """Regression (dispatch 20260812d-c): on the pre-fix branch this case
        reported STATUS_OK because ``missing`` came back empty — the parse
        error itself never touched the status. A corrupt register line means
        the register was NOT fully read, so coverage cannot be certified as
        OK even when every dispatch_id that DID parse has a receipt.
        """
        register_path = state_dir / lh.REGISTER_NAME
        register_path.write_text(
            json.dumps(_register_entry("d-001")) + "\n" + "{not json\n", encoding="utf-8"
        )
        _write_ndjson(state_dir / lh.LEDGER_NAME, _receipt("d-001"))

        result = lh.check_receipt_coverage(state_dir)

        assert result["missing_receipt_count"] == 0
        assert result["register_parse_errors"] == 1
        assert result["status"] != lh.STATUS_OK
        assert result["status"] == lh.SKIPPED_UNVERIFIED

    def test_corrupt_receipt_line_is_not_ok(self, state_dir):
        """Regression (dispatch 20260812d-c): the same blind spot on the
        receipts side. A malformed line in ``t0_receipts.ndjson`` could be
        hiding the exact receipt that would clear a dispatch off `missing` —
        or hiding nothing. Either way, coverage cannot be certified.
        """
        _write_ndjson(state_dir / lh.REGISTER_NAME, _register_entry("d-001"))
        ledger_path = state_dir / lh.LEDGER_NAME
        ledger_path.write_text(
            json.dumps(_receipt("d-001")) + "\n" + "{not json\n", encoding="utf-8"
        )

        result = lh.check_receipt_coverage(state_dir)

        assert result["receipt_parse_errors"] == 1
        assert result["status"] != lh.STATUS_OK
        assert result["status"] == lh.SKIPPED_UNVERIFIED

    def test_missing_register_is_unmeasurable(self, state_dir):
        _write_ndjson(state_dir / lh.LEDGER_NAME, _receipt("d-001"))
        result = lh.check_receipt_coverage(state_dir)
        assert result["status"] == lh.SKIPPED_UNVERIFIED

    def test_missing_ledger_is_unmeasurable(self, state_dir):
        _write_ndjson(state_dir / lh.REGISTER_NAME, _register_entry("d-001"))
        result = lh.check_receipt_coverage(state_dir)
        assert result["status"] == lh.SKIPPED_UNVERIFIED


# ---------------------------------------------------------------------------
# pull_cursor
# ---------------------------------------------------------------------------


class TestPullCursor:
    def test_fresh_cursor_is_ok(self, state_dir):
        _write_ndjson(state_dir / lh.LEDGER_NAME, _receipt("d-001"))
        ledger_size = (state_dir / lh.LEDGER_NAME).stat().st_size
        cursor_path = state_dir / lh.CURSOR_NAME
        cursor_path.write_text(json.dumps({"offset": ledger_size}), encoding="utf-8")

        result = lh.check_pull_cursor(state_dir, stale_hours=24.0)

        assert result["status"] == lh.STATUS_OK
        assert result["cursor_age_seconds"] < 5.0

    def test_stale_cursor_is_a_finding(self, state_dir):
        _write_ndjson(state_dir / lh.LEDGER_NAME, _receipt("d-001"))
        cursor_path = state_dir / lh.CURSOR_NAME
        cursor_path.write_text(json.dumps({"offset": 0}), encoding="utf-8")
        old = time.time() - (25 * 3600)  # 25h old, past the 24h default threshold
        os.utime(cursor_path, (old, old))

        result = lh.check_pull_cursor(state_dir, stale_hours=24.0)

        assert result["status"] == lh.STATUS_FINDING
        assert result["cursor_age_seconds"] > 24 * 3600

    def test_never_pulled_nonempty_ledger_is_a_finding(self, state_dir):
        _write_ndjson(state_dir / lh.LEDGER_NAME, _receipt("d-001"))
        result = lh.check_pull_cursor(state_dir)
        assert result["status"] == lh.STATUS_FINDING
        assert result["cursor_exists"] is False

    def test_never_pulled_empty_ledger_is_ok(self, state_dir):
        (state_dir / lh.LEDGER_NAME).write_text("", encoding="utf-8")
        result = lh.check_pull_cursor(state_dir)
        assert result["status"] == lh.STATUS_OK

    def test_never_mutates_cursor_on_disk(self, state_dir):
        """Hard requirement (dispatch instruction): this check must NEVER
        advance the cursor. Byte-for-byte identical before and after."""
        _write_ndjson(state_dir / lh.LEDGER_NAME, _receipt("d-001"), _receipt("d-002"))
        cursor_path = state_dir / lh.CURSOR_NAME
        cursor_path.write_text(json.dumps({"offset": 0}), encoding="utf-8")

        before_bytes = cursor_path.read_bytes()
        before_mtime = cursor_path.stat().st_mtime

        result = lh.check_pull_cursor(state_dir)

        after_bytes = cursor_path.read_bytes()
        after_mtime = cursor_path.stat().st_mtime

        assert before_bytes == after_bytes
        assert before_mtime == after_mtime
        assert result["backlog_receipt_count"] == 2

    def test_truncated_ledger_since_cursor_is_flagged(self, state_dir):
        _write_ndjson(state_dir / lh.LEDGER_NAME, _receipt("d-001"))
        cursor_path = state_dir / lh.CURSOR_NAME
        # Offset far beyond the (small) ledger — simulates a rotated/truncated ledger.
        cursor_path.write_text(json.dumps({"offset": 10_000_000}), encoding="utf-8")

        result = lh.check_pull_cursor(state_dir)

        assert result["ledger_truncated_since_cursor"] is True
        assert result["status"] == lh.STATUS_FINDING

    def test_truncated_ledger_is_not_ok_even_when_cursor_is_fresh(self, state_dir):
        """Regression (dispatch 20260812d-c): on the pre-fix branch a fresh
        (non-stale) cursor masked truncation entirely — ``truncated`` was
        computed and reported but never touched ``status``, so a rotated/
        truncated ledger read as STATUS_OK as long as the cursor file itself
        was young. Truncation is a finding on its own, independent of age.
        """
        _write_ndjson(state_dir / lh.LEDGER_NAME, _receipt("d-001"))
        cursor_path = state_dir / lh.CURSOR_NAME
        cursor_path.write_text(json.dumps({"offset": 10_000_000}), encoding="utf-8")
        # cursor_path.write_text() just now => age_seconds ~0, nowhere near stale.

        result = lh.check_pull_cursor(state_dir, stale_hours=24.0)

        assert result["cursor_age_seconds"] < 5.0
        assert result["status"] != lh.STATUS_OK
        assert result["status"] == lh.STATUS_FINDING

    def test_corrupt_cursor_file_is_not_ok_and_distinct_from_offset_zero(self, state_dir):
        """Regression (dispatch 20260812d-c): ``receipt_query.load_cursor``
        silently coerces a corrupt/unparseable cursor file to offset 0 — the
        SAME value a legitimate "cursor genuinely at byte 0" produces. Before
        the fix, ``ledger_health`` inherited that collapse and could report
        STATUS_OK on a cursor it could not actually read. A corrupt cursor
        must be its own outcome, not silently treated as a measured zero.
        """
        _write_ndjson(state_dir / lh.LEDGER_NAME, _receipt("d-001"))
        cursor_path = state_dir / lh.CURSOR_NAME
        cursor_path.write_text("{not valid json at all", encoding="utf-8")

        corrupt_result = lh.check_pull_cursor(state_dir, stale_hours=24.0)

        assert corrupt_result["status"] != lh.STATUS_OK
        assert corrupt_result["status"] == lh.SKIPPED_UNVERIFIED
        assert corrupt_result["cursor_corrupt"] is True

        # A legitimate offset of 0 against an empty (nothing-to-pull) ledger
        # is a materially different, healthy outcome — proves the two are
        # distinguished rather than both collapsing to "offset 0, fine".
        cursor_path.write_text(json.dumps({"offset": 0}), encoding="utf-8")
        (state_dir / lh.LEDGER_NAME).write_text("", encoding="utf-8")
        legit_result = lh.check_pull_cursor(state_dir, stale_hours=24.0)

        assert legit_result["cursor_corrupt"] is False
        assert legit_result["cursor_offset"] == 0
        assert legit_result["status"] == lh.STATUS_OK

    def test_missing_ledger_is_unmeasurable(self, state_dir):
        result = lh.check_pull_cursor(state_dir)
        assert result["status"] == lh.SKIPPED_UNVERIFIED


# ---------------------------------------------------------------------------
# chain_status
# ---------------------------------------------------------------------------


class TestChainStatus:
    def test_unchained_default_config_is_ok_but_its_own_class(self, state_dir, monkeypatch):
        monkeypatch.delenv("VNX_CHAIN_RECEIPTS", raising=False)
        _write_ndjson(state_dir / lh.LEDGER_NAME, _receipt("d-001"))

        result = lh.check_chain_status(state_dir)

        assert result["chain_state"] == "unchained"
        assert result["status"] == lh.STATUS_OK

    def test_unchained_while_configured_on_is_a_finding(self, state_dir, monkeypatch):
        monkeypatch.setenv("VNX_CHAIN_RECEIPTS", "1")
        _write_ndjson(state_dir / lh.LEDGER_NAME, _receipt("d-001"))

        result = lh.check_chain_status(state_dir)

        assert result["chain_state"] == "unchained"
        assert result["status"] == lh.STATUS_FINDING

    def test_verified_chain_is_ok(self, state_dir, monkeypatch):
        monkeypatch.delenv("VNX_CHAIN_RECEIPTS", raising=False)
        ledger_path = state_dir / lh.LEDGER_NAME
        for i in range(3):
            append_chained_entry(ledger_path, {"seq": i, "dispatch_id": f"d-{i:03d}"})

        result = lh.check_chain_status(state_dir)

        assert result["chain_state"] == "verified"
        assert result["status"] == lh.STATUS_OK

    def test_broken_chain_is_a_finding(self, state_dir):
        ledger_path = state_dir / lh.LEDGER_NAME
        append_chained_entry(ledger_path, {"event": "e1", "id": "1"})
        append_chained_entry(ledger_path, {"event": "e2", "id": "2"})

        lines = ledger_path.read_text().splitlines()
        tampered = json.loads(lines[0])
        tampered["id"] = "TAMPERED"
        lines[0] = json.dumps(tampered)
        ledger_path.write_text("\n".join(lines) + "\n")

        result = lh.check_chain_status(state_dir)

        assert result["chain_state"] == "broken"
        assert result["status"] == lh.STATUS_FINDING

    def test_missing_ledger_is_unmeasurable(self, state_dir):
        result = lh.check_chain_status(state_dir)
        assert result["status"] == lh.SKIPPED_UNVERIFIED


# ---------------------------------------------------------------------------
# compute_health — overall rollup precedence
# ---------------------------------------------------------------------------


class TestComputeHealth:
    def test_all_ok_rolls_up_to_ok_exit_0(self, tmp_path, state_dir, monkeypatch):
        monkeypatch.delenv("VNX_CHAIN_RECEIPTS", raising=False)
        _write_ndjson(state_dir / lh.REGISTER_NAME, _register_entry("d-001"))
        _write_ndjson(state_dir / lh.LEDGER_NAME, _receipt("d-001"))
        ledger_size = (state_dir / lh.LEDGER_NAME).stat().st_size
        (state_dir / lh.CURSOR_NAME).write_text(json.dumps({"offset": ledger_size}), encoding="utf-8")

        result = lh.compute_health(tmp_path, state_dir)

        assert result["overall_status"] == lh.STATUS_OK
        assert result["exit_code"] == lh.EXIT_OK

    def test_any_finding_rolls_up_to_finding_exit_1(self, tmp_path, state_dir, monkeypatch):
        monkeypatch.delenv("VNX_CHAIN_RECEIPTS", raising=False)
        _write_ndjson(state_dir / lh.REGISTER_NAME, _register_entry("d-001"), _register_entry("d-orphan"))
        _write_ndjson(state_dir / lh.LEDGER_NAME, _receipt("d-001"))
        ledger_size = (state_dir / lh.LEDGER_NAME).stat().st_size
        (state_dir / lh.CURSOR_NAME).write_text(json.dumps({"offset": ledger_size}), encoding="utf-8")

        result = lh.compute_health(tmp_path, state_dir)

        assert result["overall_status"] == lh.STATUS_FINDING
        assert result["exit_code"] == lh.EXIT_FINDINGS

    def test_unmeasurable_outranks_finding_exit_2(self, tmp_path, state_dir):
        # No register, no ledger at all -> every sub-check is SKIPPED_UNVERIFIED,
        # which must win over any finding-shaped result.
        result = lh.compute_health(tmp_path, state_dir)

        assert result["overall_status"] == lh.SKIPPED_UNVERIFIED
        assert result["exit_code"] == lh.EXIT_UNMEASURABLE

    def test_single_subcheck_parse_error_rolls_up_to_unverifiable_exit_2(
        self, tmp_path, state_dir, monkeypatch
    ):
        """Regression (dispatch 20260812d-c): a parse error in ONE sub-check
        (receipt_coverage) must not get diluted by two otherwise-healthy
        sub-checks. compute_health's own SKIPPED_UNVERIFIED-outranks-FINDING
        rollup already existed pre-fix; what's new is that a corrupt line
        alone (no missing receipts, no stale cursor, no chain finding) now
        actually produces that SKIPPED_UNVERIFIED in the first place.
        """
        monkeypatch.delenv("VNX_CHAIN_RECEIPTS", raising=False)
        register_path = state_dir / lh.REGISTER_NAME
        register_path.write_text(
            json.dumps(_register_entry("d-001")) + "\n" + "{not json\n", encoding="utf-8"
        )
        _write_ndjson(state_dir / lh.LEDGER_NAME, _receipt("d-001"))
        ledger_size = (state_dir / lh.LEDGER_NAME).stat().st_size
        (state_dir / lh.CURSOR_NAME).write_text(json.dumps({"offset": ledger_size}), encoding="utf-8")

        result = lh.compute_health(tmp_path, state_dir)

        assert result["checks"]["receipt_coverage"]["status"] == lh.SKIPPED_UNVERIFIED
        assert result["checks"]["pull_cursor"]["status"] == lh.STATUS_OK
        assert result["overall_status"] == lh.SKIPPED_UNVERIFIED
        assert result["exit_code"] == lh.EXIT_UNMEASURABLE


# ---------------------------------------------------------------------------
# health surface write/read round-trip
# ---------------------------------------------------------------------------


class TestHealthSurface:
    def test_write_then_read_round_trips(self, tmp_path, state_dir):
        _write_ndjson(state_dir / lh.REGISTER_NAME, _register_entry("d-001"))
        _write_ndjson(state_dir / lh.LEDGER_NAME, _receipt("d-001"))
        ledger_size = (state_dir / lh.LEDGER_NAME).stat().st_size
        (state_dir / lh.CURSOR_NAME).write_text(json.dumps({"offset": ledger_size}), encoding="utf-8")

        result = lh.compute_health(tmp_path, state_dir)
        written_path = lh.write_health_surface(tmp_path, result)

        assert written_path == tmp_path / "health" / "ledger_health.json"
        surface = lh.read_health_surface(tmp_path)

        assert surface["component"] == "ledger_health"
        assert surface["status"] == "ok"
        assert surface["details"]["overall_status"] == lh.STATUS_OK
        assert "checks" in surface["details"]

    def test_write_uses_fail_status_when_findings_present(self, tmp_path, state_dir):
        _write_ndjson(state_dir / lh.REGISTER_NAME, _register_entry("d-orphan"))
        result = lh.compute_health(tmp_path, state_dir)  # missing ledger -> SKIPPED_UNVERIFIED
        lh.write_health_surface(tmp_path, result)

        surface = lh.read_health_surface(tmp_path)
        assert surface["status"] == "fail"

    def test_read_missing_beacon_returns_none(self, tmp_path):
        assert lh.read_health_surface(tmp_path) is None

    def test_read_corrupt_beacon_returns_none(self, tmp_path):
        health_dir = tmp_path / "health"
        health_dir.mkdir()
        (health_dir / "ledger_health.json").write_text("{not json", encoding="utf-8")
        assert lh.read_health_surface(tmp_path) is None


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


class TestCli:
    def test_no_write_flag_skips_beacon(self, tmp_path, state_dir):
        _write_ndjson(state_dir / lh.REGISTER_NAME, _register_entry("d-001"))
        _write_ndjson(state_dir / lh.LEDGER_NAME, _receipt("d-001"))
        ledger_size = (state_dir / lh.LEDGER_NAME).stat().st_size
        (state_dir / lh.CURSOR_NAME).write_text(json.dumps({"offset": ledger_size}), encoding="utf-8")

        exit_code = lh.main([
            "--data-dir", str(tmp_path), "--state-dir", str(state_dir), "--no-write", "--json",
        ])

        assert exit_code == lh.EXIT_OK
        assert not (tmp_path / "health").exists()

    def test_default_writes_beacon(self, tmp_path, state_dir):
        _write_ndjson(state_dir / lh.REGISTER_NAME, _register_entry("d-001"))
        _write_ndjson(state_dir / lh.LEDGER_NAME, _receipt("d-001"))
        ledger_size = (state_dir / lh.LEDGER_NAME).stat().st_size
        (state_dir / lh.CURSOR_NAME).write_text(json.dumps({"offset": ledger_size}), encoding="utf-8")

        exit_code = lh.main(["--data-dir", str(tmp_path), "--state-dir", str(state_dir)])

        assert exit_code == lh.EXIT_OK
        assert (tmp_path / "health" / "ledger_health.json").exists()

    def test_exit_code_reflects_findings(self, tmp_path, state_dir):
        _write_ndjson(state_dir / lh.REGISTER_NAME, _register_entry("d-orphan"))
        _write_ndjson(state_dir / lh.LEDGER_NAME, _receipt("d-001"))
        ledger_size = (state_dir / lh.LEDGER_NAME).stat().st_size
        (state_dir / lh.CURSOR_NAME).write_text(json.dumps({"offset": ledger_size}), encoding="utf-8")

        exit_code = lh.main([
            "--data-dir", str(tmp_path), "--state-dir", str(state_dir), "--no-write",
        ])

        assert exit_code == lh.EXIT_FINDINGS

    def test_exit_code_2_when_unmeasurable(self, tmp_path, state_dir):
        exit_code = lh.main([
            "--data-dir", str(tmp_path), "--state-dir", str(state_dir), "--no-write",
        ])
        assert exit_code == lh.EXIT_UNMEASURABLE
