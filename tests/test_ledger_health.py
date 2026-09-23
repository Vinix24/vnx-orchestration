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

import datetime
import json
import os
import subprocess
import sys
import threading
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


# Literal on purpose: the acknowledgement file name is part of the operator
# contract (T0 reads it by hand), so a test pins it as behaviour instead of
# importing the constant from the module under test.
ACK_NAME = "ledger_coverage_acknowledged.ndjson"


def _ack_record(dispatch_id: str, reason: str = "onderzocht: testfixture uit de echte register", **overrides) -> dict:
    rec = {
        "dispatch_id": dispatch_id,
        "reason": reason,
        "acknowledged_at": "2026-09-23T12:00:00.000000Z",
        "actor": "operator",
    }
    rec.update(overrides)
    return rec


def _write_ack(state_dir: Path, *records: dict) -> None:
    _write_ndjson(state_dir / ACK_NAME, *records)


def _read_ack_lines(state_dir: Path) -> list:
    path = state_dir / ACK_NAME
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


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
# migration_staleness (OI-1169)
# ---------------------------------------------------------------------------


def _migrations_fixture(tmp_path: Path, *, with_runner, without_runner=()) -> tuple:
    """Build isolated (migrations_dir, runners_dir) so this test never depends
    on the repo's real, ever-growing schemas/migrations/ contents.

    ``with_runner`` gets both a ``NNNN_x.sql`` file and a paired
    ``apply_NNNN.py`` runner (auto_apply can reach it). ``without_runner`` gets
    only the ``.sql`` file — the date-named-migration shape (e.g. a migration
    targeting quality_intelligence.db) that must NOT count toward "highest
    available" for runtime_coordination.db.
    """
    migrations_dir = tmp_path / "migrations"
    runners_dir = tmp_path / "runners"
    migrations_dir.mkdir()
    runners_dir.mkdir()
    for number in with_runner:
        (migrations_dir / f"{number:04d}_x.sql").write_text("-- noop\n", encoding="utf-8")
        (runners_dir / f"apply_{number:04d}.py").write_text(
            "def apply_migration(db_path, migration_sql_path):\n    return False\n",
            encoding="utf-8",
        )
    for number in without_runner:
        (migrations_dir / f"{number:04d}_x.sql").write_text("-- noop\n", encoding="utf-8")
    return migrations_dir, runners_dir


def _db_at_version(state_dir: Path, version: int) -> Path:
    db_path = state_dir / lh.RUNTIME_DB_NAME
    conn = __import__("sqlite3").connect(str(db_path))
    conn.execute(f"PRAGMA user_version = {int(version)}")
    conn.commit()
    conn.close()
    return db_path


