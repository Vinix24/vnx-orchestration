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
"""
from __future__ import annotations

import shutil
from pathlib import Path

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


def install_gate_lib_modules(lib_dir: Path) -> None:
    """Copy the real gate_seat_line.py + its real deps into `lib_dir` (a fake
    VNX_HOME's scripts/lib), and the provider-switch check gate.sh runs for
    `--only` into its scripts/ and scripts/lib."""
    lib_dir.mkdir(parents=True, exist_ok=True)
    for name in (*GATE_LIB_MODULES, *PROVIDER_SWITCH_LIB_MODULES):
        shutil.copy2(REPO_ROOT / "scripts" / "lib" / name, lib_dir / name)
    for name in PROVIDER_SWITCH_SCRIPTS:
        shutil.copy2(REPO_ROOT / "scripts" / name, lib_dir.parent / name)
