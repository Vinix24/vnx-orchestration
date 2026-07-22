#!/usr/bin/env python3
"""ADR-035 §9 PR-6 — receipt_query.py: pull + by-dispatch.

Covers T12 (pull read-then-advance; a concurrent append's partial trailing line
is never consumed until it is newline-terminated) and T13 (pull --seed-now sets
the cursor to EOF; the backlog is skipped but stays on disk and readable via
by-dispatch), plus the surrounding cursor mechanics absorbed from the parked
receipt_pull.py design (branch feat/receipt-mailbox-delivery, commit 54089155)
and mixed v1/v2 ledger tolerance for both subcommands.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict

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
