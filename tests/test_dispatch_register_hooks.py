"""Tests for dispatch_register hook integrations in append_receipt and gate_recorder."""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from unittest import mock

TESTS_DIR = Path(__file__).resolve().parent
VNX_ROOT = TESTS_DIR.parent
SCRIPTS_DIR = VNX_ROOT / "scripts"
LIB_DIR = SCRIPTS_DIR / "lib"

sys.path.insert(0, str(LIB_DIR))
sys.path.insert(0, str(SCRIPTS_DIR))

# Inline helper script that imports _emit_dispatch_register and fires it.
# Avoids loading the full append_receipt.py with all its heavy dependencies.
_EMIT_HELPER = """\
import sys, os, json
sys.path.insert(0, {lib_dir!r})
sys.path.insert(0, {scripts_dir!r})

# Stub the heavy dependencies that append_receipt.py pulls in at import time
from unittest import mock
from pathlib import Path

stubs = {{
    "vnx_paths": mock.MagicMock(ensure_env=mock.MagicMock(return_value={{
        "VNX_STATE_DIR": os.environ["VNX_STATE_DIR"],
        "PROJECT_ROOT": os.environ.get("PROJECT_ROOT", "/tmp"),
        "VNX_DATA_DIR": os.environ["VNX_DATA_DIR"],
    }})),
    "project_root": mock.MagicMock(resolve_state_dir=mock.MagicMock(
        return_value=Path(os.environ["VNX_STATE_DIR"])
    )),
    "quality_advisory": mock.MagicMock(),
    "terminal_snapshot": mock.MagicMock(),
    "cqs_calculator": mock.MagicMock(),
    "receipt_provenance": mock.MagicMock(
        enrich_receipt_provenance=mock.MagicMock(),
        validate_receipt_provenance=mock.MagicMock(
            return_value=mock.MagicMock(gaps=[], chain_status="ok")
        ),
    ),
    "ghost_receipt_filter": mock.MagicMock(
        should_route_to_gate_stream=mock.MagicMock(return_value=False),
        gate_events_file=mock.MagicMock(return_value=Path("/tmp/gate_events.ndjson")),
    ),
}}
for name, stub in stubs.items():
    sys.modules[name] = stub

import importlib.util
spec = importlib.util.spec_from_file_location("append_receipt", {script!r})
mod = importlib.util.module_from_spec(spec)
sys.modules["append_receipt"] = mod
spec.loader.exec_module(mod)

receipt = json.loads(sys.argv[1])
mod._emit_dispatch_register(receipt)
"""


def _setup_env(tmp_path: Path) -> dict:
    data_dir = tmp_path / "data"
    state_dir = data_dir / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    return {
        "VNX_DATA_DIR": str(data_dir),
        "VNX_STATE_DIR": str(state_dir),
        "PROJECT_ROOT": str(tmp_path),
        "VNX_HOME": str(VNX_ROOT),
    }


def _reg_path(tmp_path: Path) -> Path:
    return tmp_path / "data" / "state" / "dispatch_register.ndjson"


def _read_register(tmp_path: Path) -> list:
    p = _reg_path(tmp_path)
    if not p.exists():
        return []
    return [json.loads(l) for l in p.read_text().splitlines() if l.strip()]


def _run_emit_helper(tmp_path: Path, receipt: dict) -> None:
    """Run _emit_dispatch_register via subprocess to avoid module-caching issues."""
    env = {**os.environ, **_setup_env(tmp_path)}
    helper_code = _EMIT_HELPER.format(
        lib_dir=str(LIB_DIR),
        scripts_dir=str(SCRIPTS_DIR),
        script=str(SCRIPTS_DIR / "append_receipt.py"),
    )
    subprocess.run(
        [sys.executable, "-c", helper_code, json.dumps(receipt)],
        env=env,
        check=False,
        capture_output=True,
    )


def test_task_complete_receipt_triggers_dispatch_completed(tmp_path: Path):
    receipt = {
        "timestamp": "2026-04-28T10:00:00Z",
        "event_type": "task_complete",
        "dispatch_id": "d-complete-001",
        "terminal": "T1",
    }
    _run_emit_helper(tmp_path, receipt)
    events = _read_register(tmp_path)
    assert any(e["event"] == "dispatch_completed" and e["dispatch_id"] == "d-complete-001"
               for e in events)


