#!/usr/bin/env python3
"""Generate and install the macOS LaunchAgent plist for auto-dream (ADR-019).

Usage:
    python3 scripts/dream/install_scheduler.py \\
        --project-id <id> \\
        --vnx-bin /path/to/vnx \\
        [--project-root /path/to/project]

Writes ~/Library/LaunchAgents/com.vnx.auto-dream.plist and prints the
launchctl load command — does NOT auto-load (operator must run it).
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
from project_root import resolve_project_root  # noqa: E402


def _render_plist(template_path: Path, ctx: dict[str, str]) -> str:
    """Render Jinja2 template with context dict."""
    try:
        from jinja2 import Environment, FileSystemLoader  # type: ignore[import]

        env = Environment(
            loader=FileSystemLoader(str(template_path.parent)),
            autoescape=False,
        )
        tmpl = env.get_template(template_path.name)
        return tmpl.render(**ctx)
    except ImportError:
        text = template_path.read_text(encoding="utf-8")
        for key, value in ctx.items():
            text = text.replace("{{ " + key + " }}", value)
        return text


def main() -> None:
    parser = argparse.ArgumentParser(description="Install auto-dream LaunchAgent")
    parser.add_argument("--project-id", required=True, help="VNX project_id (ADR-007)")
    parser.add_argument(
        "--vnx-bin",
        default=None,
        help="Path to vnx binary (default: shutil.which('vnx'))",
    )
    parser.add_argument(
        "--project-root",
        default=None,
        help="Project root (default: git-resolved)",
    )
    args = parser.parse_args()

    vnx_bin = args.vnx_bin or shutil.which("vnx") or "vnx"
    project_root = (
        Path(args.project_root).resolve()
        if args.project_root
        else resolve_project_root(__file__)
    )

    template_path = Path(__file__).resolve().parent / "templates" / "com.vnx.auto-dream.plist.j2"
    ctx = {
        "vnx_bin_path": vnx_bin,
        "project_id": args.project_id,
        "project_root": str(project_root),
    }
    rendered = _render_plist(template_path, ctx)

    out_dir = Path.home() / "Library" / "LaunchAgents"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "com.vnx.auto-dream.plist"
    # Atomic write: interrupted installs must not leave a partial plist.
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

    print(f"Wrote: {out_path}")
    print()
    print("To enable nightly auto-dream at 03:00 local time, run:")
    print(f"  launchctl load -w {out_path}")
    print()
    print("To disable:")
    print(f"  launchctl unload -w {out_path}")


if __name__ == "__main__":
    main()
