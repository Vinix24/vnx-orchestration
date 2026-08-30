"""tests/test_build_t0_state_exit_codes.py — D1 poort D regression coverage.

Before D1, build_t0_state.py's main() returned 0 for every outcome except
"build succeeded but system_health is degraded/failed" (already rc=1). A
build that raised inside the try/except (the crash poort A used to trigger
on any mixed naive/aware timestamp set) was caught, logged as a WARNING,
and then main() fell through to `return 0` — so a caller checking the exit
code (build_t0_state_hook.sh's `BUILD_RC`) could never tell "the build
crashed and wrote nothing" from "everything is fine".

This file locks the three distinct outcomes main() must now report:
  - _EXIT_OK (0):              healthy build, state written
  - _EXIT_HEALTH_DEGRADED (1): build succeeded, state WAS written, but
                                system_health.status is degraded/failed —
                                a fabric-health judgment, not a build defect
  - _EXIT_BUILD_FAILED (2):    the build raised before a state file could
                                be produced — the true "not refreshed" case

These must stay numerically distinct: build_t0_state_hook.sh branches on
the exact rc to decide whether "t0_state.json not refreshed" is true.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, Optional

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_LIB = _ROOT / "scripts" / "lib"
_SCRIPTS = _ROOT / "scripts"

for p in (_LIB, _SCRIPTS):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import build_t0_state as bts  # noqa: E402


def _run_main(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    build_result: Optional[Dict[str, Any]] = None,
    build_exc: Optional[Exception] = None,
) -> int:
    out = tmp_path / "t0_state.json"
    monkeypatch.setattr(sys, "argv", ["build_t0_state.py", "--output", str(out)])
    monkeypatch.setattr(bts, "_STATE_DIR", tmp_path)
    # Isolate from real disk/beacon side effects — this test is only about
    # the return-code contract, not the write/beacon paths those exercise
    # elsewhere (test_build_t0_state_freshness.py, hook install tests).
    monkeypatch.setattr(bts, "_write_atomic", lambda *_a, **_k: None)
    monkeypatch.setattr(bts, "_write_all_state_outputs", lambda *_a, **_k: False)
    monkeypatch.setattr(bts, "_emit_build_signal", lambda *_a, **_k: None)
    monkeypatch.setattr(bts, "_emit_health_beacon", lambda *_a, **_k: None)

    if build_exc is not None:
        def _raise(*_a: object, **_k: object) -> Dict[str, Any]:
            raise build_exc
        monkeypatch.setattr(bts, "build_t0_state", _raise)
    else:
        result = dict(build_result or {})
        monkeypatch.setattr(bts, "build_t0_state", lambda *_a, **_k: dict(result))

    return bts.main()


def test_exit_codes_are_distinct() -> None:
    codes = {bts._EXIT_OK, bts._EXIT_HEALTH_DEGRADED, bts._EXIT_BUILD_FAILED}
    assert len(codes) == 3, "the three outcomes must not collapse onto the same rc"


def test_healthy_build_returns_exit_ok(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    rc = _run_main(monkeypatch, tmp_path, build_result={"system_health": {"status": "healthy"}})
    assert rc == bts._EXIT_OK == 0


def test_build_exception_returns_exit_build_failed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """D1 root cause: a crash inside build_t0_state() used to fall through
    to `return 0` after being logged. A caller trusting rc==0 never learned
    the build stopped happening at all — this is the case that let
    t0_state.json go stale for 23 days without a single visible signal."""
    rc = _run_main(monkeypatch, tmp_path, build_exc=RuntimeError("can't compare offset-naive and offset-aware datetimes"))
    assert rc == bts._EXIT_BUILD_FAILED == 2


def test_degraded_system_health_returns_exit_health_degraded_not_failed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """A build that succeeded and wrote t0_state.json, but whose
    system_health.status is 'degraded', must NOT report the same rc as a
    build that crashed and wrote nothing (D1 'PAS OP' distinction)."""
    rc = _run_main(monkeypatch, tmp_path, build_result={"system_health": {"status": "degraded"}})
    assert rc == bts._EXIT_HEALTH_DEGRADED == 1
    assert rc != bts._EXIT_BUILD_FAILED


def test_failed_system_health_returns_exit_health_degraded(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    rc = _run_main(monkeypatch, tmp_path, build_result={"system_health": {"status": "failed"}})
    assert rc == bts._EXIT_HEALTH_DEGRADED == 1


def test_missing_system_health_defaults_to_healthy(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    rc = _run_main(monkeypatch, tmp_path, build_result={})
    assert rc == bts._EXIT_OK == 0
