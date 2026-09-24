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


# ---------------------------------------------------------------------------
# Event-driven writers (absence-is-loud, punt 1 / defect B)
#
# A component whose call site passes a literal ``expected_interval_seconds=None``
# writes only when something happens. Its silence is not a finding, so the
# register must not put it in the ``expected`` set that turns silence into
# ``absent``. Asserted through ``expected_component_names`` (the one place every
# reader gets its expected set from), not through a new attribute.
# ---------------------------------------------------------------------------


def test_keyword_none_interval_is_event_driven_and_not_expected(fixture_scripts: Path) -> None:
    _write(fixture_scripts, "evt.py", """
        from health_beacon import HealthBeacon
        HealthBeacon(state_dir, "evt", expected_interval_seconds=None)
        HealthBeacon(state_dir, "per", expected_interval_seconds=3600)
    """)
    reg = br.read_beacon_register(fixture_scripts)
    assert {s.name for s in reg} == {"evt", "per"}, "the register still lists the event-driven writer"
    assert br.expected_component_names(reg) == ("per",)


def test_positional_none_interval_is_event_driven(fixture_scripts: Path) -> None:
    _write(fixture_scripts, "evt.py", """
        from health_beacon import HealthBeacon
        HealthBeacon(state_dir, "evt", None)
    """)
    reg = br.read_beacon_register(fixture_scripts)
    assert br.expected_component_names(reg) == ()


def test_omitted_interval_is_periodic_not_event_driven(fixture_scripts: Path) -> None:
    """HealthBeacon's own default is 86400: leaving the argument out is a daily
    writer, so its absence stays loud."""
    _write(fixture_scripts, "per.py", """
        from health_beacon import HealthBeacon
        HealthBeacon(state_dir, "per")
    """)
    reg = br.read_beacon_register(fixture_scripts)
    assert br.expected_component_names(reg) == ("per",)


def test_interval_that_is_not_a_literal_none_stays_expected(fixture_scripts: Path) -> None:
    """An interval the parser cannot prove is None (a name, an expression) is
    treated as periodic. Guessing event-driven there would hide a real silence."""
    _write(fixture_scripts, "var.py", """
        from health_beacon import HealthBeacon
        INTERVAL = None
        HealthBeacon(state_dir, "by_name", expected_interval_seconds=INTERVAL)
        HealthBeacon(state_dir, "by_expr", expected_interval_seconds=cfg.interval or None)
    """)
    reg = br.read_beacon_register(fixture_scripts)
    assert br.expected_component_names(reg) == ("by_expr", "by_name")


def test_a_periodic_call_site_makes_the_whole_component_expected(fixture_scripts: Path) -> None:
    """One component, two writers: if any of them promises a cadence, the
    component owes a beacon, so it is only event-driven when EVERY site says so."""
    _write(fixture_scripts, "mixed.py", """
        from health_beacon import HealthBeacon

        def on_event():
            HealthBeacon(state_dir, "mixed", expected_interval_seconds=None)

        def on_schedule():
            HealthBeacon(state_dir, "mixed", expected_interval_seconds=3600)
    """)
    _write(fixture_scripts, "both_none.py", """
        from health_beacon import HealthBeacon

        def a():
            HealthBeacon(state_dir, "both_none", expected_interval_seconds=None)

        def b():
            HealthBeacon(state_dir, "both_none", None)
    """)
    reg = br.read_beacon_register(fixture_scripts)
    assert br.expected_component_names(reg) == ("mixed",)


def test_real_tree_cleanup_worker_exit_is_event_driven_and_fleet_role_drift_is_not() -> None:
    """The two real shapes: cleanup_worker_exit passes ``expected_interval_seconds=None``
    (it writes when a worker exits), fleet_role_drift a daily 86400."""
    expected = set(br.expected_component_names())
    assert "cleanup_worker_exit" not in expected
    assert "fleet_role_drift" in expected
    assert "cleanup_worker_exit" in {s.name for s in br.read_beacon_register()}


# ---------------------------------------------------------------------------
# PARKED_COMPONENTS / parked_component_names (golf C, C2a)
# ---------------------------------------------------------------------------


