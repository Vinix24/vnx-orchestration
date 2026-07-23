#!/usr/bin/env python3
"""Startup pin re-exec for the pip ``vnx`` console script.

Design-track ``pip-cli-honor-pin-via-reexec``. The pip CLI is API-coupled to
its engine (see the NOTE in ``vnx_cli/_engine.py::engine_root``), so honoring
a project's ``.vnx-version`` pin by swapping the engine root in-process can
crash a new CLI against an old engine. Instead, when the pin names a
DIFFERENT version than the one currently running, this module re-execs the
pinned version's ENTIRE install (its ``vnx_cli`` + its engine, consistent)
BEFORE any engine code loads: ``python -m vnx_cli.main`` with the pinned
install's root on ``PYTHONPATH``.

Safety contract (shared binary — non-negotiable):

* Loop-guard: ``VNX_PIN_REEXECED`` is set to the pin before execv; a process
  that already re-exec'd to that pin never re-execs again. This survives
  off-by-a-hair version detection in the pinned tree.
* Fail-open: ANY ambiguity (unreadable/malformed pin, pinned version missing
  from the central store, unresolvable interpreter, execv failure) logs a
  warning and continues with the CURRENT version. A pin problem must never
  break the CLI — it degrades to "ran the default version".
* Dev checkouts never re-exec: the re-exec only fires when the RUNNING
  engine root carries the ``.vnx-install-mode=central`` marker written by
  install-central.sh.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from typing import List, Optional

REEXEC_ENV_FLAG = "VNX_PIN_REEXECED"

PIN_FILE_NAME = ".vnx-version"
INSTALL_MODE_MARKER = ".vnx-install-mode"
INSTALL_MODE_VALUE = "central"

# Same pin alphabet as the central-install shim (bin/vnx in ~/.vnx-system)
# and `vnx init --set-version`. Forbids '/' and shell metacharacters.
_PIN_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def _warn(msg: str) -> None:
    print(f"[vnx-reexec] WARNING: {msg}", file=sys.stderr)


def _normalize_version(value: str) -> str:
    """Normalize a version string for comparison.

    Central version dirs are named e.g. ``v1.3.0`` while their ``VERSION``
    file contains ``1.3.0``; treat a single leading ``v`` before a digit as
    decorative so the two compare equal.
    """
    v = value.strip()
    if len(v) > 1 and v[0] in "vV" and v[1].isdigit():
        v = v[1:]
    return v


def _resolve_project_dir(argv: List[str]) -> Path:
    """Resolve the project dir the way ``main.py`` does: ``--project-dir``
    (last occurrence wins, both ``--project-dir DIR`` and ``--project-dir=DIR``
    forms) else the current directory. Fail-open to cwd on any oddity."""
    project_dir = "."
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--project-dir" and i + 1 < len(argv):
            project_dir = argv[i + 1]
            i += 2
            continue
        if arg.startswith("--project-dir="):
            project_dir = arg.split("=", 1)[1]
        i += 1
    try:
        return Path(project_dir).expanduser().resolve()
    except OSError:
        return Path.cwd()


def _read_pin(project_dir: Path) -> Optional[str]:
    """Return the validated pin from ``project_dir/.vnx-version``.

    None when there is no usable pin (absent, empty, unreadable, malformed) —
    every one of those is a no-re-exec outcome.
    """
    pin_file = project_dir / PIN_FILE_NAME
    if not pin_file.is_file():
        return None
    try:
        lines = pin_file.read_text(encoding="utf-8").splitlines()
        first = lines[0].strip() if lines else ""
    except OSError as exc:
        _warn(f"cannot read {pin_file} ({exc}); running current version")
        return None
    if not first:
        return None
    if first in (".", "..") or not _PIN_RE.match(first):
        _warn(f"malformed pin {first!r} in {pin_file}; running current version")
        return None
    return first


def _is_central_install(engine_root: Path) -> bool:
    """True when the running engine root carries the central-install marker.

    Mirrors the marker check in ``scripts/lib/vnx_paths.py::_is_central_install``
    (the marker is only written by install-central.sh). Kept as a direct file
    read so the re-exec decision needs no engine bootstrap.
    """
    marker = engine_root / INSTALL_MODE_MARKER
    try:
        return (
            marker.is_file()
            and marker.read_text(encoding="utf-8").strip() == INSTALL_MODE_VALUE
        )
    except OSError:
        return False


def _running_version(engine_root: Path) -> Optional[str]:
    """The version of the code this process actually loaded (its VERSION file)."""
    try:
        text = (engine_root / "VERSION").read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return text or None


def _versions_dir(engine_root: Path) -> Path:
    """The central store's ``versions/`` dir.

    A central install always lives at ``<root>/versions/<v>``, so the running
    engine root's parent IS the versions dir (this also honors custom roots
    naturally). Fall back to ``$VNX_HOME_ROOT/versions`` then the default
    ``~/.vnx-system/versions`` for non-standard layouts.
    """
    if engine_root.parent.name == "versions":
        return engine_root.parent
    env_root = os.environ.get("VNX_HOME_ROOT")
    if env_root:
        return Path(env_root).expanduser().resolve() / "versions"
    return Path.home() / ".vnx-system" / "versions"


def _resolve_pinned_dir(versions_dir: Path, pin: str) -> Optional[Path]:
    """Find the pinned install in the central store.

    Tries the pin as written, then the ``v``-prefixed / unprefixed spellings
    (the pin file and the dir name may differ on the decorative ``v``).
    Refuses any entry that resolves outside ``versions_dir`` (symlink escape),
    mirroring the shim's realpath guard.
    """
    candidates = [pin]
    norm = _normalize_version(pin)
    for alt in (f"v{norm}", norm):
        if alt and alt not in candidates:
            candidates.append(alt)
    for name in candidates:
        candidate = versions_dir / name
        if not candidate.is_dir():
            continue
        try:
            if candidate.resolve().parent != versions_dir.resolve():
                _warn(
                    f"pinned version {pin!r} escapes the versions root "
                    f"({versions_dir}); running current version"
                )
                return None
        except OSError as exc:
            _warn(f"cannot resolve pinned version dir {candidate} ({exc}); "
                  "running current version")
            return None
        return candidate
    return None


def maybe_reexec_pinned(argv: Optional[List[str]] = None) -> None:
    """Re-exec the pinned central install when the project pins another version.

    Call as the FIRST thing ``main()`` does, before argparse dispatch. Either
    replaces the process (os.execv — never returns) or returns to let the
    current version continue. Never raises: the whole body is fail-open.
    """
    if argv is None:
        argv = sys.argv[1:]
    try:
        _maybe_reexec_pinned(argv)
    except Exception as exc:  # fail-open: a pin problem must never break the CLI
        _warn(f"pin re-exec check failed ({exc}); running current version")


def _maybe_reexec_pinned(argv: List[str]) -> None:
    pin = _read_pin(_resolve_project_dir(argv))
    if pin is None:
        return

    # Loop-guard: already re-exec'd to this exact pin — never exec again.
    already = os.environ.get(REEXEC_ENV_FLAG, "").strip()
    if already and _normalize_version(already) == _normalize_version(pin):
        return

    from vnx_cli import _engine

    engine_root = _engine.engine_root()
    if not _is_central_install(engine_root):
        return  # dev checkout / non-central install: never re-exec

    running = _running_version(engine_root)
    if running is not None and _normalize_version(running) == _normalize_version(pin):
        return  # already running the pinned version

    versions_dir = _versions_dir(engine_root)
    pinned_dir = _resolve_pinned_dir(versions_dir, pin)
    if pinned_dir is None:
        _warn(
            f"pinned version {pin!r} is not installed under {versions_dir}; "
            f"running current version ({running or 'unknown'})"
        )
        return

    if not (pinned_dir / "vnx_cli" / "__init__.py").is_file():
        _warn(
            f"pinned install {pinned_dir} has no vnx_cli package; "
            "running current version"
        )
        return

    python = sys.executable
    if not python:
        _warn("cannot resolve the current python executable; running current version")
        return

    # Re-exec the pinned install as a WHOLE: its vnx_cli resolves its own
    # sibling engine, so CLI and engine always come from the same version.
    os.environ[REEXEC_ENV_FLAG] = pin
    pythonpath = [
        str(pinned_dir),
        str(pinned_dir / "scripts"),
        str(pinned_dir / "scripts" / "lib"),
    ]
    existing = os.environ.get("PYTHONPATH")
    if existing:
        pythonpath.append(existing)
    os.environ["PYTHONPATH"] = os.pathsep.join(pythonpath)
    # cwd-shadow hardening: `python -m` prepends the process's CWD to
    # sys.path ahead of PYTHONPATH, so a cwd-local `vnx_cli/` (a dev checkout
    # or a vendored copy in a consumer repo) would SHADOW the pinned install.
    # PYTHONSAFEPATH=1 + the explicit `-P` flag (both Python 3.11+, and
    # pyproject declares requires-python >= 3.11) tell the re-exec'd
    # interpreter not to prepend cwd, so the pinned vnx_cli on PYTHONPATH
    # always wins. Both are set belt-and-suspenders: env var survives any
    # argv rewrapping by a wrapper, -P documents the intent at the call site.
    os.environ["PYTHONSAFEPATH"] = "1"
    try:
        os.execv(python, [python, "-P", "-m", "vnx_cli.main", *argv])
    except OSError as exc:
        _warn(f"re-exec to pinned version {pin!r} failed ({exc}); running current version")
