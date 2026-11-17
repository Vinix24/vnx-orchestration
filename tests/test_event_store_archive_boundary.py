"""Archive write boundary (ADR-081 decisions 2 and 3): every writer, one lock, a gz stays one gz."""

from __future__ import annotations

import ast
import fcntl
import gzip
import json
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "lib"))

import event_store as es  # noqa: E402
from event_store import EventStore, open_archive_text  # noqa: E402

DISPATCH = "d-boundary"


def _line(dispatch_id: str, n: int) -> str:
    return json.dumps({"dispatch_id": dispatch_id, "type": "t", "n": n}, separators=(",", ":"))


def _gz_only(store: EventStore, terminal: str, name: str, lines: list) -> Path:
    directory = store.archive_dir(terminal)
    directory.mkdir(parents=True, exist_ok=True)
    gz = directory / f"{name}.ndjson.gz"
    gz.write_bytes(gzip.compress(("\n".join(lines) + "\n").encode()))
    return gz


def _assert_one_gz(store: EventStore, terminal: str, name: str, expected: list) -> None:
    directory = store.archive_dir(terminal)
    assert not (directory / f"{name}.ndjson").exists()
    gz = directory / f"{name}.ndjson.gz"
    assert gz.is_file()
    assert gzip.decompress(gz.read_bytes()).decode().splitlines() == expected


@pytest.fixture
def store(tmp_path):
    return EventStore(events_dir=tmp_path / "events")


