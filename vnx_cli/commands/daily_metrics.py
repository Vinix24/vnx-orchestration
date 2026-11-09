"""``vnx daily-metrics`` — the owner command of ``scripts/daily_metrics.py`` (decision 28).

``install`` / ``uninstall`` manage one opt-in per-project launchd job (decision 29);
every other verb is passed through to the engine's script unchanged. The job's
project checkout comes from the registry (``resolve_project_root``, decision 12).
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

from vnx_cli import _engine

PLIST_NAME = "com.vnx.daily-metrics"


def _job_parser(verb: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=f"vnx daily-metrics {verb}")
    parser.add_argument("--project-id", dest="project_id", required=True, metavar="ID")
    return parser


def _install(project_id: str) -> int:
    _engine.ensure_engine_on_path()
    from launchd_project_root import ProjectRootRefused, resolve_project_root
    from vnx_cli.commands import init_cmd

    try:
        project_root = resolve_project_root(project_id)
    except ProjectRootRefused as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    try:
        installed = init_cmd._install_launchd_agent(
            str(_engine.engine_root()), PLIST_NAME, project_id=project_id,
            project_root=str(project_root),
        )
    except (RuntimeError, OSError) as exc:
        print(f"error: {PLIST_NAME} could not be installed for {project_id}: {exc}",
              file=sys.stderr)
        return 1
    if not installed:
        print(f"error: {PLIST_NAME} was not installed for {project_id}: the launchd install "
              "guard refused this engine root, or the template is missing", file=sys.stderr)
        return 1
    return 0


def _uninstall(project_id: str) -> int:
    label = f"{PLIST_NAME}.{project_id}"
    # A non-zero exit because the job was not loaded is expected and fine: the file
    # removal below is what makes the command idempotent.
    subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{label}"],
                   capture_output=True, text=True)
    plist = Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"
    if plist.is_file():
        plist.unlink()
    return 0


def vnx_daily_metrics(args: argparse.Namespace) -> int:
    """Run one ``vnx daily-metrics`` invocation and return its exit code."""
    argv = list(getattr(args, "daily_metrics_args", None) or [])
    if not argv:
        print("usage: vnx daily-metrics <verb> [args]\n"
              "  install --project-id ID    install the opt-in per-project 04:10 launchd job\n"
              "  uninstall --project-id ID  remove exactly that one job\n"
              "  collect | daily | propose | approve | verdict | report  "
              "passed to scripts/daily_metrics.py unchanged", file=sys.stderr)
        return 2

    verb, rest = argv[0], argv[1:]
    if verb == "install":
        return _install(_job_parser("install").parse_args(rest).project_id)
    if verb == "uninstall":
        return _uninstall(_job_parser("uninstall").parse_args(rest).project_id)

    script = _engine.engine_root() / "scripts" / "daily_metrics.py"
    if not script.is_file():
        print(f"error: daily-metrics script not found: {script}", file=sys.stderr)
        return 1
    # -P: keep `<engine>/scripts` off sys.path. `bin/vnx` exports scripts/lib on
    # PYTHONPATH, so the script sees its lib dir already on the path, does not move
    # it in front of its own directory, and `from daily_metrics import collect`
    # resolves to scripts/daily_metrics.py itself (circular import; measured).
    return subprocess.run([sys.executable, "-P", str(script), *argv]).returncode
