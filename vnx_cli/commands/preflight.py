#!/usr/bin/env python3
"""vnx preflight — read-only rollout readiness for the fleet.

Thin wrapper: the measurement lives in ``scripts/rollout_preflight.py`` (single
source of truth, importable and unit-tested). This puts the packaged engine on
``sys.path`` and delegates, so ``vnx preflight`` and a bare
``python3 scripts/rollout_preflight.py`` measure identically. The command
changes nothing: no install, no reload, no write to a settings file, a plist or
a store.
"""
from vnx_cli import _engine


def vnx_preflight(args) -> int:
    _engine.ensure_engine_on_path()
    import rollout_preflight

    argv: list[str] = []
    for project in getattr(args, "project", None) or []:
        argv += ["--project", project]
    if getattr(args, "json", False):
        argv.append("--json")
    return rollout_preflight.main(argv)