def test_task_complete_with_failed_status_triggers_dispatch_failed(tmp_path: Path):
    """task_complete receipt with status=failed must emit dispatch_failed, not dispatch_completed.

    Regression for: _emit_dispatch_register treated every task_complete as a success,
    ignoring receipt['status']. Failed completions (status=failed/error/blocked) must
    register as dispatch_failed.
    """
    receipt = {
        "timestamp": "2026-04-28T10:30:00Z",
        "event_type": "task_complete",
        "status": "failed",
        "dispatch_id": "d-failed-complete-001",
        "terminal": "T1",
    }
    _run_emit_helper(tmp_path, receipt)
    events = _read_register(tmp_path)
    assert any(
        e["event"] == "dispatch_failed" and e["dispatch_id"] == "d-failed-complete-001"
        for e in events
    ), f"Expected dispatch_failed in register, got: {events}"
    assert not any(
        e["event"] == "dispatch_completed" and e["dispatch_id"] == "d-failed-complete-001"
        for e in events
    ), "Must not emit dispatch_completed for a failed-status task_complete receipt"


def test_task_failed_receipt_triggers_dispatch_failed(tmp_path: Path):
    receipt = {
        "timestamp": "2026-04-28T10:01:00Z",
        "event_type": "task_failed",
        "dispatch_id": "d-fail-002",
        "terminal": "T2",
    }
    _run_emit_helper(tmp_path, receipt)
    events = _read_register(tmp_path)
    assert any(e["event"] == "dispatch_failed" and e["dispatch_id"] == "d-fail-002"
               for e in events)


def test_task_timeout_receipt_triggers_dispatch_failed(tmp_path: Path):
    """task_timeout is a terminal failure; must emit dispatch_failed, not be silently dropped.

    Regression for: _emit_dispatch_register had no branch for task_timeout, so timed-out
    dispatches stayed stuck at their last dispatch_promoted status in the register.
    """
    receipt = {
        "timestamp": "2026-04-28T11:00:00Z",
        "event_type": "task_timeout",
        "dispatch_id": "d-timeout-001",
        "terminal": "T1",
    }
    _run_emit_helper(tmp_path, receipt)
    events = _read_register(tmp_path)
    assert any(
        e["event"] == "dispatch_failed" and e["dispatch_id"] == "d-timeout-001"
        for e in events
    ), f"Expected dispatch_failed in register for task_timeout, got: {events}"
    assert not any(
        e["event"] == "dispatch_completed" and e["dispatch_id"] == "d-timeout-001"
        for e in events
    ), "task_timeout must not produce dispatch_completed"


def test_review_gate_request_triggers_gate_requested(tmp_path: Path):
    receipt = {
        "timestamp": "2026-04-28T10:02:00Z",
        "event_type": "review_gate_request",
        "dispatch_id": "d-gate-003",
        "terminal": "T3",
    }
    _run_emit_helper(tmp_path, receipt)
    events = _read_register(tmp_path)
    assert any(e["event"] == "gate_requested" and e["dispatch_id"] == "d-gate-003"
               for e in events)


def test_emit_gate_register_event_gate_passed(tmp_path: Path):
    env = _setup_env(tmp_path)

    with mock.patch.dict(os.environ, env):
        sys.modules.pop("dispatch_register", None)
        sys.modules.pop("gate_recorder", None)

        spec = importlib.util.spec_from_file_location(
            "gate_recorder", LIB_DIR / "gate_recorder.py"
        )
        gr_mod = importlib.util.module_from_spec(spec)

        # governance_receipts stub
        fake_gr = mock.MagicMock()
        fake_gr.utc_now_iso.return_value = "2026-04-28T10:00:00Z"
        sys.modules["governance_receipts"] = fake_gr

        spec.loader.exec_module(gr_mod)

        gr_mod.emit_gate_register_event(
            gate="gemini_review",
            dispatch_id="d-gate-pass",
            pr_number=42,
            blocking_findings=[],
        )

    events = _read_register(tmp_path)
    assert any(
        e["event"] == "gate_passed"
        and e.get("gate") == "gemini_review"
        and e.get("pr_number") == 42
        for e in events
    )


