"""Lossless archive compression with a short lock and the pair rules (eventarchief-compressie C2a)."""

from __future__ import annotations

import gzip
import itertools
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "lib"))

import event_archive_compress as eac  # noqa: E402
import event_store as es  # noqa: E402

NOW = 2_000_000_000.0
OLD = NOW - 30 * 86400


def _plain(directory: Path, name: str, data: bytes, mtime: float = OLD, mode: int = 0o640) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}.ndjson"
    path.write_bytes(data)
    os.chmod(path, mode)
    os.utime(path, (mtime, mtime))
    return path


def _round(directory, **kw):
    kw.setdefault("now", lambda: NOW)
    return eac.compress_archive_round(directory, kw.pop("days", 7), **kw)


def _names(directory):
    return sorted(
        p.name for p in Path(directory).iterdir() if p.name != es.ARCHIVE_LOCK_NAME and not p.name.startswith("_vnx")
    )


def _outcomes(result):
    return [(Path(f.path).name, f.outcome, f.reason) for f in result.files]


def test_lossless_and_readable_before_and_after(tmp_path):
    data = b'{"a":1}\n{"a":2}\n{"a":3}'  # no final newline
    plain = _plain(tmp_path, "d1", data)
    before = es.open_archive_text(plain).read().splitlines()
    result = _round(tmp_path)
    assert _outcomes(result) == [("d1.ndjson", "compressed", None)]
    assert not plain.exists()
    assert gzip.decompress((tmp_path / "d1.ndjson.gz").read_bytes()) == data
    assert es.open_archive_text(plain).read().splitlines() == before  # falls back to the gz
    summary = result.summary()
    assert (summary["bytes_before"], summary["stopped"]) == (len(data), "done")
    assert summary["bytes_after"] == (tmp_path / "d1.ndjson.gz").stat().st_size


def test_mode_and_mtime_carry_over_and_recent_files_stay(tmp_path):
    _plain(tmp_path, "d1", b"x\n" * 100, mtime=OLD, mode=0o604)
    _plain(tmp_path, "new", b"n\n", mtime=NOW - 86400)
    _round(tmp_path)
    st = (tmp_path / "d1.ndjson.gz").stat()
    assert st.st_mode & 0o777 == 0o604 and int(st.st_mtime) == int(OLD)
    assert (tmp_path / "new.ndjson").exists()


def test_plain_changed_between_step_1_and_2_stays(tmp_path, monkeypatch):
    plain = _plain(tmp_path, "d1", b"one\n")
    real = eac._compress_verified

    def grow(path):
        scratch = real(path)
        with open(path, "ab") as f:
            f.write(b"two\n")
        return scratch

    monkeypatch.setattr(eac, "_compress_verified", grow)
    result = _round(tmp_path)
    assert _outcomes(result) == [("d1.ndjson", "skipped", "changed")]
    assert plain.read_bytes() == b"one\ntwo\n" and _names(tmp_path) == ["d1.ndjson"]  # no gz, scratch gone


def test_verification_mismatch_leaves_the_plain(tmp_path, monkeypatch):
    plain = _plain(tmp_path, "d1", b"one\n")
    monkeypatch.setattr(eac, "_kind", lambda gz, p: "different")
    result = _round(tmp_path)
    assert _outcomes(result) == [("d1.ndjson", "skipped", "mismatch")]
    assert plain.exists() and _names(tmp_path) == ["d1.ndjson"]


