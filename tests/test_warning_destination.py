#!/usr/bin/env python3
"""Tests for scripts/lib/append_receipt_internals/warning_destination.py
(ADR-035 §6.1/§6.2/§6.4). Covers the PR-2 mandatory subset: T7, T8, T9,
plus unit coverage for the pure helpers (`compute_requires_tracking`,
`dedup_key_for`, `derive_open_items_created`) and the `oi_pending`
fallback (§6.4).

Library-level unit tests only — PR-2 wires no writer. Real
`open_items_manager` interaction is exercised via an isolated, per-test
STATE_DIR (mirrors tests/test_open_items_dedup_status.py's pattern), not
the process-wide facade cache.
"""

from __future__ import annotations

import copy
import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).resolve().parent
VNX_ROOT = TESTS_DIR.parent
SCRIPTS_DIR = VNX_ROOT / "scripts"
SCRIPTS_LIB = SCRIPTS_DIR / "lib"
sys.path.insert(0, str(SCRIPTS_LIB))

from append_receipt_internals.warning_destination import (  # noqa: E402
    DEFAULT_RECURRENCE_THRESHOLD,
    DROP_REASON_ALLOWLIST,
    LEGAL_DESTINATIONS,
    LEGAL_SEVERITIES,
    WarningDestinationError,
    assign_destination,
    compute_requires_tracking,
    dedup_key_for,
    derive_open_items_created,
)


def _load_oim(tmp_path: Path):
    """Fresh, isolated open_items_manager module bound to a per-test
    STATE_DIR — mirrors tests/test_open_items_dedup_status.py's helper so
    T7 exercises the REAL add_item_programmatic/dedup path, not a mock."""
    env_patch = {
        "VNX_DATA_DIR": str(tmp_path / "data"),
        "VNX_DATA_DIR_EXPLICIT": "1",
        "VNX_STATE_DIR": str(tmp_path / "data" / "state"),
        "VNX_HOME": str(VNX_ROOT),
    }
    (tmp_path / "data" / "state").mkdir(parents=True, exist_ok=True)

    mod_name = f"open_items_manager_wd_test_{tmp_path.name}"
    from unittest.mock import patch

    with patch.dict(os.environ, env_patch):
        spec = importlib.util.spec_from_file_location(
            mod_name, SCRIPTS_DIR / "open_items_manager.py"
        )
        mod = importlib.util.module_from_spec(spec)
        sys.modules[mod_name] = mod
        try:
            spec.loader.exec_module(mod)
        except Exception:
            del sys.modules[mod_name]
            raise
    return mod


class _NeverCalledOIM:
    """Fails loudly if the engine ever calls add_item_programmatic on it —
    used to prove a non-tracked (counted/dropped) entry never touches the
    OI store (T8's 'no OI spam' guarantee)."""

    def add_item_programmatic(self, **kwargs):
        raise AssertionError(
            "add_item_programmatic must not be called for a non-tracked warning"
        )


class _FailingOIM:
    def __init__(self, error_message: str):
        self._error_message = error_message

    def add_item_programmatic(self, **kwargs):
        raise RuntimeError(self._error_message)


# ── T7 — warn recurring >= threshold promotes to OI, dedup continuity ─────


def test_t7_warn_at_threshold_promotes_to_oi_with_real_open_item(tmp_path):
    oim = _load_oim(tmp_path)
    entry = {"code": "worker_permission_violation", "severity": "warn", "message": "recurring"}

    result = assign_destination(
        entry,
        recurrence_count=DEFAULT_RECURRENCE_THRESHOLD,
        open_items_manager_module=oim,
        dispatch_id="DISP-1",
    )

    assert result["destination"] == "oi"
    assert result["oi_id"] is not None
    assert result["reason"] is None
    assert result["requires_tracking"] is True

    data = oim.load_items()
    assert len(data["items"]) == 1
    assert data["items"][0]["dedup_key"] == "worker_permission_violation"


def test_t7_repeat_occurrence_hits_same_dedup_key_and_does_not_duplicate(tmp_path):
    oim = _load_oim(tmp_path)
    entry = {"code": "worker_permission_violation", "severity": "warn", "message": "recurring"}

    first = assign_destination(
        entry, recurrence_count=3, open_items_manager_module=oim, dispatch_id="DISP-1"
    )
    second = assign_destination(
        entry, recurrence_count=4, open_items_manager_module=oim, dispatch_id="DISP-2"
    )

    assert first["destination"] == second["destination"] == "oi"
    assert first["oi_id"] == second["oi_id"]
    assert len(oim.load_items()["items"]) == 1


