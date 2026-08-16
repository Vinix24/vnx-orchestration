"""test_ledger_schema_version.py — direction-aware receipt-schema version reader.

Covers scripts/lib/ledger_schema_version.py, the read-side discipline for a
mixed-version append-only receipt ledger (ADR-005, ADR-035):

  - NEWER schema than the reader knows -> refuse loudly (ReceiptSchemaTooNewError)
  - OLDER schema -> convert in memory and read on, never write the conversion back
  - schema_version ABSENT -> oldest generation (version 0), not an error, convert
  - a read round never touches the ledger bytes (hash + mtime unchanged)
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "lib"))

import ledger_schema_version as lsv
from ledger_schema_version import (
    ABSENT_SCHEMA_VERSION,
    CURRENT_SCHEMA_VERSION,
    ReceiptSchemaMigrationMissingError,
    ReceiptSchemaTooNewError,
    ReceiptSchemaVersionError,
    iter_receipts,
    read_receipt,
    read_receipts,
    receipt_schema_version,
)


def _write_ledger(path: Path, records: list[dict]) -> Path:
    """Write records as compact NDJSON lines (the ledger writer's separators)."""
    path.write_text(
        "".join(json.dumps(r, separators=(",", ":")) + "\n" for r in records),
        encoding="utf-8",
    )
    return path


# ---------------------------------------------------------------------------
# version resolution
# ---------------------------------------------------------------------------

def test_absent_schema_version_is_version_zero():
    assert receipt_schema_version({}) == ABSENT_SCHEMA_VERSION
    assert receipt_schema_version({"dispatch_id": "d"}) == ABSENT_SCHEMA_VERSION
    assert receipt_schema_version({"schema_version": None}) == ABSENT_SCHEMA_VERSION
    assert ABSENT_SCHEMA_VERSION == 0


def test_schema_version_parses_int_and_int_string():
    assert receipt_schema_version({"schema_version": 2}) == 2
    assert receipt_schema_version({"schema_version": "2"}) == 2


def test_non_numeric_schema_version_raises_loudly():
    with pytest.raises(ReceiptSchemaVersionError, match="not an integer"):
        receipt_schema_version({"schema_version": "abc"})
    with pytest.raises(ReceiptSchemaVersionError, match="not an integer"):
        read_receipt({"schema_version": "abc"})


# ---------------------------------------------------------------------------
# the four dispatch requirements
# ---------------------------------------------------------------------------

def test_newer_schema_refuses_loudly():
    """A receipt stamped newer than the reader's CURRENT refuses, never guesses."""
    receipt = {"schema_version": CURRENT_SCHEMA_VERSION + 1, "dispatch_id": "d"}
    with pytest.raises(ReceiptSchemaTooNewError, match="newer"):
        read_receipt(receipt)


def test_older_schema_converts_in_memory():
    """v1 -> current: stamp bumps and the legacy ``event`` alias promotes to
    ``event_type`` (non-lossy — ``event`` is retained, history is not rewritten)."""
    receipt = {"schema_version": 1, "dispatch_id": "d", "event": "task_complete"}
    out = read_receipt(receipt)
    assert out["schema_version"] == CURRENT_SCHEMA_VERSION
    assert out["event_type"] == "task_complete"
    assert out["event"] == "task_complete"  # retained, not deleted


def test_missing_schema_version_counts_as_version_zero():
    """A receipt with no schema_version is the oldest generation: not an error,
    and it converts the full ladder (0 -> 1 -> current)."""
    receipt = {"dispatch_id": "d", "event": "task_complete"}
    assert receipt_schema_version(receipt) == 0
    out = read_receipt(receipt)
    assert out["schema_version"] == CURRENT_SCHEMA_VERSION
    assert out["event_type"] == "task_complete"


def test_reading_a_ledger_writes_nothing_back(tmp_path):
    """Hash + mtime of a mixed-version ledger are byte-identical after a full
    read round — the in-memory conversion is never persisted on read."""
    path = tmp_path / "t0_receipts.ndjson"
    records = [
        {"dispatch_id": "d-0", "event": "task_complete"},  # v0 (absent stamp)
        {"schema_version": 1, "dispatch_id": "d-1", "event": "task_complete"},  # v1
        {"schema_version": 2, "dispatch_id": "d-2", "event_type": "task_complete"},  # v2
    ]
    _write_ledger(path, records)

    before_bytes = path.read_bytes()
    before_hash = hashlib.sha256(before_bytes).hexdigest()
    before_mtime = path.stat().st_mtime_ns

    converted = read_receipts(path)

    assert len(converted) == 3
    assert all(r["schema_version"] == CURRENT_SCHEMA_VERSION for r in converted)
    assert path.read_bytes() == before_bytes
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before_hash
    assert path.stat().st_mtime_ns == before_mtime


# ---------------------------------------------------------------------------
# purity + robustness
# ---------------------------------------------------------------------------

def test_read_receipt_never_mutates_its_input():
    receipt = {"schema_version": 1, "dispatch_id": "d", "event": "task_complete"}
    snapshot = dict(receipt)
    out = read_receipt(receipt)
    assert receipt == snapshot  # input untouched
    assert out is not receipt
    assert out["schema_version"] == CURRENT_SCHEMA_VERSION


def test_equal_version_reads_as_a_copy():
    receipt = {"schema_version": 2, "dispatch_id": "d", "event_type": "task_complete"}
    out = read_receipt(receipt)
    assert out == receipt
    assert out is not receipt  # always a new dict, never the caller's


def test_missing_migration_step_raises(monkeypatch):
    """A gap in the ladder (no 1 -> 2 step) refuses instead of silently
    returning a half-converted receipt."""
    monkeypatch.delitem(lsv.SCHEMA_MIGRATIONS, 1)
    with pytest.raises(ReceiptSchemaMigrationMissingError, match="no migration registered"):
        read_receipt({"schema_version": 0, "dispatch_id": "d"})


def test_non_dict_receipt_raises_type_error():
    with pytest.raises(TypeError, match="must be a dict"):
        read_receipt(["not", "a", "dict"])  # type: ignore[arg-type]


def test_iter_receipts_streams_and_converts(tmp_path):
    path = tmp_path / "t0_receipts.ndjson"
    _write_ledger(
        path,
        [
            {"dispatch_id": "d-0", "event": "task_complete"},  # v0
            {"schema_version": 2, "dispatch_id": "d-1", "event_type": "task_complete"},  # v2
        ],
    )
    out = list(iter_receipts(path))
    assert len(out) == 2
    assert all(r["schema_version"] == CURRENT_SCHEMA_VERSION for r in out)
    assert out[0]["event_type"] == "task_complete"


def test_read_receipts_skips_torn_tail(tmp_path):
    """Built on ndjson_io: a partial final line from a crash mid-append is
    skipped, not raised, and the good records still convert."""
    path = tmp_path / "t0_receipts.ndjson"
    good = [{"schema_version": 2, "dispatch_id": "d-0", "event_type": "task_complete"}]
    _write_ledger(
        path,
        [
            good[0],
        ],
    )
    with open(path, "a", encoding="utf-8") as fh:
        fh.write('{"schema_version": 2, "dispatch_id": "d-1", "par')  # torn
    assert read_receipts(path) == good


# ---------------------------------------------------------------------------
# constant centralization (write path and reader share ONE source)
# ---------------------------------------------------------------------------

def test_write_path_and_reader_share_one_version_constant():
    """receipt_schema (Path 1) and payload.py (Path 2) both stamp from
    CURRENT_SCHEMA_VERSION — one bump site, monotonic everywhere."""
    from receipt_schema import RECEIPT_V2_SCHEMA_VERSION  # noqa: E402

    assert RECEIPT_V2_SCHEMA_VERSION == CURRENT_SCHEMA_VERSION
    assert CURRENT_SCHEMA_VERSION == 2  # ADR-035 cutover value
