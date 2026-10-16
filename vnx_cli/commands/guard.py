#!/usr/bin/env python3
"""vnx guard — install and check the user-level T0 context guard.

Thin wrapper: the logic lives in ``scripts/vnx_guard.py`` and ``scripts/lib/guard_*.py`` (single
source of truth, unit-tested). This puts the packaged engine on sys.path and hands the arguments
on, so the packaged CLI and ``bin/vnx guard`` run the identical code.
"""
from vnx_cli import _engine


def vnx_guard(args) -> int:
    _engine.ensure_engine_on_path()
    import vnx_guard as guard_main  # resolved after ensure_engine_on_path()

    argv = [getattr(args, "guard_command", None) or ""]
    argv = [a for a in argv if a]
    if getattr(args, "dry_run", False):
        argv.append("--dry-run")
    for flag, values in (("--config-dir", getattr(args, "config_dir", None)),
                         ("--scan-root", getattr(args, "scan_root", None))):
        for value in values or []:
            argv += [flag, value]
    return guard_main.main(argv)