def test_t7_blocker_always_promotes_regardless_of_recurrence(tmp_path):
    oim = _load_oim(tmp_path)
    entry = {"code": "worker_permission_violation", "severity": "blocker", "message": "m"}

    result = assign_destination(
        entry, recurrence_count=0, open_items_manager_module=oim, dispatch_id="DISP-1"
    )

    assert result["destination"] == "oi"
    assert result["oi_id"] is not None
    assert result["requires_tracking"] is True


def test_t7_default_path_persisted_counter_reaches_threshold_and_promotes(tmp_path):
    """Exercises the real rolling-window default path (no recurrence_count
    injection) end to end: three real calls against the same counter_path
    accumulate to the threshold and the third promotes."""
    oim = _load_oim(tmp_path / "oi_store")
    counter_path = tmp_path / "counts.json"
    entry = {"code": "recurring_check", "severity": "warn", "message": "m"}

    r1 = assign_destination(entry, counter_path=counter_path, open_items_manager_module=oim, dispatch_id="D1")
    r2 = assign_destination(entry, counter_path=counter_path, open_items_manager_module=oim, dispatch_id="D1")
    r3 = assign_destination(entry, counter_path=counter_path, open_items_manager_module=oim, dispatch_id="D1")

    assert r1["destination"] == "counted"
    assert r2["destination"] == "counted"
    assert r3["destination"] == "oi"
    assert r3["oi_id"] is not None


# ── T8 — warn below threshold: destination=counted, counter accumulates ───


def test_t8_warn_below_threshold_is_counted_and_never_touches_oi_store():
    entry = {"code": "low_severity_thing", "severity": "warn", "message": "m"}

    result = assign_destination(
        entry,
        recurrence_count=1,
        open_items_manager_module=_NeverCalledOIM(),
    )

    assert result["destination"] == "counted"
    assert result["oi_id"] is None
    assert result["reason"] is None
    assert result["requires_tracking"] is False


def test_t8_repeated_occurrences_increment_persisted_counter_without_oi_spam(tmp_path):
    counter_path = tmp_path / "counts.json"
    entry = {"code": "low_severity_thing", "severity": "warn", "message": "m"}

    for _ in range(2):
        result = assign_destination(
            entry,
            counter_path=counter_path,
            open_items_manager_module=_NeverCalledOIM(),
        )
        assert result["destination"] == "counted"

    data = json.loads(counter_path.read_text(encoding="utf-8"))
    assert data["low_severity_thing"] == 2


def test_t8_info_severity_never_promotes_regardless_of_recurrence():
    entry = {"code": "informational_thing", "severity": "info", "message": "m"}

    result = assign_destination(
        entry,
        recurrence_count=1000,
        open_items_manager_module=_NeverCalledOIM(),
    )

    assert result["destination"] == "counted"
    assert result["requires_tracking"] is False


# ── T9 — report_contract_invalid warning code: counted from first occurrence ─


def test_t9_report_contract_invalid_counted_from_first_occurrence():
    """Proves the 2274x-noise class is bounded+visible (destination=counted)
    from the very first occurrence — never an unwindowed dead flag."""
    entry = {"code": "report_contract_invalid", "severity": "warn", "message": "Summary missing"}

    result = assign_destination(
        entry,
        recurrence_count=1,
        open_items_manager_module=_NeverCalledOIM(),
    )

    assert result["destination"] == "counted"
    assert result["requires_tracking"] is False


# ── §6.4 — oi_pending fallback when the OI store itself fails ─────────────


def test_oi_pending_on_add_item_programmatic_failure_for_blocker():
    entry = {"code": "worker_permission_violation", "severity": "blocker", "message": "m"}
    failing = _FailingOIM("[Errno 11] store lock held by pid 4821")

    result = assign_destination(entry, open_items_manager_module=failing, dispatch_id="D1")

    assert result["destination"] == "oi_pending"
    assert result["oi_id"] is None
    assert result["reason"] == "[Errno 11] store lock held by pid 4821"
    assert result["requires_tracking"] is True


def test_oi_pending_on_add_item_programmatic_failure_for_promoted_warn():
    entry = {"code": "recurring_thing", "severity": "warn", "message": "m"}
    failing = _FailingOIM("store unreachable")

    result = assign_destination(
        entry, recurrence_count=DEFAULT_RECURRENCE_THRESHOLD, open_items_manager_module=failing
    )

    assert result["destination"] == "oi_pending"
    assert result["oi_id"] is None
    assert result["reason"] == "store unreachable"


