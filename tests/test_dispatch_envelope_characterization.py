"""test_dispatch_envelope_characterization.py — PR-0 of the dispatch_envelope
monolith split (dispatch-monolith-split, PR-0 of 7).

scripts/lib/dispatch_envelope.py (1980 lines) is being split into
envelope_types.py, envelope_prepare.py, envelope_govern_support.py,
envelope_govern.py, envelope_adapters_claude.py and envelope_adapters_provider.py
across PR-1..PR-6. dispatch_envelope.py stays the public address and
re-exports every relocated symbol. This file is the vangnet that makes the
resulting drift LOUD instead of silent, before any of that code moves.

The rule every layer below assumes: a name-string coupling
(`patch("dispatch_envelope.X")`, `logger="dispatch_envelope"`) resolves
against the globals of the module the CALLER lives in, not the new home of
the symbol X. Move the symbol but not the caller -> the facade re-export
keeps the patch working. Move the caller -> the patch stops binding, silently,
because dispatch_envelope's globals no longer hold the name the caller
resolves at call time. See dispatch_envelope_census_scanner.py's module
docstring for the concrete failure this caused across three plan-gate rounds.

Six layers:
  1. Import surface   — every name a real consumer imports stays importable.
  2. Census scanner    — generated inventory of every coupling in tests/ to
                          the dispatch_envelope module family, matched by a
                          dynamically-discovered family (never a fixed list).
  3. Site bindings      — the 6 concretely-exposed sites; each is patched,
                          exercised, and its interception verified directly.
  4. Annotation resolve — typing.get_type_hints on every symbol that will
                          move, dynamically discovered.
  5. Import graph       — no envelope_* module may import dispatch_envelope.
  6. Facade bind-form   — re-exports must use `from X import Y` + bare-name
                          calls, never the attribute form that silently
                          defeats patch-string.

No production code changes in this PR (test-only).
"""
from __future__ import annotations

import ast
import contextlib
import inspect
import json
import logging
import typing
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import dispatch_envelope
from dispatch_envelope import EnvelopeSpec, _AdapterResult

from dispatch_envelope_census_scanner import (
    CENSUS_FIXTURE,
    SCRIPTS_LIB,
    Coupling,
    discover_family,
    find_facade_attribute_calls,
    scan_facade_bindings,
    scan_tests_dir,
    verify_family_pattern_gap,
)


def _fake_adapter_result(status: str = "success") -> _AdapterResult:
    return _AdapterResult(returncode=0, completion_text="ok", status=status)


def _make_spec(tmp_path: Path, dispatch_id: str) -> EnvelopeSpec:
    state_dir = tmp_path / "state"
    state_dir.mkdir(exist_ok=True)
    data_dir = tmp_path / "data"
    (data_dir / "unified_reports").mkdir(parents=True, exist_ok=True)
    return EnvelopeSpec(
        dispatch_id=dispatch_id,
        terminal_id="T9",
        provider="codex",
        model="test-model",
        instruction="characterization probe",
        role=None,
        pr_id=None,
        state_dir=state_dir,
        data_dir=data_dir,
    )


@contextlib.contextmanager
def _stub_peripheral_govern_deps(receipt_return):
    """Stub every _govern dependency NOT under test in a given Layer 3 case,
    so each test isolates the one coupling it verifies. receipt_return is
    the Path emit_dispatch_receipt should report back (must already exist
    on disk — _govern fail-closes on a missing receipt file)."""
    with patch("governance_emit.emit_unified_report", return_value=None), \
         patch("governance_emit.emit_dispatch_receipt", return_value=receipt_return) as mock_receipt, \
         patch("provider_costs.emit_provider_cost"), \
         patch("phantom_guard.record_phantom_if_any"), \
         patch("phantom_guard.record_guard_error"):
        yield mock_receipt


# ---------------------------------------------------------------------------
# Layer 1 — import surface
# ---------------------------------------------------------------------------


