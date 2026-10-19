"""tests/test_atomic_swap.py — the exchange `vnx guard install` replaces a settings file with.

The real call runs on the real filesystem of ``tmp_path`` (APFS on the Mac it was measured on,
ext4 or tmpfs on a Linux runner). Only the refusal paths replace the bound call, because a
platform or filesystem without the exchange is not at hand.
"""
from __future__ import annotations

import ctypes
import errno
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts" / "lib"))

import atomic_swap

SUPPORTED = sys.platform == "darwin" or sys.platform.startswith("linux")


def _pair(tmp_path: Path):
    first, second = tmp_path / "first", tmp_path / "second"
    first.write_text("FIRST", encoding="utf-8")
    second.write_text("SECOND", encoding="utf-8")
    return first, second


@pytest.mark.skipif(not SUPPORTED, reason="the exchange exists on macOS and Linux only")
def test_this_platform_has_the_exchange():
    assert atomic_swap.unsupported_reason() is None


@pytest.mark.skipif(not SUPPORTED, reason="the exchange exists on macOS and Linux only")
def test_the_two_entries_trade_places_inodes_included(tmp_path):
    first, second = _pair(tmp_path)
    inode_first, inode_second = first.stat().st_ino, second.stat().st_ino
    atomic_swap.exchange(first, second)
    assert first.read_text(encoding="utf-8") == "SECOND"
    assert second.read_text(encoding="utf-8") == "FIRST"
    assert (first.stat().st_ino, second.stat().st_ino) == (inode_second, inode_first)


@pytest.mark.skipif(not SUPPORTED, reason="the exchange exists on macOS and Linux only")
def test_a_missing_side_raises_file_not_found_and_changes_nothing(tmp_path):
    first, _ = _pair(tmp_path)
    gone = tmp_path / "gone"
    with pytest.raises(FileNotFoundError):
        atomic_swap.exchange(first, gone)
    assert first.read_text(encoding="utf-8") == "FIRST" and not gone.exists()


def _failing_call(err: int):
    def call(first: bytes, second: bytes) -> int:
        ctypes.set_errno(err)
        return -1
    return call


@pytest.mark.parametrize("err", [errno.ENOTSUP, errno.EINVAL, errno.ENOSYS])
def test_a_filesystem_that_refuses_the_flag_is_unsupported_not_an_error(tmp_path, monkeypatch, err):
    first, second = _pair(tmp_path)
    monkeypatch.setattr(atomic_swap, "_bound", (_failing_call(err), ""))
    with pytest.raises(atomic_swap.ExchangeUnsupported) as raised:
        atomic_swap.exchange(first, second)
    assert errno.errorcode[err] in str(raised.value) and str(second) in str(raised.value)


def test_any_other_errno_is_an_os_error_with_both_paths(tmp_path, monkeypatch):
    first, second = _pair(tmp_path)
    monkeypatch.setattr(atomic_swap, "_bound", (_failing_call(errno.EACCES), ""))
    with pytest.raises(PermissionError) as raised:
        atomic_swap.exchange(first, second)
    assert raised.value.filename == str(first) and raised.value.filename2 == str(second)


def test_a_platform_without_the_call_says_why_and_never_falls_back(tmp_path, monkeypatch):
    first, second = _pair(tmp_path)
    monkeypatch.setattr(atomic_swap, "_bound", (None, "no atomic exchange call is known for 'x'"))
    assert atomic_swap.unsupported_reason() == "no atomic exchange call is known for 'x'"
    with pytest.raises(atomic_swap.ExchangeUnsupported):
        atomic_swap.exchange(first, second)
    assert first.read_text(encoding="utf-8") == "FIRST"
    assert second.read_text(encoding="utf-8") == "SECOND"


def test_an_unknown_platform_binds_nothing(monkeypatch):
    monkeypatch.setattr(atomic_swap.sys, "platform", "plan9")
    call, reason = atomic_swap._bind()
    assert call is None and "plan9" in reason
