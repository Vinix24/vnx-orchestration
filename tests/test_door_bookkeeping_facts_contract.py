"""tests/test_door_bookkeeping_facts_contract.py — dispatch-20260903-deur-boekhouding-luid /
OI-1617: a call-site guard for the door's bookkeeping exception handlers, mirroring
tests/test_phantom_guard_context_contract.py's AST-scan pattern (PR #1748, OI-1614) — which
itself mirrors tests/test_sessionstart_hook_beacon_health.py's (PR #1742, OI-1594) — rather
than inventing a new one.

Defect this closes: five ``except Exception`` blocks in the door (``scripts/lib/dispatch_cli.py``
— ``_persist_dispatch_row``, ``_register_gate_obligation``, ``_persist_track_id``, the
checkout-lag resolution, the scout pre-pass) each caught every exception (correctly — door
bookkeeping must never block the door) and called ONLY ``logger.debug(...)`` on it. Measured on
this process: the root logger carries no handlers and ``dispatch_cli``'s own logger has none
either, so a ``.debug()`` call is discarded before it reaches any sink — the failure left
literally no trace, to no one, ever. On 2026-09-03 this is exactly what happened to dispatch
``20260903-oi1614-guard-contract``: ``_register_gate_obligation`` failed silently, the gate had
already PASSed with valid evidence on the correct head-sha, and the door answered "NOT READY —
no review-gate obligation declared" with zero trace of why (OI-1617).

The fix (``scripts/lib/dispatch_cli.py::_record_bookkeeping_failure``) does NOT stop the
swallowing — a bookkeeping failure must still never block the door, that contract stands. It
makes the swallow leave a FACT: a ``door_bookkeeping_failed`` event appended to
``dispatch_register.ndjson``, the same ledger ``build_t0_state.py`` already folds into
``dispatch_register_events`` on every T0 state build (a consumer that is ALREADY running, not a
new file nobody opens), carrying a ``site`` field so each of the five call sites — or a future
sixth — is distinguishable, never collapsed into "something in the door went wrong".

This test is the STATIC backstop: an AST scan over the door module's ``except Exception``
handlers, flagging any whose body calls ``logger.debug(...)`` (the literal historic bug shape)
without also calling ``_record_bookkeeping_failure(...)`` — so a future silent-except added
anywhere in the door is caught in CI before it ever runs dark, the same "catch the call site
that doesn't exist yet" property the beacon test's call-site guard and the phantom-guard
call-site guard both have.

Three fixture-tree test classes prove the mechanism can actually fail, mirroring
TestCallSiteGuardCanActuallyFail in both prior guards: a debug-only except with no fact call
turns the assertion red; a compliant one (calling ``_record_bookkeeping_failure``) passes; an
untracked file and a docstring-only mention are both invisible to the scan, same as the priors.

Part 4 (mirroring the phantom-guard test's own Part 4) is a BEHAVIORAL verdict test — not the
call-site shape, but the actual OUTCOME: monkeypatch a real bookkeeping writer to raise, call the
real ``dispatch_cli`` function, and assert (a) it does not raise (the door keeps going) and (b) a
``door_bookkeeping_failed`` fact with the right ``site`` actually landed in
``dispatch_register.ndjson`` — proving the wiring, not just the shape.
"""
from __future__ import annotations

import ast
import json
import subprocess
import sys
import types
from pathlib import Path
from typing import Optional

import pytest

REPO = Path(__file__).resolve().parent.parent
LIB = REPO / "scripts" / "lib"
sys.path.insert(0, str(LIB))

import dispatch_cli  # noqa: E402

_FACT_RECORDER = "_record_bookkeeping_failure"
_DEBUG_CALL_NAME = "debug"
_DOOR_RELATIVE_PATHS = frozenset({"scripts/lib/dispatch_cli.py"})
_KNOWN_SITES = frozenset({
    "_persist_dispatch_row",
    "_register_gate_obligation",
    "_persist_track_id",
    "checkout_lag_resolution",
    "scout_prepass",
})