def _discover_consumer_surface() -> frozenset:
    """Derived from the real consumers via AST — not hand-typed. Every name
    dispatch_cli.py / provider_dispatch.py actually import from
    dispatch_envelope today, plus the four PR-1 types (EnvelopeSpec,
    EnvelopeResult, EnvelopeGovernError, _AdapterResult) which no consumer
    imports today but which PR-1 moves and which the facade must re-export."""
    names = set()
    for consumer in ("dispatch_cli.py", "provider_dispatch.py"):
        path = SCRIPTS_LIB / consumer
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "dispatch_envelope":
                names.update(alias.name for alias in node.names)
    names |= {"EnvelopeSpec", "EnvelopeResult", "EnvelopeGovernError", "_AdapterResult"}
    return frozenset(names)


CONSUMER_SURFACE = _discover_consumer_surface()


class TestLayer1ImportSurface:
    """dispatch_envelope.py stays the public address: every name a real
    consumer imports today, plus the four PR-1 types, must remain importable
    from the facade through every step of the PR-1..PR-6 split."""

    @pytest.mark.parametrize("name", sorted(CONSUMER_SURFACE))
    def test_name_importable_from_facade(self, name):
        assert hasattr(dispatch_envelope, name), (
            f"dispatch_envelope.{name} is no longer importable — a re-export "
            f"was dropped in a split PR. Real consumers: "
            f"scripts/lib/dispatch_cli.py, scripts/lib/provider_dispatch.py."
        )


# ---------------------------------------------------------------------------
# Layer 2 — census scanner
# ---------------------------------------------------------------------------


def _census_diff(live: list, expected: list) -> str:
    live_set, expected_set = set(live), set(expected)
    added = sorted(live_set - expected_set)
    removed = sorted(expected_set - live_set)
    lines = ["census mismatch vs tests/data/dispatch_envelope_census.json"]
    if added:
        lines.append(f"  + {len(added)} coupling(s) the live scan found but the fixture doesn't have:")
        lines.extend(f"      {c.file}:{c.line} [{c.mechanism}] {c.module_target}.{c.symbol}" for c in added)
    if removed:
        lines.append(f"  - {len(removed)} coupling(s) the fixture has but the live scan no longer finds:")
        lines.extend(f"      {c.file}:{c.line} [{c.mechanism}] {c.module_target}.{c.symbol}" for c in removed)
    lines.append("  regenerate with: python3 tests/dispatch_envelope_census_scanner.py")
    return "\n".join(lines)


class TestLayer2Census:
    """The census is GENERATED (tests/data/dispatch_envelope_census.json),
    not hand-maintained — see dispatch_envelope_census_scanner.py's module
    docstring for why a fixed module-name list is the wrong design.

    THIS COMPARISON IS SUPPOSED TO GO RED when PR-3/PR-4 relocate the exposed
    sites onto envelope_* modules. That is bedoeld gedrag, not flake:
    regenerate the fixture in the SAME commit that moves the code, and let
    the diff in the failure message BE the review evidence. Do not "fix" a
    red here by loosening this comparison — that defeats the entire point of
    this file.
    """

    def test_no_family_pattern_gap(self):
        gap = verify_family_pattern_gap()
        assert not gap, "\n".join(gap)

    def test_every_envelope_module_in_scope(self):
        """A scripts/lib/envelope_*.py that exists on disk must be part of
        the discovered family. Vacuous today (glob is empty pre-PR-1); binds
        the moment the first envelope_*.py file appears."""
        family = discover_family()
        for f in SCRIPTS_LIB.glob("envelope_*.py"):
            assert f.stem in family, f"{f} exists on disk but discover_family() missed it"

    def test_census_matches_fixture(self):
        family = discover_family()
        live = scan_tests_dir(family=family)
        fixture = json.loads(CENSUS_FIXTURE.read_text(encoding="utf-8"))
        assert sorted(fixture["family"]) == sorted(family), (
            f"fixture was generated against family={sorted(fixture['family'])!r} "
            f"but the live family is {sorted(family)!r} — regenerate the fixture"
        )
        expected = sorted(Coupling.from_dict(d) for d in fixture["couplings"])
        assert live == expected, _census_diff(live, expected)


# ---------------------------------------------------------------------------
# Layer 3 — binding per exposed site
# ---------------------------------------------------------------------------


