"""Tests for scripts/lib/beacon_register.py (D3a gap 2).

Mirrors tests/test_daemon_register.py's own pattern: a fixture scripts/
tree exercises the parser's edge cases in isolation, plus one sanity test
against the real repo tree.
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

import beacon_register as br  # noqa: E402


@pytest.fixture()
def fixture_scripts(tmp_path: Path) -> Path:
    root = tmp_path / "scripts"
    root.mkdir()
    return root


def _write(root: Path, relative: str, content: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(content), encoding="utf-8")


def test_literal_string_argument_resolved(fixture_scripts: Path) -> None:
    _write(fixture_scripts, "a.py", """
        from health_beacon import HealthBeacon
        HealthBeacon(state_dir, "cleanup_worker_exit", expected_interval_seconds=None).heartbeat()
    """)
    reg = br.read_beacon_register(fixture_scripts)
    assert {s.name for s in reg} == {"cleanup_worker_exit"}


def test_module_level_constant_argument_resolved(fixture_scripts: Path) -> None:
    _write(fixture_scripts, "b.py", """
        from health_beacon import HealthBeacon
        COMPONENT_NAME = "ledger_health"
        BEACON_EXPECTED_INTERVAL_SECONDS = 86400

        def run():
            HealthBeacon(data_dir, COMPONENT_NAME, expected_interval_seconds=BEACON_EXPECTED_INTERVAL_SECONDS)
    """)
    reg = br.read_beacon_register(fixture_scripts)
    assert {s.name for s in reg} == {"ledger_health"}


def test_loop_variable_argument_is_excluded_not_guessed(fixture_scripts: Path) -> None:
    """subsystem_health.py's real shape: the component name is a per-iteration
    loop variable, not a literal or a module-level constant. There is no
    fixed name to register, so the call site must be silently excluded."""
    _write(fixture_scripts, "subsystem_health.py", """
        from health_beacon import HealthBeacon

        def aggregate(state_dir, subsystems):
            for name in subsystems:
                HealthBeacon(state_dir, name).heartbeat(status="ok")
    """)
    reg = br.read_beacon_register(fixture_scripts)
    assert reg == ()


def test_function_local_constant_is_not_module_level_so_excluded(fixture_scripts: Path) -> None:
    """Only MODULE-level string constants are resolved -- a same-named local
    assigned inside a function is a different, unresolvable binding as far
    as this static parser is concerned (it never executes the function)."""
    _write(fixture_scripts, "c.py", """
        from health_beacon import HealthBeacon

        def run():
            COMPONENT_NAME = "local_only"
            HealthBeacon(state_dir, COMPONENT_NAME)
    """)
    reg = br.read_beacon_register(fixture_scripts)
    assert reg == ()


def test_component_keyword_argument_resolved(fixture_scripts: Path) -> None:
    _write(fixture_scripts, "d.py", """
        from health_beacon import HealthBeacon
        HealthBeacon(state_dir, component="kwarg_writer")
    """)
    reg = br.read_beacon_register(fixture_scripts)
    assert {s.name for s in reg} == {"kwarg_writer"}


def test_duplicate_call_sites_same_name_dedupe(fixture_scripts: Path) -> None:
    """learning_loop.py's and intelligence_daemon.py's real shape: the same
    literal name appears at two call sites in one file."""
    _write(fixture_scripts, "e.py", """
        from health_beacon import HealthBeacon

        def a():
            HealthBeacon(state_dir, "learning_loop", expected_interval_seconds=86400)

        def b():
            HealthBeacon(state_dir, "learning_loop", expected_interval_seconds=86400)
    """)
    reg = br.read_beacon_register(fixture_scripts)
    assert len(reg) == 1
    assert reg[0].name == "learning_loop"


def test_syntax_error_file_skipped_not_raised(fixture_scripts: Path) -> None:
    # Must mention "HealthBeacon(" so the substring pre-filter doesn't skip
    # it before ast.parse ever runs — this test is specifically about the
    # ast.parse(SyntaxError) except-branch, not the pre-filter.
    _write(fixture_scripts, "broken.py", """
        def not valid python(:::
        HealthBeacon(state_dir, "unreachable")
    """)
    _write(fixture_scripts, "ok.py", """
        from health_beacon import HealthBeacon
        HealthBeacon(state_dir, "fine")
    """)
    reg = br.read_beacon_register(fixture_scripts)
    assert {s.name for s in reg} == {"fine"}


def test_file_with_no_healthbeacon_mention_is_never_parsed(fixture_scripts: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The cheap substring pre-filter must actually skip ast.parse for files
    that don't mention HealthBeacon( at all -- this is what keeps the
    register cheap enough to call on every T0 session start."""
    _write(fixture_scripts, "unrelated.py", "x = 1\n")

    import ast as _ast
    calls = []
    real_parse = _ast.parse

    def _spy(*a, **k):
        calls.append(a[1] if len(a) > 1 else k.get("filename"))
        return real_parse(*a, **k)

    monkeypatch.setattr(br.ast, "parse", _spy)
    br.read_beacon_register(fixture_scripts)
    assert not any("unrelated.py" in str(c) for c in calls)


def test_expected_component_names_is_just_the_names(fixture_scripts: Path) -> None:
    _write(fixture_scripts, "f.py", """
        from health_beacon import HealthBeacon
        HealthBeacon(state_dir, "one")
    """)
    reg = br.read_beacon_register(fixture_scripts)
    assert br.expected_component_names(reg) == ("one",)


def test_real_scripts_tree_gives_the_nine_measured_writers() -> None:
    """Sanity check against the actual repo (measured 2026-08-30): 9
    statically-resolvable HealthBeacon(...) call sites. subsystem_health.py
    is the 10th HealthBeacon(...)-mentioning file but its call site is a
    loop variable, so it is correctly excluded (see the fixture test above)."""
    reg = br.read_beacon_register()
    names = {s.name for s in reg}
    assert names == {
        "t0_state_builder",
        "conversation_analyzer",
        "fleet_role_drift",
        "intelligence_daemon",
        "learning_loop",
        "ledger_health",
        "cleanup_worker_exit",
        "producer_freshness_monitor",
        "report_to_receipt_converter",
    }
