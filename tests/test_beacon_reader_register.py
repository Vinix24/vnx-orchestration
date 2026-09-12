"""Tests for scripts/lib/beacon_reader_register.py (golf C, C2a).

Mirrors tests/test_beacon_register.py's own pattern: fixture trees exercise
the parser's edge cases in isolation, plus a sanity check against the real
repo tree. Per the C2a dispatch's own instruction, the RED case for
"a beacon with zero readers" is forced with a synthetic/injected register,
never asserted against the real tree -- a real-tree assertion would go
green the moment anyone adds a reader and then test nothing further.
"""
from __future__ import annotations

import sys
import textwrap
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_LIB_DIR = _REPO_ROOT / "scripts" / "lib"
if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))

import beacon_reader_register as brr  # noqa: E402


def _write(root: Path, relative: str, content: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(content), encoding="utf-8")


# ---------------------------------------------------------------------------
# Generic reader detection
# ---------------------------------------------------------------------------


def test_generic_reader_with_expected_kwarg_is_detected(tmp_path: Path) -> None:
    _write(tmp_path, "scripts/consumer.py", """
        from health_beacon import beacon_summary
        beacon_summary(data_dir, expected=expected_component_names())
    """)
    readers = brr.read_reader_register(("any_name",), project_root_dir=tmp_path)
    assert brr.has_generic_reader(readers)
    generic = [r for r in readers if r.kind == "generic"]
    assert generic and generic[0].source_file == "scripts/consumer.py"


def test_all_beacons_call_without_expected_is_not_generic(tmp_path: Path) -> None:
    """The pre-D3a call shape (no `expected=` at all) never surfaces an
    absent component -- it must not count as a reader that covers silence."""
    _write(tmp_path, "scripts/consumer.py", """
        from health_beacon import all_beacons
        all_beacons(data_dir)
    """)
    readers = brr.read_reader_register(("any_name",), project_root_dir=tmp_path)
    assert not brr.has_generic_reader(readers)


def test_expected_none_literal_is_not_generic(tmp_path: Path) -> None:
    """`expected=None` is the explicit "no expectation declared" value
    (health_beacon.py's own docstring) -- a call site passing it literally
    must not be mistaken for a generic reader."""
    _write(tmp_path, "scripts/consumer.py", """
        from health_beacon import all_beacons
        all_beacons(data_dir, expected=None)
    """)
    readers = brr.read_reader_register(("any_name",), project_root_dir=tmp_path)
    assert not brr.has_generic_reader(readers)


# ---------------------------------------------------------------------------
# Specific reader detection
# ---------------------------------------------------------------------------


def test_specific_reader_quoted_literal_is_detected(tmp_path: Path) -> None:
    _write(tmp_path, "hooks/watcher.sh", """
        #!/bin/bash
        CANDIDATES="$VNX_DATA_DIR/health/producer_freshness_monitor.json"
    """)
    readers = brr.read_reader_register(("producer_freshness_monitor",), project_root_dir=tmp_path)
    specific = [r for r in readers if r.kind == "specific"]
    assert any(r.component == "producer_freshness_monitor" and r.source_file == "hooks/watcher.sh" for r in specific)


def test_docstring_mention_is_not_a_specific_reader(tmp_path: Path) -> None:
    """A component name that only appears in a module/function DOCSTRING
    (this repo's own heavily cross-referenced commentary style) must not
    count as a reader -- narrative prose is not code that acts on the
    signal."""
    _write(tmp_path, "scripts/unrelated.py", '''
        """This module has nothing to do with cleanup_worker_exit, but its
        docstring name-drops it for context."""
        x = 1
    ''')
    readers = brr.read_reader_register(("cleanup_worker_exit",), project_root_dir=tmp_path)
    assert not any(r.component == "cleanup_worker_exit" for r in readers)


def test_comment_mention_in_bash_is_not_a_specific_reader(tmp_path: Path) -> None:
    _write(tmp_path, "hooks/unrelated.sh", """
        #!/bin/bash
        # this hook has nothing to do with "ledger_health"
        echo hi
    """)
    readers = brr.read_reader_register(("ledger_health",), project_root_dir=tmp_path)
    assert not any(r.component == "ledger_health" for r in readers)