class TestLayer3ArchiveClearBinding:
    """Sites 1, 2, 3, 4 all patch the SAME two symbols
    (_archive_dispatch_events, _clear_dispatch_events) against the SAME
    caller (_govern, dispatch_envelope.py lines ~840 and ~1070):
      - site 1: tests/test_dispatch_envelope.py:236  (asserted at line 248)
      - site 2: tests/test_dispatch_envelope.py:237  (sibling assert at 248)
      - site 3: tests/test_role_applied_provider_lane.py:211 (NO assertion)
      - site 4: tests/test_role_applied_provider_lane.py:212 (NO assertion)
    Sites 3/4 are the ones that would go silently green after a relocation:
    the patch stops binding, the REAL functions run instead, and nothing in
    that test file notices because it only asserts role_applied fields. This
    class supplies the missing assertion independently of whether either
    pre-existing file keeps testing it.

    SAFETY (sites 3/4's real risk): if the patch below did NOT bind, the REAL
    _clear_dispatch_events would truncate the live event stream for
    spec.terminal_id under whatever VNX_DATA_DIR is ambient. Every fixture
    here uses tmp_path exclusively for state_dir/data_dir — an unbound patch
    can only ever touch a throwaway directory, never a live
    .vnx-data/events/T{n}.ndjson.
    """

    def test_archive_and_clear_called_via_govern(self, tmp_path):
        spec = _make_spec(tmp_path, "layer3-archive-clear")
        result = _fake_adapter_result()
        start = end = datetime.now(timezone.utc)
        receipt_file = spec.state_dir / "t0_receipts.ndjson"
        receipt_file.touch()

        mock_archive = MagicMock(return_value=(None, True))
        mock_clear = MagicMock()

        with patch("dispatch_envelope._archive_dispatch_events", mock_archive), \
             patch("dispatch_envelope._clear_dispatch_events", mock_clear), \
             _stub_peripheral_govern_deps(receipt_file):
            dispatch_envelope._govern(spec, result, start, end)

        mock_archive.assert_called_once_with(spec.terminal_id, spec.dispatch_id)
        mock_clear.assert_called_once_with(spec.terminal_id, spec.dispatch_id)


class TestLayer3Site5And6LoggerName:
    """Sites 5 & 6 both capture via
    `caplog.at_level(logging.WARNING, logger="dispatch_envelope")`:
      - site 5: tests/test_dispatch_envelope.py:557 (reached via _govern)
      - site 6: tests/test_dispatch_envelope.py:591 (direct call, dispatch_envelope.py:593)

    FINDING (not corrected — see Open Items): both actually bind against
    _receipt_exists_for_dispatch's OWN `logging.getLogger(__name__)`, i.e.
    the SYMBOL's eventual home module — not wherever _govern (site 5's
    caller) ends up. The PR-0 dispatch predicted site 5 breaks in "PR-3 or
    PR-4" and site 6 in "PR-3"; since both routes execute the same
    _receipt_exists_for_dispatch logger call, they will actually break
    together, in whichever PR relocates _receipt_exists_for_dispatch itself.
    """

    def test_site5_via_govern_logs_under_dispatch_envelope_logger(self, tmp_path, caplog):
        spec = _make_spec(tmp_path, "layer3-site5-logger")
        result = _fake_adapter_result()
        start = end = datetime.now(timezone.utc)

        receipt_path = spec.state_dir / "t0_receipts.ndjson"
        receipt_path.write_text('{"dispatch_id":"other-dispatch"}\n', encoding="utf-8")

        _real_open = open

        def _selective_open(path, *args, **kwargs):
            if str(receipt_path) in str(path):
                raise OSError("Permission denied")
            return _real_open(path, *args, **kwargs)

        with caplog.at_level(logging.WARNING, logger="dispatch_envelope"), \
             patch("dispatch_envelope._archive_dispatch_events", return_value=(None, True)), \
             patch("dispatch_envelope._clear_dispatch_events"), \
             _stub_peripheral_govern_deps(receipt_path) as mock_receipt, \
             patch("builtins.open", side_effect=_selective_open):
            dispatch_envelope._govern(spec, result, start, end)

        # fail-closed dedup: the OSError makes _receipt_exists_for_dispatch
        # return True, so the emit branch (and mock_receipt) is skipped.
        mock_receipt.assert_not_called()
        assert any(
            "cannot read receipt ledger" in r.message and "Permission denied" in r.message
            for r in caplog.records
        ), f"expected a WARNING about the unreadable ledger, got: {[r.message for r in caplog.records]}"

    def test_site6_direct_call_logs_under_dispatch_envelope_logger(self, tmp_path, caplog):
        receipt_path = tmp_path / "t0_receipts.ndjson"
        receipt_path.write_text('{"dispatch_id":"some-id"}\n', encoding="utf-8")

        _real_open = open

        def _raise_oserror(path, *args, **kwargs):
            if str(receipt_path) in str(path):
                raise OSError("Permission denied")
            return _real_open(path, *args, **kwargs)

        with caplog.at_level(logging.WARNING, logger="dispatch_envelope"), \
             patch("builtins.open", side_effect=_raise_oserror):
            result = dispatch_envelope._receipt_exists_for_dispatch(receipt_path, "some-id")

        assert result is True
        assert any(
            "cannot read receipt ledger" in r.message and "Permission denied" in r.message
            for r in caplog.records
        ), f"expected a WARNING about the unreadable ledger, got: {[r.message for r in caplog.records]}"


