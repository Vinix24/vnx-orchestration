#!/usr/bin/env python3
"""ADR-035 §9 PR-6/PR-7 — receipt_query.py: the full pull-model query interface.

PR-6 covers T12 (pull read-then-advance; a concurrent append's partial
trailing line is never consumed until it is newline-terminated) and T13 (pull
--seed-now sets the cursor to EOF; the backlog is skipped but stays on disk
and readable via by-dispatch), plus the surrounding cursor mechanics absorbed
from the parked receipt_pull.py design (branch feat/receipt-mailbox-delivery,
commit 54089155) and mixed v1/v2 ledger tolerance for both subcommands.

PR-7 covers T14 (by-pr), T15 (by-track join, mixed v1/v2), T16 (since), T17
(digest buckets a verdict-less/v1 line under an explicit "unknown"), T26
(by-track returns [] rather than erroring when the track has no dispatches or
the state DB predates the track column), and T34 (an unresolved oi_pending
warning counts in digest's tally; reconcile-oi-pending resolving it — by
creating a real open item with a matching dedup_key — drops it out, proving
the join reads the CURRENT open-items store rather than the immutable
receipt line).
"""

from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict
from unittest.mock import patch

import pytest

TESTS_DIR = Path(__file__).resolve().parent
VNX_ROOT = TESTS_DIR.parent
SCRIPTS_DIR = VNX_ROOT / "scripts"
SCRIPTS_LIB = SCRIPTS_DIR / "lib"
sys.path.insert(0, str(SCRIPTS_LIB))
sys.path.insert(0, str(SCRIPTS_DIR))

import receipt_query as rq  # noqa: E402


def _write_ledger(state_dir: Path, *receipts: Dict[str, Any], trailing_newline: bool = True) -> None:
    lines = [json.dumps(r) for r in receipts]
    text = "\n".join(lines)
    if receipts and trailing_newline:
        text += "\n"
    (state_dir / rq.LEDGER_NAME).write_text(text, encoding="utf-8")


def _r(did: str, terminal: str = "T2", status: str = "success", **overrides: Any) -> Dict[str, Any]:
    receipt = {"dispatch_id": did, "terminal_id": terminal, "status": status, "pr_id": None}
    receipt.update(overrides)
    return receipt


# ---------------------------------------------------------------------------
# T12 — read-then-advance; concurrent partial trailing line is not consumed
# ---------------------------------------------------------------------------

def test_t12_incomplete_trailing_line_is_not_advanced_past(tmp_path):
    _write_ledger(tmp_path, _r("d1"))
    ledger = tmp_path / rq.LEDGER_NAME
    # a concurrent append wrote a partial line (no closing brace/newline yet)
    with open(ledger, "a", encoding="utf-8") as f:
        f.write('{"dispatch_id": "d2-partial"')

    receipts, off = rq.pull_new_receipts(ledger, 0)
    assert [x["dispatch_id"] for x in receipts] == ["d1"]

    # a second pull at the same cursor still doesn't see the partial line
    receipts_again, off_again = rq.pull_new_receipts(ledger, off)
    assert receipts_again == []
    assert off_again == off

    # now the writer completes the line
    with open(ledger, "a", encoding="utf-8") as f:
        f.write(', "terminal_id": "T1", "status": "success"}\n')
    receipts2, off2 = rq.pull_new_receipts(ledger, off)
    assert [x["dispatch_id"] for x in receipts2] == ["d2-partial"]
    assert off2 > off


def test_t12_read_then_advance_no_double_surface(tmp_path):
    _write_ledger(tmp_path, _r("d1"), _r("d2"), _r("d3"))
    ledger = tmp_path / rq.LEDGER_NAME
    receipts, off = rq.pull_new_receipts(ledger, 0)
    assert [x["dispatch_id"] for x in receipts] == ["d1", "d2", "d3"]
    # second pull from the advanced cursor surfaces nothing (no double-surface)
    receipts2, off2 = rq.pull_new_receipts(ledger, off)
    assert receipts2 == []
    assert off2 == off


