"""test_ndjson_io.py — shared NDJSON durability (fsync) + torn-tail read guard.

Covers scripts/lib/ndjson_io.py:
  - read_ndjson / iter_ndjson skip a torn/partial final line from a crash
    mid-append instead of raising, and read every complete record otherwise.
  - fsync_fileno is best-effort: it syncs a healthy handle and degrades to a
    logged warning (returns False, never raises) when the fs rejects fsync.
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "lib"))

import ndjson_io
from ndjson_io import fsync_fileno, iter_ndjson, read_ndjson


# ---------------------------------------------------------------------------
# read guard
# ---------------------------------------------------------------------------

def test_normal_multiline_reads_all_records(tmp_path):
    path = tmp_path / "t0_receipts.ndjson"
    records = [{"dispatch_id": f"d-{i}", "status": "success"} for i in range(3)]
    path.write_text(
        "".join(json.dumps(r, separators=(",", ":")) + "\n" for r in records),
        encoding="utf-8",
    )
    assert read_ndjson(path) == records


def test_torn_tail_truncated_json_is_skipped(tmp_path):
    """A crash mid-append leaves a partial final line — read the complete ones only."""
    path = tmp_path / "t0_receipts.ndjson"
    good = [{"dispatch_id": "d-0"}, {"dispatch_id": "d-1"}]
    path.write_text(
        json.dumps(good[0], separators=(",", ":")) + "\n"
        + json.dumps(good[1], separators=(",", ":")) + "\n"
        # torn tail: writer crashed after part of the record, before the newline
        + '{"dispatch_id": "d-2", "par',
        encoding="utf-8",
    )
    out = read_ndjson(path)  # must not raise
    assert out == good


def test_torn_tail_missing_newline_but_valid_json_is_read(tmp_path):
    """A complete final record without a trailing newline still parses and is kept."""
    path = tmp_path / "t0_receipts.ndjson"
    path.write_text(
        json.dumps({"dispatch_id": "d-0"}, separators=(",", ":")) + "\n"
        + json.dumps({"dispatch_id": "d-1"}, separators=(",", ":")),  # no trailing \n
        encoding="utf-8",
    )
    assert read_ndjson(path) == [{"dispatch_id": "d-0"}, {"dispatch_id": "d-1"}]


def test_midfile_malformed_line_is_skipped_with_warning(tmp_path, caplog):
    """Genuine mid-file corruption is skipped (loud) — reader still returns the good records."""
    path = tmp_path / "t0_receipts.ndjson"
    path.write_text(
        json.dumps({"dispatch_id": "d-0"}, separators=(",", ":")) + "\n"
        + "{not valid json\n"
        + json.dumps({"dispatch_id": "d-2"}, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    with caplog.at_level(logging.WARNING, logger="ndjson_io"):
        out = read_ndjson(path)
    assert out == [{"dispatch_id": "d-0"}, {"dispatch_id": "d-2"}]
    assert any("malformed" in r.message.lower() for r in caplog.records)


def test_blank_lines_are_skipped(tmp_path):
    path = tmp_path / "t0_receipts.ndjson"
    path.write_text(
        json.dumps({"dispatch_id": "d-0"}, separators=(",", ":")) + "\n"
        + "\n   \n"
        + json.dumps({"dispatch_id": "d-1"}, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    assert read_ndjson(path) == [{"dispatch_id": "d-0"}, {"dispatch_id": "d-1"}]


def test_missing_file_yields_nothing(tmp_path):
    assert read_ndjson(tmp_path / "does-not-exist.ndjson") == []


def test_empty_file_yields_nothing(tmp_path):
    path = tmp_path / "empty.ndjson"
    path.write_text("", encoding="utf-8")
    assert read_ndjson(path) == []


def test_iter_ndjson_is_lazy(tmp_path):
    path = tmp_path / "t0_receipts.ndjson"
    path.write_text(json.dumps({"dispatch_id": "d-0"}) + "\n", encoding="utf-8")
    it = iter_ndjson(path)
    assert iter(it) is it  # a generator/iterator, not a materialized list


# ---------------------------------------------------------------------------
# fsync helper
# ---------------------------------------------------------------------------

def test_fsync_fileno_syncs_healthy_handle(tmp_path):
    path = tmp_path / "durable.ndjson"
    with open(path, "a", encoding="utf-8") as fh:
        fh.write("line\n")
        fh.flush()
        assert fsync_fileno(fh) is True


def test_fsync_fileno_degrades_on_oserror(tmp_path, monkeypatch, caplog):
    """A filesystem that rejects fsync must degrade to a warning, never raise."""
    def boom(_fd):
        raise OSError("fsync not supported on this filesystem")

    monkeypatch.setattr(ndjson_io.os, "fsync", boom)
    path = tmp_path / "durable.ndjson"
    with open(path, "a", encoding="utf-8") as fh:
        fh.write("line\n")
        fh.flush()
        with caplog.at_level(logging.WARNING, logger="ndjson_io"):
            result = fsync_fileno(fh, context="dispatch=x")  # must not raise
    assert result is False
    assert any("fsync failed" in r.message.lower() for r in caplog.records)
