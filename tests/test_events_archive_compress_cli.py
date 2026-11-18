"""CLI for compressing old event archives, dry-run by default (eventarchief-compressie C2b)."""

from __future__ import annotations

import gzip
import json
import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import events_archive_compress as cli

OLD = time.time() - 60 * 86400


@pytest.fixture
def store(tmp_path, monkeypatch):
    archive = tmp_path / "events" / "archive"
    (archive / "T1").mkdir(parents=True)
    monkeypatch.setattr(cli, "resolve_central_data_dir", lambda pid: tmp_path)
    return archive


def _plain(directory: Path, name: str, data: bytes, mtime: float = OLD) -> Path:
    path = directory / f"{name}.ndjson"
    path.write_bytes(data)
    os.utime(path, (mtime, mtime))
    return path


def _snapshot(root: Path):
    return {str(p): (p.stat().st_size, p.stat().st_mtime_ns, p.read_bytes()) for p in root.rglob("*") if p.is_file()}


def test_dry_run_changes_nothing(store, capsys):
    _plain(store / "T1", "a", b'{"a":1}\n' * 50)
    _plain(store, "b", b'{"b":2}\n' * 50)
    _plain(store, "fresh", b"{}\n", mtime=time.time())
    before = _snapshot(store)
    assert cli.main(["--project-id", "demo"]) == 0
    assert _snapshot(store) == before
    assert not (store / ".archive.lock").exists()
    out = capsys.readouterr().out
    assert "would_compress" in out and "totals: files=2" in out and "fresh" not in out