class TestMigrationStaleness:
    def test_no_db_yet_is_ok_not_unmeasurable(self, state_dir):
        """A store with no runtime_coordination.db has nothing to be stale
        against — a legitimate nothing-to-check case, not a read failure."""
        result = lh.check_migration_staleness(state_dir)
        assert result["status"] == lh.STATUS_OK
        assert result["db_exists"] is False

    def test_store_at_31_with_0032_available_is_a_finding(self, tmp_path, state_dir):
        """The exact scenario OI-1169 exists for: a store at the numbered-walk
        terminal (31) with a runner-backed 0032 sitting unapplied."""
        migrations_dir, runners_dir = _migrations_fixture(tmp_path, with_runner=[22, 24, 31, 32])
        _db_at_version(state_dir, 31)

        result = lh.check_migration_staleness(
            state_dir, migrations_dir=migrations_dir, runners_dir=runners_dir
        )

        assert result["status"] == lh.STATUS_FINDING
        assert result["current_user_version"] == 31
        assert result["highest_available_migration"] == 32
        assert result["versions_behind"] == 1

    def test_store_at_highest_available_is_ok(self, tmp_path, state_dir):
        migrations_dir, runners_dir = _migrations_fixture(tmp_path, with_runner=[22, 24, 31, 32])
        _db_at_version(state_dir, 32)

        result = lh.check_migration_staleness(
            state_dir, migrations_dir=migrations_dir, runners_dir=runners_dir
        )

        assert result["status"] == lh.STATUS_OK
        assert result["versions_behind"] == 0

    def test_runnerless_migration_is_not_counted_as_available(self, tmp_path, state_dir):
        """A date-named migration (e.g. targeting quality_intelligence.db) with
        no apply_NNNN.py runner must never count as 'available' for
        runtime_coordination.db — otherwise every store would read as
        permanently behind a version it can never reach."""
        migrations_dir, runners_dir = _migrations_fixture(
            tmp_path, with_runner=[22, 24, 31, 32], without_runner=[2026]
        )
        _db_at_version(state_dir, 32)

        result = lh.check_migration_staleness(
            state_dir, migrations_dir=migrations_dir, runners_dir=runners_dir
        )

        assert result["status"] == lh.STATUS_OK
        assert result["highest_available_migration"] == 32

    def test_no_runner_backed_migration_at_all_is_unmeasurable(self, tmp_path, state_dir):
        migrations_dir, runners_dir = _migrations_fixture(tmp_path, with_runner=[], without_runner=[2026])
        _db_at_version(state_dir, 31)

        result = lh.check_migration_staleness(
            state_dir, migrations_dir=migrations_dir, runners_dir=runners_dir
        )

        assert result["status"] == lh.SKIPPED_UNVERIFIED

    def test_unreadable_db_is_unmeasurable(self, tmp_path, state_dir):
        migrations_dir, runners_dir = _migrations_fixture(tmp_path, with_runner=[22, 32])
        db_path = state_dir / lh.RUNTIME_DB_NAME
        db_path.write_text("not a sqlite file", encoding="utf-8")

        result = lh.check_migration_staleness(
            state_dir, migrations_dir=migrations_dir, runners_dir=runners_dir
        )

        assert result["status"] == lh.SKIPPED_UNVERIFIED

    def test_wired_into_compute_health(self, tmp_path, state_dir, monkeypatch):
        """compute_health includes migration_staleness in its checks dict and
        a finding there rolls up to overall STATUS_FINDING (vnx doctor reads
        this via the existing beacon-fail fallback — see _check_ledger_health)."""
        monkeypatch.delenv("VNX_CHAIN_RECEIPTS", raising=False)
        _write_ndjson(state_dir / lh.REGISTER_NAME, _register_entry("d-001"))
        _write_ndjson(state_dir / lh.LEDGER_NAME, _receipt("d-001"))
        ledger_size = (state_dir / lh.LEDGER_NAME).stat().st_size
        (state_dir / lh.CURSOR_NAME).write_text(json.dumps({"offset": ledger_size}), encoding="utf-8")
        _db_at_version(state_dir, 31)

        migrations_dir, runners_dir = _migrations_fixture(tmp_path, with_runner=[22, 31, 32])
        monkeypatch.setattr(lh, "_AUTO_APPLY_MIGRATIONS_DIR", migrations_dir)
        monkeypatch.setattr(lh, "_AUTO_APPLY_RUNNERS_DIR", runners_dir)

        result = lh.compute_health(tmp_path, state_dir)

        assert result["checks"]["migration_staleness"]["status"] == lh.STATUS_FINDING
        assert result["overall_status"] == lh.STATUS_FINDING
        assert result["exit_code"] == lh.EXIT_FINDINGS


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


# ---------------------------------------------------------------------------
# receipt_coverage — lane events are not dispatches (absence-is-loud, punt 1)
# ---------------------------------------------------------------------------


