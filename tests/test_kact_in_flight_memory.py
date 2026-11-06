"""ADR-045 audit-spoor-kern K-act (OI-2559 point 2): ``v3_in_flight`` streams the ledger.

The lifecycle sweep runs before every real fire of the door, and the K-9 count
calls ``v3_in_flight`` too. The pre-K-act reader held the whole ledger blob and
every parsed record at once: 586 MB (main) / 976 MB (K-9c) on the live 183 MB
ledger. This file pins the two properties of the streaming rewrite:

1. bounded memory through the real entry point (``dispatch_lifecycle.sweep``,
   lifecycle off, the way the door calls it): the traced peak stays under 8 MB
   for a ~20 MB ledger with 400 50 KB records, and the sweep still counts the
   one attempt in flight. On the pre-change code the peak assertion fails while
   every record is kept — that is the red run in the completion report.
2. the same answer: ``v3_in_flight`` returns the same attempts, in the same
   order, with the same error text and order as the K-9c version, over a ledger
   mixing starts, ends, an unkeyable start, a sealed torn fragment, an interior
   broken line and a torn final fragment.
3. ``ndjson_io.scan_ndjson_stream`` yields exactly the tuples
   ``scan_ndjson_lines(handle.read())`` yields for the same bytes.

Every ledger is a real file under ``tmp_path``; the reader is never mocked.
"""

from __future__ import annotations

import json
import sys
import tracemalloc
from pathlib import Path

import pytest

VNX_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(VNX_ROOT / "scripts" / "lib"))

import dispatch_lifecycle as lifecycle  # noqa: E402
import ndjson_io  # noqa: E402

PROJECT = "vnx-dev"

#: The bound the dispatch sets: well above one 50 KB line, far below any
#: structure that keeps the 20 MB ledger or its 400 parsed records.
PEAK_LIMIT_BYTES = 8 * 1024 * 1024


def _line(record: dict) -> bytes:
    return json.dumps(record, separators=(",", ":")).encode("utf-8") + b"\n"


def _state_dir(tmp_path: Path) -> Path:
    state = tmp_path / "state"
    state.mkdir(parents=True, exist_ok=True)
    return state


def _write(path: Path, chunks: list) -> None:
    with path.open("wb") as handle:
        for chunk in chunks:
            handle.write(chunk)


# ---------------------------------------------------------------------------
# 1. bounded memory through the real entry point
# ---------------------------------------------------------------------------

def test_the_sweep_reads_a_big_ledger_without_keeping_it(tmp_path, monkeypatch):
    """The red-run test: on the pre-change code the peak fails, not the import."""
    monkeypatch.delenv("VNX_LEDGER_LIFECYCLE", raising=False)
    state = _state_dir(tmp_path)
    ledger = state / "t0_receipts.ndjson"

    unfinished = {"event_type": "dispatch_started", "lifecycle_mode": "v3",
                  "project_id": PROJECT, "dispatch_id": "d-live", "attempt": 1}
    finished_start = {"event_type": "dispatch_started", "lifecycle_mode": "v3",
                      "project_id": PROJECT, "dispatch_id": "d-done", "attempt": 1}
    finished_end = {"event_type": "dispatch_finished", "project_id": PROJECT,
                    "dispatch_id": "d-done", "attempt": 1, "end_state": "success"}

    def _chunks():
        yield _line(unfinished)
        yield _line(finished_start)
        yield _line(finished_end)
        for index in range(400):
            yield _line({"event_type": "task_complete", "project_id": PROJECT,
                         "n": index, "note": "x" * 50_000})

    _write(ledger, list(_chunks()))
    assert ledger.stat().st_size > 19 * 1024 * 1024  # the size the old reader held whole

    tracemalloc.start()
    try:
        result = lifecycle.sweep(state_dir=state, project_id=PROJECT, budget_seconds=2.0)
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()

    print(f"traced peak: {peak} bytes over a {ledger.stat().st_size} byte ledger")
    assert result.lifecycle_off is True
    assert result.in_flight == 1
    assert result.lifecycle_off_in_flight == 1
    assert peak < PEAK_LIMIT_BYTES, (
        f"traced peak {peak} bytes over a {ledger.stat().st_size} byte ledger: "
        "the sweep is still holding more than one line")