def test_writer_own_file_is_excluded_from_its_own_specific_readers(tmp_path: Path) -> None:
    """A component's own writer file inevitably mentions its own name (log
    messages, the HealthBeacon(...) call itself) -- that must never count as
    the component's OWN reader."""
    _write(tmp_path, "scripts/lib/some_writer.py", '''
        from health_beacon import HealthBeacon
        HealthBeacon(state_dir, "self_writer").heartbeat(status="ok")
        print("self_writer wrote a beacon")
    ''')
    readers = brr.read_reader_register(
        ("self_writer",),
        project_root_dir=tmp_path,
        writer_source_by_component={"self_writer": "scripts/lib/some_writer.py"},
    )
    assert not any(r.component == "self_writer" for r in readers)


def test_tests_directory_is_never_scanned(tmp_path: Path) -> None:
    _write(tmp_path, "scripts/tests/test_something.py", '''
        x = "cleanup_worker_exit"
    ''')
    readers = brr.read_reader_register(("cleanup_worker_exit",), project_root_dir=tmp_path)
    assert not any(r.component == "cleanup_worker_exit" for r in readers)


# ---------------------------------------------------------------------------
# components_without_reader -- forced RED/GREEN via an injected register,
# per the dispatch's own instruction (never asserted against the real tree).
# ---------------------------------------------------------------------------


def test_components_without_reader_flags_a_synthetic_orphan() -> None:
    fake_register = (
        brr.ReaderSpec(component="has_specific", source_file="hooks/fake.sh", line=1, kind="specific"),
    )
    orphans = brr.components_without_reader(("has_specific", "orphan"), fake_register)
    assert orphans == ("orphan",)


def test_a_single_generic_reader_covers_every_name_including_future_ones() -> None:
    fake_register = (
        brr.ReaderSpec(component=None, source_file="dashboard/api_health.py", line=96, kind="generic"),
    )
    orphans = brr.components_without_reader(("a", "b", "never_registered_yet"), fake_register)
    assert orphans == ()


def test_no_readers_at_all_flags_every_name() -> None:
    orphans = brr.components_without_reader(("a", "b"), ())
    assert set(orphans) == {"a", "b"}


def test_readers_for_component_includes_generic_and_matching_specific() -> None:
    register = (
        brr.ReaderSpec(component=None, source_file="dashboard/api_health.py", line=1, kind="generic"),
        brr.ReaderSpec(component="x", source_file="hooks/fake.sh", line=2, kind="specific"),
        brr.ReaderSpec(component="y", source_file="hooks/other.sh", line=3, kind="specific"),
    )
    readers = brr.readers_for_component("x", register)
    assert {r.source_file for r in readers} == {"dashboard/api_health.py", "hooks/fake.sh"}


# ---------------------------------------------------------------------------
# check_coverage -- the producer_freshness_monitor.py integration point
# ---------------------------------------------------------------------------


def test_check_coverage_reports_no_reader_finding_when_forced(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(brr, "read_reader_register", lambda *a, **k: ())
    result = brr.check_coverage(beacon_names=("orphan_one",))
    assert result["status"] == "stale"
    assert result["findings"] == [
        {"producer": "beacon_reader_coverage", "key": "orphan_one", "kind": "no_reader", "cadence_seconds": None}
    ]


def test_check_coverage_is_clean_when_a_generic_reader_exists(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        brr,
        "read_reader_register",
        lambda *a, **k: (brr.ReaderSpec(component=None, source_file="x.py", line=1, kind="generic"),),
    )
    result = brr.check_coverage(beacon_names=("anything",))
    assert result["status"] == "ok"
    assert result["findings"] == []


# ---------------------------------------------------------------------------
# Sanity check against the real repo tree
# ---------------------------------------------------------------------------


def test_real_tree_has_a_generic_reader_covering_every_ast_registered_beacon() -> None:
    """Measured 2026-09-09: dashboard/api_health.py and
    scripts/build_t0_state.py both already call
    beacon_summary(data_dir, expected=expected_component_names()) -- a
    generic reader that covers every beacon_register.py-derived name,
    present or not. This is what makes ALL of them mechanically covered
    already; the C2a per-beacon decisions (park learning_loop/
    intelligence_daemon, dedupe report_to_receipt_converter's stale leftover
    file) are about health classification and writer hygiene, not about a
    missing reader."""
    import beacon_register as br

    names = tuple(spec.name for spec in br.read_beacon_register())
    register = brr.read_reader_register(names)
    assert brr.has_generic_reader(register)
    assert brr.components_without_reader(names, register) == ()