def _git_tracked_py_files(root: Path) -> list[Path]:
    """Absolute paths to every git-tracked ``.py`` file under ``root``.

    Same property as the beacon test's and the phantom-guard test's identically-named
    helper (OI-1597): tracked-ness, not a directory namelist, keeps a build artifact /
    nested worktree / CI rollout copy out of the scan regardless of what it's named or
    where it lives. Fails CLOSED — raises ``RuntimeError`` — when git is unavailable,
    rather than silently falling back to an incomplete namelist.
    """
    try:
        result = subprocess.run(
            ["git", "ls-files", "-z", "--", "*.py"],
            cwd=str(root), capture_output=True, timeout=10,
        )
    except OSError as exc:
        raise RuntimeError(
            f"git is not available to determine tracked .py files under {root}"
        ) from exc
    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"`git ls-files` failed under {root} (not a git work tree?): {stderr}")
    stdout = result.stdout.decode("utf-8")
    return [root / rel for rel in stdout.split("\0") if rel]


def _call_target_name(node: ast.Call) -> Optional[str]:
    """The bare callee name of an ast.Call — 'foo' for both foo(...) and mod.foo(...)."""
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _handler_catches_exception(handler: ast.ExceptHandler) -> bool:
    """True for a bare ``except:`` or an ``except Exception[ as x]:`` (incl. a tuple
    that includes ``Exception``) — the broad catch-everything shape this guard cares
    about. A narrower ``except OSError:`` etc. is out of scope: it cannot swallow an
    arbitrary bookkeeping failure the same way."""
    t = handler.type
    if t is None:
        return True
    if isinstance(t, ast.Name) and t.id == "Exception":
        return True
    if isinstance(t, ast.Tuple):
        return any(isinstance(elt, ast.Name) and elt.id == "Exception" for elt in t.elts)
    return False


def _find_debug_only_swallowed_exceptions(root: Path) -> list[tuple[str, int]]:
    """Every ``except Exception`` handler in the door module(s) that calls
    ``logger.debug(...)`` (or ``log.debug(...)``) without also calling
    ``_record_bookkeeping_failure(...)`` anywhere in its body.

    An unparseable or non-UTF-8 file raises a clean AssertionError naming the file,
    instead of letting the raw SyntaxError/UnicodeDecodeError traceback escape
    (mirrors both prior guards' advisory).
    """
    findings: list[tuple[str, int]] = []
    for full_path in _git_tracked_py_files(root):
        rel = str(full_path.relative_to(root))
        if rel not in _DOOR_RELATIVE_PATHS:
            continue
        try:
            source = full_path.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(full_path))
        except (SyntaxError, UnicodeDecodeError, ValueError) as exc:
            raise AssertionError(
                f"cannot parse {rel} while scanning for door bookkeeping exception handlers: {exc}"
            ) from exc
        for node in ast.walk(tree):
            if not isinstance(node, ast.ExceptHandler):
                continue
            if not _handler_catches_exception(node):
                continue
            calls = [n for n in ast.walk(node) if isinstance(n, ast.Call)]
            has_debug_call = any(_call_target_name(c) == _DEBUG_CALL_NAME for c in calls)
            if not has_debug_call:
                continue
            has_fact_call = any(_call_target_name(c) == _FACT_RECORDER for c in calls)
            if not has_fact_call:
                findings.append((rel, node.lineno))
    return findings


def _find_bookkeeping_failure_sites(root: Path) -> set[str]:
    """The set of literal ``site=`` (first positional arg) strings passed to
    ``_record_bookkeeping_failure(...)`` across the door module(s).

    Only literal string first arguments are resolved — the same "refuse to guess, an
    unresolvable call site is simply absent from the result" discipline the phantom-guard
    test's ``_context_field_names`` applies, kept intentionally permissive here (a
    non-literal call site is not itself a defect this guard is chartered to catch).
    """
    sites: set[str] = set()
    for full_path in _git_tracked_py_files(root):
        rel = str(full_path.relative_to(root))
        if rel not in _DOOR_RELATIVE_PATHS:
            continue
        source = full_path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(full_path))
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and _call_target_name(node) == _FACT_RECORDER):
                continue
            if node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
                sites.add(node.args[0].value)
    return sites