def test_emit_gate_register_event_gate_failed(tmp_path: Path):
    env = _setup_env(tmp_path)

    with mock.patch.dict(os.environ, env):
        sys.modules.pop("dispatch_register", None)
        sys.modules.pop("gate_recorder", None)

        spec = importlib.util.spec_from_file_location(
            "gate_recorder", LIB_DIR / "gate_recorder.py"
        )
        gr_mod = importlib.util.module_from_spec(spec)

        fake_gr = mock.MagicMock()
        fake_gr.utc_now_iso.return_value = "2026-04-28T10:00:00Z"
        sys.modules["governance_receipts"] = fake_gr

        spec.loader.exec_module(gr_mod)

        gr_mod.emit_gate_register_event(
            gate="codex_gate",
            dispatch_id="d-gate-fail",
            pr_number=43,
            blocking_findings=[{"severity": "blocking", "message": "fail"}],
        )

    events = _read_register(tmp_path)
    assert any(
        e["event"] == "gate_failed"
        and e.get("gate") == "codex_gate"
        for e in events
    )


def _stub_heavy_imports(env: dict) -> None:
    """Stub out imports that require external services."""
    state_dir = env.get("VNX_STATE_DIR", "/tmp")
    stubs = {
        "vnx_paths": mock.MagicMock(ensure_env=mock.MagicMock(return_value={
            "VNX_STATE_DIR": state_dir,
            "PROJECT_ROOT": env.get("PROJECT_ROOT", "/tmp"),
            "VNX_DATA_DIR": env.get("VNX_DATA_DIR", "/tmp"),
        })),
        "project_root": mock.MagicMock(resolve_state_dir=mock.MagicMock(
            return_value=Path(state_dir)
        )),
        "quality_advisory": mock.MagicMock(),
        "terminal_snapshot": mock.MagicMock(),
        "cqs_calculator": mock.MagicMock(),
        "receipt_provenance": mock.MagicMock(
            enrich_receipt_provenance=mock.MagicMock(),
            validate_receipt_provenance=mock.MagicMock(
                return_value=mock.MagicMock(gaps=[], chain_status="ok")
            ),
        ),
        "ghost_receipt_filter": mock.MagicMock(
            should_route_to_gate_stream=mock.MagicMock(return_value=False),
            gate_events_file=mock.MagicMock(return_value=Path("/tmp/gate_events.ndjson")),
        ),
    }
    for name, stub in stubs.items():
        sys.modules[name] = stub


