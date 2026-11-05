"""test_ndjson_io.py — shared NDJSON durability (fsync) + torn-tail read guard.

Covers scripts/lib/ndjson_io.py:
  - read_ndjson / iter_ndjson skip a torn/partial final line from a crash
    mid-append instead of raising, and read every complete record otherwise.
    Their lenient mid-file skip is pinned here: other callers rely on it.
  - read_ndjson_strict tolerates only the torn tail: the final fragment of a
    file that does not end in a newline. Any other unparseable line — including
    a newline-terminated final one — is interior corruption and refuses with
    ``NdjsonCorruptionError`` naming the line and the file. An optional
    predicate scopes that refusal to raw lines a caller can own.
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
from ndjson_io import (NdjsonCorruptionError, fsync_fileno, iter_ndjson, read_ndjson,
                       read_ndjson_strict)


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
# strict reader: the torn tail is tolerated, an interior hole refuses
# ---------------------------------------------------------------------------

def test_strict_reader_matches_read_ndjson_on_a_clean_file(tmp_path):
    path = tmp_path / "t0_receipts.ndjson"
    records = [{"dispatch_id": f"d-{i}", "status": "success"} for i in range(3)]
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")

    assert read_ndjson_strict(path) == read_ndjson(path) == records


def test_strict_reader_matches_read_ndjson_on_a_torn_tail(tmp_path):
    path = tmp_path / "t0_receipts.ndjson"
    good = [{"dispatch_id": "d-0"}, {"dispatch_id": "d-1"}]
    path.write_text(
        json.dumps(good[0]) + "\n" + json.dumps(good[1]) + "\n" + '{"dispatch_id": "d-2", "par',
        encoding="utf-8",
    )

    assert read_ndjson_strict(path) == read_ndjson(path) == good


def test_strict_reader_skips_blank_lines(tmp_path):
    path = tmp_path / "t0_receipts.ndjson"
    path.write_text(
        json.dumps({"dispatch_id": "d-0"}) + "\n\n   \n" + json.dumps({"dispatch_id": "d-1"}) + "\n",
        encoding="utf-8",
    )
    assert read_ndjson_strict(path) == [{"dispatch_id": "d-0"}, {"dispatch_id": "d-1"}]


def test_strict_reader_keeps_a_final_record_without_a_trailing_newline(tmp_path):
    path = tmp_path / "t0_receipts.ndjson"
    path.write_text(
        json.dumps({"dispatch_id": "d-0"}) + "\n" + json.dumps({"dispatch_id": "d-1"}),
        encoding="utf-8",
    )
    assert read_ndjson_strict(path) == [{"dispatch_id": "d-0"}, {"dispatch_id": "d-1"}]


def test_strict_reader_refuses_an_interior_broken_line_with_its_number(tmp_path):
    path = tmp_path / "t0_receipts.ndjson"
    path.write_text(
        json.dumps({"dispatch_id": "d-0"}) + "\n"
        + '{"dispatch_id": "d-1", "event_ty\n'   # line 2: interior corruption
        + json.dumps({"dispatch_id": "d-2"}) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(NdjsonCorruptionError) as exc:
        read_ndjson_strict(path)

    assert exc.value.line_number == 2
    assert exc.value.path == path
    assert "line 2" in str(exc.value)
    assert str(path) in str(exc.value)


def test_strict_reader_refuses_a_broken_first_line_when_another_follows(tmp_path):
    path = tmp_path / "t0_receipts.ndjson"
    path.write_text("{broken\n" + json.dumps({"dispatch_id": "d-1"}) + "\n", encoding="utf-8")

    with pytest.raises(NdjsonCorruptionError) as exc:
        read_ndjson_strict(path)

    assert exc.value.line_number == 1


def test_strict_reader_refuses_an_undecodable_interior_byte(tmp_path):
    """A byte that is not UTF-8 is unparseable too — never papered over with U+FFFD."""
    path = tmp_path / "t0_receipts.ndjson"
    path.write_bytes(
        json.dumps({"dispatch_id": "d-0"}).encode("utf-8") + b"\n"
        + b'{"dispatch_id": "d-\xff1"}\n'
        + json.dumps({"dispatch_id": "d-2"}).encode("utf-8") + b"\n"
    )

    with pytest.raises(NdjsonCorruptionError) as exc:
        read_ndjson_strict(path)

    assert exc.value.line_number == 2


def test_strict_reader_refuses_a_broken_final_line_that_ends_in_a_newline(tmp_path):
    """A newline-terminated final line was written completely: committed, not torn."""
    path = tmp_path / "t0_receipts.ndjson"
    path.write_text(
        json.dumps({"dispatch_id": "d-0"}) + "\n"
        + '{"dispatch_id": "d-1", "event_ty\n',
        encoding="utf-8",
    )

    with pytest.raises(NdjsonCorruptionError) as exc:
        read_ndjson_strict(path)

    assert exc.value.line_number == 2


def test_strict_reader_refuses_an_undecodable_final_line_that_ends_in_a_newline(tmp_path):
    """Codex's reproduction: a newline-terminated final byte break is interior corruption."""
    path = tmp_path / "t0_receipts.ndjson"
    path.write_bytes(
        json.dumps({"dispatch_id": "d-0"}).encode("utf-8") + b"\n"
        + b'{"dispatch_id": "d-\xff1"}\n'
    )

    with pytest.raises(NdjsonCorruptionError) as exc:
        read_ndjson_strict(path)

    assert exc.value.line_number == 2