def test_t12_surfaces_only_new_after_append(tmp_path):
    _write_ledger(tmp_path, _r("d1"), _r("d2"))
    ledger = tmp_path / rq.LEDGER_NAME
    _, off = rq.pull_new_receipts(ledger, 0)
    with open(ledger, "a", encoding="utf-8") as f:
        f.write(json.dumps(_r("d3")) + "\n")
    receipts, _ = rq.pull_new_receipts(ledger, off)
    assert [x["dispatch_id"] for x in receipts] == ["d3"]


def test_malformed_complete_line_skipped_but_advances(tmp_path):
    ledger = tmp_path / rq.LEDGER_NAME
    ledger.write_text(
        json.dumps(_r("d1")) + "\n" + "{not json}\n" + json.dumps(_r("d2")) + "\n",
        encoding="utf-8",
    )
    receipts, off = rq.pull_new_receipts(ledger, 0)
    assert [x["dispatch_id"] for x in receipts] == ["d1", "d2"]
    # cursor advanced past the garbage line too — it will never parse
    assert off == ledger.stat().st_size


def test_truncation_resets_cursor(tmp_path):
    _write_ledger(tmp_path, _r("d1"), _r("d2"), _r("d3"))
    ledger = tmp_path / rq.LEDGER_NAME
    _, off = rq.pull_new_receipts(ledger, 0)
    # ledger rotated/truncated to fewer bytes than the cursor
    _write_ledger(tmp_path, _r("dX"))
    receipts, _ = rq.pull_new_receipts(ledger, off)
    assert [x["dispatch_id"] for x in receipts] == ["dX"]


def test_missing_ledger_is_empty(tmp_path):
    ledger = tmp_path / rq.LEDGER_NAME
    receipts, off = rq.pull_new_receipts(ledger, 0)
    assert receipts == [] and off == 0


def test_cursor_persist_roundtrip(tmp_path):
    cursor_path = rq._default_cursor_path(tmp_path)
    assert rq.load_cursor(cursor_path) == 0
    rq.save_cursor(cursor_path, 42)
    assert rq.load_cursor(cursor_path) == 42


def test_cursor_corrupt_file_defaults_to_zero(tmp_path):
    cursor_path = rq._default_cursor_path(tmp_path)
    cursor_path.write_text("not json", encoding="utf-8")
    assert rq.load_cursor(cursor_path) == 0


# ---------------------------------------------------------------------------
# T13 — --seed-now sets cursor to EOF; backlog stays on disk + readable
# ---------------------------------------------------------------------------

def test_t13_seed_now_sets_cursor_to_eof_and_skips_backlog(tmp_path):
    _write_ledger(tmp_path, _r("d1"), _r("d2"))
    ledger = tmp_path / rq.LEDGER_NAME
    eof = ledger.stat().st_size

    rc = rq.main(["pull", "--state-dir", str(tmp_path), "--seed-now"])
    assert rc == 0

    cursor_path = rq._default_cursor_path(tmp_path)
    assert rq.load_cursor(cursor_path) == eof

    # backlog is skipped by a subsequent pull...
    receipts, _ = rq.pull_new_receipts(ledger, rq.load_cursor(cursor_path))
    assert receipts == []


def test_t13_seed_now_backlog_stays_on_disk_and_readable_by_dispatch(tmp_path):
    _write_ledger(tmp_path, _r("d1"), _r("d2"))
    ledger = tmp_path / rq.LEDGER_NAME
    assert ledger.exists()

    rq.main(["pull", "--state-dir", str(tmp_path), "--seed-now"])

    # the backlog receipt is still on disk, untouched — by-dispatch still finds it
    receipts = rq.find_receipts_by_dispatch(ledger, "d1")
    assert len(receipts) == 1
    assert receipts[0]["dispatch_id"] == "d1"
    assert ledger.read_text(encoding="utf-8").count("d1") >= 1


def test_seed_now_on_missing_ledger_seeds_zero(tmp_path):
    rc = rq.main(["pull", "--state-dir", str(tmp_path), "--seed-now"])
    assert rc == 0
    assert rq.load_cursor(rq._default_cursor_path(tmp_path)) == 0


# ---------------------------------------------------------------------------
# --peek and --cursor-file
# ---------------------------------------------------------------------------

