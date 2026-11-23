#!/usr/bin/env python3
"""vnx main-window — the read-only probe of the window on main (ADR-073 besluit 8).

    vnx main-window status [--json]

Reads the window (the merges since the last green heavy VNX CI run) and the
admission with the two registry keys VNX_MAIN_UNVERIFIED_MAX and
VNX_MAIN_RUN_MAX_AGE_MIN. Writes nothing. The probe reads the central store, the same one the
merge door reads, so it answers what the door will decide.

Exit codes: 0 GO (also mode off), 1 NO-GO, 2 the probe could not run.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, List, Optional

SCRIPT_DIR = Path(__file__).resolve().parent
for _path in (SCRIPT_DIR, SCRIPT_DIR / "lib"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

import main_window as mw  # noqa: E402
from project_root import resolve_project_id, resolve_project_root  # noqa: E402

EXIT_GO = 0
EXIT_NO_GO = 1
EXIT_COULD_NOT_RUN = 2


def _human(admission: mw.Admission) -> str:
    window = admission.window
    red = window.first_red
    lines = [
        f"verdict: {admission.verdict}",
        f"mode: {admission.mode}",
        f"reason: {admission.reason}",
        f"base: {window.base_sha or '-'} (run {window.base_run_id or '-'})",
        f"running: {', '.join(admission.running_shas) or '-'}",
        f"first red: {f'{red.sha} run {red.run_id} (vermoedelijk, niet bewezen)' if red else '-'}",
    ]
    lines += [f"config: {item}" for item in admission.invalid_config]
    return "\n".join(lines)


def run_status(
    as_json: bool, *, runner: Optional[mw.Runner] = None, now: Optional[datetime] = None,
    project_root: Optional[Path] = None, state_dir: Optional[Path] = None, project_id: Optional[str] = None,
    out: Callable[[str], Any] = print,
) -> int:
    root = project_root or resolve_project_root(__file__)
    if state_dir is None:
        state, store_pid = mw.active_store()
    else:
        state, store_pid = state_dir, None
    pid = project_id or store_pid or (resolve_project_id(root) if state_dir is not None else None)
    max_n, max_age, invalid = mw.read_admission_config(pid, state)
    window = mw.read_window(root, state_dir=state, runner=runner, now=now, max_age_min=max_age)
    admission = mw.admit(window, max_unverified=max_n, max_age_min=max_age, invalid_config=invalid)
    out(json.dumps(admission.to_dict(), sort_keys=True, indent=2) if as_json else _human(admission))
    return EXIT_GO if admission.verdict == mw.GO else EXIT_NO_GO


def main(argv: Optional[List[str]] = None, **kwargs: Any) -> int:
    parser = argparse.ArgumentParser(prog="vnx main-window", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    status = sub.add_parser("status", help="the window on main and whether one more merge may enter")
    status.add_argument("--json", action="store_true", help="the admission record as JSON, sorted keys")
    try:
        args = parser.parse_args(argv)
    except SystemExit:
        return EXIT_COULD_NOT_RUN
    try:
        return run_status(args.json, **kwargs)
    except Exception as exc:  # vnx-silent-except: the probe names the failure and exits 2; it never reads as GO
        print(f"vnx main-window: could not run: {type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_COULD_NOT_RUN


if __name__ == "__main__":
    sys.exit(main())
