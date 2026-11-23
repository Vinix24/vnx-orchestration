#!/usr/bin/env python3
"""Nightly rework attribution job (ADR-071 besluit 7 and 13) and its launchd installer.

    rework_attribution_job.py run [--project-id ID] [--since SHA]
    rework_attribution_job.py install [--project-id ID] [--dry-run]
    rework_attribution_job.py uninstall
    rework_attribution_job.py status [--project-id ID]

``run`` executes ``attribute_rework.py --persist`` (blame edges from its own watermark, so a
missed night is caught up by the next run) and then ``rework_report.py --json --out
<state>/rework_report.json``. It writes a ``rework_attribution`` HealthBeacon: ``ok`` when both
steps succeed, ``degraded`` with the reason of the first failing step. It always exits 0.
A failing attribution step skips the report: the window is incomplete. ``--since`` seeds the
first run (no watermark yet). The installer reuses the dream scheduler's plist helpers.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import List, Optional, Tuple

SCRIPT_DIR = Path(__file__).resolve().parent
for _p in (SCRIPT_DIR / "dream", SCRIPT_DIR / "lib"):
    sys.path.insert(0, str(_p))

import scheduler as _dream  # noqa: E402  (scripts/dream/scheduler.py: generic plist helpers)
import vnx_paths  # noqa: E402
from health_beacon import HealthBeacon  # noqa: E402
from project_root import resolve_central_data_dir, resolve_project_id, resolve_project_root  # noqa: E402

COMPONENT_NAME = "rework_attribution"
BEACON_EXPECTED_INTERVAL_SECONDS = 90000
LABEL = "com.vnx.rework-attribution"
TEMPLATE = SCRIPT_DIR / "templates" / f"{LABEL}.plist.j2"
REPORT_FILE = "rework_report.json"
STEP_TIMEOUT_SECONDS = 3600


def _run_step(script: str, args: List[str]) -> Tuple[int, str]:
    """The seam for both steps: run one script of this tree, return (exit code, stdout+stderr)."""
    proc = subprocess.run([sys.executable, str(SCRIPT_DIR / script), *args], capture_output=True,
                          text=True, timeout=STEP_TIMEOUT_SECONDS)
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def _failure(step: str, args: List[str]) -> Optional[str]:
    """None when the step succeeded, else its reason. Evidence-based: only exit 0 without a
    ``stopped`` summary counts as success; a crash, a timeout or an unreadable run is a failure."""
    try:
        rc, text = _run_step(step, args)
    except (OSError, subprocess.SubprocessError) as exc:
        return f"{step}: {type(exc).__name__}: {exc}"
    if rc != 0:
        stopped = ""
        for line in text.splitlines():
            try:
                stopped = str(json.loads(line).get("stopped") or "")
            except (ValueError, AttributeError):
                continue
        return f"{step} exit {rc}: {stopped or text.strip()[-300:] or 'no output'}"
    return None


def run_job(project_id: str, data_dir: Optional[Path] = None, since: Optional[str] = None) -> int:
    data = Path(data_dir) if data_dir is not None else resolve_central_data_dir(project_id)
    attribute = ["--persist", "--json", "--project-id", project_id] + (["--since", since] if since else [])
    report = ["--json", "--project-id", project_id, "--data-dir", str(data),
              "--out", str(data / "state" / REPORT_FILE)]
    reason = _failure("attribute_rework.py", attribute)
    if reason is None:
        reason = _failure("rework_report.py", report)
    window = "incomplete" if reason else "complete"
    beacon = HealthBeacon(data, COMPONENT_NAME, expected_interval_seconds=BEACON_EXPECTED_INTERVAL_SECONDS)
    beacon.heartbeat(status="ok" if reason is None else "degraded",
                     details={"project_id": project_id, "window": window, **({"reason": reason} if reason else {})})
    return 0


def _log_dir(project_id: str) -> Path:
    return resolve_central_data_dir(project_id) / "events" / "rework-attribution"


def render(project_id: str) -> str:
    ctx = {"vnx_home": str(SCRIPT_DIR.parent), "project_id": project_id,
           "path_env": _dream._path_env(), "log_dir": str(_log_dir(project_id))}
    return _dream._render_plist(TEMPLATE, ctx)


def _plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def install(project_id: str, dry_run: bool) -> str:
    rendered = render(project_id)
    if dry_run:
        return rendered
    vnx_paths.refuse_real_launchctl_under_test_runner()  # before anything is written (OI-1891)
    out = _plist_path()
    _log_dir(project_id).mkdir(parents=True, exist_ok=True)  # launchd does not create log dirs
    out.parent.mkdir(parents=True, exist_ok=True)
    _dream._write_plist_atomic(out.parent, out, rendered)
    _dream._load_plist(out)
    return f"Installed and loaded: {out}"


def uninstall() -> str:
    out = _plist_path()
    if not out.exists():
        return f"Not installed: {out}"
    vnx_paths.refuse_real_launchctl_under_test_runner()
    subprocess.run(["launchctl", "unload", "-w", str(out)], capture_output=True)
    out.unlink()
    return f"Unloaded and removed: {out}"


def status(project_id: str) -> str:
    out = _plist_path()
    beacon = resolve_central_data_dir(project_id) / "health" / f"{COMPONENT_NAME}.json"
    try:
        doc = json.loads(beacon.read_text(encoding="utf-8"))
        seen = f"{doc.get('status')} at {doc.get('last_run_iso')} {json.dumps(doc.get('details'), sort_keys=True)}"
    except (OSError, ValueError):
        seen = "no readable beacon"
    return f"plist: {out if out.exists() else 'not installed'}\nbeacon: {seen}"


def main(argv: "list[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=("run", "install", "uninstall", "status"))
    parser.add_argument("--project-id", default=None)
    parser.add_argument("--since", default=None, help="run: start commit for the very first run")
    parser.add_argument("--dry-run", action="store_true", help="install: print the plist, touch nothing")
    args = parser.parse_args(argv)
    try:
        if args.command == "uninstall":
            print(uninstall())
            return 0
        project_id = args.project_id or resolve_project_id(resolve_project_root(__file__))
        if args.command == "run":
            return run_job(project_id, since=args.since)
        print(install(project_id, args.dry_run) if args.command == "install" else status(project_id))
    except (RuntimeError, OSError) as exc:
        print(f"rework_attribution_job: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
