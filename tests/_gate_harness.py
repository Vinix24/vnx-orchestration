"""Shared harness pieces for tests that source scripts/commands/gate.sh
against a fake VNX_HOME (OI-1888 fix-forward).

`gate.sh` shells out to `scripts/lib/gate_seat_line.py` after every gate run
to resolve, per requested seat, which gate actually answered it (a takeover
successor's exit code is correct even when its name is not the seat's own).
Any test harness that fabricates a VNX_HOME to source gate.sh's `cmd_gate`
must therefore carry the REAL gate_seat_line.py plus its real dependencies —
a fake VNX_HOME with only a stubbed review_gate_manager.py leaves
gate_seat_line.py missing on disk, and gate.sh's `python3 .../gate_seat_line.py`
call fails with ENOENT before it ever gets to interpret a report.

`GATE_LIB_MODULES` is the closure of gate_seat_line.py's own imports:
gate_enforcement_verify (seat/takeover resolution), gate_status (PASS/FAIL
classification, itself importing gate_depth), and gate_depth.

`vnx gate --only <gate>` also asks the provider switch first
(provider-schakelaar C): it runs `scripts/providers_cli.py check`, which needs
`provider_switch.py` and its two imports (`atomic_io`, `vnx_ids`). A fake
VNX_HOME without them would make every `--only` run refuse fail-closed.
`PROVIDER_SWITCH_LIB_MODULES` and `PROVIDER_SWITCH_SCRIPTS` are that closure.
The decision reads the switch file at `VNX_PROVIDER_SWITCH_FILE`, so a harness
that strips `VNX_*` from the environment must set that variable to a tmp path
again: without it the check would read the operator's real `~/.vnx`.

A refused `--only` is recorded through `gate_recorder.write_skip_rationale`
(the GATE-9 line in `gate_execution_audit.ndjson`). `gate_recorder` imports a
wide closure (receipts, governance emit, provider reachability, ...), so that
closure is measured, not listed: `module_closure` imports the module in a
subprocess and returns every file under `scripts/` it loaded. A listed copy
would break the moment one of those modules gains an import.

Before the switch is asked, `--only` checks that the name is a live gate:
registered in `gate_recorder.GATE_PROVIDERS` and not retired in
`dispatch_spec.RETIRED_GATE_NAMES` (OI-2070 round 2). `dispatch_spec` is not
in the recorder's closure, so its own closure is installed too
(`GATE_NAME_MODULES`).
"""
from __future__ import annotations

import functools
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent
GATE_SH = REPO_ROOT / "scripts" / "commands" / "gate.sh"

GATE_LIB_MODULES = (
    "gate_enforcement_verify.py",
    "gate_status.py",
    "gate_depth.py",
    "gate_seat_line.py",
)

PROVIDER_SWITCH_LIB_MODULES = (
    "provider_switch.py",
    "atomic_io.py",
    "vnx_ids.py",
)

PROVIDER_SWITCH_SCRIPTS = (
    "providers_cli.py",
)

# The module gate.sh imports to record a refused `--only` (see the module docstring).
SWITCH_REFUSAL_RECORDER = "gate_recorder"

# The modules gate.sh's `--only` name check imports (see the module docstring).
GATE_NAME_MODULES = (SWITCH_REFUSAL_RECORDER, "dispatch_spec")

_CLOSURE_PROBE = """\
import json, os, sys
scripts = sys.argv[1]
sys.path[:0] = [os.path.join(scripts, "lib"), scripts]
__import__(sys.argv[2])
loaded = {
    os.path.relpath(os.path.realpath(module.__file__), scripts)
    for module in list(sys.modules.values())
    if getattr(module, "__file__", None) and os.path.realpath(module.__file__).startswith(scripts + os.sep)
}
print(json.dumps(sorted(loaded)))
"""


@functools.lru_cache(maxsize=None)
def module_closure(module: str) -> Tuple[str, ...]:
    """Every file under scripts/ that importing ``module`` loads, relative to scripts/."""
    scripts = (REPO_ROOT / "scripts").resolve()
    proc = subprocess.run(
        [sys.executable, "-c", _CLOSURE_PROBE, str(scripts), module],
        capture_output=True, text=True, timeout=60, check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"cannot import {module} to measure its closure: {proc.stderr.strip()}")
    return tuple(json.loads(proc.stdout))


def install_gate_lib_modules(lib_dir: Path) -> None:
    """Copy the real gate_seat_line.py + its real deps into `lib_dir` (a fake
    VNX_HOME's scripts/lib), the provider-switch check gate.sh runs for
    `--only` into its scripts/ and scripts/lib, and the closures of the
    name check and of the recorder that books a refused `--only`."""
    lib_dir.mkdir(parents=True, exist_ok=True)
    scripts_dir = lib_dir.parent
    for module in GATE_NAME_MODULES:
        for relative in module_closure(module):
            target = scripts_dir / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(REPO_ROOT / "scripts" / relative, target)
    for name in (*GATE_LIB_MODULES, *PROVIDER_SWITCH_LIB_MODULES):
        shutil.copy2(REPO_ROOT / "scripts" / "lib" / name, lib_dir / name)
    for name in PROVIDER_SWITCH_SCRIPTS:
        shutil.copy2(REPO_ROOT / "scripts" / name, scripts_dir / name)
