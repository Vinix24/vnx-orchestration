from __future__ import annotations

import fcntl
import multiprocessing
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "scripts" / "lib"))

import backfill_headless_receipts as bfr  # noqa: E402
import compact_state as cs  # noqa: E402
import state_writer  # noqa: E402


def _probe_lock(lock_path: str, ready: multiprocessing.synchronize.Event, result_queue: multiprocessing.queues.Queue) -> None:
    ready.wait(timeout=5)
    with Path(lock_path).open("a+", encoding="utf-8") as fh:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            result_queue.put("blocked")
            return
        result_queue.put("acquired")
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def test_compact_receipts_uses_state_writer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    live_file = state_dir / "t0_receipts.ndjson"
    live_file.write_text("".join(f'{{"seq":{index}}}\n' for index in range(12000)), encoding="utf-8")

    captured: list[tuple[Path, object]] = []

    def _fake_rewrite_locked(path: Path, writer: object) -> None:
        captured.append((path, writer))
        current_content = path.read_bytes()
        rewritten = writer(current_content) if callable(writer) else writer
        if rewritten != current_content:
            path.write_bytes(rewritten)

    monkeypatch.setattr(cs.state_writer, "rewrite_locked", _fake_rewrite_locked)

    rc = cs.compact_receipts(state_dir)

    assert rc == 0
    assert len(captured) == 1
    assert captured[0][0] == live_file


def test_backfill_headless_receipts_uses_state_writer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    live_file = tmp_path / "t0_receipts.ndjson"
    live_file.write_text(
        '{"dispatch_id":"unknown","task_id":"unknown","terminal":"unknown","track":"unknown","gate":"unknown","status":"unknown","report_file":"20260401-075225-HEADLESS-codex_gate-pr-0.md","report_path":"/some/path/20260401-075225-HEADLESS-codex_gate-pr-0.md","missing_fields":["task_id","dispatch_id"],"legacy_format":true}\n',
        encoding="utf-8",
    )
    captured: list[tuple[Path, object]] = []

    def _fake_rewrite_locked(path: Path, writer: object) -> None:
        captured.append((path, writer))
        current_content = path.read_bytes()
        rewritten = writer(current_content) if callable(writer) else writer
        if rewritten != current_content:
            path.write_bytes(rewritten)

    monkeypatch.setattr(bfr, "T0_RECEIPTS_NDJSON", live_file)
    monkeypatch.setattr(bfr.state_writer, "rewrite_locked", _fake_rewrite_locked)

    patched, skipped = bfr._update_ndjson({}, {}, dry_run=False)

    assert patched == 1
    assert skipped == 0
    assert len(captured) == 1
    assert captured[0][0] == live_file


def test_rewrite_locked_atomic(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "t0_receipts.ndjson"
    path.write_text("old\n", encoding="utf-8")
    sentinel = state_writer._sentinel_path(path)
    ctx = multiprocessing.get_context("spawn")
    ready = ctx.Event()
    result_queue = ctx.Queue()
    process = ctx.Process(target=_probe_lock, args=(str(sentinel), ready, result_queue))
    original_replace = state_writer.os.replace

    def _replace(src: str | bytes | Path, dst: str | bytes | Path) -> None:
        ready.set()
        assert result_queue.get(timeout=5) == "blocked"
        original_replace(src, dst)

    monkeypatch.setattr(state_writer.os, "replace", _replace)

    process.start()
    try:
        state_writer.rewrite_locked(path, b"new\n")
        process.join(timeout=5)
        assert process.exitcode == 0
    finally:
        if process.is_alive():
            process.terminate()
            process.join(timeout=5)

    assert path.read_text(encoding="utf-8") == "new\n"