def test_strict_reader_refuses_a_committed_break_before_a_whitespace_only_tail(tmp_path):
    """Only the final physical fragment can be torn: trailing spaces never rescue a break."""
    path = tmp_path / "t0_receipts.ndjson"
    path.write_text('{"dispatch_id":"d-own", broken\n   ', encoding="utf-8")

    with pytest.raises(NdjsonCorruptionError) as exc:
        read_ndjson_strict(path)

    assert exc.value.line_number == 1

    terminated = tmp_path / "terminated.ndjson"
    terminated.write_text('{"dispatch_id":"d-own", broken\n', encoding="utf-8")
    with pytest.raises(NdjsonCorruptionError) as terminated_exc:
        read_ndjson_strict(terminated)
    assert terminated_exc.value.line_number == 1


def test_strict_reader_skips_a_real_torn_tail(tmp_path):
    path = tmp_path / "t0_receipts.ndjson"
    path.write_text('{"a": 1}\n{"dispatch_id": "x", brok', encoding="utf-8")

    assert read_ndjson_strict(path) == [{"a": 1}]


def test_strict_reader_keeps_records_before_a_whitespace_only_final_fragment(tmp_path):
    path = tmp_path / "t0_receipts.ndjson"
    path.write_text('{"a": 1}\n   ', encoding="utf-8")

    assert read_ndjson_strict(path) == [{"a": 1}]


def test_strict_reader_reads_an_unterminated_valid_final_line(tmp_path):
    path = tmp_path / "t0_receipts.ndjson"
    path.write_text('{"a": 1}\n{"b": 2}', encoding="utf-8")

    assert read_ndjson_strict(path) == [{"a": 1}, {"b": 2}]


def test_strict_reader_with_a_predicate_skips_another_dispatches_broken_line(
        tmp_path, caplog):
    path = tmp_path / "t0_receipts.ndjson"
    path.write_text(
        json.dumps({"dispatch_id": "d-0"}) + "\n"
        + '{"dispatch_id": "d-other", "event_ty\n'
        + json.dumps({"dispatch_id": "d-2"}) + "\n",
        encoding="utf-8",
    )

    with caplog.at_level(logging.WARNING, logger="ndjson_io"):
        out = read_ndjson_strict(path, lambda raw: "d-0" in raw)

    assert out == [{"dispatch_id": "d-0"}, {"dispatch_id": "d-2"}]
    assert any("malformed" in record.message.lower() for record in caplog.records)


