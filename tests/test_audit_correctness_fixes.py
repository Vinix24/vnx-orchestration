#!/usr/bin/env python3
"""Regression tests for the four audit-correctness fixes (C3, C4, C6, C7).

Dispatch-ID: feat/audit-correctness-fixes

C3 — VNX_OVERRIDE_* truthy spellings (config_registry.get_bool)
C4 — two project_ids in one process get distinct stores (config_runtime.autowire)
C6 — two receipts with a missing timestamp get distinct idempotency keys
C7 — an in-window key survives a duplicate receipt append
"""

from __future__ import annotations

import json
import sqlite3
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts" / "lib"))

import config_registry as cr  # noqa: E402
import config_store_db as cs  # noqa: E402
import config_runtime as crt  # noqa: E402
from append_receipt_internals.idempotency import (  # noqa: E402
    _compute_idempotency_key,
    _write_receipt_under_lock,
    _cache_file_for,
    _load_cache,
)


# ---------------------------------------------------------------------------
# Shared fixture: isolate registry / runtime state between each test
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    # Clear all registry env vars and overrides so tests start clean.
    for k in list(cr.CONFIG_REGISTRY):
        monkeypatch.delenv(k, raising=False)
        monkeypatch.delenv(f"VNX_OVERRIDE_{cr._bare(k)}", raising=False)
    monkeypatch.delenv("VNX_STATE_DIR", raising=False)
    monkeypatch.delenv("VNX_PROJECT_ID", raising=False)
    crt._wired_for.clear()
    cr.set_db_resolver(None)
    cr.set_default_project_id(None)
    yield
    crt._wired_for.clear()
    cr.set_db_resolver(None)
    cr.set_default_project_id(None)


def _state_dir_with(tmp_path: Path, key: str, value: str, project_id: str = "proj-a") -> Path:
    sd = tmp_path / project_id / "state"
    sd.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(sd / "runtime_coordination.db")
    cs.set_config(conn, project_id, key, value, actor="op")
    conn.close()
    return sd


# ===========================================================================
# C3 — VNX_OVERRIDE_* must accept all truthy spellings, not just "1"
# ===========================================================================

class TestC3OverrideTruthySpellings:
    """C3: VNX_OVERRIDE_X=true / =yes / =on must resolve truthy; =false must resolve falsy."""

    def test_override_true_is_truthy(self, monkeypatch):
        monkeypatch.setenv("VNX_OVERRIDE_SCOUT_PREPASS", "true")
        assert cr.get_bool("VNX_SCOUT_PREPASS") is True

    def test_override_TRUE_upper_is_truthy(self, monkeypatch):
        monkeypatch.setenv("VNX_OVERRIDE_SCOUT_PREPASS", "TRUE")
        assert cr.get_bool("VNX_SCOUT_PREPASS") is True

    def test_override_yes_is_truthy(self, monkeypatch):
        monkeypatch.setenv("VNX_OVERRIDE_SCOUT_PREPASS", "yes")
        assert cr.get_bool("VNX_SCOUT_PREPASS") is True

    def test_override_on_is_truthy(self, monkeypatch):
        monkeypatch.setenv("VNX_OVERRIDE_SCOUT_PREPASS", "on")
        assert cr.get_bool("VNX_SCOUT_PREPASS") is True

    def test_override_1_is_truthy(self, monkeypatch):
        """The canonical "1" must still work."""
        monkeypatch.setenv("VNX_OVERRIDE_SCOUT_PREPASS", "1")
        assert cr.get_bool("VNX_SCOUT_PREPASS") is True

    def test_override_false_is_falsy(self, monkeypatch):
        """VNX_OVERRIDE_X=false disables even when VNX_X=1."""
        monkeypatch.setenv("VNX_SCOUT_PREPASS", "1")
        monkeypatch.setenv("VNX_OVERRIDE_SCOUT_PREPASS", "false")
        assert cr.get_bool("VNX_SCOUT_PREPASS") is False

    def test_override_0_is_falsy(self, monkeypatch):
        monkeypatch.setenv("VNX_SCOUT_PREPASS", "1")
        monkeypatch.setenv("VNX_OVERRIDE_SCOUT_PREPASS", "0")
        assert cr.get_bool("VNX_SCOUT_PREPASS") is False

    def test_get_returns_raw_string_for_override(self, monkeypatch):
        """get() itself still returns the raw string — only get_bool() interprets truthy."""
        monkeypatch.setenv("VNX_OVERRIDE_SCOUT_PREPASS", "true")
        assert cr.get("VNX_SCOUT_PREPASS") == "true"

    def test_override_beats_db_and_env_with_truthy_spelling(self, monkeypatch):
        """Override with spelling=true must beat both a DB "0" and env "0"."""
        monkeypatch.setenv("VNX_SCOUT_PREPASS", "0")
        cr.set_db_resolver(lambda pid, key: "0" if key == "VNX_SCOUT_PREPASS" else None)
        monkeypatch.setenv("VNX_OVERRIDE_SCOUT_PREPASS", "true")
        assert cr.get_bool("VNX_SCOUT_PREPASS") is True