def test_peek_does_not_advance_cursor(tmp_path, capsys):
    _write_ledger(tmp_path, _r("d1"), _r("d2"))
    rc = rq.main(["pull", "--state-dir", str(tmp_path), "--peek", "--json"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["count"] == 2
    # cursor was never persisted
    assert rq.load_cursor(rq._default_cursor_path(tmp_path)) == 0

    # a real (non-peek) pull still sees both receipts
    rc2 = rq.main(["pull", "--state-dir", str(tmp_path), "--json"])
    out2 = json.loads(capsys.readouterr().out)
    assert out2["count"] == 2
    assert rq.load_cursor(rq._default_cursor_path(tmp_path)) == out2["cursor"]


def test_cursor_file_override(tmp_path):
    _write_ledger(tmp_path, _r("d1"))
    custom_cursor = tmp_path / "custom" / "my_cursor.json"
    custom_cursor.parent.mkdir(parents=True, exist_ok=True)

    rc = rq.main([
        "pull", "--state-dir", str(tmp_path), "--cursor-file", str(custom_cursor),
    ])
    assert rc == 0
    assert custom_cursor.exists()
    # the default cursor location was never touched
    assert not rq._default_cursor_path(tmp_path).exists()


# ---------------------------------------------------------------------------
# by-dispatch — thin wrapper over receipt_provenance.find_receipts_by_dispatch
# ---------------------------------------------------------------------------

def test_by_dispatch_returns_matching_receipts(tmp_path, capsys):
    _write_ledger(tmp_path, _r("d1"), _r("d2"), _r("d1", status="failed"))
    rc = rq.main(["by-dispatch", "d1", "--state-dir", str(tmp_path), "--json"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["dispatch_id"] == "d1"
    assert out["count"] == 2
    assert {r["status"] for r in out["receipts"]} == {"success", "failed"}


def test_by_dispatch_no_matches_is_empty_not_error(tmp_path, capsys):
    _write_ledger(tmp_path, _r("d1"))
    rc = rq.main(["by-dispatch", "does-not-exist", "--state-dir", str(tmp_path), "--json"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["count"] == 0
    assert out["receipts"] == []


def test_by_dispatch_missing_ledger_is_empty(tmp_path, capsys):
    rc = rq.main(["by-dispatch", "d1", "--state-dir", str(tmp_path), "--json"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["count"] == 0


# ---------------------------------------------------------------------------
# Mixed v1/v2 ledger — neither subcommand crashes
# ---------------------------------------------------------------------------

def _v1_receipt(did: str) -> Dict[str, Any]:
    # v1 lines carry no schema_version at all.
    return {"dispatch_id": did, "terminal_id": "T1", "status": "success"}


def _v2_receipt(did: str) -> Dict[str, Any]:
    return {
        "schema_version": 2,
        "event_type": "task_complete",
        "dispatch_id": did,
        "terminal_id": "T2",
        "status": "done",
        "verdict": {"decision": "accept", "reason": "ok", "evidence_complete": True},
    }


def test_pull_handles_mixed_v1_v2_ledger(tmp_path):
    _write_ledger(tmp_path, _v1_receipt("d1"), _v2_receipt("d2"), _v1_receipt("d3"))
    ledger = tmp_path / rq.LEDGER_NAME
    receipts, off = rq.pull_new_receipts(ledger, 0)
    assert [r["dispatch_id"] for r in receipts] == ["d1", "d2", "d3"]
    assert receipts[0].get("schema_version") is None
    assert receipts[1]["schema_version"] == 2
    assert off == ledger.stat().st_size


def test_by_dispatch_handles_mixed_v1_v2_ledger(tmp_path, capsys):
    _write_ledger(tmp_path, _v1_receipt("shared"), _v2_receipt("shared"))
    rc = rq.main(["by-dispatch", "shared", "--state-dir", str(tmp_path), "--json"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["count"] == 2
    versions = [r.get("schema_version") for r in out["receipts"]]
    assert versions == [None, 2]


def test_pull_text_output_handles_mixed_ledger(tmp_path, capsys):
    _write_ledger(tmp_path, _v1_receipt("d1"), _v2_receipt("d2"))
    rc = rq.main(["pull", "--state-dir", str(tmp_path)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "d1" in out and "schema_version=1" in out
    assert "d2" in out and "schema_version=2" in out


# ---------------------------------------------------------------------------
# PR-7 helpers
# ---------------------------------------------------------------------------

def _v1_with(did: str, **overrides: Any) -> Dict[str, Any]:
    r = _v1_receipt(did)
    r.update(overrides)
    return r


def _v2_with(did: str, **overrides: Any) -> Dict[str, Any]:
    r = _v2_receipt(did)
    r.update(overrides)
    return r


class _NeverCalledOIM:
    """Fails loudly if digest ever touches the OI store when there are no
    `oi_pending` warnings to resolve — proves the join is skipped, not just
    empty, when there is nothing to join against."""

    def load_items(self):
        raise AssertionError("open_items_manager must not be touched with no oi_pending warnings")

    def _find_by_dedup_key(self, data, key):
        raise AssertionError("open_items_manager must not be touched with no oi_pending warnings")


def _load_oim(tmp_path: Path):
    """Fresh, isolated open_items_manager module bound to a per-test STATE_DIR
    (mirrors tests/test_warning_destination.py's helper) — exercises the REAL
    add_item_programmatic/dedup path, not a mock."""
    env_patch = {
        "VNX_DATA_DIR": str(tmp_path / "data"),
        "VNX_DATA_DIR_EXPLICIT": "1",
        "VNX_STATE_DIR": str(tmp_path / "data" / "state"),
        "VNX_HOME": str(VNX_ROOT),
    }
    (tmp_path / "data" / "state").mkdir(parents=True, exist_ok=True)

    mod_name = f"open_items_manager_rq_test_{tmp_path.name}"
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


# ---------------------------------------------------------------------------
# T14 — by-pr: linear scan over pr_id, mixed v1/v2 ledger
# ---------------------------------------------------------------------------

def test_t14_by_pr_returns_every_matching_receipt(tmp_path):
    _write_ledger(
        tmp_path,
        _v1_with("d1", pr_id="100"),
        _v2_with("d2", pr_id="200"),
        _v1_with("d3", pr_id="100"),
        _v2_with("d4", pr_id="300"),
    )
    ledger = tmp_path / rq.LEDGER_NAME
    receipts = rq.find_receipts_by_pr(ledger, "100")
    assert {r["dispatch_id"] for r in receipts} == {"d1", "d3"}


def test_t14_by_pr_matches_across_schema_versions(tmp_path):
    _write_ledger(tmp_path, _v1_with("d1", pr_id="7"), _v2_with("d2", pr_id="7"))
    ledger = tmp_path / rq.LEDGER_NAME
    receipts = rq.find_receipts_by_pr(ledger, "7")
    assert {r["dispatch_id"] for r in receipts} == {"d1", "d2"}


def test_by_pr_matches_int_and_str_pr_id(tmp_path):
    _write_ledger(tmp_path, _r("d1", pr_id=123))
    ledger = tmp_path / rq.LEDGER_NAME
    assert [r["dispatch_id"] for r in rq.find_receipts_by_pr(ledger, "123")] == ["d1"]


def test_by_pr_no_match_is_empty_not_error(tmp_path):
    _write_ledger(tmp_path, _r("d1", pr_id="1"))
    ledger = tmp_path / rq.LEDGER_NAME
    assert rq.find_receipts_by_pr(ledger, "999") == []


def test_by_pr_missing_ledger_is_empty(tmp_path):
    ledger = tmp_path / rq.LEDGER_NAME
    assert rq.find_receipts_by_pr(ledger, "1") == []


def test_by_pr_cli(tmp_path, capsys):
    _write_ledger(tmp_path, _r("d1", pr_id="42"), _r("d2", pr_id="43"))
    rc = rq.main(["by-pr", "42", "--state-dir", str(tmp_path), "--json"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["count"] == 1
    assert out["receipts"][0]["dispatch_id"] == "d1"


# ---------------------------------------------------------------------------
# T16 — since: linear scan over timestamp, v2 + legacy v1
# ---------------------------------------------------------------------------

def test_t16_since_filters_v2_and_legacy_v1_timestamps(tmp_path):
    _write_ledger(
        tmp_path,
        _v1_with("d1", timestamp="2026-07-01T00:00:00Z"),
        _v2_with("d2", timestamp="2026-07-15T00:00:00Z"),
        _v1_with("d3", timestamp="2026-07-20T00:00:00Z"),
    )
    ledger = tmp_path / rq.LEDGER_NAME
    receipts = rq.find_receipts_since(ledger, "2026-07-10T00:00:00Z")
    assert {r["dispatch_id"] for r in receipts} == {"d2", "d3"}


def test_since_boundary_is_inclusive(tmp_path):
    _write_ledger(tmp_path, _r("d1", timestamp="2026-07-10T00:00:00Z"))
    ledger = tmp_path / rq.LEDGER_NAME
    receipts = rq.find_receipts_since(ledger, "2026-07-10T00:00:00Z")
    assert [r["dispatch_id"] for r in receipts] == ["d1"]


def test_since_skips_missing_or_unparseable_timestamp_without_crashing(tmp_path):
    _write_ledger(
        tmp_path,
        _r("d1"),  # no timestamp field at all
        _r("d2", timestamp="not-a-date"),
        _r("d3", timestamp="2026-07-20T00:00:00Z"),
    )
    ledger = tmp_path / rq.LEDGER_NAME
    receipts = rq.find_receipts_since(ledger, "2026-01-01T00:00:00Z")
    assert [r["dispatch_id"] for r in receipts] == ["d3"]


def test_since_invalid_argument_raises_value_error(tmp_path):
    ledger = tmp_path / rq.LEDGER_NAME
    with pytest.raises(ValueError):
        rq.find_receipts_since(ledger, "not-iso8601")


def test_since_cli_reports_error_for_invalid_timestamp(tmp_path):
    rc = rq.main(["since", "garbage", "--state-dir", str(tmp_path)])
    assert rc == 1


def test_since_cli(tmp_path, capsys):
    _write_ledger(
        tmp_path,
        _r("d1", timestamp="2026-07-01T00:00:00Z"),
        _r("d2", timestamp="2026-07-20T00:00:00Z"),
    )
    rc = rq.main(["since", "2026-07-10T00:00:00Z", "--state-dir", str(tmp_path), "--json"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["count"] == 1
    assert out["receipts"][0]["dispatch_id"] == "d2"


# ---------------------------------------------------------------------------
# T15/T26 — by-track: SQLite join (dispatches.track), NOT a linear scan
# ---------------------------------------------------------------------------

def _create_runtime_db(state_dir: Path, *, with_track_column: bool = True) -> Path:
    db_path = state_dir / rq.RUNTIME_COORDINATION_DB_NAME
    conn = sqlite3.connect(str(db_path))
    try:
        columns = (
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "dispatch_id TEXT NOT NULL, "
            "project_id TEXT NOT NULL DEFAULT 'vnx-dev'"
        )
        if with_track_column:
            columns += ", track TEXT"
        conn.execute(f"CREATE TABLE dispatches ({columns}, UNIQUE(dispatch_id, project_id))")
        conn.commit()
    finally:
        conn.close()
    return db_path


def _insert_dispatch(db_path: Path, dispatch_id: str, project_id: str, track: str) -> None:
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "INSERT INTO dispatches (dispatch_id, project_id, track) VALUES (?, ?, ?)",
            (dispatch_id, project_id, track),
        )
        conn.commit()
    finally:
        conn.close()


def test_t15_by_track_returns_receipts_for_joined_dispatch_ids_mixed_shapes(tmp_path):
    db_path = _create_runtime_db(tmp_path)
    _insert_dispatch(db_path, "d1", "vnx-dev", "track-a")
    _insert_dispatch(db_path, "d2", "vnx-dev", "track-a")
    _insert_dispatch(db_path, "d3", "vnx-dev", "track-b")  # different track — excluded

    _write_ledger(tmp_path, _v1_with("d1"), _v2_with("d2"), _v1_with("d3"))
    ledger = tmp_path / rq.LEDGER_NAME

    receipts = rq.find_receipts_by_track(tmp_path, ledger, "track-a", "vnx-dev")
    assert {r["dispatch_id"] for r in receipts} == {"d1", "d2"}
    # join key is dispatch_id, not schema_version — both shapes are returned
    versions = {r.get("schema_version") for r in receipts}
    assert versions == {None, 2}


def test_t15_by_track_scopes_by_project_id(tmp_path):
    db_path = _create_runtime_db(tmp_path)
    _insert_dispatch(db_path, "d1", "proj-a", "track-x")
    _insert_dispatch(db_path, "d2", "proj-b", "track-x")  # same track, different project

    _write_ledger(tmp_path, _r("d1"), _r("d2"))
    ledger = tmp_path / rq.LEDGER_NAME

    receipts = rq.find_receipts_by_track(tmp_path, ledger, "track-x", "proj-a")
    assert [r["dispatch_id"] for r in receipts] == ["d1"]


def test_t26_by_track_empty_when_track_has_no_dispatches(tmp_path):
    _create_runtime_db(tmp_path)  # table exists, no rows at all
    _write_ledger(tmp_path, _r("d1"))
    ledger = tmp_path / rq.LEDGER_NAME
    assert rq.find_receipts_by_track(tmp_path, ledger, "no-such-track", "vnx-dev") == []


def test_t26_by_track_empty_when_state_db_missing(tmp_path):
    _write_ledger(tmp_path, _r("d1"))
    ledger = tmp_path / rq.LEDGER_NAME
    assert rq.find_receipts_by_track(tmp_path, ledger, "track-a", "vnx-dev") == []


def test_t26_by_track_empty_when_track_column_missing(tmp_path):
    _create_runtime_db(tmp_path, with_track_column=False)
    _write_ledger(tmp_path, _r("d1"))
    ledger = tmp_path / rq.LEDGER_NAME
    assert rq.find_receipts_by_track(tmp_path, ledger, "track-a", "vnx-dev") == []


def test_t26_by_track_empty_when_dispatches_table_missing(tmp_path):
    db_path = tmp_path / rq.RUNTIME_COORDINATION_DB_NAME
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE other_table (id INTEGER)")
    conn.commit()
    conn.close()

    _write_ledger(tmp_path, _r("d1"))
    ledger = tmp_path / rq.LEDGER_NAME
    assert rq.find_receipts_by_track(tmp_path, ledger, "track-a", "vnx-dev") == []


def test_by_track_cli(tmp_path, capsys):
    db_path = _create_runtime_db(tmp_path)
    _insert_dispatch(db_path, "d1", "vnx-dev", "track-a")
    _write_ledger(tmp_path, _r("d1"))
    rc = rq.main([
        "by-track", "track-a", "--state-dir", str(tmp_path), "--project-id", "vnx-dev", "--json",
    ])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["count"] == 1
    assert out["receipts"][0]["dispatch_id"] == "d1"


# ---------------------------------------------------------------------------
# T17 — digest: mixed ledger buckets a verdict-less line as "unknown"
# ---------------------------------------------------------------------------

def test_t17_digest_buckets_v1_lines_as_unknown_verdict(tmp_path):
    now = datetime(2026, 7, 22, 12, 0, 0, tzinfo=timezone.utc)
    _write_ledger(
        tmp_path,
        _v1_with("d1", timestamp="2026-07-22T00:00:00Z"),  # no verdict at all -> unknown
        _v2_with("d2", timestamp="2026-07-22T01:00:00Z"),  # verdict.decision == accept
        {
            **_v2_with("d3", timestamp="2026-07-22T02:00:00Z"),
            "verdict": {"decision": "reject", "reason": "x", "evidence_complete": True},
        },
    )
    ledger = tmp_path / rq.LEDGER_NAME
    result = rq.compute_digest(
        ledger, window="24h", now=now, open_items_manager_module=_NeverCalledOIM(),
    )
    assert result["verdict_counts"] == {"accept": 1, "investigate": 0, "reject": 1, "unknown": 1}


def test_digest_never_crashes_on_non_dict_verdict(tmp_path):
    now = datetime(2026, 7, 22, 12, 0, 0, tzinfo=timezone.utc)
    _write_ledger(
        tmp_path,
        {"dispatch_id": "d1", "timestamp": "2026-07-22T00:00:00Z", "verdict": "not-a-dict"},
    )
    ledger = tmp_path / rq.LEDGER_NAME
    result = rq.compute_digest(
        ledger, window="24h", now=now, open_items_manager_module=_NeverCalledOIM(),
    )
    assert result["verdict_counts"]["unknown"] == 1


def test_digest_window_excludes_older_receipts(tmp_path):
    now = datetime(2026, 7, 22, 12, 0, 0, tzinfo=timezone.utc)
    _write_ledger(
        tmp_path,
        _v2_with("old", timestamp="2026-07-20T00:00:00Z"),  # > 24h ago -> excluded
        _v2_with("new", timestamp="2026-07-22T11:00:00Z"),  # within window
    )
    ledger = tmp_path / rq.LEDGER_NAME
    result = rq.compute_digest(
        ledger, window="24h", now=now, open_items_manager_module=_NeverCalledOIM(),
    )
    assert sum(result["verdict_counts"].values()) == 1


def test_digest_counted_warnings_top_codes(tmp_path):
    now = datetime(2026, 7, 22, 12, 0, 0, tzinfo=timezone.utc)

    def _receipt_with_counted(did: str, code: str, ts: str) -> Dict[str, Any]:
        r = _v2_with(did, timestamp=ts)
        r["warnings"] = [{
            "code": code, "severity": "warn", "destination": "counted",
            "oi_id": None, "reason": None, "requires_tracking": False,
        }]
        return r

    _write_ledger(
        tmp_path,
        _receipt_with_counted("d1", "report_contract_invalid", "2026-07-22T01:00:00Z"),
        _receipt_with_counted("d2", "report_contract_invalid", "2026-07-22T02:00:00Z"),
        _receipt_with_counted("d3", "other_code", "2026-07-22T03:00:00Z"),
    )
    ledger = tmp_path / rq.LEDGER_NAME
    result = rq.compute_digest(
        ledger, window="24h", now=now, open_items_manager_module=_NeverCalledOIM(),
    )
    assert result["counted_warnings"][0] == {"code": "report_contract_invalid", "count": 2}


def test_digest_invalid_window_raises(tmp_path):
    ledger = tmp_path / rq.LEDGER_NAME
    with pytest.raises(ValueError):
        rq.compute_digest(ledger, window="banana")


def test_digest_cli(tmp_path, capsys):
    _write_ledger(tmp_path, _v2_with("d1", timestamp="2026-07-22T01:00:00Z"))
    rc = rq.main(["digest", "--state-dir", str(tmp_path), "--json"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert "verdict_counts" in out
    assert out["window"] == "24h"


# ---------------------------------------------------------------------------
# T34 — oi_pending drops out of digest's tally after reconcile-oi-pending
# ---------------------------------------------------------------------------

def _oi_pending_receipt(did: str, code: str, ts: str, **overrides: Any) -> Dict[str, Any]:
    r = _v2_with(did, timestamp=ts, **overrides)
    r["warnings"] = [{
        "code": code,
        "severity": "blocker",
        "message": "worker wrote outside declared scope",
        "destination": "oi_pending",
        "oi_id": None,
        "reason": "store lock held by pid 4821",
        "requires_tracking": True,
    }]
    return r


def test_t34_oi_pending_drops_out_of_digest_tally_after_reconcile(tmp_path):
    oim = _load_oim(tmp_path)
    now = datetime(2026, 7, 22, 12, 0, 0, tzinfo=timezone.utc)

    receipt = _oi_pending_receipt(
        "d1", "worker_permission_violation", "2026-07-22T01:00:00Z",
        report_path="reports/d1.md", pr_id="42",
    )
    _write_ledger(tmp_path, receipt)
    ledger = tmp_path / rq.LEDGER_NAME
    raw_before = ledger.read_text(encoding="utf-8")

    before = rq.compute_digest(ledger, window="24h", now=now, open_items_manager_module=oim)
    assert before["oi_pending_unresolved_count"] == 1
    assert before["oi_pending_unresolved"][0]["dispatch_id"] == "d1"

    result = rq.reconcile_oi_pending(ledger, now=now, open_items_manager_module=oim)
    assert result["scanned"] == 1
    assert result["reconciled"] == 1
    assert result["still_pending"] == 0

    # a real open item now exists with a dedup_key matching the warning's code
    data = oim.load_items()
    match = oim._find_by_dedup_key(data, "worker_permission_violation")
    assert match is not None
    assert match["status"] == "open"

    after = rq.compute_digest(ledger, window="24h", now=now, open_items_manager_module=oim)
    assert after["oi_pending_unresolved_count"] == 0

    # the receipt line itself never changed — the join reads the OI store,
    # never a rewrite of the immutable ledger line (ADR-005/§6.4).
    assert ledger.read_text(encoding="utf-8") == raw_before


def test_reconcile_oi_pending_escalates_stale_still_failing_entries(tmp_path):
    now = datetime(2026, 7, 22, 12, 0, 0, tzinfo=timezone.utc)
    receipt = _oi_pending_receipt(
        "d1", "worker_permission_violation", "2026-07-01T00:00:00Z",  # 21 days old
    )
    _write_ledger(tmp_path, receipt)
    ledger = tmp_path / rq.LEDGER_NAME

    class _AlwaysFailingOIM:
        def add_item_programmatic(self, **kwargs):
            raise RuntimeError("store still unreachable")

    result = rq.reconcile_oi_pending(
        ledger, now=now, max_age_days=7.0, open_items_manager_module=_AlwaysFailingOIM(),
    )
    assert result["scanned"] == 1
    assert result["reconciled"] == 0
    assert result["still_pending"] == 1
    assert len(result["escalated"]) == 1
    assert result["escalated"][0]["code"] == "worker_permission_violation"


def test_reconcile_oi_pending_no_escalation_for_recent_failures(tmp_path):
    now = datetime(2026, 7, 22, 12, 0, 0, tzinfo=timezone.utc)
    receipt = _oi_pending_receipt(
        "d1", "worker_permission_violation", "2026-07-22T11:00:00Z",  # 1 hour old
    )
    _write_ledger(tmp_path, receipt)
    ledger = tmp_path / rq.LEDGER_NAME

    class _AlwaysFailingOIM:
        def add_item_programmatic(self, **kwargs):
            raise RuntimeError("store still unreachable")

    result = rq.reconcile_oi_pending(
        ledger, now=now, max_age_days=7.0, open_items_manager_module=_AlwaysFailingOIM(),
    )
    assert result["still_pending"] == 1
    assert result["escalated"] == []


def test_reconcile_oi_pending_missing_code_is_skipped_not_crashed(tmp_path):
    receipt = _v2_with("d1", timestamp="2026-07-22T01:00:00Z")
    receipt["warnings"] = [{
        "severity": "blocker", "message": "m", "destination": "oi_pending",
        "oi_id": None, "reason": "err", "requires_tracking": True,
    }]
    _write_ledger(tmp_path, receipt)
    ledger = tmp_path / rq.LEDGER_NAME

    result = rq.reconcile_oi_pending(ledger, open_items_manager_module=_NeverCalledOIM())
    assert result["scanned"] == 1
    assert result["reconciled"] == 0
    assert result["still_pending"] == 1


def test_reconcile_oi_pending_cli(tmp_path, capsys):
    oim = _load_oim(tmp_path)
    receipt = _oi_pending_receipt("d1", "some_check", "2026-07-22T01:00:00Z")
    _write_ledger(tmp_path, receipt)

    with patch.object(rq, "_load_open_items_manager", return_value=oim):
        rc = rq.main(["reconcile-oi-pending", "--state-dir", str(tmp_path), "--json"])
    assert rc == 0
    # open_items_manager.add_item_programmatic prints its own "Digest updated"
    # line as a side effect (generate_digest()) — parse from the JSON's own
    # opening brace so that real, unrelated stdout noise doesn't fail the test.
    raw = capsys.readouterr().out
    out = json.loads(raw[raw.index("{"):])
    assert out["scanned"] == 1
    assert out["reconciled"] == 1