class TestNoDebugOnlySwallowedExceptionsInTheDoor:
    """The wachter: every ``except Exception`` in the door that logs on debug must also
    record a fact. Does not enumerate a fixed baseline of line numbers (unlike a simple
    regression list) — a brand-new bookkeeping call site added later that repeats the old
    debug-only shape is caught automatically, without this test needing to be edited."""

    def test_all_known_bookkeeping_sites_are_found(self):
        sites = _find_bookkeeping_failure_sites(REPO)
        missing = _KNOWN_SITES - sites
        assert not missing, f"expected door bookkeeping site(s) not found: {missing}"

    def test_no_debug_only_swallowed_exception_in_the_door(self):
        findings = _find_debug_only_swallowed_exceptions(REPO)
        assert not findings, (
            "except Exception block(s) in the door log on debug only, with no durable "
            "fact recorded (dispatch-20260903-deur-boekhouding-luid / OI-1617):\n"
            + "\n".join(f"{rel}:{lineno}" for rel, lineno in findings)
        )


def _git_init(root: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=str(root), check=True)


def _git_add(root: Path, *relative_paths: str) -> None:
    subprocess.run(["git", "add", "--", *relative_paths], cwd=str(root), check=True)


def _write_door_fixture(root: Path, body: str) -> None:
    door = root / "scripts" / "lib" / "dispatch_cli.py"
    door.parent.mkdir(parents=True, exist_ok=True)
    door.write_text(body, encoding="utf-8")


_COMPLIANT_HANDLER = (
    "def f(spec, state_dir):\n"
    "    try:\n"
    "        risky()\n"
    "    except Exception as exc:\n"
    "        logger.debug('site failed: %s', exc)\n"
    "        _record_bookkeeping_failure('f', spec.dispatch_id, exc, state_dir=state_dir)\n"
)

_DEBUG_ONLY_HANDLER = (
    "def f(spec, state_dir):\n"
    "    try:\n"
    "        risky()\n"
    "    except Exception as exc:\n"
    "        logger.debug('site failed: %s', exc)\n"
)


class TestCallSiteGuardCanActuallyFail:
    """A wachter die niet kan falen, faalt stil. ``_find_debug_only_swallowed_exceptions``
    is exercised directly against isolated fixture trees (never the real repo) so this
    guard's MECHANISM is proven, not just today's five call sites."""

    def test_a_compliant_handler_passes(self, tmp_path):
        _git_init(tmp_path)
        _write_door_fixture(tmp_path, _COMPLIANT_HANDLER)
        _git_add(tmp_path, "scripts/lib/dispatch_cli.py")

        assert _find_debug_only_swallowed_exceptions(tmp_path) == []

    def test_a_debug_only_handler_turns_the_assertion_red(self, tmp_path):
        _git_init(tmp_path)
        _write_door_fixture(tmp_path, _DEBUG_ONLY_HANDLER)
        _git_add(tmp_path, "scripts/lib/dispatch_cli.py")

        findings = _find_debug_only_swallowed_exceptions(tmp_path)
        assert findings == [("scripts/lib/dispatch_cli.py", 4)]

    def test_a_non_debug_except_without_a_fact_call_is_not_flagged(self, tmp_path):
        """Out of scope by design: an except-Exception fallback with no logging at all
        (e.g. a safe default value) is a different, already-visible-downstream pattern —
        not the debug-swallow shape this guard targets. Flagging it would be noise."""
        _git_init(tmp_path)
        _write_door_fixture(
            tmp_path,
            "def f():\n"
            "    try:\n"
            "        return risky()\n"
            "    except Exception:\n"
            "        return 'fallback'\n",
        )
        _git_add(tmp_path, "scripts/lib/dispatch_cli.py")

        assert _find_debug_only_swallowed_exceptions(tmp_path) == []

    def test_a_docstring_mention_does_not_trip_it(self, tmp_path):
        _git_init(tmp_path)
        _write_door_fixture(
            tmp_path,
            '"""Eventually calls logger.debug(...) without _record_bookkeeping_failure."""\n'
            "# see logger.debug for details\n",
        )
        _git_add(tmp_path, "scripts/lib/dispatch_cli.py")

        assert _find_debug_only_swallowed_exceptions(tmp_path) == []

    def test_an_untracked_door_file_is_invisible(self, tmp_path):
        """A door file that exists on disk but was never `git add`-ed (a build artifact,
        a nested worktree, a scratch file) is invisible to the scan — same property the
        beacon and phantom-guard tests' guards rely on (OI-1597). `git add`-ing the exact
        same content afterward makes it visible, isolating tracked-ness as the variable."""
        _git_init(tmp_path)
        _write_door_fixture(tmp_path, _DEBUG_ONLY_HANDLER)
        # Deliberately never git add-ed.
        assert _find_debug_only_swallowed_exceptions(tmp_path) == []

        _git_add(tmp_path, "scripts/lib/dispatch_cli.py")
        assert _find_debug_only_swallowed_exceptions(tmp_path) == [
            ("scripts/lib/dispatch_cli.py", 4)
        ]

    def test_an_unparseable_door_file_gives_a_clean_assertion_not_a_traceback(self, tmp_path):
        _git_init(tmp_path)
        _write_door_fixture(tmp_path, "def broken(:\n    pass\n")
        _git_add(tmp_path, "scripts/lib/dispatch_cli.py")

        with pytest.raises(AssertionError, match="dispatch_cli.py"):
            _find_debug_only_swallowed_exceptions(tmp_path)