# ===========================================================================
# C4 — two project_ids in one process resolve different stores
# ===========================================================================

class TestC4TwoProjectsDistinctStores:
    """C4: autowire keyed by (state_dir, project_id) — second project gets its own store."""

    def test_two_project_ids_resolve_distinct_db_values(self, tmp_path):
        sd_a = _state_dir_with(tmp_path, "VNX_SCOUT_PREPASS", "1", project_id="proj-a")
        sd_b = _state_dir_with(tmp_path, "VNX_SCOUT_PREPASS", "0", project_id="proj-b")

        # Wire project A
        assert crt.autowire(state_dir=sd_a, project_id="proj-a") is True
        val_a = cr.get("VNX_SCOUT_PREPASS", project_id="proj-a")

        # Wire project B (different state_dir + project_id)
        assert crt.autowire(state_dir=sd_b, project_id="proj-b") is True
        val_b = cr.get("VNX_SCOUT_PREPASS", project_id="proj-b")

        assert val_a == "1", f"proj-a expected '1', got {val_a!r}"
        assert val_b == "0", f"proj-b expected '0', got {val_b!r}"
        assert val_a != val_b

    def test_same_pair_is_idempotent(self, tmp_path):
        sd = _state_dir_with(tmp_path, "VNX_TAGGER_ENABLED", "1", project_id="proj-a")
        assert crt.autowire(state_dir=sd, project_id="proj-a") is True
        # Same args a second time must return True without error.
        assert crt.autowire(state_dir=sd, project_id="proj-a") is True

    def test_bogus_dir_returns_false_after_first_project_wired(self, tmp_path):
        sd = _state_dir_with(tmp_path, "VNX_TAGGER_ENABLED", "1", project_id="proj-a")
        assert crt.autowire(state_dir=sd, project_id="proj-a") is True
        # A different (bogus, other-proj) key must not piggyback on the first wiring.
        result = crt.autowire(state_dir=tmp_path / "bogus", project_id="other-proj")
        assert result is False

    def test_rewire_back_to_first_project_restores_its_default(self, tmp_path):
        # Gate finding: A -> B -> A must restore A's resolver+default on the cache hit,
        # not leave the registry pointed at B. Checks the DEFAULT (no explicit project_id).
        sd_a = _state_dir_with(tmp_path, "VNX_SCOUT_PREPASS", "1", project_id="proj-a")
        sd_b = _state_dir_with(tmp_path, "VNX_SCOUT_PREPASS", "0", project_id="proj-b")
        assert crt.autowire(state_dir=sd_a, project_id="proj-a") is True
        assert crt.autowire(state_dir=sd_b, project_id="proj-b") is True
        assert cr.get("VNX_SCOUT_PREPASS") == "0"  # default now resolves B
        assert crt.autowire(state_dir=sd_a, project_id="proj-a") is True  # cache hit
        assert cr.get("VNX_SCOUT_PREPASS") == "1", "re-wire to A must restore A's default, not stay on B"


# ===========================================================================
# C6 — two receipts with a missing timestamp get distinct keys
# ===========================================================================