class TestReceiptCoverageLaneEvents:
    """``provider_lane_exhausted`` / ``provider_lane_reopened`` reuse the
    register's ``dispatch_id`` field for something that is not a dispatch that
    fired: a lane label (``lane-reopen:kimi``) or a dispatch the door REFUSED.
    Neither can land a receipt, so counting them makes the check permanently
    red on a gap that is not one. Exclusion is on the ``event`` field, never
    on the id prefix."""

    def test_lane_reopened_without_receipt_is_not_a_finding(self, state_dir):
        _write_ndjson(
            state_dir / lh.REGISTER_NAME,
            _register_entry("d-001", "dispatch_created"),
            _register_entry("lane-reopen:kimi", "provider_lane_reopened"),
        )
        _write_ndjson(state_dir / lh.LEDGER_NAME, _receipt("d-001"))

        result = lh.check_receipt_coverage(state_dir)

        assert result["status"] == lh.STATUS_OK
        assert result["missing_receipt_dispatch_ids"] == []
        assert result["register_dispatch_count"] == 1

    def test_lane_exhausted_without_receipt_is_not_a_finding(self, state_dir):
        """The door refused to fire this dispatch, so no receipt can exist."""
        _write_ndjson(
            state_dir / lh.REGISTER_NAME,
            _register_entry("d-refused", "provider_lane_exhausted"),
        )
        _write_ndjson(state_dir / lh.LEDGER_NAME, _receipt("d-other"))

        result = lh.check_receipt_coverage(state_dir)

        assert result["status"] == lh.STATUS_OK
        assert result["missing_receipt_count"] == 0

    def test_lane_event_and_real_dispatch_event_for_same_id_still_counts(self, state_dir):
        """A refused dispatch that the operator then fired through the door has
        a ``dispatch_created`` line: that line still counts."""
        _write_ndjson(
            state_dir / lh.REGISTER_NAME,
            _register_entry("d-refused-then-fired", "provider_lane_exhausted"),
            _register_entry("d-refused-then-fired", "dispatch_created"),
        )
        _write_ndjson(state_dir / lh.LEDGER_NAME, _receipt("d-other"))

        result = lh.check_receipt_coverage(state_dir)

        assert result["status"] == lh.STATUS_FINDING
        assert result["missing_receipt_dispatch_ids"] == ["d-refused-then-fired"]

    def test_exclusion_is_on_the_event_field_not_the_id_prefix(self, state_dir):
        """A real dispatch whose id merely looks like a lane label still counts."""
        _write_ndjson(
            state_dir / lh.REGISTER_NAME,
            _register_entry("lane-reopen:lookalike", "dispatch_created"),
        )
        _write_ndjson(state_dir / lh.LEDGER_NAME, _receipt("d-other"))

        result = lh.check_receipt_coverage(state_dir)

        assert result["status"] == lh.STATUS_FINDING
        assert result["missing_receipt_dispatch_ids"] == ["lane-reopen:lookalike"]

    def test_unknown_event_type_counts_fail_safe(self, state_dir):
        """A future event type nobody classified must stay visible."""
        _write_ndjson(
            state_dir / lh.REGISTER_NAME,
            _register_entry("d-future", "some_event_from_the_future"),
        )
        _write_ndjson(state_dir / lh.LEDGER_NAME, _receipt("d-other"))

        result = lh.check_receipt_coverage(state_dir)

        assert result["status"] == lh.STATUS_FINDING
        assert result["missing_receipt_dispatch_ids"] == ["d-future"]

    def test_register_line_without_event_field_counts_fail_safe(self, state_dir):
        _write_ndjson(state_dir / lh.REGISTER_NAME, {"dispatch_id": "d-no-event"})
        _write_ndjson(state_dir / lh.LEDGER_NAME, _receipt("d-other"))

        result = lh.check_receipt_coverage(state_dir)

        assert result["missing_receipt_dispatch_ids"] == ["d-no-event"]

    def test_receipts_side_is_not_filtered_by_event(self, state_dir):
        """Only the register is filtered: a receipt is a receipt whatever its shape."""
        _write_ndjson(state_dir / lh.REGISTER_NAME, _register_entry("d-001", "dispatch_created"))
        _write_ndjson(
            state_dir / lh.LEDGER_NAME,
            _receipt("d-001", event="provider_lane_reopened"),
        )

        result = lh.check_receipt_coverage(state_dir)

        assert result["status"] == lh.STATUS_OK


# ---------------------------------------------------------------------------
# receipt_coverage — acknowledged gaps (absence-is-loud, punt 1)
# ---------------------------------------------------------------------------