def _write_live(store: EventStore, terminal: str, lines: list, run_id=None) -> Path:
    path = store._stream_path(terminal, run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n")
    return path


def test_copy_to_archive_extends_gz(store):
    old = [_line(DISPATCH, 0)]
    _gz_only(store, "T1", DISPATCH, old)
    new = [_line(DISPATCH, 0), _line(DISPATCH, 1)]
    live = _write_live(store, "T1", new)
    dest = store._copy_to_archive(live, "T1", DISPATCH)
    assert dest.name.endswith(".gz")
    _assert_one_gz(store, "T1", DISPATCH, [old[0], new[1]])


def test_archive_run_stream_extends_gz(store):
    old = [_line(DISPATCH, 0)]
    _gz_only(store, "plan-gate", DISPATCH, old)
    _write_live(store, "plan-gate", [_line(DISPATCH, 1), _line(DISPATCH, 2)], run_id=DISPATCH)
    store.archive("plan-gate", DISPATCH)
    _assert_one_gz(store, "plan-gate", DISPATCH, [old[0], _line(DISPATCH, 1), _line(DISPATCH, 2)])


def test_append_late_to_pinned_archive_extends_gz(store):
    old = [_line(DISPATCH, 0)]
    _gz_only(store, "T1", DISPATCH, old)
    _write_live(store, "T1", [_line(DISPATCH, 1)])
    store.archive_late_events("T1", DISPATCH)
    _assert_one_gz(store, "T1", DISPATCH, [old[0], _line(DISPATCH, 1)])


def test_archive_completed_stream_pinned_branch_extends_gz(store):
    old = [_line(DISPATCH, 0)]
    _gz_only(store, "T1", DISPATCH, old)
    _write_live(store, "T1", [_line(DISPATCH, 0), _line(DISPATCH, 3)])
    dest = store.archive_completed_stream("T1", DISPATCH)
    assert dest.name.endswith(".gz")
    _assert_one_gz(store, "T1", DISPATCH, [old[0], _line(DISPATCH, 3)])


def test_archive_completed_stream_run_branch_extends_gz(store):
    old = [_line(DISPATCH, 0)]
    _gz_only(store, "ephemeral", DISPATCH, old)
    _write_live(store, "ephemeral", [_line(DISPATCH, 4)], run_id=DISPATCH)
    store.archive_completed_stream("ephemeral", DISPATCH)
    _assert_one_gz(store, "ephemeral", DISPATCH, [old[0], _line(DISPATCH, 4)])


def test_dispatch_boundary_rotation_extends_gz(store):
    prev = "d-prev"
    _gz_only(store, "T1", prev, [_line(prev, 0)])
    _write_live(store, "T1", [_line(prev, 1)])
    store._rotate_at_dispatch_boundary("T1", "d-next")
    _assert_one_gz(store, "T1", prev, [_line(prev, 0), _line(prev, 1)])


def test_gz_without_final_newline_is_not_glued(store):
    directory = store.archive_dir("T1")
    directory.mkdir(parents=True)
    (directory / f"{DISPATCH}.ndjson.gz").write_bytes(gzip.compress(_line(DISPATCH, 0).encode()))
    live = _write_live(store, "T1", [_line(DISPATCH, 1)])
    store._copy_to_archive(live, "T1", DISPATCH)
    _assert_one_gz(store, "T1", DISPATCH, [_line(DISPATCH, 0), _line(DISPATCH, 1)])


def test_writer_waits_for_archive_lock_taken_after_live_lock(store):
    live = _write_live(store, "T1", [_line(DISPATCH, 1)])
    directory = store.archive_dir("T1")
    directory.mkdir(parents=True)
    holder = open(directory / ".archive.lock", "ab")
    fcntl.flock(holder.fileno(), fcntl.LOCK_EX)
    done = threading.Event()

    def writer():
        store._copy_to_archive(live, "T1", DISPATCH)
        done.set()

    thread = threading.Thread(target=writer)
    thread.start()
    try:
        time.sleep(0.3)
        assert not done.is_set()
        # The writer already holds its lock on the live stream: live first, then archive.
        with open(live, "rb") as probe:
            with pytest.raises(BlockingIOError):
                fcntl.flock(probe.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        fcntl.flock(holder.fileno(), fcntl.LOCK_UN)
        holder.close()
    thread.join(timeout=10)
    assert done.is_set()
    assert (directory / f"{DISPATCH}.ndjson").is_file()


def test_nothing_outside_the_boundary_writes_under_an_archive_dir():
    tree = ast.parse(Path(es.__file__).read_text())
    allowed = {
        "os.replace": {"_replace_from_scratch"},
        "_replace_from_scratch": {"_extend_archive", "_extend_gz_archive"},
        "_extend_archive": {"_write_archive"},
        "_extend_gz_archive": {"_write_archive"},
    }
    found = {key: set() for key in allowed}
    for func in ast.walk(tree):
        if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for node in ast.walk(func):
            if not isinstance(node, ast.Call):
                continue
            target = ast.unparse(node.func)
            if target in found:
                found[target].add(func.name)
    assert found == allowed


def test_open_archive_text_falls_back_to_gz_of_the_same_id(tmp_path):
    plain = tmp_path / f"{DISPATCH}.ndjson"
    plain.write_text(_line(DISPATCH, 0) + "\n")
    resolved = es.resolve_archive_file(tmp_path, DISPATCH)
    assert resolved == plain
    plain.unlink()
    (tmp_path / f"{DISPATCH}.ndjson.gz").write_bytes(gzip.compress((_line(DISPATCH, 0) + "\n").encode()))
    with open_archive_text(resolved) as fh:
        assert fh.read().splitlines() == [_line(DISPATCH, 0)]


def test_open_archive_text_missing_plain_and_gz_still_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        open_archive_text(tmp_path / "absent.ndjson")
    (tmp_path / "other.ndjson.gz").write_bytes(gzip.compress(b"x\n"))
    with pytest.raises(FileNotFoundError):
        open_archive_text(tmp_path / "absent.ndjson")


def test_size_rotation_never_beside_a_gz_and_takes_the_lock(store):
    store.append("T1", {"type": "t", "data": {}}, dispatch_id=DISPATCH)
    store._rotation_threshold_bytes = 1
    store._rotate_on_size("T1")
    directory = store.archive_dir("T1")
    rotated = [p for p in directory.iterdir() if p.name.startswith("size-rotation-")]
    assert len(rotated) == 1 and rotated[0].name.endswith(".ndjson")
    assert (directory / ".archive.lock").is_file()
    assert store._terminal_path("T1").stat().st_size == 0