# ---------------------------------------------------------------------------
# Layer 4 — annotation resolution
# ---------------------------------------------------------------------------

# OI-288 (tests/test_dispatch_envelope_annotations_resolve.py) already covers
# these four — not duplicated here.
_OI288_COVERED = frozenset({
    ("dispatch_envelope.ProviderAdapter", "run"),
    ("dispatch_envelope.ProviderAdapter", "_run_kimi"),
    ("dispatch_envelope", "run_envelope_plan"),
    ("dispatch_envelope", "run_envelope_headless_plan"),
})


def _discover_module_functions() -> dict:
    """Every function/method DEFINED in dispatch_envelope.py (not imported
    from elsewhere), keyed by (owner_label, attr_name). get_type_hints
    resolves against a function object's OWN __globals__ regardless of which
    module name is used to reach it — so this stays a valid regression test
    after the symbol relocates, as long as the facade keeps re-exporting it."""
    found = {}
    for name, obj in vars(dispatch_envelope).items():
        if name.startswith("__"):
            continue
        if inspect.isfunction(obj) and getattr(obj, "__module__", None) == "dispatch_envelope":
            found[("dispatch_envelope", name)] = obj
        elif inspect.isclass(obj) and getattr(obj, "__module__", None) == "dispatch_envelope":
            for meth_name, meth in vars(obj).items():
                if inspect.isfunction(meth):
                    found[(f"dispatch_envelope.{name}", meth_name)] = meth
    return found


def _discover_module_classes() -> dict:
    return {
        ("dispatch_envelope", name): obj
        for name, obj in vars(dispatch_envelope).items()
        if inspect.isclass(obj) and getattr(obj, "__module__", None) == "dispatch_envelope"
    }


_ALL_FUNCS = _discover_module_functions()
_NEW_FUNC_KEYS = sorted(k for k in _ALL_FUNCS if k not in _OI288_COVERED)
_ALL_CLASSES = _discover_module_classes()
_ALL_CLASS_KEYS = sorted(_ALL_CLASSES)


class TestLayer4AnnotationResolution:
    """typing.get_type_hints resolves against func.__globals__ — the module a
    symbol is DEFINED in, not wherever a facade re-exports it from. A symbol
    that moves to a module which only imports its annotated type under
    TYPE_CHECKING breaks introspection with NameError post-move (this is the
    exact bug OI-288 caught for ExecutionPlan/ExecutionPermit before the
    runtime imports at dispatch_envelope.py:47-48 existed).

    Discovered dynamically (inspect.isfunction/isclass filtered to
    __module__ == "dispatch_envelope") instead of hand-listed, so a symbol
    added after this PR is covered automatically without editing this file.
    """

    @pytest.mark.parametrize("owner,attr", _NEW_FUNC_KEYS)
    def test_function_annotations_resolve(self, owner, attr):
        func = _ALL_FUNCS[(owner, attr)]
        try:
            typing.get_type_hints(func)
        except NameError as exc:
            pytest.fail(f"{owner}.{attr}: unresolved annotation name: {exc}")

    @pytest.mark.parametrize("owner,name", _ALL_CLASS_KEYS)
    def test_class_annotations_resolve(self, owner, name):
        cls = _ALL_CLASSES[(owner, name)]
        try:
            typing.get_type_hints(cls)
        except NameError as exc:
            pytest.fail(f"{owner}.{name}: unresolved annotation name: {exc}")


