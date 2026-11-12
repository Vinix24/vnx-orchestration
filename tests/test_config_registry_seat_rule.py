"""The seat-rule key in the registry (track ``tweede-stoel-op-signaal``, PR 5).

Plan: ``claudedocs/specs/tweede-stoel-op-signaal/plan.md``, Deliverable 5 and
``Terugdraaien``. PR 4 wired ``VNX_REVIEW_SEAT_RULE`` through
``config_runtime.get`` but left the key out of ``config_registry.CONFIG_REGISTRY``,
so ``config_store_db.set_config`` answered ``unknown config key`` and no project
could carry a value. This file pins the registration and the guarantee that an
unset project changes nothing: the registry default is the empty string, so
``active_seat_rule()`` still answers the exact triple every merge on main records
today -- ``ActiveSeatRule("first_seat_or_every_seat_by_risk", "", True)``.

Everything runs against a throwaway project store built by
``tests/review_stack_support.py``; the live store under ``~/.vnx-data`` is never
read or written.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

VNX_ROOT = Path(__file__).resolve().parent.parent
for _p in (str(VNX_ROOT / "scripts"), str(VNX_ROOT / "scripts" / "lib")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import config_registry
import config_runtime
import config_store_db
import review_seat_evaluation as rse
import review_stack_support

KEY = rse.SEAT_RULE_KEY
OVERRIDE = "VNX_OVERRIDE_REVIEW_SEAT_RULE"
PROJECT = "seat-rule-project"
APPROVAL = "appr-seat-rule"

#: The triple an unset project records today (the merge record's ``rule``, its
#: empty ``value`` and ``recognised=True``). It must stay byte-for-byte the same.
UNSET = rse.ActiveSeatRule(rse.RISK_SEAT_RULE, "", True)


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    # Same isolation tests/review_stack_support.py gives the review-stack tests:
    # no env var, no wired resolver and no default project survives a test.
    yield from review_stack_support.isolate_config_registry(monkeypatch)


def _store(tmp_path: Path, monkeypatch, value=None):
    """A throwaway project store with the seed ``runtime_coordination.db``.

    ``value`` is written as the project's ``VNX_REVIEW_SEAT_RULE`` row (with an
    approval id) before ``config_runtime`` is wired; ``None`` leaves the project
    unset so the registry default decides.
    """
    store = review_stack_support.build_project_store(tmp_path, monkeypatch)
    monkeypatch.setenv("VNX_PROJECT_ID", PROJECT)
    db = store["state_dir"] / "runtime_coordination.db"
    conn = sqlite3.connect(db)
    try:
        if value is None:
            config_store_db.ensure_config_tables(conn)
            conn.commit()
        else:
            config_store_db.set_config(
                conn, PROJECT, KEY, value, actor="test", approval_id=APPROVAL,
            )
    finally:
        conn.close()
    assert config_runtime.autowire(state_dir=store["state_dir"], project_id=PROJECT) is True
    return store


def _rows(store, table: str) -> int:
    conn = sqlite3.connect(store["state_dir"] / "runtime_coordination.db")
    try:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# The registration
# ---------------------------------------------------------------------------


def test_the_key_is_a_gate_flag_that_needs_approval():
    assert KEY in config_registry.CONFIG_REGISTRY, (
        f"{KEY} is not registered: config_store_db.set_config answers 'unknown config key'"
    )
    entry = config_registry.CONFIG_REGISTRY[KEY]
    assert entry.category == "gate"
    assert entry.requires_approval is True
    assert entry.writable_from_ui is True
    assert entry.status == "ACTIVATE"


def test_the_registry_default_is_empty_so_an_unset_project_does_not_change():
    assert config_registry.CONFIG_REGISTRY[KEY].default == ""


# ---------------------------------------------------------------------------
# The write path: approval required, value + one audit row
# ---------------------------------------------------------------------------


def test_set_config_without_an_approval_id_is_refused(tmp_path, monkeypatch):
    store = _store(tmp_path, monkeypatch)
    conn = sqlite3.connect(store["state_dir"] / "runtime_coordination.db")
    try:
        with pytest.raises(PermissionError):
            config_store_db.set_config(
                conn, PROJECT, KEY, rse.SIGNAL_SEAT_RULE, actor="test",
            )
        assert config_store_db.read_config(conn, PROJECT, KEY) is None
        assert _rows(store, "project_config") == 0
        assert _rows(store, "project_config_audit") == 0
    finally:
        conn.close()


def test_with_an_approval_id_it_writes_the_row_and_one_audit_row(tmp_path, monkeypatch):
    store = _store(tmp_path, monkeypatch)
    conn = sqlite3.connect(store["state_dir"] / "runtime_coordination.db")
    try:
        result = config_store_db.set_config(
            conn, PROJECT, KEY, rse.SIGNAL_SEAT_RULE, actor="test", approval_id=APPROVAL,
        )
        assert result["new_value"] == rse.SIGNAL_SEAT_RULE
        assert config_store_db.read_config(conn, PROJECT, KEY) == rse.SIGNAL_SEAT_RULE
        assert _rows(store, "project_config") == 1
        assert _rows(store, "project_config_audit") == 1
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Unset: the triple every merge on main records today is unchanged
# ---------------------------------------------------------------------------


def test_an_unset_project_keeps_the_exact_recorded_triple(tmp_path, monkeypatch):
    _store(tmp_path, monkeypatch)

    assert rse.active_seat_rule() == UNSET
    assert rse.active_seat_rule() == rse.ActiveSeatRule(rse.RISK_SEAT_RULE, "", True)
    # The value the door reads is the registry default, not an env var or a row.
    assert config_registry.get(KEY) == ""
    assert config_registry.resolve_with_source(KEY) == ("", "default")


# ---------------------------------------------------------------------------
# The row, the brake, and an unknown value
# ---------------------------------------------------------------------------


def test_a_signal_rule_row_is_read_from_project_config(tmp_path, monkeypatch):
    _store(tmp_path, monkeypatch, rse.SIGNAL_SEAT_RULE)

    assert rse.active_seat_rule() == rse.ActiveSeatRule(
        rse.SIGNAL_SEAT_RULE, rse.SIGNAL_SEAT_RULE, True,
    )
    assert config_registry.resolve_with_source(KEY) == (rse.SIGNAL_SEAT_RULE, "project_config")


def test_the_override_brake_wins_over_the_row(tmp_path, monkeypatch):
    _store(tmp_path, monkeypatch, rse.SIGNAL_SEAT_RULE)
    monkeypatch.setenv(OVERRIDE, rse.RISK_SEAT_RULE)

    assert rse.active_seat_rule() == rse.ActiveSeatRule(
        rse.RISK_SEAT_RULE, rse.RISK_SEAT_RULE, True,
    )
    assert config_registry.resolve_with_source(KEY) == (rse.RISK_SEAT_RULE, "override")


def test_an_unknown_row_value_fails_closed_and_keeps_the_raw_value(tmp_path, monkeypatch):
    _store(tmp_path, monkeypatch, "every_seat_always")

    setting = rse.active_seat_rule()
    assert setting == rse.ActiveSeatRule(rse.RISK_SEAT_RULE, "every_seat_always", False)
    assert config_registry.resolve_with_source(KEY) == ("every_seat_always", "project_config")


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