# ── compute_requires_tracking — pure rule (§6.1 rule 1) ───────────────────


def test_compute_requires_tracking_blocker_always_true():
    assert compute_requires_tracking("blocker", 0) is True
    assert compute_requires_tracking("blocker", 999) is True


def test_compute_requires_tracking_warn_below_threshold_false():
    assert compute_requires_tracking("warn", 2, threshold=3) is False


def test_compute_requires_tracking_warn_at_or_above_threshold_true():
    assert compute_requires_tracking("warn", 3, threshold=3) is True
    assert compute_requires_tracking("warn", 4, threshold=3) is True


def test_compute_requires_tracking_info_never_true():
    assert compute_requires_tracking("info", 1000) is False


# ── dedup_key_for — §6.3 continuity ────────────────────────────────────────


def test_dedup_key_for_is_verbatim_code():
    entry = {"code": "qa:missing_docstring:foo.py:bar_func", "severity": "warn"}
    assert dedup_key_for(entry) == "qa:missing_docstring:foo.py:bar_func"


def test_dedup_key_for_missing_code_is_empty_string():
    assert dedup_key_for({}) == ""


# ── derive_open_items_created — §6.2 ───────────────────────────────────────


def test_derive_open_items_created_counts_oi_destination_only():
    warnings = [
        {"destination": "oi"},
        {"destination": "oi_pending"},
        {"destination": "counted"},
        {"destination": "dropped"},
        {"destination": "oi"},
    ]
    assert derive_open_items_created(warnings) == 2


def test_derive_open_items_created_oi_pending_not_counted():
    assert derive_open_items_created([{"destination": "oi_pending"}]) == 0


@pytest.mark.parametrize("value", [None, []])
def test_derive_open_items_created_empty_or_none_is_zero(value):
    assert derive_open_items_created(value) == 0


# ── drop_reason — caller-requested drop, allow-listed only ─────────────────


def test_assign_destination_drop_reason_allowlisted_accepted():
    entry = {"code": "retired_thing", "severity": "info", "message": "m"}
    result = assign_destination(entry, recurrence_count=0, drop_reason="retired_check")
    assert result["destination"] == "dropped"
    assert result["reason"] == "retired_check"
    assert result["oi_id"] is None


@pytest.mark.parametrize("reason", sorted(DROP_REASON_ALLOWLIST))
def test_assign_destination_every_allowlisted_reason_accepted(reason):
    entry = {"code": "x", "severity": "info"}
    result = assign_destination(entry, recurrence_count=0, drop_reason=reason)
    assert result["destination"] == "dropped"
    assert result["reason"] == reason


def test_assign_destination_drop_reason_outside_allowlist_raises():
    entry = {"code": "x", "severity": "info"}
    with pytest.raises(WarningDestinationError):
        assign_destination(entry, recurrence_count=0, drop_reason="because I said so")


def test_assign_destination_drop_reason_rejected_when_requires_tracking():
    entry = {"code": "x", "severity": "blocker"}
    with pytest.raises(WarningDestinationError):
        assign_destination(entry, drop_reason="retired_check")


# ── input validation — defense in depth ────────────────────────────────────


def test_assign_destination_rejects_missing_code():
    with pytest.raises(WarningDestinationError):
        assign_destination({"severity": "info"}, recurrence_count=0)


def test_assign_destination_rejects_unrecognized_severity():
    with pytest.raises(WarningDestinationError):
        assign_destination({"code": "x", "severity": "critical"}, recurrence_count=0)


def test_assign_destination_does_not_mutate_input_entry():
    entry = {"code": "x", "severity": "info", "message": "m"}
    before = copy.deepcopy(entry)
    assign_destination(entry, recurrence_count=0)
    assert entry == before


def test_assign_destination_result_is_new_dict():
    entry = {"code": "x", "severity": "info", "message": "m"}
    result = assign_destination(entry, recurrence_count=0)
    assert result is not entry
    assert result["code"] == "x"


# ── module constants sanity ────────────────────────────────────────────────


def test_legal_destinations_are_exactly_the_four_from_adr():
    assert LEGAL_DESTINATIONS == {"oi", "oi_pending", "counted", "dropped"}


def test_legal_severities_match_open_items_manager_severity_level():
    assert LEGAL_SEVERITIES == {"blocker", "warn", "info"}
