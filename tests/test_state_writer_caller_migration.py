from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts" / "lib"))

import cleanup_worker_exit as cwe  # noqa: E402
import gate_register_emit as gre  # noqa: E402
import state_writer  # noqa: E402


def test_gate_register_emit_uses_append_locked(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "dispatch_register.ndjson"
    captured: list[tuple[Path, dict]] = []

    monkeypatch.setattr(gre, "_resolve_register_path", lambda: path)
    monkeypatch.setattr(
        state_writer,
        "append_locked",
        lambda file_path, record: captured.append((file_path, record)),
    )

    result = gre.emit_codex_gate_to_register(
        "gate_passed",
        dispatch_id="dispatch-123",
        pr_number=42,
        pr_id="42",
        gate="codex_gate",
    )

    assert result is True
    assert len(captured) == 1
    file_path, record = captured[0]
    assert file_path == path
    assert record["event"] == "gate_passed"
    assert record["gate"] == "codex_gate"
    assert record["dispatch_id"] == "dispatch-123"
    assert record["pr_number"] == 42
    assert record["timestamp"].endswith("Z")


def test_cleanup_worker_exit_uses_append_locked(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "dispatch_register.ndjson"
    captured: list[tuple[Path, dict]] = []
    result = cwe.CleanupResult(
        lease_released=True,
        worker_transitioned=False,
        dispatch_moved=tmp_path / "completed" / "dispatch-123.md",
    )

    monkeypatch.setattr(cwe, "_resolve_dispatch_register_path", lambda: path)
    monkeypatch.setattr(
        state_writer,
        "append_locked",
        lambda file_path, record: captured.append((file_path, record)),
    )

    cwe._append_audit_event_step(
        terminal_id="T2",
        dispatch_id="dispatch-123",
        exit_status="success",
        result=result,
    )

    assert result.errors == []
    assert len(captured) == 1
    file_path, record = captured[0]
    assert file_path == path
    assert record["event"] == "worker_exited"
    assert record["dispatch_id"] == "dispatch-123"
    assert record["terminal_id"] == "T2"
    assert record["exit_status"] == "success"
    assert record["lease_released"] is True
    assert record["worker_transitioned"] is False
    assert record["dispatch_moved"] == str(tmp_path / "completed" / "dispatch-123.md")
    assert record["timestamp"].endswith("Z")


def test_callers_no_longer_use_direct_open_append() -> None:
    migrated_files = [
        ROOT / "scripts" / "lib" / "gate_register_emit.py",
        ROOT / "scripts" / "lib" / "cleanup_worker_exit.py",
    ]

    for path in migrated_files:
        text = path.read_text(encoding="utf-8")
        assert 'with path.open("a", encoding="utf-8") as fh:' not in text
        assert 'fh.write(json.dumps(record, separators=(",", ":")) + "\\n")' not in text
        assert "state_writer.append_locked(path, record)" in text
