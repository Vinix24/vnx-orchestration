"""The door runs with only ``scripts/lib`` on ``sys.path`` (OI-2022).

``bin/vnx dispatch <id> --dry-run`` starts the door as
``PYTHONPATH=<vnx home>/scripts/lib python3 scripts/lib/dispatch_cli.py --spec-file <spec>``.
``scripts/`` is not on that path. Two door paths imported a module that lives in
``scripts/`` and broke there, while every in-process test passed because pytest
had ``scripts/`` on its own path:

1. A spec that names no gate. ``_resolve_gate_via_router`` -> ``smart_router``
   ranks the configured review stack by billing, and read the billing table
   through ``gate_recorder`` -> ``governance_receipts`` -> ``append_receipt``.
   The import failed, and the door refused every gate-silent spec as
   ``REJECT [gate-config-unreadable]``. The table now lives in
   ``scripts/lib/gate_billing_table.py``.
2. ``_record_bookkeeping_failure`` wrote its ``door_bookkeeping_failed`` receipt
   through ``from append_receipt import``, with no path guard. The receipt was
   silently lost; only the ``dispatch_register.ndjson`` copy landed.

Every test here starts a fresh Python process with the environment the
``bin/vnx`` wrapper gives the door, so the in-process ``sys.path`` of pytest
cannot hide the defect. The store, ``HOME`` and the state dir are all under
``tmp_path``. ``claude``, ``codex``, ``kimi`` and ``gemini`` are shadowed on
``PATH`` by tripwires that record any start; a dry-run must start none of them.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Optional

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "scripts"
LIB = SCRIPTS / "lib"

PROJECT_ID = "doorpathonly"
_TRIPWIRES = ("claude", "codex", "kimi", "gemini")
_SUBPROCESS_TIMEOUT = 180


def _store(tmp_path: Path) -> dict:
    """A central-layout store under a tmp HOME: ``<HOME>/.vnx-data/<project>``."""
    home = tmp_path / "home"
    data_dir = home / ".vnx-data" / PROJECT_ID
    state_dir = data_dir / "state"
    state_dir.mkdir(parents=True)
    tripwire_dir = tmp_path / "tripwire-bin"
    tripwire_dir.mkdir()
    tripwire_log = tmp_path / "tripwire.log"
    for name in _TRIPWIRES:
        stub = tripwire_dir / name
        stub.write_text(f'#!/bin/sh\necho "{name} $*" >> "{tripwire_log}"\nexit 97\n', encoding="utf-8")
        stub.chmod(0o755)
    return {
        "home": home,
        "data_dir": data_dir,
        "state_dir": state_dir,
        "tripwire_dir": tripwire_dir,
        "tripwire_log": tripwire_log,
        "workdir": tmp_path,
    }


def _door_env(store: dict, *, review_stack: Optional[str]) -> dict:
    """The environment ``bin/vnx dispatch`` gives the door, built from scratch.

    Nothing is inherited except ``PATH`` (behind the tripwires) and ``TMPDIR``:
    no ``PYTHONPATH`` of the test process, no ``VNX_*`` of the operator's
    shell, no provider key, no forge token.
    """
    env = {
        "PATH": f"{store['tripwire_dir']}{os.pathsep}{os.environ.get('PATH', '')}",
        "HOME": str(store["home"]),
        "PYTHONPATH": str(LIB),
        "VNX_HOME": str(REPO),
        "VNX_PROJECT_ID": PROJECT_ID,
        "VNX_DATA_DIR": str(store["data_dir"]),
        "VNX_DATA_DIR_EXPLICIT": "1",
        "VNX_STATE_DIR": str(store["state_dir"]),
    }
    if os.environ.get("TMPDIR"):
        env["TMPDIR"] = os.environ["TMPDIR"]
    if review_stack is not None:
        env["VNX_DEFAULT_REVIEW_STACK"] = review_stack
    return env


def _stage_gate_silent_spec(store: dict, env: dict, dispatch_id: str) -> Path:
    """Stage through the real stage-only CLI (``vnx dispatch stage``), which has no
    ``--gate`` flag: every bundle it writes is gate-silent."""
    instruction = store["workdir"] / "instruction.md"
    instruction.write_text(
        "# Gate-silent dispatch\n\nRole: backend-developer\n\nChange one line.\n",
        encoding="utf-8",
    )
    staged = subprocess.run(
        [
            sys.executable, str(LIB / "dispatch_bridge.py"), "stage",
            "--instruction", str(instruction),
            "--dispatch-id", dispatch_id,
            "--role", "backend-developer",
            "--slot", "T1",
            "--data-dir", str(store["data_dir"]),
        ],
        env=env, cwd=str(store["workdir"]), capture_output=True, text=True,
        timeout=_SUBPROCESS_TIMEOUT,
    )
    assert staged.returncode == 0, f"staging failed:\n{staged.stderr}"
    spec_file = Path(staged.stdout.strip().splitlines()[-1])
    spec = json.loads(spec_file.read_text(encoding="utf-8"))
    assert spec["gate"] == "", "the staged spec must name no gate, or this test proves nothing"
    return spec_file


def _door_dry_run(store: dict, env: dict, spec_file: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(LIB / "dispatch_cli.py"), "--spec-file", str(spec_file), "--dry-run"],
        env=env, cwd=str(store["workdir"]), capture_output=True, text=True,
        timeout=_SUBPROCESS_TIMEOUT,
    )


def _route_reason(stdout: str) -> str:
    lines = [line for line in stdout.splitlines() if line.strip().startswith("route_reason:")]
    assert lines, f"dry-run printed no route_reason line:\n{stdout}"
    return lines[0]


def _assert_no_tripwire(store: dict) -> None:
    log = store["tripwire_log"]
    started = log.read_text(encoding="utf-8") if log.exists() else ""
    assert started == "", f"a dry-run started a provider or claude process:\n{started}"


# ---------------------------------------------------------------------------
# Step 2: a gate-silent spec gets its gate from the configured stack
# ---------------------------------------------------------------------------


class TestGateSilentSpecDryRun:
    @pytest.mark.parametrize("review_stack,expected_gate", [
        # The operator's configured stack names one seat: that seat is declared.
        ("kimi_gate", "kimi_gate"),
        # glm comes first in the stack, codex wins on billing: the seat can only
        # be ranked this way if the door actually read the billing table.
        ("glm_gate,codex_gate", "codex_gate"),
        # No operator value: the registry default stack (codex_gate,kimi_gate).
        (None, "codex_gate"),
    ])
    def test_dry_run_resolves_a_gate_from_the_configured_stack(
        self, tmp_path, review_stack, expected_gate,
    ):
        store = _store(tmp_path)
        env = _door_env(store, review_stack=review_stack)
        spec_file = _stage_gate_silent_spec(store, env, "20261009-door-path-gate-silent")

        result = _door_dry_run(store, env, spec_file)

        assert "gate-config-unreadable" not in result.stderr, result.stderr
        assert result.returncode == 0, (
            f"the door refused a gate-silent spec (rc={result.returncode}):\n{result.stderr}"
        )
        assert f"gate={expected_gate}" in _route_reason(result.stdout)
        _assert_no_tripwire(store)

    @pytest.mark.parametrize("review_stack,named", [
        ("", "resolved to an empty value"),
        ("not_a_registered_gate", "not_a_registered_gate"),
    ])
    def test_an_unreadable_review_stack_still_refuses_by_name(self, tmp_path, review_stack, named):
        store = _store(tmp_path)
        env = _door_env(store, review_stack=review_stack)
        spec_file = _stage_gate_silent_spec(store, env, "20261009-door-path-bad-stack")

        result = _door_dry_run(store, env, spec_file)

        assert result.returncode == 1
        assert "REJECT [gate-config-unreadable]" in result.stderr, result.stderr
        assert "VNX_DEFAULT_REVIEW_STACK" in result.stderr
        assert named in result.stderr
        # Refused on the stack itself, never on a module the door could not import.
        assert "No module named" not in result.stderr, result.stderr
        _assert_no_tripwire(store)


# ---------------------------------------------------------------------------
# Step 3: the door's own failure record reaches the receipt ledger
# ---------------------------------------------------------------------------

_BOOKKEEPING_DRIVER = """
import importlib.util, json, sys
from pathlib import Path