class TestReceiptCoverageAcknowledged:
    def test_acknowledged_gap_is_no_longer_a_finding(self, state_dir):
        _write_ndjson(state_dir / lh.REGISTER_NAME, _register_entry("d-001"), _register_entry("d-fixture"))
        _write_ndjson(state_dir / lh.LEDGER_NAME, _receipt("d-001"))
        _write_ack(state_dir, _ack_record("d-fixture", reason="testfixture van voor de pytest-guard"))

        result = lh.check_receipt_coverage(state_dir)

        assert result["status"] == lh.STATUS_OK
        assert result["missing_receipt_count"] == 0
        assert result["missing_receipt_dispatch_ids"] == []
        assert result["acknowledged_count"] == 1
        assert result["acknowledged_gaps"] == [
            {
                "dispatch_id": "d-fixture",
                "reason": "testfixture van voor de pytest-guard",
                "acknowledged_at": "2026-09-23T12:00:00.000000Z",
                "actor": "operator",
            }
        ]
        assert result["acknowledged_not_missing_count"] == 0

    def test_one_of_two_gaps_acknowledged_is_still_a_finding(self, state_dir):
        _write_ndjson(
            state_dir / lh.REGISTER_NAME,
            _register_entry("d-known"),
            _register_entry("d-new-gap"),
        )
        _write_ndjson(state_dir / lh.LEDGER_NAME, _receipt("d-other"))
        _write_ack(state_dir, _ack_record("d-known"))

        result = lh.check_receipt_coverage(state_dir)

        assert result["status"] == lh.STATUS_FINDING
        assert result["missing_receipt_dispatch_ids"] == ["d-new-gap"]
        assert result["missing_receipt_count"] == 1
        assert result["acknowledged_count"] == 1
        assert [g["dispatch_id"] for g in result["acknowledged_gaps"]] == ["d-known"]

    def test_acknowledgement_that_covers_nothing_is_counted_apart(self, state_dir):
        """An id that is receipted (or was never in the register) is not an
        error. It is reported as an acknowledgement that no longer covers
        anything, so a stale erkenning stays visible."""
        _write_ndjson(state_dir / lh.REGISTER_NAME, _register_entry("d-001"))
        _write_ndjson(state_dir / lh.LEDGER_NAME, _receipt("d-001"))
        _write_ack(
            state_dir,
            _ack_record("d-001", reason="kreeg later alsnog een receipt"),
            _ack_record("d-never-in-register"),
        )

        result = lh.check_receipt_coverage(state_dir)

        assert result["status"] == lh.STATUS_OK
        assert result["acknowledged_count"] == 0
        assert result["acknowledged_gaps"] == []
        assert result["acknowledged_not_missing_count"] == 2
        assert result["acknowledged_not_missing_dispatch_ids"] == ["d-001", "d-never-in-register"]

    def test_acknowledgement_for_a_lane_event_only_id_covers_nothing(self, state_dir):
        _write_ndjson(state_dir / lh.REGISTER_NAME, _register_entry("lane-reopen:kimi", "provider_lane_reopened"))
        _write_ndjson(state_dir / lh.LEDGER_NAME, _receipt("d-other"))
        _write_ack(state_dir, _ack_record("lane-reopen:kimi"))

        result = lh.check_receipt_coverage(state_dir)

        assert result["status"] == lh.STATUS_OK
        assert result["acknowledged_count"] == 0
        assert result["acknowledged_not_missing_count"] == 1

    def test_absent_acknowledgement_file_reports_zero_counts(self, state_dir):
        _write_ndjson(state_dir / lh.REGISTER_NAME, _register_entry("d-orphan"))
        _write_ndjson(state_dir / lh.LEDGER_NAME, _receipt("d-other"))

        result = lh.check_receipt_coverage(state_dir)

        assert result["status"] == lh.STATUS_FINDING
        assert result["acknowledged_count"] == 0
        assert result["acknowledged_gaps"] == []
        assert result["acknowledged_not_missing_count"] == 0
        assert result["acknowledged_parse_errors"] == 0

    def test_later_acknowledgement_for_the_same_id_replaces_the_reason(self, state_dir):
        _write_ndjson(state_dir / lh.REGISTER_NAME, _register_entry("d-gap"))
        _write_ndjson(state_dir / lh.LEDGER_NAME, _receipt("d-other"))
        _write_ack(
            state_dir,
            _ack_record("d-gap", reason="eerste oordeel"),
            _ack_record("d-gap", reason="herzien oordeel", acknowledged_at="2026-09-24T09:00:00.000000Z"),
        )

        result = lh.check_receipt_coverage(state_dir)

        assert result["acknowledged_count"] == 1
        assert result["acknowledged_gaps"][0]["reason"] == "herzien oordeel"

    def test_corrupt_acknowledgement_line_is_unverified(self, state_dir):
        """Same discipline as the register and ledger branches: a line we
        cannot read could have covered any id, so coverage is not certified."""
        _write_ndjson(state_dir / lh.REGISTER_NAME, _register_entry("d-001"))
        _write_ndjson(state_dir / lh.LEDGER_NAME, _receipt("d-001"))
        (state_dir / ACK_NAME).write_text(
            json.dumps(_ack_record("d-x")) + "\n" + "{not json\n", encoding="utf-8"
        )

        result = lh.check_receipt_coverage(state_dir)

        assert result["missing_receipt_count"] == 0
        assert result["acknowledged_parse_errors"] == 1
        assert result["status"] == lh.SKIPPED_UNVERIFIED
        assert "acknowledg" in result["reason"]

    @pytest.mark.parametrize(
        "bad_record",
        [
            {"dispatch_id": "d-gap", "reason": "   ", "acknowledged_at": "2026-09-23T12:00:00Z", "actor": "operator"},
            {"dispatch_id": "d-gap", "acknowledged_at": "2026-09-23T12:00:00Z", "actor": "operator"},
            {"dispatch_id": "  ", "reason": "x", "acknowledged_at": "2026-09-23T12:00:00Z", "actor": "operator"},
            {"reason": "x", "acknowledged_at": "2026-09-23T12:00:00Z", "actor": "operator"},
        ],
    )
    def test_hand_edited_acknowledgement_without_reason_or_id_never_silences_a_gap(
        self, state_dir, bad_record
    ):
        _write_ndjson(state_dir / lh.REGISTER_NAME, _register_entry("d-gap"))
        _write_ndjson(state_dir / lh.LEDGER_NAME, _receipt("d-other"))
        _write_ack(state_dir, bad_record)

        result = lh.check_receipt_coverage(state_dir)

        assert result["status"] == lh.SKIPPED_UNVERIFIED
        assert result["acknowledged_parse_errors"] == 1
        assert result["acknowledged_count"] == 0
        assert result["missing_receipt_dispatch_ids"] == ["d-gap"]

    def test_non_object_acknowledgement_line_is_unverified(self, state_dir):
        _write_ndjson(state_dir / lh.REGISTER_NAME, _register_entry("d-001"))
        _write_ndjson(state_dir / lh.LEDGER_NAME, _receipt("d-001"))
        (state_dir / ACK_NAME).write_text('["d-001"]\n', encoding="utf-8")

        result = lh.check_receipt_coverage(state_dir)

        assert result["status"] == lh.SKIPPED_UNVERIFIED
        assert result["acknowledged_parse_errors"] == 1

    def test_unreadable_acknowledgement_file_is_unverified(self, state_dir):
        """A directory sitting where the file should be raises OSError on open."""
        _write_ndjson(state_dir / lh.REGISTER_NAME, _register_entry("d-001"))
        _write_ndjson(state_dir / lh.LEDGER_NAME, _receipt("d-001"))
        (state_dir / ACK_NAME).mkdir()

        result = lh.check_receipt_coverage(state_dir)

        assert result["status"] == lh.SKIPPED_UNVERIFIED
        assert "acknowledg" in result["reason"]

    def test_register_and_acknowledgement_errors_are_both_named_in_the_reason(self, state_dir):
        (state_dir / lh.REGISTER_NAME).write_text(
            json.dumps(_register_entry("d-001")) + "\n" + "{not json\n", encoding="utf-8"
        )
        _write_ndjson(state_dir / lh.LEDGER_NAME, _receipt("d-001"))
        (state_dir / ACK_NAME).write_text("{not json\n", encoding="utf-8")

        result = lh.check_receipt_coverage(state_dir)

        assert result["status"] == lh.SKIPPED_UNVERIFIED
        assert "1 register" in result["reason"]
        assert "acknowledg" in result["reason"]

    def test_acknowledged_gap_rolls_up_to_healthy_exit_0(self, tmp_path, state_dir, monkeypatch):
        monkeypatch.delenv("VNX_CHAIN_RECEIPTS", raising=False)
        _write_ndjson(state_dir / lh.REGISTER_NAME, _register_entry("d-001"), _register_entry("d-fixture"))
        _write_ndjson(state_dir / lh.LEDGER_NAME, _receipt("d-001"))
        ledger_size = (state_dir / lh.LEDGER_NAME).stat().st_size
        (state_dir / lh.CURSOR_NAME).write_text(json.dumps({"offset": ledger_size}), encoding="utf-8")
        _write_ack(state_dir, _ack_record("d-fixture"))

        result = lh.compute_health(tmp_path, state_dir)

        assert result["checks"]["receipt_coverage"]["status"] == lh.STATUS_OK
        assert result["overall_status"] == lh.STATUS_OK
        assert result["exit_code"] == lh.EXIT_OK

    def test_acknowledgement_stays_visible_in_the_beacon(self, tmp_path, state_dir, monkeypatch):
        monkeypatch.delenv("VNX_CHAIN_RECEIPTS", raising=False)
        _write_ndjson(state_dir / lh.REGISTER_NAME, _register_entry("d-fixture"))
        _write_ndjson(state_dir / lh.LEDGER_NAME, _receipt("d-other"))
        ledger_size = (state_dir / lh.LEDGER_NAME).stat().st_size
        (state_dir / lh.CURSOR_NAME).write_text(json.dumps({"offset": ledger_size}), encoding="utf-8")
        _write_ack(state_dir, _ack_record("d-fixture", reason="zichtbaar in de beacon"))

        lh.write_health_surface(tmp_path, lh.compute_health(tmp_path, state_dir))
        coverage = lh.read_health_surface(tmp_path)["details"]["checks"]["receipt_coverage"]

        assert coverage["acknowledged_count"] == 1
        assert coverage["acknowledged_gaps"][0]["reason"] == "zichtbaar in de beacon"

    def test_human_output_lists_acknowledged_gaps_with_their_reason(self, tmp_path, state_dir):
        _write_ndjson(
            state_dir / lh.REGISTER_NAME,
            _register_entry("d-known"),
            _register_entry("d-new-gap"),
        )
        _write_ndjson(state_dir / lh.LEDGER_NAME, _receipt("d-other"))
        _write_ack(state_dir, _ack_record("d-known", reason="verklaard door T0"))

        text = lh._format_human(lh.compute_health(tmp_path, state_dir))

        assert "d-new-gap" in text
        assert "d-known" in text
        assert "verklaard door T0" in text