def test_strict_reader_with_a_predicate_refuses_when_the_raw_line_holds(tmp_path):
    path = tmp_path / "t0_receipts.ndjson"
    path.write_text(
        json.dumps({"dispatch_id": "d-0"}) + "\n"
        + '{"dispatch_id": "d-1", "event_ty\n'
        + json.dumps({"dispatch_id": "d-2"}) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(NdjsonCorruptionError) as exc:
        read_ndjson_strict(path, lambda raw: "d-1" in raw)

    assert exc.value.line_number == 2


def test_strict_reader_predicate_never_rescues_a_torn_tail(tmp_path):
    """The predicate scopes interior corruption only; the unterminated tail stays skipped."""
    path = tmp_path / "t0_receipts.ndjson"
    path.write_text(
        json.dumps({"dispatch_id": "d-0"}) + "\n"
        + '{"dispatch_id": "d-1", "event_ty',
        encoding="utf-8",
    )

    assert read_ndjson_strict(path, lambda raw: "d-1" in raw) == [{"dispatch_id": "d-0"}]


def test_strict_reader_missing_file_yields_nothing(tmp_path):
    assert read_ndjson_strict(tmp_path / "does-not-exist.ndjson") == []


def test_strict_reader_refuses_a_final_broken_line_that_ends_in_a_newline(tmp_path):
    """A final line written completely is committed corruption, not a torn tail."""
    path = tmp_path / "t0_receipts.ndjson"
    path.write_bytes(
        json.dumps({"dispatch_id": "d-0"}).encode("utf-8") + b"\n"
        + b'{"dispatch_id": "d-1", "event_type"\n')  # line 2, terminated by a newline

    with pytest.raises(NdjsonCorruptionError) as exc:
        read_ndjson_strict(path)

    assert exc.value.line_number == 2


def test_strict_reader_refuses_a_final_undecodable_line_that_ends_in_a_newline(tmp_path):
    """Codex on #2373: a complete final record carrying invalid UTF-8 is corruption."""
    path = tmp_path / "t0_receipts.ndjson"
    path.write_bytes(
        json.dumps({"dispatch_id": "d-0"}).encode("utf-8") + b"\n"
        + b'{"dispatch_id": "d-\xff1", "event_type": "dispatch_started"}\n')

    with pytest.raises(NdjsonCorruptionError) as exc:
        read_ndjson_strict(path)

    assert exc.value.line_number == 2


def test_tolerates_an_unterminated_final_fragment_that_names_a_dispatch(tmp_path):
    path = tmp_path / "t0_receipts.ndjson"
    path.write_bytes(
        json.dumps({"dispatch_id": "d-0"}).encode("utf-8") + b"\n"
        + b'{"dispatch_id": "d-1", "event_ty')  # no trailing newline: torn

    assert read_ndjson_strict(
        path, predicate=lambda raw: "d-1" in raw) == [{"dispatch_id": "d-0"}]


def test_the_predicate_raises_only_for_the_line_it_names(tmp_path, caplog):
    """The hot-path scope: a broken line of this dispatch refuses; another's is skipped."""
    path = tmp_path / "t0_receipts.ndjson"
    path.write_bytes(
        json.dumps({"dispatch_id": "d-0"}).encode("utf-8") + b"\n"
        + b'{"dispatch_id": "d-1", "event_type\n'  # interior, carries d-1
        + json.dumps({"dispatch_id": "d-2"}).encode("utf-8") + b"\n")

    with pytest.raises(NdjsonCorruptionError) as exc:
        read_ndjson_strict(path, predicate=lambda raw: "d-1" in raw)
    assert exc.value.line_number == 2

    with caplog.at_level(logging.WARNING, logger="ndjson_io"):
        assert read_ndjson_strict(
            path, predicate=lambda raw: "d-9" in raw) == [
            {"dispatch_id": "d-0"}, {"dispatch_id": "d-2"}]
    assert any("malformed" in record.message.lower() for record in caplog.records)


def test_the_bytes_form_applies_the_same_rule(tmp_path):
    good = json.dumps({"dispatch_id": "d-0"}).encode("utf-8")
    broken = b'{"dispatch_id": "d-1", "event_type'

    assert ndjson_io.read_ndjson_strict_bytes(good + b"\n" + broken) == [{"dispatch_id": "d-0"}]
    with pytest.raises(NdjsonCorruptionError):
        ndjson_io.read_ndjson_strict_bytes(good + b"\n" + broken + b"\n")
    assert ndjson_io.read_ndjson_strict_bytes(good + b"\n" + broken, source="blob",
        predicate=lambda raw: "d-9" in raw) == [{"dispatch_id": "d-0"}]


def test_iter_ndjson_still_skips_an_interior_broken_line_unchanged(tmp_path):
    """Pinned: the lenient reader keeps its mid-file skip; only the strict one refuses."""
    path = tmp_path / "t0_receipts.ndjson"
    path.write_text(
        json.dumps({"dispatch_id": "d-0"}) + "\n"
        + "{not valid json\n"
        + json.dumps({"dispatch_id": "d-2"}) + "\n",
        encoding="utf-8",
    )
    assert read_ndjson(path) == [{"dispatch_id": "d-0"}, {"dispatch_id": "d-2"}]
    with pytest.raises(NdjsonCorruptionError):
        read_ndjson_strict(path)


# ---------------------------------------------------------------------------
# K-9c round 3: a closed crash fragment is debris; torn is the last fragment
# ---------------------------------------------------------------------------

def _closed(fragment: bytes = b'{"dispatch_id": "d-1", "event_ty') -> bytes:
    """A fragment the append closer sealed: its own bytes, the sentinel, one newline."""
    return fragment + ndjson_io.TORN_TAIL_CLOSED + b"\n"


def test_the_strict_reader_skips_a_closed_fragment_instead_of_refusing(tmp_path, caplog):
    """A write that never completed is no evidence, even when it names this dispatch."""
    path = tmp_path / "t0_receipts.ndjson"
    path.write_bytes(json.dumps({"dispatch_id": "d-0"}).encode("utf-8") + b"\n"
                     + _closed() + json.dumps({"dispatch_id": "d-2"}).encode("utf-8") + b"\n")

    with caplog.at_level(logging.WARNING, logger="ndjson_io"):
        assert read_ndjson_strict(path, predicate=lambda raw: "d-1" in raw) == [
            {"dispatch_id": "d-0"}, {"dispatch_id": "d-2"}]
    assert any("closed torn fragment" in record.message for record in caplog.records)


def test_the_bytes_form_reads_a_closed_fragment_as_debris():
    good = json.dumps({"dispatch_id": "d-0"}).encode("utf-8")
    blob = good + b"\n" + _closed() + good + b"\n"

    assert ndjson_io.read_ndjson_strict_bytes(blob, predicate=lambda raw: "d-1" in raw) == [
        {"dispatch_id": "d-0"}, {"dispatch_id": "d-0"}]


def test_the_tolerant_reader_skips_a_closed_fragment_with_a_warning(tmp_path, caplog):
    path = tmp_path / "t0_receipts.ndjson"
    path.write_bytes(json.dumps({"dispatch_id": "d-0"}).encode("utf-8") + b"\n" + _closed())

    with caplog.at_level(logging.WARNING, logger="ndjson_io"):
        assert read_ndjson(path) == [{"dispatch_id": "d-0"}]
    assert any("closed torn fragment" in record.message for record in caplog.records)


def test_only_a_line_ending_in_exactly_the_sentinel_is_debris(tmp_path):
    """Bytes after the marker are not the closer's seal: that line stays corruption."""
    path = tmp_path / "t0_receipts.ndjson"
    path.write_bytes(json.dumps({"dispatch_id": "d-0"}).encode("utf-8") + b"\n"
                     + b'{"dispatch_id": "d-1", "event_ty' + ndjson_io.TORN_TAIL_CLOSED
                     + b" x\n" + json.dumps({"dispatch_id": "d-2"}).encode("utf-8") + b"\n")

    with pytest.raises(NdjsonCorruptionError):
        read_ndjson_strict(path, predicate=lambda raw: "d-1" in raw)


def test_a_newline_followed_by_whitespace_terminated_the_broken_record(tmp_path):
    """Rule 2: only the last split element can be torn; whitespace is no torn fragment."""
    path = tmp_path / "t0_receipts.ndjson"
    path.write_bytes(json.dumps({"dispatch_id": "d-0"}).encode("utf-8") + b"\n"
                     + b'{"dispatch_id": "d-1", "event_ty\n   ')

    with pytest.raises(NdjsonCorruptionError) as exc:
        read_ndjson_strict(path, predicate=lambda raw: "d-1" in raw)

    assert exc.value.line_number == 2


def test_the_same_broken_record_with_no_newline_at_all_is_torn(tmp_path):
    path = tmp_path / "t0_receipts.ndjson"
    path.write_bytes(json.dumps({"dispatch_id": "d-0"}).encode("utf-8") + b"\n"
                     + b'{"dispatch_id": "d-1", "event_ty')

    assert read_ndjson_strict(path, predicate=lambda raw: "d-1" in raw) == [
        {"dispatch_id": "d-0"}]


def test_the_bytes_form_makes_the_same_whitespace_tail_decision():
    good = json.dumps({"dispatch_id": "d-0"}).encode("utf-8")
    broken = b'{"dispatch_id": "d-1", "event_ty'

    with pytest.raises(NdjsonCorruptionError):
        ndjson_io.read_ndjson_strict_bytes(good + b"\n" + broken + b"\n   ")
    assert ndjson_io.read_ndjson_strict_bytes(good + b"\n" + broken) == [{"dispatch_id": "d-0"}]


def test_the_tolerant_reader_makes_the_same_whitespace_tail_decision(tmp_path, caplog):
    path = tmp_path / "t0_receipts.ndjson"
    path.write_bytes(json.dumps({"dispatch_id": "d-0"}).encode("utf-8") + b"\n"
                     + b'{"dispatch_id": "d-1", "event_ty\n   ')

    with caplog.at_level(logging.WARNING, logger="ndjson_io"):
        assert read_ndjson(path) == [{"dispatch_id": "d-0"}]
    assert any("malformed" in record.message.lower() for record in caplog.records)


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
