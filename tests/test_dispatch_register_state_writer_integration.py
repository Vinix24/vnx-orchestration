from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts" / "lib"))

import dispatch_register  # noqa: E402


def test_append_event_delegates_to_state_writer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "dispatch_register.ndjson"
    captured: list[tuple[Path, dict]] = []

    monkeypatch.setattr(dispatch_register, "_resolve_register_path", lambda: path)
    monkeypatch.setattr(dispatch_register, "_resolve_identity_for_register", lambda: {})
    monkeypatch.setattr(dispatch_register, "_mirror_to_decision_log", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        dispatch_register.state_writer,
        "append_locked",
        lambda file_path, record: captured.append((file_path, record)),
    )

    result = dispatch_register.append_event("dispatch_created", dispatch_id="dispatch-123")

    assert result is True
    assert len(captured) == 1
    file_path, record = captured[0]
    assert file_path == path
    assert record["event"] == "dispatch_created"
    assert record["dispatch_id"] == "dispatch-123"
    assert record["timestamp"].endswith("Z")


def test_dispatch_register_public_api_preserved(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "dispatch_register.ndjson"

    monkeypatch.setattr(dispatch_register, "_resolve_register_path", lambda: path)
    monkeypatch.setattr(dispatch_register, "_resolve_identity_for_register", lambda: {})
    monkeypatch.setattr(dispatch_register, "_mirror_to_decision_log", lambda *args, **kwargs: None)

    result = dispatch_register.append_event(
        "gate_passed",
        dispatch_id="dispatch-legacy",
        pr_number=42,
        feature_id="OI-1370",
        terminal="T1",
        gate="codex_gate",
        extra={"source": "legacy-caller"},
    )

    assert result is True
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record == {
        "timestamp": record["timestamp"],
        "event": "gate_passed",
        "dispatch_id": "dispatch-legacy",
        "pr_number": 42,
        "feature_id": "OI-1370",
        "terminal": "T1",
        "gate": "codex_gate",
        "extra": {"source": "legacy-caller"},
    }
    assert record["timestamp"].endswith("Z")