# ---------------------------------------------------------------------------
# Layer 5 — import graph
# ---------------------------------------------------------------------------


class TestLayer5ImportGraph:
    """No envelope_* module may import dispatch_envelope — one-way traffic,
    facade depends on siblings, never the reverse. Vacuous today (no
    envelope_* modules exist); the >=540-line guard makes sure "empty"
    means "nothing has moved yet", not "this check stopped looking"."""

    def test_no_envelope_module_imports_the_facade(self):
        family = discover_family()
        submodules = sorted(family - {"dispatch_envelope"})
        facade_path = SCRIPTS_LIB / "dispatch_envelope.py"
        facade_lines = len(facade_path.read_text(encoding="utf-8").splitlines())

        if not submodules:
            assert facade_lines >= 540, (
                "zero envelope_* modules exist AND the facade has already shrunk "
                f"below 540 lines ({facade_lines}) — PR-1..PR-6 have started "
                "moving code out but no envelope_*.py landed on disk; this check "
                "can no longer tell 'nothing moved yet' from 'stopped looking'"
            )
            pytest.skip(
                f"no envelope_* modules exist yet (facade is {facade_lines} lines) "
                "— this test binds once the first envelope_*.py module lands"
            )

        for modname in submodules:
            path = SCRIPTS_LIB / f"{modname}.py"
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module == "dispatch_envelope":
                    pytest.fail(f"{modname}.py:{node.lineno} imports FROM dispatch_envelope — cycle")
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        if alias.name == "dispatch_envelope":
                            pytest.fail(f"{modname}.py:{node.lineno} imports dispatch_envelope — cycle")


# ---------------------------------------------------------------------------
# Layer 6 — facade bind-form
# ---------------------------------------------------------------------------


class TestLayer6FacadeBindForm:
    """Re-export alone is not enough. Nine patch-sites stay safe only as
    long as the facade both re-exports a relocated symbol AND calls it by
    BARE NAME:

        from envelope_govern_support import _archive_dispatch_events   # OK: patch binds
        import envelope_govern_support                                 # BAD: patch does NOT bind

    Calling it as `envelope_govern_support._archive_dispatch_events(...)`
    resolves through the module object, and `patch("dispatch_envelope.X")`
    never sees it — a silent green. Vacuous today (no envelope_* modules
    exist); binds the moment the first one appears (mirrors Layer 5).
    """

    def test_facade_reimports_use_bare_name_form(self):
        family = discover_family()
        facade_path = SCRIPTS_LIB / "dispatch_envelope.py"
        submodules = sorted(family - {"dispatch_envelope"})

        if not submodules:
            facade_lines = len(facade_path.read_text(encoding="utf-8").splitlines())
            assert facade_lines >= 540, (
                "zero envelope_* modules exist AND the facade has already shrunk "
                f"below 540 lines ({facade_lines}) — see TestLayer5's identical guard"
            )
            pytest.skip("no envelope_* modules exist yet — binds once the first one lands")

        tracker = scan_facade_bindings(facade_path, family)

        assert not tracker.module_imports, (
            "dispatch_envelope.py imports an envelope_* module via the bare "
            "`import X` (attribute-call) form — patch(\"dispatch_envelope.<name>\") "
            f"will not bind for anything re-exported that way: {tracker.module_imports}"
        )
        assert tracker.from_imports, (
            "expected at least one `from envelope_* import ...` re-export in the "
            "facade now that envelope_* modules exist"
        )

        # Defence in depth: redundant with the module_imports check above
        # (that's the only way such an alias could get bound today) — kept
        # explicit so a future indirect binding mechanism doesn't slip through.
        module_alias_names = frozenset(local for (_, _, local) in tracker.module_imports)
        hits = find_facade_attribute_calls(facade_path, module_alias_names)
        assert not hits, f"facade calls an envelope_* symbol via attribute form: {hits}"
