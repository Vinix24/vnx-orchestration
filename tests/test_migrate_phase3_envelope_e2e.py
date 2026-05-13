from __future__ import annotations

import concurrent.futures
import hashlib
import json
import multiprocessing
import os
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))
sys.path.insert(0, str(REPO_ROOT / "scripts" / "lib"))

import migrate_phase3_envelope as migrator  # noqa: E402


ENVELOPE = {
    "operator_id": "op-test",
    "project_id": "proj-test",
    "orchestrator_id": "orch-test",
    "agent_id": "agent-test",
}


def _write_seed_records(path: Path, count: int) -> list[str]:
    path.parent.mkdir(parents=True, exist_ok=True)
    dispatch_ids = [f"seed-{index:04d}" for index in range(count)]
    with path.open("w", encoding="utf-8") as fh:
        for dispatch_id in dispatch_ids:
            fh.write(
                json.dumps(
                    {
                        "event": "dispatch_created",
                        "dispatch_id": dispatch_id,
                    },
                    separators=(",", ":"),
                )
                + "\n"
            )
    return dispatch_ids


def _append_records_worker(
    state_dir: str,
    start_at: float,
    worker_index: int,
    records_per_worker: int,
) -> list[str]:
    sys.path.insert(0, str(REPO_ROOT / "scripts" / "lib"))
    from dispatch_register import append_event

    os.environ["VNX_STATE_DIR"] = state_dir
    os.environ.pop("VNX_PROJECT_ID", None)
    os.environ.pop("VNX_OPERATOR_ID", None)
    os.environ.pop("VNX_ORCHESTRATOR_ID", None)
    os.environ.pop("VNX_AGENT_ID", None)

    while time.time() < start_at:
        time.sleep(0.001)

    written_ids: list[str] = []
    for seq in range(records_per_worker):
        dispatch_id = f"worker-{worker_index:02d}-{seq:03d}"
        ok = append_event("dispatch_created", dispatch_id=dispatch_id)
        if not ok:
            raise AssertionError(f"append_event returned False for {dispatch_id}")
        written_ids.append(dispatch_id)
    return written_ids


def _migrate_worker(ndjson_path: str, start_at: float, hold_lock_delay: float) -> int:
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    sys.path.insert(0, str(REPO_ROOT / "scripts" / "lib"))
    import migrate_phase3_envelope as _migrator

    while time.time() < start_at:
        time.sleep(0.001)

    return _migrator._restamp_ndjson_inplace(
        Path(ndjson_path),
        ENVELOPE,
        hold_lock_delay=hold_lock_delay,
    )


def _read_records(path: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def test_migrate_envelope_uses_state_writer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ndjson = tmp_path / "dispatch_register.ndjson"
    ndjson.write_text('{"event":"dispatch_created","dispatch_id":"d-001"}\n', encoding="utf-8")
    captured: list[tuple[Path, object]] = []

    def _fake_rewrite_locked(path: Path, writer: object) -> None:
        captured.append((path, writer))
        current_content = path.read_bytes()
        rewritten = writer(current_content) if callable(writer) else writer
        if rewritten != current_content:
            path.write_bytes(rewritten)

    monkeypatch.setattr(migrator.state_writer, "rewrite_locked", _fake_rewrite_locked)

    stamped = migrator._restamp_ndjson_inplace(ndjson, ENVELOPE)

    assert stamped == 1
    assert len(captured) == 1
    assert captured[0][0] == ndjson
    record = _read_records(ndjson)[0]
    assert record["dispatch_id"] == "d-001"
    assert record["project_id"] == ENVELOPE["project_id"]


def test_concurrent_writes_during_migration(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    ndjson = state_dir / "dispatch_register.ndjson"
    seed_ids = _write_seed_records(ndjson, count=200)

    appenders = 50
    records_per_appender = 20
    start_at = time.time() + 1.0
    ctx = multiprocessing.get_context("fork")

    appended_ids: list[str] = []
    with concurrent.futures.ProcessPoolExecutor(
        max_workers=appenders + 1,
        mp_context=ctx,
    ) as pool:
        append_futures = [
            pool.submit(
                _append_records_worker,
                str(state_dir),
                start_at,
                worker_index,
                records_per_appender,
            )
            for worker_index in range(appenders)
        ]
        migrate_future = pool.submit(
            _migrate_worker,
            str(ndjson),
            start_at,
            0.25,
        )

        for future in append_futures:
            appended_ids.extend(future.result(timeout=30))
        migrated_count = migrate_future.result(timeout=30)

    final_records = _read_records(ndjson)
    final_ids = [record["dispatch_id"] for record in final_records]
    expected_ids = seed_ids + appended_ids

    assert migrated_count >= len(seed_ids)
    assert len(appended_ids) == appenders * records_per_appender
    assert len(final_records) == len(expected_ids)
    assert len(final_ids) == len(set(final_ids))
    assert set(final_ids) == set(expected_ids)

    final_digest = hashlib.sha256("\n".join(sorted(final_ids)).encode("utf-8")).hexdigest()
    expected_digest = hashlib.sha256("\n".join(sorted(expected_ids)).encode("utf-8")).hexdigest()
    assert final_digest == expected_digest

    for record in final_records:
        assert isinstance(record, dict)

