from __future__ import annotations

import json
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts" / "lib"))

import state_writer as SW  # noqa: E402


def test_append_locked_writes_single_record(tmp_path: Path):
    path = tmp_path / "state.ndjson"

    SW.append_locked(path, {"event": "dispatch_created", "dispatch_id": "d-001"})

    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0]) == {
        "event": "dispatch_created",
        "dispatch_id": "d-001",
    }


def test_append_locked_concurrent_100_threads_100_writes(tmp_path: Path):
    path = tmp_path / "state.ndjson"
    start = threading.Event()

    def _worker(index: int) -> None:
        start.wait(timeout=5)
        for seq in range(100):
            SW.append_locked(path, {"thread": index, "seq": seq})

    threads = [
        threading.Thread(target=_worker, args=(index,), daemon=True)
        for index in range(100)
    ]

    for thread in threads:
        thread.start()

    start.set()

    for thread in threads:
        thread.join(timeout=10)
        assert not thread.is_alive(), "worker thread did not finish"

    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 10000
    for line in lines:
        parsed = json.loads(line)
        assert isinstance(parsed, dict)


def test_sentinel_registry_historical_names(tmp_path: Path):
    path = tmp_path / "dispatch_register.ndjson"

    assert SW._sentinel_path(path) == tmp_path / ".state.lock"


def test_sentinel_registry_default_pattern(tmp_path: Path):
    path = tmp_path / "foo.ndjson"

    assert SW._sentinel_path(path) == tmp_path / ".foo.ndjson.sentinel.lock"