class TestC6MissingTimestampDistinctKeys:
    """C6: receipts without timestamp (and no dispatch_id/task_id/report_path) must get distinct
    idempotency keys so the second is not silently dropped."""

    def _receipt_no_ts(self, extra: dict | None = None) -> dict:
        base = {"event_type": "task_complete", "status": "success", "source": "pytest"}
        if extra:
            base.update(extra)
        return base

    def test_two_distinct_receipts_without_timestamp_get_distinct_keys(self):
        r1 = self._receipt_no_ts({"terminal": "T1", "note": "alpha"})
        r2 = self._receipt_no_ts({"terminal": "T2", "note": "beta"})
        k1 = _compute_idempotency_key(r1, "task_complete")
        k2 = _compute_idempotency_key(r2, "task_complete")
        assert k1 != k2, "Distinct receipts without timestamp must not share an idempotency key"

    def test_identical_receipts_without_timestamp_get_the_same_key(self):
        """Truly identical receipts (same content) must still deduplicate correctly."""
        r = self._receipt_no_ts({"terminal": "T1"})
        k1 = _compute_idempotency_key(r, "task_complete")
        k2 = _compute_idempotency_key(dict(r), "task_complete")
        assert k1 == k2

    def test_receipt_with_null_timestamp_gets_content_based_key(self):
        """A receipt where timestamp is explicitly None must not collapse with a different receipt."""
        r1 = {"event_type": "task_complete", "terminal": "T1", "timestamp": None}
        r2 = {"event_type": "task_complete", "terminal": "T2", "timestamp": None}
        k1 = _compute_idempotency_key(r1, "task_complete")
        k2 = _compute_idempotency_key(r2, "task_complete")
        assert k1 != k2

    def test_receipt_with_blank_timestamp_behaves_like_missing(self):
        r1 = {"event_type": "task_complete", "terminal": "T1", "timestamp": "  "}
        r2 = {"event_type": "task_complete", "terminal": "T2", "timestamp": "  "}
        k1 = _compute_idempotency_key(r1, "task_complete")
        k2 = _compute_idempotency_key(r2, "task_complete")
        assert k1 != k2

    def test_receipt_with_valid_timestamp_uses_timestamp_based_key(self):
        """When a valid timestamp exists, the key should differ only when the timestamp differs."""
        r_ts = {"event_type": "task_complete", "timestamp": "2026-06-30T10:00:00Z"}
        r_no_ts = {"event_type": "task_complete"}
        k_ts = _compute_idempotency_key(r_ts, "task_complete")
        k_no_ts = _compute_idempotency_key(r_no_ts, "task_complete")
        assert k_ts != k_no_ts

    def test_two_receipts_without_timestamp_both_append_to_file(self, tmp_path):
        """End-to-end: both receipts must appear in the NDJSON file, not just the first."""
        receipt_path = tmp_path / "t0_receipts.ndjson"
        cache_path = _cache_file_for(receipt_path)
        window = 300

        r1 = {"event_type": "task_complete", "terminal": "T1", "status": "success"}
        r2 = {"event_type": "task_complete", "terminal": "T2", "status": "success"}

        k1 = _compute_idempotency_key(r1, "task_complete")
        k2 = _compute_idempotency_key(r2, "task_complete")
        assert k1 != k2, "pre-condition: distinct receipts must have distinct keys"

        res1 = _write_receipt_under_lock(r1, receipt_path, cache_path, k1, window)
        res2 = _write_receipt_under_lock(r2, receipt_path, cache_path, k2, window)

        assert res1.status == "appended"
        assert res2.status == "appended", f"Second receipt was {res2.status!r} — must be 'appended'"

        lines = [l for l in receipt_path.read_text().splitlines() if l.strip()]
        assert len(lines) == 2, f"Expected 2 receipts in file, got {len(lines)}"


# ===========================================================================
# C7 — in-window key survives a duplicate receipt append
# ===========================================================================

class TestC7InWindowKeyNotEvicted:
    """C7: the duplicate path must NOT rewrite/trim the cache so in-window keys are preserved."""

    def test_in_window_key_survives_duplicate_append(self, tmp_path):
        receipt_path = tmp_path / "t0_receipts.ndjson"
        cache_path = _cache_file_for(receipt_path)
        window = 300

        # First append: write a unique receipt to establish an in-window key.
        r_unique = {"event_type": "task_complete", "dispatch_id": "unique-dispatch-001"}
        k_unique = _compute_idempotency_key(r_unique, "task_complete")
        res_unique = _write_receipt_under_lock(r_unique, receipt_path, cache_path, k_unique, window)
        assert res_unique.status == "appended"

        # Second append: a different receipt to be repeated as a duplicate.
        r_dup = {"event_type": "task_complete", "dispatch_id": "dup-dispatch-002"}
        k_dup = _compute_idempotency_key(r_dup, "task_complete")
        res_first = _write_receipt_under_lock(r_dup, receipt_path, cache_path, k_dup, window)
        assert res_first.status == "appended"

        # Third append: duplicate of the second (triggers the duplicate path).
        res_dup = _write_receipt_under_lock(r_dup, receipt_path, cache_path, k_dup, window)
        assert res_dup.status == "duplicate"

        # The in-window key for the FIRST receipt must still be in the cache.
        entries = _load_cache(cache_path, min_epoch=time.time() - window)
        cached_keys = {e["key"] for e in entries}
        assert k_unique in cached_keys, (
            "In-window key from the first append was evicted by the duplicate path"
        )
        assert k_dup in cached_keys

    def test_duplicate_append_does_not_write_additional_line_to_receipt_file(self, tmp_path):
        """The duplicate path must not write to the NDJSON file."""
        receipt_path = tmp_path / "t0_receipts.ndjson"
        cache_path = _cache_file_for(receipt_path)
        window = 300

        r = {"event_type": "task_complete", "dispatch_id": "dispatch-c7-check"}
        k = _compute_idempotency_key(r, "task_complete")

        _write_receipt_under_lock(r, receipt_path, cache_path, k, window)
        line_count_before = len([l for l in receipt_path.read_text().splitlines() if l.strip()])

        res = _write_receipt_under_lock(r, receipt_path, cache_path, k, window)
        assert res.status == "duplicate"

        line_count_after = len([l for l in receipt_path.read_text().splitlines() if l.strip()])
        assert line_count_before == line_count_after, (
            f"Duplicate append wrote to the receipt file: {line_count_before} → {line_count_after} lines"
        )


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
