#!/usr/bin/env python3
"""path_parity.py — PATH/interpreter parity check: foreground vs background.

OI-852: a Homebrew relink left the PATH-resolved ``python3`` that launchd /
cron / nohup jobs see broken (or different) while the interactive foreground
shell — with its aliases and brewed PATH — kept working. Every diagnosis made
from the foreground was contaminated: "works here" proved nothing about the
jobs that were actually failing. OI-852 is closed, but the relink that caused
it can happen again, so the check stays.

The check probes ``python3`` twice:
  foreground — the ambient environment this process inherited;
  background — a scrubbed environment with the launchd/cron default PATH
               (``/usr/bin:/bin:/usr/sbin:/sbin``), no aliases, no profile.

Parity fails when the background interpreter is unrunnable, or when its
major.minor version differs from the foreground's. An executable-PATH
difference alone is informational (macOS jobs legitimately resolve a
different binary than a brewed shell) — it is recorded, not flagged.

The library is pure/injectable for tests; the CLI is used by
scripts/hooks/path_parity_check.sh at SessionStart.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

_LOG = logging.getLogger(__name__)

# launchd/cron default PATH on macOS — what a background job actually gets.
BACKGROUND_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"

_PROBE = (
    "import json,sys;"
    "print(json.dumps({"
    "'executable':sys.executable,"
    "'version':sys.version.split()[0],"
    "'prefix':sys.prefix}))"
)


def probe_interpreter(runner: Any = None, *, env: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """Probe what ``python3`` resolves to under ``env`` (None = ambient).

    Returns {ok, executable, version, prefix, error}. Never raises — an
    unstartable interpreter is exactly what this check exists to report.
    """
    run = runner or subprocess.run
    try:
        proc = run(
            ["python3", "-c", _PROBE],
            capture_output=True,
            text=True,
            check=False,
            env=env,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"ok": False, "executable": None, "version": None, "prefix": None, "error": str(exc)}
    if proc.returncode != 0:
        return {
            "ok": False,
            "executable": None,
            "version": None,
            "prefix": None,
            "error": (proc.stderr or proc.stdout or "").strip()[:500] or f"exit {proc.returncode}",
        }
    try:
        data = json.loads(proc.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError) as exc:
        return {
            "ok": False,
            "executable": None,
            "version": None,
            "prefix": None,
            "error": f"unparseable probe output: {exc}",
        }
    return {
        "ok": True,
        "executable": data.get("executable"),
        "version": data.get("version"),
        "prefix": data.get("prefix"),
        "error": None,
    }


def background_env() -> Dict[str, str]:
    """A scrubbed environment mimicking launchd/cron: default PATH, no profile."""
    return {
        "PATH": BACKGROUND_PATH,
        "HOME": os.environ.get("HOME", "/"),
        "USER": os.environ.get("USER", ""),
        "LANG": os.environ.get("LANG", "en_US.UTF-8"),
    }


def _major_minor(version: Optional[str]) -> Optional[str]:
    if not version:
        return None
    parts = version.split(".")
    return ".".join(parts[:2]) if len(parts) >= 2 else version


def compare_parity(foreground: Dict[str, Any], background: Dict[str, Any]) -> Dict[str, Any]:
    """Decide parity from two probe results. Pure — no I/O, fully testable."""
    mismatches = []
    info = []

    if not foreground.get("ok"):
        # The foreground being broken is reported but cannot be compared
        # against — treat as a parity failure of its own kind.
        mismatches.append({"kind": "foreground_interpreter_broken", "error": foreground.get("error")})
    if not background.get("ok"):
        mismatches.append({"kind": "background_interpreter_broken", "error": background.get("error")})

    fg_mm = _major_minor(foreground.get("version"))
    bg_mm = _major_minor(background.get("version"))
    if foreground.get("ok") and background.get("ok") and fg_mm != bg_mm:
        mismatches.append(
            {
                "kind": "version_mismatch",
                "foreground_version": foreground.get("version"),
                "background_version": background.get("version"),
            }
        )

    if (
        foreground.get("ok")
        and background.get("ok")
        and foreground.get("executable") != background.get("executable")
    ):
        info.append(
            {
                "kind": "executable_differs",
                "foreground": foreground.get("executable"),
                "background": background.get("executable"),
            }
        )

    return {
        "parity": not mismatches,
        "mismatches": mismatches,
        "info": info,
    }


def check_parity(runner: Any = None) -> Dict[str, Any]:
    """Probe foreground + background and decide parity."""
    foreground = probe_interpreter(runner=runner)
    background = probe_interpreter(runner=runner, env=background_env())
    result = compare_parity(foreground, background)
    result.update(
        {
            "checked_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "foreground": foreground,
            "background": background,
            "background_path": BACKGROUND_PATH,
        }
    )
    return result


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description="Foreground/background python3 parity check")
    parser.add_argument("--write", default=None, help="Write the result JSON to this path")
    parser.add_argument("--no-fail", action="store_true", help="Always exit 0 (hook mode)")
    args = parser.parse_args(argv)

    result = check_parity()

    if args.write:
        try:
            out = Path(args.write)
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")
        except OSError as exc:
            _LOG.warning("path_parity: could not write %s: %s", args.write, exc)

    print(json.dumps(result))
    if result["parity"] or args.no_fail:
        return 0
    return 11


if __name__ == "__main__":
    raise SystemExit(main())