def test_lock_held_by_another_process_skips_without_waiting(tmp_path):
    plain = _plain(tmp_path, "d1", b"one\n")
    code = "import fcntl,sys;f=open(sys.argv[1],'ab');fcntl.flock(f.fileno(),fcntl.LOCK_EX);print('held',flush=True);sys.stdin.readline()"
    lock = str(tmp_path / es.ARCHIVE_LOCK_NAME)
    proc = subprocess.Popen([sys.executable, "-c", code, lock], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        assert proc.stdout.readline().strip() == "held"
        result = _round(tmp_path)
    finally:
        proc.communicate("\n", timeout=10)
    assert _outcomes(result) == [("d1.ndjson", "skipped", "locked")] and plain.exists()


@pytest.mark.parametrize(
    "gz_bytes,outcome",
    [
        (gzip.compress(b"one\n", mtime=0) + gzip.compress(b"two\nthree", mtime=0), "pair_removed_plain"),  # equal, two members
        (gzip.compress(b"one\ntw", mtime=0), "pair_recompressed"),  # a prefix
    ],
)
def test_pair_equal_or_prefix_ends_in_one_gz_of_the_plain(tmp_path, gz_bytes, outcome):
    plain = _plain(tmp_path, "d1", b"one\ntwo\nthree")
    (tmp_path / "d1.ndjson.gz").write_bytes(gz_bytes)
    assert _outcomes(_round(tmp_path)) == [("d1.ndjson", outcome, None)]
    assert not plain.exists()
    assert gzip.decompress((tmp_path / "d1.ndjson.gz").read_bytes()) == b"one\ntwo\nthree"


@pytest.mark.parametrize("gz_bytes", [gzip.compress(b"other\n", mtime=0), gzip.compress(b"one\ntwo\nthree\nfour\n", mtime=0), b"not gzip"])
def test_pair_anything_else_or_unreadable_is_divergent_and_removes_nothing(tmp_path, gz_bytes):
    plain = _plain(tmp_path, "d1", b"one\ntwo\nthree\n")
    gz = tmp_path / "d1.ndjson.gz"
    gz.write_bytes(gz_bytes)
    result = _round(tmp_path)
    (f,) = result.files
    assert f.outcome == "divergent" and result.has_divergent
    assert (f.path, f.gz_path, f.plain_size, f.gz_size) == (str(plain), str(gz), 14, len(gz_bytes))
    assert plain.read_bytes() == b"one\ntwo\nthree\n" and gz.read_bytes() == gz_bytes


def test_gz_appearing_between_step_1_and_2_goes_to_the_pair_rules(tmp_path, monkeypatch):
    _plain(tmp_path, "d1", b"one\ntwo\n")
    real = eac._compress_verified

    def meanwhile(path):
        scratch = real(path)
        (tmp_path / "d1.ndjson.gz").write_bytes(gzip.compress(b"zzz\n"))
        return scratch

    monkeypatch.setattr(eac, "_compress_verified", meanwhile)
    result = _round(tmp_path)
    assert _outcomes(result) == [("d1.ndjson", "divergent", None)]
    assert _names(tmp_path) == ["d1.ndjson", "d1.ndjson.gz"]  # nothing removed, no scratch left


@pytest.mark.parametrize(
    "kw,stopped,count",
    [
        ({"max_files": 2}, "max_files", 2),
        ({"max_bytes": 50}, "max_bytes", 2),  # 43 bytes actually read per 10-byte file (OI-2982)
        ({"max_seconds": 2.5, "clock": itertools.count().__next__}, "max_seconds", 2),
        ({}, "done", 3),
    ],
)
def test_each_budget_stops_the_round(tmp_path, kw, stopped, count):
    for i, name in enumerate("abc"):
        _plain(tmp_path, name, b"x" * 10, mtime=OLD + i)
    result = _round(tmp_path, **kw)
    assert result.stopped == stopped
    assert len(result.files) == count
    assert len(list(tmp_path.glob("*.ndjson"))) == 3 - count


def test_file_over_the_size_limit_is_skipped_automatically_and_taken_manually(tmp_path):
    plain = _plain(tmp_path, "big", b"x" * 100)
    assert _outcomes(_round(tmp_path, max_file_bytes=50)) == [("big.ndjson", "skipped", "too_large")]
    assert plain.exists()
    assert _outcomes(_round(tmp_path, max_file_bytes=50, manual=True)) == [("big.ndjson", "compressed", None)]


def test_not_enough_free_space_refuses_the_file(tmp_path):
    _plain(tmp_path, "d1", b"x" * 100)
    assert _outcomes(_round(tmp_path, free_space=lambda d: 199)) == [("d1.ndjson", "skipped", "no_space")]
    assert _names(tmp_path) == ["d1.ndjson"]
    assert _outcomes(_round(tmp_path, free_space=lambda d: 200)) == [("d1.ndjson", "compressed", None)]


def test_a_writer_and_the_compressor_on_one_id_end_in_one_gz_in_order(tmp_path, monkeypatch):
    def line(n):
        return f'{{"dispatch_id":"d1","n":{n}}}'

    _plain(tmp_path, "d1", (line(1) + "\n" + line(2) + "\n").encode())
    real = eac._compress_verified

    def writer_in_the_gap(path):
        scratch = real(path)
        es._write_archive(tmp_path, "d1", 0o640, (line(3) + "\n").encode())
        return scratch

    monkeypatch.setattr(eac, "_compress_verified", writer_in_the_gap)
    later = {"days": 0, "now": lambda: NOW + 10**7}
    assert _outcomes(_round(tmp_path, **later)) == [("d1.ndjson", "skipped", "changed")]
    monkeypatch.setattr(eac, "_compress_verified", real)
    assert _outcomes(_round(tmp_path, **later)) == [("d1.ndjson", "compressed", None)]
    es._write_archive(tmp_path, "d1", 0o640, (line(4) + "\n").encode())
    gz = tmp_path / "d1.ndjson.gz"
    assert gzip.decompress(gz.read_bytes()).decode().splitlines() == [line(1), line(2), line(3), line(4)]
    assert _names(tmp_path) == ["d1.ndjson.gz"]  # one gz, no plain


def test_bytes_read_counts_what_is_read_and_stops_the_round_earlier(tmp_path):
    """OI-2982: compression and verification read the plain twice and the gz once."""
    data = os.urandom(1000)
    _plain(tmp_path, "a", data, mtime=OLD)
    _plain(tmp_path, "b", data, mtime=OLD + 1)
    result = _round(tmp_path, max_bytes=1500)  # counting the plain size once took both files
    assert _outcomes(result) == [("a.ndjson", "compressed", None)] and result.stopped == "max_bytes"
    assert result.bytes_read == 2 * len(data) + (tmp_path / "a.ndjson.gz").stat().st_size
    assert (tmp_path / "b.ndjson").exists()


def test_a_skipped_file_counts_only_what_was_read(tmp_path, monkeypatch):
    _plain(tmp_path, "a", b"x" * 1000)
    monkeypatch.setattr(eac, "_compress_verified", lambda path: None)
    result = _round(tmp_path)
    assert _outcomes(result) == [("a.ndjson", "skipped", "mismatch")] and result.bytes_read == 0


def test_gz_changed_after_the_compare_is_changed_and_the_compare_runs_outside_the_lock(tmp_path, monkeypatch):
    """OI-2983: the pair compare runs before the archive lock; a gz that moves after it is skipped."""
    import fcntl

    plain = _plain(tmp_path, "d1", b"one\ntwo\n")
    gz = tmp_path / "d1.ndjson.gz"
    real_verified, real_compare, held = eac._compress_verified, eac._compare, []

    def gz_appears(path):
        scratch = real_verified(path)
        gz.write_bytes(gzip.compress(b"one\ntwo\n"))
        return scratch

    def probing_compare(g, p):
        with open(tmp_path / es.ARCHIVE_LOCK_NAME, "ab") as probe:
            try:
                fcntl.flock(probe.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(probe.fileno(), fcntl.LOCK_UN)
                held.append(False)
            except BlockingIOError:
                held.append(True)
        kind = real_compare(g, p)
        if g == gz:
            with open(gz, "ab") as f:
                f.write(gzip.compress(b"three\n"))
        return kind

    monkeypatch.setattr(eac, "_compress_verified", gz_appears)
    monkeypatch.setattr(eac, "_compare", probing_compare)
    result = _round(tmp_path)
    assert held and not any(held)
    assert _outcomes(result) == [("d1.ndjson", "skipped", "changed")]
    assert plain.read_bytes() == b"one\ntwo\n" and _names(tmp_path) == ["d1.ndjson", "d1.ndjson.gz"]


def test_a_symlinked_plain_file_is_no_candidate_and_its_target_is_untouched(tmp_path):
    outside = tmp_path / "outside"
    target = _plain(outside, "t", b"keep\n")
    directory = tmp_path / "T1"
    directory.mkdir()
    link = directory / "old.ndjson"
    link.symlink_to(target)
    assert eac.candidates([directory], 7, now=lambda: NOW) == []
    _round(directory)
    assert target.read_bytes() == b"keep\n" and link.is_symlink()
    assert not (outside / "t.ndjson.gz").exists() and not (directory / "old.ndjson.gz").exists()


def test_symlinked_root_is_named_and_a_plain_root_is_not(tmp_path):
    store = tmp_path / "store"
    archive = store / "events" / "archive"
    archive.parent.mkdir(parents=True)
    real = tmp_path / "real"
    real.mkdir()
    archive.mkdir()
    assert eac.symlinked_root(store, archive) is None
    archive.rmdir()
    archive.symlink_to(real, target_is_directory=True)
    assert eac.symlinked_root(store, archive) == archive
    assert eac.symlinked_root(store, tmp_path / "other" / "archive") == tmp_path / "other" / "archive"