def test_parked_component_names_is_the_intelligence_layer() -> None:
    """Operator decision 2026-09-09: the intelligence layer stays parked
    until the governance ledger is falsifiable (per the PRD) -- both
    already-silent components get a deliberate status, not a removal.
    Extended 2026-09-24 with the cockpit subsystem beacon that measures the
    same learning loop."""
    assert br.parked_component_names() == (
        "intelligence-self-learning-loop", "intelligence_daemon", "learning_loop",
    )


def test_parked_components_are_names_a_beacon_writer_can_actually_carry() -> None:
    """A parked name that no writer can ever produce parks nothing, silently.
    Each name is either a resolvable HealthBeacon(...) call site
    (read_beacon_register) or a cockpit subsystem whose beacon
    subsystem_health.aggregate() writes through a loop variable, which the
    register deliberately cannot resolve (see the module docstring)."""
    import subsystem_health

    register_names = {s.name for s in br.read_beacon_register()}
    subsystem_names = set(subsystem_health.known_subsystems())
    unreachable = set(br.parked_component_names()) - register_names - subsystem_names
    assert not unreachable, f"parked names no beacon writer can produce: {unreachable}"


def test_the_self_learning_loop_beacon_is_parked_but_not_an_expected_writer() -> None:
    """It is a subsystem beacon, not a HealthBeacon(...) literal: parking must not
    make it ``expected``, which would turn its silence into ``absent``."""
    assert "intelligence-self-learning-loop" in br.parked_component_names()
    assert "intelligence-self-learning-loop" not in br.expected_component_names()


# ---------------------------------------------------------------------------
# find_duplicate_beacon_writers (golf C, C2a) -- forced red/green via
# tmp_path fixtures, per the dispatch's own "een test die twee schrijfpaden
# voor dezelfde component rood maakt" instruction.
# ---------------------------------------------------------------------------


def test_find_duplicate_beacon_writers_flags_a_forced_collision(tmp_path: Path) -> None:
    primary = tmp_path / "health"
    secondary = tmp_path / "state" / "health"
    primary.mkdir(parents=True)
    secondary.mkdir(parents=True)
    (primary / "report_to_receipt_converter.json").write_text("{}", encoding="utf-8")
    (secondary / "report_to_receipt_converter.json").write_text("{}", encoding="utf-8")

    dupes = br.find_duplicate_beacon_writers(tmp_path)
    assert set(dupes.keys()) == {"report_to_receipt_converter"}
    primary_path, secondary_path = dupes["report_to_receipt_converter"]
    assert primary_path == primary / "report_to_receipt_converter.json"
    assert secondary_path == secondary / "report_to_receipt_converter.json"


def test_find_duplicate_beacon_writers_clean_when_only_one_root_has_the_file(tmp_path: Path) -> None:
    primary = tmp_path / "health"
    secondary = tmp_path / "state" / "health"
    primary.mkdir(parents=True)
    secondary.mkdir(parents=True)
    (primary / "only_here.json").write_text("{}", encoding="utf-8")

    assert br.find_duplicate_beacon_writers(tmp_path) == {}


def test_find_duplicate_beacon_writers_missing_secondary_root_is_clean(tmp_path: Path) -> None:
    (tmp_path / "health").mkdir(parents=True)
    (tmp_path / "health" / "only_here.json").write_text("{}", encoding="utf-8")

    assert br.find_duplicate_beacon_writers(tmp_path) == {}


def test_real_scripts_tree_gives_the_nine_measured_writers() -> None:
    """Sanity check against the actual repo (measured 2026-09-05, was 9 on
    2026-08-30): 10 statically-resolvable HealthBeacon(...) call sites.
    subsystem_health.py is an 11th HealthBeacon(...)-mentioning file but its
    call site is a loop variable, so it is correctly excluded (see the
    fixture test above).

    golf3b / F1-2 legitimately adds the 10th: receipt_processor.sh's own
    stderr-capture fix pipes the converter's captured stderr into
    scripts/lib/receipt_conversion_rejection_beacon.py, a NEW beacon writer
    (component "receipt_conversion_rejections") that records per-report
    dispatch_id/file/reason detail the converter's own beacon (counts only)
    does not carry. This is the same category of deliberate addition the
    D3a/D3b fix-forwards already bumped this measurement for — not a name
    accidentally picked up by the scan.
    """
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
        "receipt_conversion_rejections",
    }
