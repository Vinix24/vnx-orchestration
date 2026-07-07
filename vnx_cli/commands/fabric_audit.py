#!/usr/bin/env python3
"""vnx fabric-audit — phase-0 fabric hardening audit (ADR-028 §6 phase 0).

Thin wrapper: the audit logic lives in ``scripts/fabric_audit.py`` (single
source of truth, unit-tested). This puts the packaged engine on sys.path and
delegates, so the packaged CLI and a bare ``python3 scripts/fabric_audit.py``
run the identical checks.
"""
from vnx_cli import _engine


def vnx_fabric_audit(args) -> int:
    _engine.ensure_engine_on_path()
    import fabric_audit  # noqa: E402 — resolved after ensure_engine_on_path()

    argv: list[str] = []
    if getattr(args, "data_home", None):
        argv += ["--data-home", args.data_home]
    if getattr(args, "registry", None):
        argv += ["--registry", args.registry]
    if getattr(args, "json", False):
        argv.append("--json")
    return fabric_audit.main(argv)
