import pytest
from pathlib import Path
from scripts.migrate_phase3_envelope import _migrate_envelope_atomically


def test_migrate_smoke(tmp_path):
    envelope = tmp_path / "envelope.ndjson"
    envelope.write_text('{"event": "dispatch_promoted", "dispatch_id": "d1"}\n')
    # Migration should complete without error
    _migrate_envelope_atomically(envelope)
    # File still exists, still valid NDJSON
    content = envelope.read_text()
    assert content
    assert "d1" in content


def test_sentinel_lock_created(tmp_path):
    envelope = tmp_path / "envelope.ndjson"
    envelope.write_text("{}\n")
    _migrate_envelope_atomically(envelope)
    lock_path = envelope.parent / f".{envelope.name}.migration.lock"
    assert lock_path.exists()
