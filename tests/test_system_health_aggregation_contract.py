"""tests/test_system_health_aggregation_contract.py — OI-1511 forward guard (Golf 3B).

OI-1511 named a real, reproduced defect: t0_state.json carried
``system_health.status == "healthy"`` sitting right next to
``system_health.beacon_health.overall == "fail"`` in the SAME object -- a
summary field that never revisited itself after reading the nested signal
underneath it. Measured live 2026-08-29.

That literal defect is ALREADY fixed on this branch's main (PR #1732 / D2,
extended by PR #1755 / Golf 1B): ``_build_system_health`` now folds every
nested signal it reads through ``health_status.worst_status()`` before
assigning ``status``, and 24 behavior tests in
``tests/test_system_health_monotonic.py`` cover beacon_health,
daemon_liveness, launchd_liveness and the producer_liveness composite --
including the exact "producer measured as NOT running next to
status=healthy" scenario this dispatch asked to reproduce (see that file's
line 227). Live reproduction against this repo's real central store
(``~/.vnx-data/vnx-dev/state``) on 2026-09-04 also could not reproduce the
literal bug: ``status`` correctly floors at ``"degraded"`` while every
nested ``.overall`` reads ``"fail"``.

What none of those tests catch: the NEXT nested signal someone adds. The
function's own comment (build_t0_state.py, inside ``_build_system_health``)
says "Every nested health field added here must feed this aggregation" --
but that is prose, not an enforced contract. A worker who later adds a
fifth signal (say ``queue_watcher_liveness``) by writing
``result["queue_watcher_liveness"] = queue_watcher_liveness`` and forgetting
to also read it into the ``worst_status(...)`` call reproduces OI-1511's
exact defect shape in a brand-new field -- and every existing behavior test
stays green, because none of them know that field exists yet. That is
precisely "een reparatie op één samenvattingsplek laat de volgende die
iemand toevoegt ongedekt."

This is the static backstop for that gap, mirroring
``tests/test_phantom_guard_context_contract.py``'s AST-scan pattern rather
than inventing a new one: it re-parses ``_build_system_health``'s actual
source on every run and checks a structural invariant, so a future
violation is caught by CI before any specific runtime scenario reproduces
it.

Scope decision: unlike ``record_phantom_if_any`` (called from THREE separate
files, which is why that guard scans the whole git-tracked tree),
``system_health``'s nested signals are folded in exactly ONE place --
``_build_system_health`` in this one file. A tree-wide ``git ls-files`` scan
would buy nothing extra over checking the one function where the mechanism
actually lives, so this guard targets that function directly. This file's
write scope is limited to ``scripts/build_t0_state.py`` and its tests (Golf
3B dispatch instruction) -- ``scripts/lib/health_status.py`` and
``scripts/cli/vnx_status.py`` (the other documented ``worst_status`` caller)
are out of scope here and untouched.
"""
from __future__ import annotations

import ast
import sys
import textwrap
from pathlib import Path
from typing import Optional

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "scripts"
LIB = SCRIPTS / "lib"
for _p in (LIB, SCRIPTS):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import build_t0_state as bts  # noqa: E402

_TARGET_FUNC = "_build_system_health"
_FOLD_CALL = "worst_status"


def _call_target_name(node: ast.Call) -> Optional[str]:
    """The bare callee name of an ast.Call -- 'foo' for both foo(...) and mod.foo(...)."""
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _find_function(tree: ast.Module, name: str) -> ast.FunctionDef:
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"could not find function {name!r} in the parsed source")