# ---------------------------------------------------------------------------
# acknowledge subcommand
# ---------------------------------------------------------------------------


class TestAcknowledgeCli:
    def test_writes_one_line_with_the_documented_fields(self, state_dir, monkeypatch):
        monkeypatch.delenv("VNX_ACTOR", raising=False)

        exit_code = lh.main([
            "acknowledge", "--state-dir", str(state_dir),
            "--dispatch-id", "d-fixture", "--reason", "testfixture uit de echte register",
        ])

        assert exit_code == 0
        lines = _read_ack_lines(state_dir)
        assert len(lines) == 1
        record = lines[0]
        assert set(record) == {"dispatch_id", "reason", "acknowledged_at", "actor"}
        assert record["dispatch_id"] == "d-fixture"
        assert record["reason"] == "testfixture uit de echte register"
        assert record["actor"] == "operator"
        parsed = datetime.datetime.fromisoformat(record["acknowledged_at"].replace("Z", "+00:00"))
        assert parsed.tzinfo is not None
        assert abs((datetime.datetime.now(datetime.timezone.utc) - parsed).total_seconds()) < 60

    def test_actor_comes_from_env_and_the_flag_beats_the_env(self, state_dir, monkeypatch):
        monkeypatch.setenv("VNX_ACTOR", "t0")
        lh.main(["acknowledge", "--state-dir", str(state_dir), "--dispatch-id", "d-a", "--reason", "r"])
        lh.main([
            "acknowledge", "--state-dir", str(state_dir),
            "--dispatch-id", "d-b", "--reason", "r", "--actor", "vincent",
        ])

        actors = {rec["dispatch_id"]: rec["actor"] for rec in _read_ack_lines(state_dir)}
        assert actors == {"d-a": "t0", "d-b": "vincent"}

    @pytest.mark.parametrize("blank_reason", ["", "   ", "\t\n "])
    def test_empty_or_whitespace_reason_is_refused_and_writes_nothing(self, state_dir, blank_reason, capsys):
        # A valid acknowledgement first: the refusal must leave exactly that
        # one line, so this also proves the subcommand works at all.
        assert lh.main([
            "acknowledge", "--state-dir", str(state_dir), "--dispatch-id", "d-ok", "--reason", "echte reden",
        ]) == 0
        before = (state_dir / ACK_NAME).read_bytes()

        exit_code = lh.main([
            "acknowledge", "--state-dir", str(state_dir), "--dispatch-id", "d-blank", "--reason", blank_reason,
        ])

        assert exit_code != 0
        assert "reason" in capsys.readouterr().err
        assert (state_dir / ACK_NAME).read_bytes() == before

    def test_refused_reason_does_not_create_the_file(self, state_dir):
        assert lh.main([
            "acknowledge", "--state-dir", str(state_dir), "--dispatch-id", "d-ok", "--reason", "echte reden",
        ]) == 0
        (state_dir / ACK_NAME).unlink()

        exit_code = lh.main([
            "acknowledge", "--state-dir", str(state_dir), "--dispatch-id", "d-blank", "--reason", "  ",
        ])

        assert exit_code != 0
        assert not (state_dir / ACK_NAME).exists()

    def test_missing_reason_flag_is_refused(self, state_dir):
        assert lh.main([
            "acknowledge", "--state-dir", str(state_dir), "--dispatch-id", "d-ok", "--reason", "echte reden",
        ]) == 0
        before = (state_dir / ACK_NAME).read_bytes()

        with pytest.raises(SystemExit) as exc:
            lh.main(["acknowledge", "--state-dir", str(state_dir), "--dispatch-id", "d-no-reason"])

        assert exc.value.code != 0
        assert (state_dir / ACK_NAME).read_bytes() == before

    @pytest.mark.parametrize("blank_id", ["", "   "])
    def test_blank_dispatch_id_is_refused(self, state_dir, blank_id, capsys):
        assert lh.main([
            "acknowledge", "--state-dir", str(state_dir), "--dispatch-id", "d-ok", "--reason", "echte reden",
        ]) == 0
        before = (state_dir / ACK_NAME).read_bytes()

        exit_code = lh.main([
            "acknowledge", "--state-dir", str(state_dir), "--dispatch-id", blank_id, "--reason", "x",
        ])

        assert exit_code != 0
        assert "dispatch-id" in capsys.readouterr().err
        assert (state_dir / ACK_NAME).read_bytes() == before

    def test_nonexistent_state_dir_is_refused_and_not_created(self, tmp_path, capsys):
        """A mistyped --state-dir must not silently grow a directory nobody reads."""
        typo = tmp_path / "stat"
        assert lh.main([
            "acknowledge", "--state-dir", str(tmp_path), "--dispatch-id", "d-ok", "--reason", "echte reden",
        ]) == 0

        exit_code = lh.main([
            "acknowledge", "--state-dir", str(typo), "--dispatch-id", "d-gap", "--reason", "r",
        ])

        assert exit_code != 0
        assert "state dir not found" in capsys.readouterr().err
        assert not typo.exists()

    def test_reason_and_id_are_stored_stripped(self, state_dir):
        lh.main([
            "acknowledge", "--state-dir", str(state_dir),
            "--dispatch-id", "  d-padded  ", "--reason", "  met spaties eromheen \n",
        ])

        record = _read_ack_lines(state_dir)[0]
        assert record["dispatch_id"] == "d-padded"
        assert record["reason"] == "met spaties eromheen"

    def test_is_append_only(self, state_dir):
        lh.main(["acknowledge", "--state-dir", str(state_dir), "--dispatch-id", "d-1", "--reason", "een"])
        first = (state_dir / ACK_NAME).read_bytes()
        lh.main(["acknowledge", "--state-dir", str(state_dir), "--dispatch-id", "d-2", "--reason", "twee"])

        assert (state_dir / ACK_NAME).read_bytes().startswith(first)
        assert [r["dispatch_id"] for r in _read_ack_lines(state_dir)] == ["d-1", "d-2"]

    def test_never_touches_register_or_ledger(self, state_dir):
        _write_ndjson(state_dir / lh.REGISTER_NAME, _register_entry("d-gap"))
        _write_ndjson(state_dir / lh.LEDGER_NAME, _receipt("d-other"))
        register_before = (state_dir / lh.REGISTER_NAME).read_bytes()
        ledger_before = (state_dir / lh.LEDGER_NAME).read_bytes()

        lh.main(["acknowledge", "--state-dir", str(state_dir), "--dispatch-id", "d-gap", "--reason", "r"])

        assert (state_dir / lh.REGISTER_NAME).read_bytes() == register_before
        assert (state_dir / lh.LEDGER_NAME).read_bytes() == ledger_before

    def test_state_dir_flag_is_accepted_before_the_subcommand_too(self, state_dir):
        exit_code = lh.main([
            "--state-dir", str(state_dir), "acknowledge", "--dispatch-id", "d-1", "--reason", "r",
        ])

        assert exit_code == 0
        assert [r["dispatch_id"] for r in _read_ack_lines(state_dir)] == ["d-1"]

    def test_acknowledge_then_check_round_trip(self, tmp_path, state_dir, monkeypatch):
        """The CLI writes what the check reads: run both for real."""
        monkeypatch.delenv("VNX_CHAIN_RECEIPTS", raising=False)
        _write_ndjson(state_dir / lh.REGISTER_NAME, _register_entry("d-001"), _register_entry("d-gap"))
        _write_ndjson(state_dir / lh.LEDGER_NAME, _receipt("d-001"))
        ledger_size = (state_dir / lh.LEDGER_NAME).stat().st_size
        (state_dir / lh.CURSOR_NAME).write_text(json.dumps({"offset": ledger_size}), encoding="utf-8")

        before = lh.compute_health(tmp_path, state_dir)
        assert before["checks"]["receipt_coverage"]["status"] == lh.STATUS_FINDING
        assert before["exit_code"] == lh.EXIT_FINDINGS

        assert lh.main([
            "acknowledge", "--state-dir", str(state_dir), "--dispatch-id", "d-gap", "--reason", "verklaard",
        ]) == 0

        after = lh.compute_health(tmp_path, state_dir)
        coverage = after["checks"]["receipt_coverage"]
        assert coverage["status"] == lh.STATUS_OK
        assert coverage["acknowledged_count"] == 1
        assert coverage["acknowledged_gaps"][0]["reason"] == "verklaard"
        assert after["exit_code"] == lh.EXIT_OK

    def test_concurrent_acknowledgements_do_not_interleave(self, state_dir):
        ids = [f"d-{i:02d}" for i in range(16)]

        def _ack(dispatch_id: str) -> None:
            lh.main([
                "acknowledge", "--state-dir", str(state_dir),
                "--dispatch-id", dispatch_id, "--reason", f"reden voor {dispatch_id}",
            ])

        threads = [threading.Thread(target=_ack, args=(i,)) for i in ids]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        lines = _read_ack_lines(state_dir)
        assert sorted(r["dispatch_id"] for r in lines) == ids
        assert all(r["reason"] == f"reden voor {r['dispatch_id']}" for r in lines)

    def test_real_process_refuses_blank_reason_with_nonzero_exit(self, state_dir):
        script = str(SCRIPTS_DIR / "ledger_health.py")
        ok = subprocess.run(
            [sys.executable, script, "acknowledge", "--state-dir", str(state_dir),
             "--dispatch-id", "d-ok", "--reason", "echte reden"],
            capture_output=True, text=True, timeout=60,
        )
        assert ok.returncode == 0, ok.stderr
        before = (state_dir / ACK_NAME).read_bytes()

        blank = subprocess.run(
            [sys.executable, script, "acknowledge", "--state-dir", str(state_dir),
             "--dispatch-id", "d-blank", "--reason", "   "],
            capture_output=True, text=True, timeout=60,
        )

        assert blank.returncode != 0
        assert (state_dir / ACK_NAME).read_bytes() == before

    def test_real_process_without_subcommand_still_reports_health(self, tmp_path, state_dir):
        """The launchd plist runs the bare script: no subcommand must keep
        meaning "run the health check", with exit 1 on findings."""
        _write_ndjson(state_dir / lh.REGISTER_NAME, _register_entry("d-orphan"))
        _write_ndjson(state_dir / lh.LEDGER_NAME, _receipt("d-001"))
        ledger_size = (state_dir / lh.LEDGER_NAME).stat().st_size
        (state_dir / lh.CURSOR_NAME).write_text(json.dumps({"offset": ledger_size}), encoding="utf-8")

        proc = subprocess.run(
            [sys.executable, str(SCRIPTS_DIR / "ledger_health.py"),
             "--data-dir", str(tmp_path), "--state-dir", str(state_dir), "--no-write", "--json"],
            capture_output=True, text=True, timeout=60,
        )

        assert proc.returncode == lh.EXIT_FINDINGS
        assert json.loads(proc.stdout)["checks"]["receipt_coverage"]["missing_receipt_dispatch_ids"] == ["d-orphan"]