lib, scripts, state_dir, dispatch_id = sys.argv[1:5]
before = {
    "scripts_on_path": scripts in sys.path,
    "append_receipt_findable": importlib.util.find_spec("append_receipt") is not None,
}

import dispatch_cli

dispatch_cli._record_bookkeeping_failure(
    "scripts_lib_only_probe", dispatch_id, RuntimeError("probe failure"),
    state_dir=Path(state_dir),
)
print(json.dumps({
    "before": before,
    "lib_index": sys.path.index(lib) if lib in sys.path else -1,
    "scripts_index": sys.path.index(scripts) if scripts in sys.path else -1,
}))
"""


class TestBookkeepingFailureReceiptWithScriptsLibOnly:
    def test_door_bookkeeping_failed_receipt_is_written(self, tmp_path):
        store = _store(tmp_path)
        env = _door_env(store, review_stack=None)
        dispatch_id = "20261009-door-path-bookkeeping"

        result = subprocess.run(
            [
                sys.executable, "-c", _BOOKKEEPING_DRIVER,
                str(LIB), str(SCRIPTS), str(store["state_dir"]), dispatch_id,
            ],
            env=env, cwd=str(store["workdir"]), capture_output=True, text=True,
            timeout=_SUBPROCESS_TIMEOUT,
        )
        assert result.returncode == 0, result.stderr
        report = json.loads(result.stdout.strip().splitlines()[-1])

        # The precondition that makes this a test of the door's real environment:
        # nothing but the door itself could have put scripts/ on the path.
        assert report["before"] == {"scripts_on_path": False, "append_receipt_findable": False}

        receipts_file = store["state_dir"] / "t0_receipts.ndjson"
        receipts = (
            [json.loads(line) for line in receipts_file.read_text(encoding="utf-8").splitlines() if line.strip()]
            if receipts_file.exists() else []
        )
        facts = [r for r in receipts if r.get("event_type") == "door_bookkeeping_failed"]
        assert facts, (
            "no door_bookkeeping_failed receipt in t0_receipts.ndjson: the failure record "
            f"was lost in a process with only scripts/lib on sys.path.\nstderr:\n{result.stderr}"
        )
        assert facts[-1]["dispatch_id"] == dispatch_id
        assert facts[-1]["site"] == "scripts_lib_only_probe"
        assert "probe failure" in facts[-1]["error"]

        # scripts/ is added behind scripts/lib, never ahead of it: seven module stems
        # exist in both directories and the door's scripts/lib copies must keep winning.
        assert 0 <= report["lib_index"] < report["scripts_index"], report
        _assert_no_tripwire(store)
