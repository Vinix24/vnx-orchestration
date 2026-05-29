"""Dream scheduler — platform-aware install/uninstall of nightly auto-dream job.

macOS: LaunchAgent plist in ~/Library/LaunchAgents/ + `launchctl load -w`.
Linux: crontab entry (nightly 03:00).

ADR-007: project_id stamped for multi-project isolation.
ADR-019: auto-dream nightly consolidation.
"""
from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
import vnx_paths as _vnx_paths

_PLIST_LABEL = "com.vnx.auto-dream"
_PLIST_NAME = f"{_PLIST_LABEL}.plist"
_CRON_MARKER = f"# vnx-auto-dream:{_PLIST_LABEL}"


def _render_plist(template_path: Path, ctx: dict) -> str:
    try:
        from jinja2 import Environment, FileSystemLoader  # type: ignore[import]
        env = Environment(loader=FileSystemLoader(str(template_path.parent)), autoescape=False)
        return env.get_template(template_path.name).render(**ctx)
    except ImportError:
        text = template_path.read_text(encoding="utf-8")
        for key, value in ctx.items():
            text = text.replace("{{ " + key + " }}", value)
        return text


def _install_macos(project_id: str, project_root: Path, vnx_bin: str) -> str:
    template_path = (
        Path(__file__).resolve().parent / "templates" / "com.vnx.auto-dream.plist.j2"
    )
    ctx = {
        "vnx_bin_path": vnx_bin,
        "project_id": project_id,
        "project_root": str(project_root),
    }
    rendered = _render_plist(template_path, ctx)

    out_dir = Path.home() / "Library" / "LaunchAgents"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / _PLIST_NAME

    fd, tmp_name = tempfile.mkstemp(dir=out_dir, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(rendered)
        os.replace(tmp_name, out_path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise

    # Idempotent: unload before re-loading (suppressed if not loaded yet)
    subprocess.run(["launchctl", "unload", "-w", str(out_path)], capture_output=True)
    result = subprocess.run(
        ["launchctl", "load", "-w", str(out_path)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"launchctl load failed: {result.stderr.strip() or result.stdout.strip()}"
        )
    return f"Installed and loaded: {out_path}"


def _uninstall_macos() -> str:
    out_path = Path.home() / "Library" / "LaunchAgents" / _PLIST_NAME
    if not out_path.exists():
        return f"Not installed: {out_path}"
    subprocess.run(["launchctl", "unload", "-w", str(out_path)], capture_output=True)
    out_path.unlink()
    return f"Unloaded and removed: {out_path}"


def _cron_line(vnx_bin: str, project_id: str) -> str:
    return f"0 3 * * * {vnx_bin} dream run --project-id {project_id}  {_CRON_MARKER}"


def _install_linux(project_id: str, vnx_bin: str) -> str:
    new_line = _cron_line(vnx_bin, project_id)
    result = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
    existing = result.stdout if result.returncode == 0 else ""
    lines = [ln for ln in existing.splitlines() if _CRON_MARKER not in ln]
    lines.append(new_line)
    new_crontab = "\n".join(lines) + "\n"
    proc = subprocess.run(["crontab", "-"], input=new_crontab, text=True, capture_output=True)
    if proc.returncode != 0:
        raise RuntimeError(f"crontab install failed: {proc.stderr.strip()}")
    return f"Cron entry installed: {new_line}"


def _uninstall_linux() -> str:
    result = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
    if result.returncode != 0:
        return "No crontab found; nothing to remove."
    existing = result.stdout
    if _CRON_MARKER not in existing:
        return "Auto-dream cron entry not found; nothing to remove."
    lines = [ln for ln in existing.splitlines() if _CRON_MARKER not in ln]
    new_crontab = "\n".join(lines) + "\n" if lines else ""
    proc = subprocess.run(["crontab", "-"], input=new_crontab, text=True, capture_output=True)
    if proc.returncode != 0:
        raise RuntimeError(f"crontab removal failed: {proc.stderr.strip()}")
    return "Auto-dream cron entry removed."


def install_scheduler(
    project_id: str,
    project_root: Path | None = None,
    vnx_bin: str | None = None,
) -> str:
    """Install nightly auto-dream scheduler. Returns status string.

    macOS: writes ~/Library/LaunchAgents/com.vnx.auto-dream.plist, runs launchctl load -w.
    Linux: adds crontab entry at 03:00 daily.
    ADR-007: project_id on plist/cron for isolation.
    """
    if project_root is None:
        project_root = Path(_vnx_paths.resolve_paths()["PROJECT_ROOT"])
    if vnx_bin is None:
        vnx_bin = shutil.which("vnx") or "vnx"

    sys_platform = platform.system()
    if sys_platform == "Darwin":
        return _install_macos(project_id, project_root, vnx_bin)
    elif sys_platform == "Linux":
        return _install_linux(project_id, vnx_bin)
    else:
        raise RuntimeError(
            f"Unsupported platform: {sys_platform}. Only macOS and Linux supported."
        )


def uninstall_scheduler() -> str:
    """Remove nightly auto-dream scheduler. Returns status string."""
    sys_platform = platform.system()
    if sys_platform == "Darwin":
        return _uninstall_macos()
    elif sys_platform == "Linux":
        return _uninstall_linux()
    else:
        raise RuntimeError(f"Unsupported platform: {sys_platform}.")