# ---------------------------------------------------------------------------
# 2. the same answer as the K-9c reader
# ---------------------------------------------------------------------------

def test_the_attempts_and_errors_are_exactly_the_k9c_answer(tmp_path):
    state = _state_dir(tmp_path)
    ledger = state / "t0_receipts.ndjson"

    start_a = {"event_type": "dispatch_started", "lifecycle_mode": "v3",
               "project_id": "p", "dispatch_id": "d-a", "attempt": 1}
    end_b = {"event_type": "dispatch_finished", "project_id": "p",
             "dispatch_id": "d-b", "attempt": 1, "end_state": "success"}
    start_b = {"event_type": "dispatch_started", "lifecycle_mode": "v3",
               "project_id": "p", "dispatch_id": "d-b", "attempt": 1}
    unkeyable = {"event_type": "dispatch_started", "lifecycle_mode": "v3",
                 "project_id": "p", "dispatch_id": "d-c", "attempt": "nope"}
    sealed = (b'{"event_type": "dispatch_started", "lifecycle_mode": "v3"'
              b'\t#vnx-torn-tail-closed\n')
    interior_broken = b'{not json\n'
    torn_tail = b'{"event_type": "dispatch_started", "lifecycle_mode": "v3", "project_id": "p"'

    _write(ledger, [
        _line(start_a),      # line 1: in flight
        _line(end_b),        # line 2: ends d-b
        _line(start_b),      # line 3: already ended, not in flight
        _line(unkeyable),    # line 4: unkeyable start
        sealed,              # line 5: sealed debris, skipped
        interior_broken,     # line 6: interior corruption, named
        torn_tail,           # torn final fragment, skipped
    ])

    attempts, errors = lifecycle.v3_in_flight(state, "p")

    assert [(a.project_id, a.dispatch_id, a.attempt) for a in attempts] == [
        ("p", "d-a", 1)]
    assert attempts[0].start_line == start_a
    assert errors == (
        "ledger_corrupt line 6",
        "unkeyable dispatch_started: ('p', 'd-c') attempt='nope'",
    )


def test_an_attempt_ended_by_another_project_is_still_in_flight(tmp_path):
    """The key is the whole (project, dispatch, attempt) triple — not the dispatch id."""
    state = _state_dir(tmp_path)
    ledger = state / "t0_receipts.ndjson"
    start = {"event_type": "dispatch_started", "lifecycle_mode": "v3",
             "project_id": "p", "dispatch_id": "d-1", "attempt": 1}
    other_end = {"event_type": "dispatch_finished", "project_id": "other",
                 "dispatch_id": "d-1", "attempt": 1, "end_state": "success"}
    _write(ledger, [_line(start), _line(other_end)])

    attempts, errors = lifecycle.v3_in_flight(state, "p")

    assert [(a.project_id, a.dispatch_id, a.attempt) for a in attempts] == [("p", "d-1", 1)]
    assert errors == ()


def test_a_missing_ledger_is_still_no_attempts_and_no_errors(tmp_path):
    attempts, errors = lifecycle.v3_in_flight(_state_dir(tmp_path), PROJECT)
    assert attempts == ()
    assert errors == ()


# ---------------------------------------------------------------------------
# 3. the stream reader is the blob reader
# ---------------------------------------------------------------------------

def _normalise(verdicts):
    """Exceptions never compare equal across two runs; compare their type and text."""
    return [(number, raw, record,
             None if error is None else type(error).__name__,
             None if error is None else str(error), torn)
            for number, raw, record, error, torn in verdicts]


@pytest.mark.parametrize("data", [
    b'{"a": 1}\n{"b": 2}\n',                      # ends in a newline
    b'{"a": 1}\n{"b": ',                           # torn final fragment
    b'{"a": 1}\n{oops\n',                          # broken final line, newline-terminated
    b'',                                           # empty file
], ids=["terminated", "torn-tail", "interior-final-line", "empty"])
def test_the_stream_yields_exactly_what_the_blob_yields(tmp_path, data):
    path = tmp_path / "ledger.ndjson"
    path.write_bytes(data)

    with path.open("rb") as handle:
        streamed = _normalise(list(ndjson_io.scan_ndjson_stream(handle)))
    blobbed = _normalise(list(ndjson_io.scan_ndjson_lines(path.read_bytes())))

    assert streamed == blobbed