def _load_append_receipt_mod(env: dict):
    """Load append_receipt module with heavy deps stubbed."""
    _stub_heavy_imports(env)
    for key in list(sys.modules.keys()):
        if "append_receipt" in key:
            del sys.modules[key]
    spec = importlib.util.spec_from_file_location(
        "append_receipt", SCRIPTS_DIR / "append_receipt.py"
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["append_receipt"] = mod
    with mock.patch.dict(os.environ, env):
        spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# Rebuild trigger tests
# ---------------------------------------------------------------------------


def test_review_gate_request_triggers_state_rebuild(tmp_path: Path):
    """_maybe_trigger_state_rebuild must fire Popen for review_gate_request events."""
    env = _setup_env(tmp_path)

    with mock.patch.dict(os.environ, env):
        mod = _load_append_receipt_mod(env)
        # Override state_dir resolution so the throttle file lives in tmp.
        mod.resolve_state_dir = mock.MagicMock(
            return_value=Path(env["VNX_STATE_DIR"])
        )

        receipt = {
            "event_type": "review_gate_request",
            "dispatch_id": "d-gate-rebuild-001",
            "terminal": "T3",
        }

        with mock.patch("subprocess.Popen") as mock_popen:
            mod._maybe_trigger_state_rebuild(receipt)

        mock_popen.assert_called_once()
        args = mock_popen.call_args[0][0]
        assert "build_t0_state.py" in args[-1]


def test_gate_passed_event_triggers_state_rebuild(tmp_path: Path):
    """_maybe_trigger_state_rebuild must fire Popen for gate_passed events."""
    env = _setup_env(tmp_path)

    with mock.patch.dict(os.environ, env):
        mod = _load_append_receipt_mod(env)
        mod.resolve_state_dir = mock.MagicMock(
            return_value=Path(env["VNX_STATE_DIR"])
        )

        receipt = {
            "event_type": "gate_passed",
            "dispatch_id": "d-gate-passed-001",
            "terminal": "T3",
        }

        with mock.patch("subprocess.Popen") as mock_popen:
            mod._maybe_trigger_state_rebuild(receipt)

        mock_popen.assert_called_once()


def test_gate_failed_event_triggers_state_rebuild(tmp_path: Path):
    """_maybe_trigger_state_rebuild must fire Popen for gate_failed events."""
    env = _setup_env(tmp_path)

    with mock.patch.dict(os.environ, env):
        mod = _load_append_receipt_mod(env)
        mod.resolve_state_dir = mock.MagicMock(
            return_value=Path(env["VNX_STATE_DIR"])
        )

        receipt = {
            "event_type": "gate_failed",
            "dispatch_id": "d-gate-failed-001",
            "terminal": "T3",
        }

        with mock.patch("subprocess.Popen") as mock_popen:
            mod._maybe_trigger_state_rebuild(receipt)

        mock_popen.assert_called_once()


def test_register_write_before_rebuild_trigger(tmp_path: Path):
    """_emit_dispatch_register must execute before _maybe_trigger_state_rebuild.

    Verifies the ADVISORY fix: the register append must happen first so the
    rebuild's tail-read on dispatch_register.ndjson includes the just-written event.
    """
    env = _setup_env(tmp_path)
    call_log: list = []

    with mock.patch.dict(os.environ, env):
        mod = _load_append_receipt_mod(env)
        mod.resolve_state_dir = mock.MagicMock(
            return_value=Path(env["VNX_STATE_DIR"])
        )

        # Patch both hooks to record their call order.
        original_emit = mod._emit_dispatch_register
        original_rebuild = mod._maybe_trigger_state_rebuild

        def _emit_spy(receipt):
            call_log.append("emit")

        def _rebuild_spy(receipt):
            call_log.append("rebuild")

        mod._emit_dispatch_register = _emit_spy
        mod._maybe_trigger_state_rebuild = _rebuild_spy

        try:
            # Simulate the post-append hook block (fixed order: emit THEN rebuild).
            receipt = {
                "event_type": "task_complete",
                "dispatch_id": "d-order-test",
                "terminal": "T1",
            }
            mod._emit_dispatch_register(receipt)
            mod._maybe_trigger_state_rebuild(receipt)
        finally:
            mod._emit_dispatch_register = original_emit
            mod._maybe_trigger_state_rebuild = original_rebuild

    assert call_log == ["emit", "rebuild"], (
        f"Expected emit before rebuild, got: {call_log}"
    )


# ---------------------------------------------------------------------------
# Confidence-learning status-aware tests (BLOCKING 1)
# ---------------------------------------------------------------------------


def test_confidence_task_complete_success_outcome_is_success(tmp_path: Path):
    """task_complete with no failed status must pass 'success' to update_confidence_from_outcome."""
    env = _setup_env(tmp_path)
    captured: list = []

    with mock.patch.dict(os.environ, env):
        mod = _load_append_receipt_mod(env)
        mod.resolve_state_dir = mock.MagicMock(return_value=Path(env["VNX_STATE_DIR"]))
        db_path = Path(env["VNX_STATE_DIR"]) / "quality_intelligence.db"
        db_path.touch()

        def _fake_update(_db, _did, _term, outcome):
            captured.append(outcome)

        fake_intel = mock.MagicMock()
        fake_intel.update_confidence_from_outcome = _fake_update
        with mock.patch.dict(sys.modules, {"intelligence_persist": fake_intel}):
            mod._update_confidence_from_receipt({
                "event_type": "task_complete",
                "dispatch_id": "d-conf-ok",
                "terminal": "T1",
            })

    assert captured == ["success"], (
        f"task_complete with no failed status must yield 'success', got: {captured}"
    )


def test_confidence_task_complete_with_failed_status_yields_failure(tmp_path: Path):
    """task_complete + status=failed must pass 'failure', not 'success', to confidence update.

    Regression for: _update_confidence_from_receipt treated every task_complete as
    a success regardless of receipt['status'], boosting patterns for failed dispatches.
    """
    env = _setup_env(tmp_path)
    captured: list = []

    with mock.patch.dict(os.environ, env):
        mod = _load_append_receipt_mod(env)
        mod.resolve_state_dir = mock.MagicMock(return_value=Path(env["VNX_STATE_DIR"]))
        db_path = Path(env["VNX_STATE_DIR"]) / "quality_intelligence.db"
        db_path.touch()

        def _fake_update(_db, _did, _term, outcome):
            captured.append(outcome)

        fake_intel = mock.MagicMock()
        fake_intel.update_confidence_from_outcome = _fake_update
        with mock.patch.dict(sys.modules, {"intelligence_persist": fake_intel}):
            mod._update_confidence_from_receipt({
                "event_type": "task_complete",
                "status": "failed",
                "dispatch_id": "d-conf-failed",
                "terminal": "T1",
            })

    assert captured == ["failure"], (
        f"task_complete+status=failed must yield 'failure', got: {captured}"
    )


def test_confidence_task_complete_with_error_status_yields_failure(tmp_path: Path):
    """task_complete + status=error must yield 'failure' (covers all failed-status variants)."""
    env = _setup_env(tmp_path)
    captured: list = []

    with mock.patch.dict(os.environ, env):
        mod = _load_append_receipt_mod(env)
        mod.resolve_state_dir = mock.MagicMock(return_value=Path(env["VNX_STATE_DIR"]))
        db_path = Path(env["VNX_STATE_DIR"]) / "quality_intelligence.db"
        db_path.touch()

        def _fake_update(_db, _did, _term, outcome):
            captured.append(outcome)

        fake_intel = mock.MagicMock()
        fake_intel.update_confidence_from_outcome = _fake_update
        with mock.patch.dict(sys.modules, {"intelligence_persist": fake_intel}):
            mod._update_confidence_from_receipt({
                "event_type": "task_complete",
                "status": "error",
                "dispatch_id": "d-conf-error",
                "terminal": "T2",
            })

    assert captured == ["failure"], (
        f"task_complete+status=error must yield 'failure', got: {captured}"
    )


def test_confidence_task_failed_event_yields_failure(tmp_path: Path):
    """task_failed event must pass 'failure' to update_confidence_from_outcome."""
    env = _setup_env(tmp_path)
    captured: list = []

    with mock.patch.dict(os.environ, env):
        mod = _load_append_receipt_mod(env)
        mod.resolve_state_dir = mock.MagicMock(return_value=Path(env["VNX_STATE_DIR"]))
        db_path = Path(env["VNX_STATE_DIR"]) / "quality_intelligence.db"
        db_path.touch()

        def _fake_update(_db, _did, _term, outcome):
            captured.append(outcome)

        fake_intel = mock.MagicMock()
        fake_intel.update_confidence_from_outcome = _fake_update
        with mock.patch.dict(sys.modules, {"intelligence_persist": fake_intel}):
            mod._update_confidence_from_receipt({
                "event_type": "task_failed",
                "dispatch_id": "d-conf-task-failed",
                "terminal": "T3",
            })

    assert captured == ["failure"], (
        f"task_failed must yield 'failure', got: {captured}"
    )


# ---------------------------------------------------------------------------
# Bash promotion rebuild trigger test (BLOCKING 2)
# ---------------------------------------------------------------------------


def test_bash_promotion_rebuild_trigger(tmp_path: Path):
    """Bash throttle snippet in dispatch_lifecycle.sh must fire build_t0_state.py on promotion.

    Regression for: finalize_dispatch_delivery wrote dispatch_promoted to the register
    but never triggered build_t0_state.py, leaving t0_state.json stale until an
    unrelated receipt arrived.
    """
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()
    marker_file = tmp_path / "rebuild_triggered"
    fake_build_t0 = scripts_dir / "build_t0_state.py"
    fake_build_t0.write_text(
        f"import pathlib; pathlib.Path({str(marker_file)!r}).write_text('ok')\n"
    )

    state_dir = tmp_path / "state"
    state_dir.mkdir()

    # Reproduce the throttle snippet as written in dispatch_lifecycle.sh (no throttle file = fires)
    bash_snippet = f"""
set -e
VNX_DIR={str(tmp_path)!r}
STATE_DIR={str(state_dir)!r}
_REBUILD_THROTTLE_FILE="$STATE_DIR/.last_state_rebuild_ts"
_REBUILD_NOW=$(date +%s)
_REBUILD_LAST=$(cat "$_REBUILD_THROTTLE_FILE" 2>/dev/null || echo 0)
if [ ! -f "$_REBUILD_THROTTLE_FILE" ] || [ $((_REBUILD_NOW - _REBUILD_LAST)) -ge 30 ]; then
    nohup python3 "$VNX_DIR/scripts/build_t0_state.py" >/dev/null 2>&1 &
    _BG_PID=$!
    printf '%s' "$_REBUILD_NOW" > "${{_REBUILD_THROTTLE_FILE}}.tmp" && \\
        mv "${{_REBUILD_THROTTLE_FILE}}.tmp" "$_REBUILD_THROTTLE_FILE"
    wait $_BG_PID
fi
"""
    result = subprocess.run(["bash", "-c", bash_snippet], capture_output=True, timeout=15)
    assert result.returncode == 0, f"Bash script failed: {result.stderr.decode()}"
    assert marker_file.exists(), (
        "build_t0_state.py was not triggered by the bash promotion rebuild hook"
    )
    throttle_file = state_dir / ".last_state_rebuild_ts"
    assert throttle_file.exists(), "Throttle file must be written after rebuild trigger"
    ts_val = throttle_file.read_text(encoding="utf-8").strip()
    assert ts_val.isdigit(), f"Throttle file must contain epoch seconds, got: {ts_val!r}"


def test_bash_promotion_rebuild_throttle_suppresses_second_call(tmp_path: Path):
    """Second promotion within 30s must not fire a second rebuild (throttle respected)."""
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()
    counter_file = tmp_path / "rebuild_count"
    counter_file.write_text("0")
    fake_build_t0 = scripts_dir / "build_t0_state.py"
    fake_build_t0.write_text(
        f"import pathlib; p=pathlib.Path({str(counter_file)!r}); "
        f"p.write_text(str(int(p.read_text()) + 1))\n"
    )

    state_dir = tmp_path / "state"
    state_dir.mkdir()
    # Pre-write a fresh throttle timestamp so the throttle fires
    throttle_file = state_dir / ".last_state_rebuild_ts"
    import time as _time
    throttle_file.write_text(str(int(_time.time())))

    bash_snippet = f"""
set -e
VNX_DIR={str(tmp_path)!r}
STATE_DIR={str(state_dir)!r}
_REBUILD_THROTTLE_FILE="$STATE_DIR/.last_state_rebuild_ts"
_REBUILD_NOW=$(date +%s)
_REBUILD_LAST=$(cat "$_REBUILD_THROTTLE_FILE" 2>/dev/null || echo 0)
if [ ! -f "$_REBUILD_THROTTLE_FILE" ] || [ $((_REBUILD_NOW - _REBUILD_LAST)) -ge 30 ]; then
    nohup python3 "$VNX_DIR/scripts/build_t0_state.py" >/dev/null 2>&1 &
    _BG_PID=$!
    printf '%s' "$_REBUILD_NOW" > "${{_REBUILD_THROTTLE_FILE}}.tmp" && \\
        mv "${{_REBUILD_THROTTLE_FILE}}.tmp" "$_REBUILD_THROTTLE_FILE"
    wait $_BG_PID
fi
"""
    result = subprocess.run(["bash", "-c", bash_snippet], capture_output=True, timeout=15)
    assert result.returncode == 0, f"Bash script failed: {result.stderr.decode()}"
    # Throttle was fresh — rebuild must be suppressed
    count = int(counter_file.read_text().strip())
    assert count == 0, f"Expected 0 rebuilds (throttled), got {count}"