def _raw_signal_result_assignments(func: ast.FunctionDef) -> dict:
    """Map of ``result["<key>"]`` -> RHS variable name, for every direct
    ``result["<key>"] = <bare name>`` assignment inside ``func`` (at any
    nesting depth -- some real ones sit inside an ``if x is not None:``
    guard).

    A dict-LITERAL RHS (e.g. ``result["producer_liveness"] = {...}``) is a
    DERIVED value built from signals that are themselves already raw
    passthroughs elsewhere -- not a second raw source -- and is deliberately
    excluded by this structural pattern. No name list to keep in sync: the
    exclusion falls straight out of "is the RHS a bare Name or not", which is
    exactly the distinction between "this IS one of system_health's raw
    nested signals" and "this is computed FROM raw signals already covered".
    """
    out: dict = {}
    for node in ast.walk(func):
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if not (
            isinstance(target, ast.Subscript)
            and isinstance(target.value, ast.Name)
            and target.value.id == "result"
        ):
            continue
        key_node = target.slice
        if not isinstance(key_node, ast.Constant) or not isinstance(key_node.value, str):
            continue
        if not isinstance(node.value, ast.Name):
            continue  # derived value, not a raw signal passthrough -- see docstring
        out[key_node.value] = node.value.id
    return out


def _fold_call_referenced_names(func: ast.FunctionDef, fold_call: str) -> set:
    """Every ``ast.Name`` id referenced anywhere inside the argument list of
    the (first) call to ``fold_call`` found in ``func``. Raises loudly if no
    such call exists at all -- an absent fold call is not "zero violations",
    it is "nothing is being checked"."""
    for node in ast.walk(func):
        if isinstance(node, ast.Call) and _call_target_name(node) == fold_call:
            names: set = set()
            for arg in node.args:
                for sub in ast.walk(arg):
                    if isinstance(sub, ast.Name):
                        names.add(sub.id)
            return names
    raise AssertionError(
        f"no call to {fold_call}(...) found inside {func.name} -- the aggregation "
        "mechanism itself is missing, not merely incomplete"
    )


def _violations(func: ast.FunctionDef) -> dict:
    """Keys assigned a raw signal dict into ``result`` whose source variable
    is never referenced inside the ``worst_status(...)`` call -- i.e. a
    nested health signal that can disagree with the top-level ``status``
    without anything catching it. Empty dict means every raw signal this
    function assigns into ``result`` is also read by the fold call."""
    raw = _raw_signal_result_assignments(func)
    folded = _fold_call_referenced_names(func, _FOLD_CALL)
    return {key: var for key, var in raw.items() if var not in folded}


