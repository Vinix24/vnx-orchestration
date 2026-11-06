#!/usr/bin/env python3
"""Per-process memo of `vnx doctor`'s embedded-path census (test-suite-kosten TSK-2).

Every ``vnx_doctor()`` call paid a 9-10 s AST census over ``scripts/lib``
(`doctor._check_embedded_path_assumptions` -> ``scan_dir``). The census is now
memoized in ``doctor`` per process, keyed on the resolved engine root plus
``(path, size, mtime_ns)`` for every file it reads; ``scan_dir`` itself stays a
pure function.

These tests build a real engine tree under ``tmp_path`` and count the calls
``_check_embedded_path_assumptions`` makes into ``scan_dir`` (the wrapper calls
the real one). ``_engine.ensure_engine_on_path`` is redirected at the same
tree; no other production code is patched.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from vnx_cli.commands import doctor

TESTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS_DIR.parent / "scripts"))
import check_no_file_derived_data_paths as checker  # noqa: E402

CLEAN_SOURCE = "VALUE = 1\n"
VIOLATING_SOURCE = (
    "from pathlib import Path\n"
    'STATE = Path(__file__).resolve().parent / ".vnx-data" / "state"\n'
)


@pytest.fixture(autouse=True)
def _empty_cache():
    """No census entry survives a test, in either direction."""
    doctor.clear_embedded_path_cache()
    yield
    doctor.clear_embedded_path_cache()


@pytest.fixture
def engine(tmp_path, monkeypatch):
    root = tmp_path / "engine"
    lib = root / "scripts" / "lib"
    lib.mkdir(parents=True)
    (lib / "subject.py").write_text(CLEAN_SOURCE, encoding="utf-8")
    monkeypatch.setattr(doctor._engine, "ensure_engine_on_path", lambda: root)
    return root


@pytest.fixture
def scan_calls(monkeypatch):
    """Count (and really run) the census's single ``scan_dir`` entry point."""
    calls = []
    real_scan = checker.scan_dir

    def counting(root):
        calls.append(Path(root))
        return real_scan(root)

    monkeypatch.setattr(checker, "scan_dir", counting)
    return calls


def test_second_run_on_unchanged_tree_scans_once(engine, scan_calls):
    first = doctor._check_embedded_path_assumptions()
    second = doctor._check_embedded_path_assumptions()

    assert len(scan_calls) == 1
    assert first == second


def test_rewritten_file_is_a_miss_and_shows_the_new_content(engine, scan_calls):
    first = doctor._check_embedded_path_assumptions()
    assert first.status == doctor.PASS

    subject = engine / "scripts" / "lib" / "subject.py"
    subject.write_text(VIOLATING_SOURCE, encoding="utf-8")
    # Pin a distinct mtime_ns as well: a coarse filesystem clock must not hide
    # the rewrite from a key that also carries the file size.
    stat = subject.stat()
    os.utime(subject, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))

    second = doctor._check_embedded_path_assumptions()

    assert len(scan_calls) == 2
    assert second.status == doctor.WARN
    assert second != first
    assert "1 __file__-anchored" in second.detail


def test_clear_embedded_path_cache_forces_a_rescan(engine, scan_calls):
    doctor._check_embedded_path_assumptions()
    doctor.clear_embedded_path_cache()
    doctor._check_embedded_path_assumptions()

    assert len(scan_calls) == 2


def test_hit_and_miss_on_the_same_tree_return_equal_checks(engine, scan_calls):
    first = doctor._check_embedded_path_assumptions()
    subject = engine / "scripts" / "lib" / "subject.py"
    subject.write_text(CLEAN_SOURCE + "\n" + CLEAN_SOURCE, encoding="utf-8")

    second = doctor._check_embedded_path_assumptions()

    assert len(scan_calls) == 2
    assert first == second


def test_unstattable_file_takes_the_uncached_path(engine, scan_calls, monkeypatch):
    """A stat failure yields no key, so the census runs again instead of
    keying a partial file set."""
    real_stat = Path.stat

    def flaky_stat(self, *args, **kwargs):
        if self.name == "subject.py":
            raise OSError("file vanished between rglob and stat")
        return real_stat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", flaky_stat)

    doctor._check_embedded_path_assumptions()
    doctor._check_embedded_path_assumptions()

    assert len(scan_calls) == 2