# ---------------------------------------------------------------------------
# Part 4: behavioral verdict test — the fix's OUTCOME, not the call-site shape.
# A test that only asserted "_record_bookkeeping_failure appears in the source" would
# test this PR's own mechanism and break on the next refactor; this forces a REAL
# bookkeeping writer to raise and asserts on the actual ndjson record that lands.
# ---------------------------------------------------------------------------


def _read_ndjson(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


class TestSwallowedBookkeepingFailureStillRecordsAFact:
    """The concrete OI-1617 incident, replayed through the real dispatch_cli functions:
    force the underlying write to raise, then assert on the OUTCOME (the door kept going,
    and dispatch_register.ndjson holds the fact) — never on the presence of a log call."""

    def test_persist_dispatch_row_failure_does_not_raise_and_leaves_a_fact(self, tmp_path, monkeypatch):
        state_dir = tmp_path / "state"
        state_dir.mkdir(parents=True)
        (state_dir / "runtime_coordination.db").touch()

        import sqlite3

        def _raising_connect(*_args, **_kwargs):
            raise sqlite3.OperationalError("simulated: database is locked")

        monkeypatch.setattr(sqlite3, "connect", _raising_connect)
        spec = types.SimpleNamespace(dispatch_id="20260903-behavioral-persist-row")

        # (a) the door keeps going — no exception escapes _persist_dispatch_row.
        dispatch_cli._persist_dispatch_row(spec, state_dir=state_dir)

        # (b) the failure left a durable, per-site fact in the already-consumed ledger.
        records = _read_ndjson(state_dir / "dispatch_register.ndjson")
        facts = [r for r in records if r.get("event") == "door_bookkeeping_failed"]
        assert facts, f"expected a door_bookkeeping_failed record, got: {records}"
        assert facts[-1]["dispatch_id"] == "20260903-behavioral-persist-row"
        assert facts[-1]["extra"]["site"] == "_persist_dispatch_row"
        assert "OperationalError" in facts[-1]["extra"]["error"]

    def test_register_gate_obligation_failure_does_not_raise_and_leaves_a_fact(self, tmp_path, monkeypatch):
        state_dir = tmp_path / "state"
        state_dir.mkdir(parents=True)

        def _raising_register_obligation(*_args, **_kwargs):
            raise OSError("simulated: obligation write failed")

        import gate_obligations
        monkeypatch.setattr(gate_obligations, "register_obligation", _raising_register_obligation)

        spec = types.SimpleNamespace(
            gate="codex_gate", dispatch_id="20260903-behavioral-gate-obligation",
            project_id="vnx-dev", pr_id=None,
        )

        # (a) the door keeps going — no exception escapes _register_gate_obligation.
        dispatch_cli._register_gate_obligation(spec, state_dir=state_dir)

        # (b) the failure left a durable, per-site fact — the exact OI-1617 gap.
        records = _read_ndjson(state_dir / "dispatch_register.ndjson")
        facts = [r for r in records if r.get("event") == "door_bookkeeping_failed"]
        assert facts, f"expected a door_bookkeeping_failed record, got: {records}"
        assert facts[-1]["dispatch_id"] == "20260903-behavioral-gate-obligation"
        assert facts[-1]["extra"]["site"] == "_register_gate_obligation"
        assert "obligation write failed" in facts[-1]["extra"]["error"]


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