class TestSystemHealthAggregationContract:
    """The wachter: every raw nested health signal _build_system_health
    assigns into its result dict must also be read inside the
    worst_status(...) call in that same function."""

    def test_target_function_exists_in_the_real_module(self) -> None:
        assert hasattr(bts, _TARGET_FUNC), (
            f"{_TARGET_FUNC} was renamed or removed -- this guard's target no longer exists"
        )

    def test_current_source_folds_every_raw_nested_signal(self) -> None:
        source = (SCRIPTS / "build_t0_state.py").read_text(encoding="utf-8")
        tree = ast.parse(source, filename="build_t0_state.py")
        func = _find_function(tree, _TARGET_FUNC)
        violations = _violations(func)
        assert not violations, (
            f"{_TARGET_FUNC} assigns result[...] from a raw signal variable that is never "
            f"read inside worst_status(...): {violations!r} -- a summary field that ignores "
            "one of its own nested signals is OI-1511's exact defect shape"
        )

    def test_known_signals_are_actually_detected_not_a_vacuous_pass(self) -> None:
        """Nul is eerst een meetfout: prove the scanner finds the THREE
        signals known to exist today (beacon_health, daemon_liveness,
        launchd_liveness), so an empty-violations result above means
        'checked three real signals and found them clean', not 'found
        nothing to check'."""
        source = (SCRIPTS / "build_t0_state.py").read_text(encoding="utf-8")
        tree = ast.parse(source, filename="build_t0_state.py")
        func = _find_function(tree, _TARGET_FUNC)
        raw = _raw_signal_result_assignments(func)
        assert raw == {
            "beacon_health": "beacon_health",
            "daemon_liveness": "daemon_liveness",
            "launchd_liveness": "launchd_liveness",
        }

    def test_producer_liveness_is_excluded_as_a_derived_value_not_a_raw_signal(self) -> None:
        """producer_liveness is itself folded FROM daemon_liveness and
        launchd_liveness (both already required above) via
        _combine_liveness_overall -- it must not show up in the raw-signal
        map at all, since its RHS is a dict literal, not a bare passthrough."""
        source = (SCRIPTS / "build_t0_state.py").read_text(encoding="utf-8")
        tree = ast.parse(source, filename="build_t0_state.py")
        func = _find_function(tree, _TARGET_FUNC)
        raw = _raw_signal_result_assignments(func)
        assert "producer_liveness" not in raw

    def test_guard_can_actually_fail_on_an_unfolded_new_signal(self) -> None:
        """Fixture-tree proof (mirrors TestCallSiteGuardCanActuallyFail in
        test_phantom_guard_context_contract.py): a synthetic function that
        adds a fourth raw signal but forgets to fold it into worst_status(...)
        must turn this scanner red -- the literal shape a future OI-1511
        regression would take."""
        broken_source = textwrap.dedent(
            '''
            def _build_system_health(state_dir, db_initialized, *, some_new_liveness=None):
                status = "healthy"
                beacon_health = {"overall": "ok"}
                daemon_liveness = {"overall": "ok"}
                launchd_liveness = {"overall": "ok"}
                if some_new_liveness is None:
                    some_new_liveness = {"overall": "fail"}
                status = worst_status(
                    status,
                    beacon_health.get("overall") if beacon_health else None,
                    daemon_liveness.get("overall") if daemon_liveness else None,
                    launchd_liveness.get("overall") if launchd_liveness else None,
                )
                result = {"status": status}
                result["beacon_health"] = beacon_health
                result["daemon_liveness"] = daemon_liveness
                result["launchd_liveness"] = launchd_liveness
                result["some_new_liveness"] = some_new_liveness
                return result
            '''
        )
        tree = ast.parse(broken_source, filename="<fixture-broken>")
        func = _find_function(tree, _TARGET_FUNC)
        violations = _violations(func)
        assert violations == {"some_new_liveness": "some_new_liveness"}, (
            "the guard failed to detect an unfolded new signal in the broken fixture -- "
            f"got {violations!r}"
        )

    def test_guard_passes_once_the_fixture_folds_the_new_signal(self) -> None:
        """Same fixture, fixed -- proves the scanner is not simply always
        red, and shows the exact one-line fix a future worker would make."""
        fixed_source = textwrap.dedent(
            '''
            def _build_system_health(state_dir, db_initialized, *, some_new_liveness=None):
                status = "healthy"
                beacon_health = {"overall": "ok"}
                daemon_liveness = {"overall": "ok"}
                launchd_liveness = {"overall": "ok"}
                if some_new_liveness is None:
                    some_new_liveness = {"overall": "fail"}
                status = worst_status(
                    status,
                    beacon_health.get("overall") if beacon_health else None,
                    daemon_liveness.get("overall") if daemon_liveness else None,
                    launchd_liveness.get("overall") if launchd_liveness else None,
                    some_new_liveness.get("overall") if some_new_liveness else None,
                )
                result = {"status": status}
                result["beacon_health"] = beacon_health
                result["daemon_liveness"] = daemon_liveness
                result["launchd_liveness"] = launchd_liveness
                result["some_new_liveness"] = some_new_liveness
                return result
            '''
        )
        tree = ast.parse(fixed_source, filename="<fixture-fixed>")
        func = _find_function(tree, _TARGET_FUNC)
        assert _violations(func) == {}

    def test_fold_call_missing_entirely_raises_not_silently_passes(self) -> None:
        """A fixture with no worst_status(...) call at all must raise loudly
        -- 'the mechanism is gone' is a louder failure than 'zero
        violations', and must never be silently read as the latter."""
        no_fold_source = textwrap.dedent(
            '''
            def _build_system_health(state_dir, db_initialized):
                beacon_health = {"overall": "fail"}
                result = {"status": "healthy"}
                result["beacon_health"] = beacon_health
                return result
            '''
        )
        tree = ast.parse(no_fold_source, filename="<fixture-no-fold>")
        func = _find_function(tree, _TARGET_FUNC)
        try:
            _violations(func)
        except AssertionError as exc:
            assert "worst_status" in str(exc)
        else:
            raise AssertionError("expected _violations to raise when no fold call exists")