def test_apply_compresses_and_totals_match(store, capsys):
    a = _plain(store / "T1", "a", b'{"a":1}\n' * 200)
    b = _plain(store, "b", b'{"b":2}\n' * 300)
    sizes = a.stat().st_size + b.stat().st_size
    assert cli.main(["--project-id", "demo", "--apply", "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert not a.exists() and not b.exists()
    gz_total = sum(p.stat().st_size for p in store.rglob("*.ndjson.gz"))
    assert report["totals"] == {"files": 2, "bytes_before": sizes, "bytes_after": gz_total,
                                "skipped": 0, "divergent": 0}
    assert gzip.decompress((store / "b.ndjson.gz").read_bytes()) == b'{"b":2}\n' * 300


def test_divergent_pair_exits_3_and_keeps_both(store, capsys):
    plain = _plain(store, "d", b"plain content\n")
    gz = store / "d.ndjson.gz"
    gz.write_bytes(gzip.compress(b"something else\n"))
    assert cli.main(["--project-id", "demo", "--apply", "--json"]) == 3
    report = json.loads(capsys.readouterr().out)
    assert report["totals"]["divergent"] == 1
    assert plain.read_bytes() == b"plain content\n" and gz.exists()


def test_unknown_project_exits_2(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "resolve_central_data_dir", lambda pid: tmp_path / "nope")
    assert cli.main(["--project-id", "ghost"]) == 2
    captured = capsys.readouterr()
    assert captured.out == "" and len(captured.err.strip().splitlines()) == 1


def test_invalid_project_id_exits_2(capsys):
    assert cli.main(["--project-id", "../etc"]) == 2
    assert capsys.readouterr().err.count("\n") == 1


def test_json_dry_run_has_totals_only_on_stdout(store, capsys):
    _plain(store, "a", b"x\n" * 10)
    assert cli.main(["--project-id", "demo", "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["mode"] == "dry-run"
    assert set(report["totals"]) == {"files", "bytes_before", "bytes_after", "skipped", "divergent"}
    assert report["totals"]["files"] == 1


def test_failed_round_exits_1(store, monkeypatch, capsys):
    _plain(store, "a", b"x\n")

    def boom(*a, **k):
        raise OSError("disk read failed")

    monkeypatch.setattr(cli.eac, "compress_archive_round", boom)
    assert cli.main(["--project-id", "demo", "--apply"]) == 1
    assert "disk read failed" in capsys.readouterr().err


def _skipped_paths(capsys):
    report = json.loads(capsys.readouterr().out)
    return report, [r for r in report["files"] if r["reason"] == "outside_store"]


@pytest.mark.parametrize("flags", [["--apply"], []])
def test_symlinked_directory_is_never_followed(store, tmp_path, capsys, flags):
    outside = tmp_path / "outside"
    outside.mkdir()
    victim = _plain(outside, "old", b'{"x":1}\n' * 50)
    before = victim.read_bytes()
    (store / "T9").symlink_to(outside, target_is_directory=True)
    real = _plain(store / "T1", "real", b'{"r":1}\n' * 50)
    assert cli.main(["--project-id", "demo", "--json", *flags]) == 0
    report, skipped = _skipped_paths(capsys)
    assert victim.read_bytes() == before and not list(outside.glob("*.gz"))
    assert [s["outcome"] for s in skipped] == ["skipped"] and "T9" in skipped[0]["path"]
    assert report["totals"]["skipped"] == 1
    assert (not real.exists()) == bool(flags)


def test_symlinked_file_is_never_followed(store, tmp_path, capsys):
    outside = tmp_path / "outside"
    outside.mkdir()
    target = _plain(outside, "target", b'{"x":1}\n' * 50)
    before = target.read_bytes()
    link = store / "T1" / "link.ndjson"
    link.symlink_to(target)
    assert cli.main(["--project-id", "demo", "--apply", "--json"]) == 0
    report, skipped = _skipped_paths(capsys)
    assert target.read_bytes() == before and link.is_symlink() and not list(outside.glob("*.gz"))
    assert not list((store / "T1").glob("*.gz"))
    assert len(skipped) == 1 and skipped[0]["path"].endswith("link.ndjson")
    assert report["totals"]["skipped"] == 1


def test_real_subdirectory_is_still_compressed(store, capsys):
    sub = store / "T1" / "deep"
    sub.mkdir()
    plain = _plain(sub, "a", b'{"a":1}\n' * 50)
    assert cli.main(["--project-id", "demo", "--apply", "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert not plain.exists() and (sub / "a.ndjson.gz").exists()
    assert report["totals"]["skipped"] == 0


def _linked_store(tmp_path, monkeypatch, parent_link: bool):
    """A store whose events/archive (or its parent events) is a symlink to a directory holding an old archive."""
    store = tmp_path / "store"
    store.mkdir()
    real = tmp_path / "elsewhere"
    holder = real / "archive" if parent_link else real
    holder.mkdir(parents=True)
    plain = _plain(holder, "old", b'{"a":1}\n' * 20)
    if parent_link:
        (store / "events").symlink_to(real, target_is_directory=True)
    else:
        (store / "events").mkdir()
        (store / "events" / "archive").symlink_to(real, target_is_directory=True)
    monkeypatch.setattr(cli, "resolve_central_data_dir", lambda pid: store)
    return plain


@pytest.mark.parametrize("parent_link", [False, True])
def test_symlinked_archive_root_or_parent_is_refused(tmp_path, monkeypatch, capsys, parent_link):
    plain = _linked_store(tmp_path, monkeypatch, parent_link)
    before = _snapshot(plain.parent)
    assert cli.main(["--project-id", "demo", "--apply"]) == 2
    assert _snapshot(plain.parent) == before and not list(plain.parent.rglob("*.gz"))
    assert "archive root refused" in capsys.readouterr().err


def test_symlinked_archive_root_dry_run_is_refused_too(tmp_path, monkeypatch):
    _linked_store(tmp_path, monkeypatch, False)
    assert cli.main(["--project-id", "demo"]) == 2


@pytest.mark.parametrize("days", ["nan", "inf", "-inf", "-1"])
def test_non_finite_or_negative_days_is_refused(store, capsys, days):
    plain = _plain(store, "old", b'{"a":1}\n' * 20)
    fresh = _plain(store, "fresh", b"{}\n", mtime=time.time())
    before = _snapshot(store)
    assert cli.main(["--project-id", "demo", "--apply", f"--days={days}"]) == 2
    assert _snapshot(store) == before and plain.exists() and fresh.exists()
    assert "--days must be a finite number" in capsys.readouterr().err


def test_normal_store_still_compresses(store):
    plain = _plain(store, "old", b'{"a":1}\n' * 20)
    assert cli.main(["--project-id", "demo", "--apply"]) == 0
    assert not plain.exists() and plain.with_name("old.ndjson.gz").exists()
