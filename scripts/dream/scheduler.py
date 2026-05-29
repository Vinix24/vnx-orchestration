#!/usr/bin/env python3
"""Auto-dream scheduler: install/uninstall nightly schedule (ADR-019).

macOS: LaunchAgent plist installed and loaded via launchctl.
Linux: crontab entry written via crontab(1).

Usage:
    python3 scripts/dream/scheduler.py install --project-id <id> [--vnx-bin <path>] [--no-load]
    python3 scripts/dream/scheduler.py uninstall
    python3 scripts/dream/scheduler.py status
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
from project_root import resolve_project_root  # noqa: E402

_PLIST_LABEL = "com.vnx.auto-dream"
_PLIST_NAME = f"{_PLIST_LABEL}.plist"
_CRON_MARKER = "# vnx-auto-dream"


def _launchagents_dir() -> Path:
    return Path.home() / "Library" / "LaunchAgents"


def plist_path() -> Path:
    return _launchagents_dir() / _PLIST_NAME


def _render_plist(vnx_bin: str, project_id: str, project_root: str) -> str:
    template = Path(__file__).resolve().parent / "templates" / "com.vnx.auto-dream.plist.j2"
    try:
        from jinja2 import Environment, FileSystemLoader  # type: ignore[import]

        env = Environment(loader=FileSystemLoader(str(template.parent)), autoescape=False)
        return env.get_template(template.name).render(
            vnx_bin_path=vnx_bin, project_id=project_id, project_root=project_root
        )
    except ImportError:
        text = template.read_text(encoding="utf-8")
        for key, val in [
            ("vnx_bin_path", vnx_bin),
            ("project_id", project_id),
            ("project_root", project_root),
        ]:
            text = text.replace("{{ " + key + " }}", val)
        return text


def install_macos(
    project_id: str, vnx_bin: str, project_root: str, load: bool = True
) -> None:
    """Install LaunchAgent plist and optionally load it."""
    out_dir = _launchagents_dir()
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = plist_path()
    rendered = _render_plist(vnx_bin, project_id, project_root)

    fd, tmp = tempfile.mkstemp(dir=out_dir, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(rendered)
        os.replace(tmp, out_path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise

    print(f"[scheduler] Wrote: {out_path}")

    if not load:
        print(f"[scheduler] Dry-run: to activate, run: launchctl load -w {out_path}")
        return

    # Idempotent: unload before re-load
    subprocess.run(["launchctl", "unload", "-w", str(out_path)], capture_output=True)
    result = subprocess.run(
        ["launchctl", "load", "-w", str(out_path)], capture_output=True, text=True
    )
    if result.returncode != 0:
        sys.stderr.write(f"[scheduler] launchctl load warning: {result.stderr.strip()}\n")
    else:
        print(f"[scheduler] Loaded: {_PLIST_LABEL}")

    list_result = subprocess.run(
        ["launchctl", "list", _PLIST_LABEL], capture_output=True, text=True
    )
    if list_result.returncode == 0:
        print(f"[scheduler] Active: {_PLIST_LABEL}")
    else:
        print(f"[scheduler] Registered (verify with: launchctl list | grep vnx)")


def uninstall_macos() -> None:
    """Unload and remove LaunchAgent plist."""
    path = plist_path()
    if path.exists():
        subprocess.run(["launchctl", "unload", "-w", str(path)], capture_output=True)
        path.unlink()
        print(f"[scheduler] Removed: {path}")
    else:
        print(f"[scheduler] Not installed (plist not found: {path})")


def _cron_line(vnx_bin: str, project_id: str) -> str:
    return f"0 3 * * * {vnx_bin} dream run --project-id {project_id}  {_CRON_MARKER}"


def install_linux(project_id: str, vnx_bin: str) -> None:
    """Write crontab entry for nightly dream run at 03:00."""
    new_line = _cron_line(vnx_bin, project_id)
    result = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
    existing = result.stdout if result.returncode == 0 else ""
    lines = [ln for ln in existing.splitlines() if _CRON_MARKER not in ln]
    lines.append(new_line)
    new_crontab = "\n".join(lines) + "\n"

    proc = subprocess.run(["crontab", "-"], input=new_crontab, capture_output=True, text=True)
    if proc.returncode != 0:
        sys.stderr.write(f"[scheduler] crontab error: {proc.stderr.strip()}\n")
        sys.exit(1)
    print(f"[scheduler] Cron entry installed: {new_line}")


def uninstall_linux() -> None:
    """Remove vnx-auto-dream crontab entry."""
    result = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
    if result.returncode != 0:
        print("[scheduler] No crontab found.")
        return
    lines = result.stdout.splitlines()
    filtered = [ln for ln in lines if _CRON_MARKER not in ln]
    if len(filtered) == len(lines):
        print("[scheduler] No vnx-auto-dream cron entry found.")
        return
    new_crontab = "\n".join(filtered) + "\n"
    proc = subprocess.run(["crontab", "-"], input=new_crontab, capture_output=True, text=True)
    if proc.returncode != 0:
        sys.stderr.write(f"[scheduler] crontab error: {proc.stderr.strip()}\n")
        sys.exit(1)
    print("[scheduler] Cron entry removed.")


def status_macos() -> None:
    path = plist_path()
    if not path.exists():
        print("[scheduler] macOS LaunchAgent: not installed")
        return
    result = subprocess.run(
        ["launchctl", "list", _PLIST_LABEL], capture_output=True, text=True
    )
    state = "active" if result.returncode == 0 else "installed-not-loaded"
    print(f"[scheduler] macOS LaunchAgent: {state}")
    print(f"[scheduler] Plist: {path}")


def status_linux() -> None:
    result = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
    if result.returncode == 0 and _CRON_MARKER in result.stdout:
        for line in result.stdout.splitlines():
            if _CRON_MARKER in line:
                print(f"[scheduler] Cron: {line}")
    else:
        print("[scheduler] Cron: not installed")


def main() -> None:
    parser = argparse.ArgumentParser(description="VNX auto-dream scheduler (ADR-019)")
    sub = parser.add_subparsers(dest="action", required=True)

    p_install = sub.add_parser("install", help="Install and activate nightly scheduler")
    p_install.add_argument("--project-id", required=True, help="Project ID (ADR-007)")
    p_install.add_argument("--vnx-bin", default=None, help="Path to vnx binary")
    p_install.add_argument("--project-root", default=None, help="Project root")
    p_install.add_argument(
        "--no-load", action="store_true", help="Write config but do not activate (CI/dry-run)"
    )

    sub.add_parser("uninstall", help="Remove nightly scheduler")
    sub.add_parser("status", help="Show scheduler status")

    args = parser.parse_args()

    if args.action == "install":
        vnx_bin = args.vnx_bin or shutil.which("vnx") or "vnx"
        project_root = (
            str(Path(args.project_root).resolve())
            if args.project_root
            else str(resolve_project_root(__file__))
        )
        if sys.platform == "darwin":
            install_macos(args.project_id, vnx_bin, project_root, load=not args.no_load)
        else:
            install_linux(args.project_id, vnx_bin)

    elif args.action == "uninstall":
        if sys.platform == "darwin":
            uninstall_macos()
        else:
            uninstall_linux()

    elif args.action == "status":
        if sys.platform == "darwin":
            status_macos()
        else:
            status_linux()


if __name__ == "__main__":
    main()
