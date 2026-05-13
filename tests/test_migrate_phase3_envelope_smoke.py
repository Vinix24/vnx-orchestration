import pytest
from pathlib import Path
from scripts.migrate_phase3_envelope import _migrate_envelope_atomically, _lock_path_for


def test_migrate_smoke(tmp_path):
    envelope = tmp_path / "envelope.ndjson"
    envelope.write_text('{"event": "dispatch_promoted", "dispatch_id": "d1"}\n')
    # Migration should complete without error
    _migrate_envelope_atomically(envelope)
    # File still exists, still valid NDJSON
    content = envelope.read_text()
    assert content
    assert "d1" in content


def test_state_lock_used_for_dispatch_register(tmp_path):
    # dispatch_register.ndjson must use .state.lock (same as live writers)
    dr = tmp_path / "dispatch_register.ndjson"
    dr.write_text('{"event": "dispatch_created", "dispatch_id": "d2"}\n')
    _migrate_envelope_atomically(dr)
    assert _lock_path_for(dr) == dr.parent / ".state.lock"
    # .state.lock is created (opened a+) by the migrator
    assert (tmp_path / ".state.lock").exists()
    # No migration.lock sentinel should be created
    assert not (tmp_path / f".{dr.name}.migration.lock").exists()


def test_append_receipt_lock_used_for_t0_receipts(tmp_path):
    # t0_receipts.ndjson must use append_receipt.lock (same as receipt appenders)
    receipts = tmp_path / "t0_receipts.ndjson"
    receipts.write_text('{"receipt_id": "r1"}\n')
    _migrate_envelope_atomically(receipts)
    assert _lock_path_for(receipts) == receipts.parent / "append_receipt.lock"
    # append_receipt.lock is created (opened a+) by the migrator
    assert (tmp_path / "append_receipt.lock").exists()
    # No migration.lock sentinel should be created
    assert not (tmp_path / f".{receipts.name}.migration.lock").exists()
