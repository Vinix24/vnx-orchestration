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
from typing import List, Optional, Tuple

REEXEC_ENV_FLAG = "VNX_PIN_REEXECED"

PIN_FILE_NAME = ".vnx-version"
INSTALL_MODE_MARKER = ".vnx-install-mode"
INSTALL_MODE_VALUE = "central"

# Same pin alphabet as the central-install shim (bin/vnx in ~/.vnx-system)
# and `vnx init --set-version`. Forbids '/' and shell metacharacters.
_PIN_RE = re.compile(r"^[A-Za-z0-9._-]+$")

# Rolling (living-checkout) version dir names. `vnx update --to edge` materializes
# main as ``versions/edge``; such a dir carries arbitrary commits while its VERSION
# file can lag behind the released versions. Its VERSION must never be treated as a
# released version (OI-892 doubleganger), so ``_running_version`` reports the dir
# name instead of the stale VERSION file.
_ROLLING_DIR_RE = re.compile(r"^(?:edge|latest)$", re.IGNORECASE)


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


def _find_pin_dir(start: Path) -> Optional[Path]:
    """Walk UP from ``start`` looking for the nearest ancestor holding a
    ``.vnx-version`` file. Returns that ancestor, or None when none of the
    directories walked has one.

    Same walk SHAPE as ``scripts/lib/vnx_paths.py::_project_id_from_marker``
    (check ``start``, then each parent in turn, first hit wins) — not
    imported from there, on purpose: that module pulls in ``vnx_ids`` and
    ``data_dir_guard`` (~8ms just to import, measured), and this check runs
    on the startup path of EVERY ``vnx`` invocation, including the common
    case of no pin at all, before this module has decided whether an engine
    bootstrap is even warranted (see the module docstring). Re-implementing
    six lines of ``pathlib`` here keeps that path cheap; other command
    modules already import lightweight symbols FROM ``_reexec`` rather than
    the reverse (see ``vnx_cli/commands/update.py`` and ``doctor.py``), so
    this direction of no-shared-import is the established boundary, not a
    new one.

    Bounded at the resolved home directory (exclusive) — deliberately
    NARROWER than the marker walk, which climbs unbounded to the filesystem
    root. The two markers carry different blast radii: a stray
    ``.vnx-project-id`` picked up from a shared ancestor mis-routes state
    storage for one project; a stray ``.vnx-version`` picked up the same way
    would silently pin the ENGINE CODE VERSION for every project a user runs
    ``vnx`` from underneath that ancestor — a home-directory-wide footgun,
    since nobody deliberately places a version pin above their own project
    root. git-toplevel was considered instead and rejected: resolving it
    means spawning a ``git`` subprocess on every invocation before we even
    know a pin exists, which the fail-fast design of this module (no engine
    bootstrap until a pin is confirmed) is built to avoid. When the home
    directory itself cannot be resolved (rare — a misconfigured environment)
    this falls open to the marker's unbounded behavior rather than refusing
    to search at all, matching the fail-open contract of this whole module.
    """
    try:
        home = Path.home().resolve()
    except (OSError, RuntimeError):
        home = None
    for ancestor in [start, *start.parents]:
        if home is not None and ancestor == home:
            break
        if (ancestor / PIN_FILE_NAME).is_file():
            return ancestor
    return None


def _read_pin(project_dir: Path) -> Tuple[Optional[str], Optional[Path]]:
    """Return ``(pin, pin_dir)``: the validated pin and the directory that
    held the ``.vnx-version`` file it came from.

    ``(None, None)`` when no usable pin exists anywhere in the walk
    (absent everywhere within the boundary, empty, unreadable, malformed) —
    every one of those is a no-re-exec outcome. Once an ancestor WITH the
    file is found, only that file is considered: an empty, unreadable, or
    malformed pin there is terminal (warn + no re-exec), never a reason to
    keep climbing past it in search of a valid one further up. ``pin_dir``
    is still returned as ``None`` in that failure case — there is no
    honored pin to attribute a directory to.
    """
    try:
        start = project_dir.expanduser().resolve()
    except OSError:
        return None, None
    pin_dir = _find_pin_dir(start)
    if pin_dir is None:
        return None, None
    pin_file = pin_dir / PIN_FILE_NAME
    try:
        lines = pin_file.read_text(encoding="utf-8").splitlines()
        first = lines[0].strip() if lines else ""
    except OSError as exc:
        _warn(f"cannot read {pin_file} ({exc}); running current version")
        return None, None
    if not first:
        return None, None
    if first in (".", "..") or not _PIN_RE.match(first):
        _warn(f"malformed pin {first!r} in {pin_file}; running current version")
        return None, None
    return first, pin_dir


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
    """The version of the code this process actually loaded.

    For a central version dir this is its VERSION file. A rolling dir
    (``edge``/``latest``) is reported by its dir NAME: its VERSION file is not
    authoritative and can impersonate a released version it is not running
    (OI-892) — the dir name is the honest identity.
    """
    if _ROLLING_DIR_RE.match(engine_root.name):
        return engine_root.name
    try:
        text = (engine_root / "VERSION").read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return text or None


def _store_root(engine_root: Path) -> Path:
    """The central store root (the dir holding ``versions/`` and ``current``).

    A central install always lives at ``<root>/versions/<v>``, so the running
    engine root's grandparent IS the store root (this also honors custom roots
    naturally). Fall back to ``$VNX_HOME_ROOT`` then the default
    ``~/.vnx-system`` for non-standard layouts.
    """
    if engine_root.parent.name == "versions":
        return engine_root.parent.parent
    env_root = os.environ.get("VNX_HOME_ROOT")
    if env_root:
        return Path(env_root).expanduser().resolve()
    return Path.home() / ".vnx-system"


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


def _resolved_current_version(engine_root: Path) -> Optional[str]:
    """The version the ``current`` symlink ACTUALLY resolves to.

    Reads the ``current`` symlink in the central store root and reports the
    target's directory NAME (e.g. ``v1.4.4``), not a version LABEL read from
    a VERSION file. This is the honest identity of what is running: a stale
    VERSION file (OI-1070) can name a version that is not the one installed
    under ``current``, so the resolved dir name is the only trustworthy value.

    Returns None when ``current`` is absent, not a symlink, or unresolvable.
    """
    current = _store_root(engine_root) / "current"
    if not current.is_symlink():
        return None
    try:
        target = current.resolve()
    except OSError:
        return None
    if not target.exists():
        return None
    # Report the dir name (``v1.4.4``), normalized to strip a decorative ``v``
    # so it reads as a version the way the rest of the output does.
    return _normalize_version(target.name) or target.name


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


def _warn_diverged_dev_checkout(pin: str, pin_dir: Path, engine_root: Path) -> None:
    """Warn when a dev checkout is running a version the found pin disagrees
    with. This is the one branch where a pin was actually LOCATED but no
    re-exec is possible (dev checkouts run whatever code tree they are), so
    without this the divergence between "pin says X" and "actually running
    Y" was completely silent — the exact symptom this dispatch exists to
    close (OI-1170)."""
    running = _running_version(engine_root)
    if running is not None and _normalize_version(running) == _normalize_version(pin):
        return
    _warn(
        f"pin {pin!r} (from {pin_dir / PIN_FILE_NAME}) names a different "
        f"version than the one running ({running or 'unknown'}), but this is "
        "a dev checkout (no .vnx-install-mode=central marker) so the pin "
        "cannot be honored by re-exec here; running current version"
    )


def _maybe_reexec_pinned(argv: List[str]) -> None:
    pin, pin_dir = _read_pin(_resolve_project_dir(argv))
    if pin is None:
        return

    # Loop-guard: already re-exec'd to this exact pin — never exec again.
    already = os.environ.get(REEXEC_ENV_FLAG, "").strip()
    if already and _normalize_version(already) == _normalize_version(pin):
        return

    from vnx_cli import _engine

    engine_root = _engine.engine_root()
    if not _is_central_install(engine_root):
        _warn_diverged_dev_checkout(pin, pin_dir, engine_root)
        return  # dev checkout / non-central install: never re-exec

    versions_dir = _versions_dir(engine_root)
    pinned_dir = _resolve_pinned_dir(versions_dir, pin)
    if pinned_dir is None:
        # OI-1070: name the version the ``current`` symlink ACTUALLY resolves
        # to, not a VERSION label that can disagree with the installed dir.
        resolved = _resolved_current_version(engine_root)
        _warn(
            f"pin {pin!r} (from {pin_dir / PIN_FILE_NAME}) is not installed "
            f"under {versions_dir}; running current version ({resolved or 'unknown'})"
        )
        return

    # OI-892: compare install IDENTITY (the resolved dir), not the VERSION
    # string. A rolling dir like versions/edge carries arbitrary commits while
    # its VERSION file stays behind, so a VERSION-string match would let it
    # impersonate the pin and silently skip the re-exec forever. Re-exec
    # whenever the running engine root is not the pinned dir itself.
    try:
        running_is_pinned = engine_root.resolve() == pinned_dir.resolve()
    except OSError:
        running_is_pinned = False
    if running_is_pinned:
        return  # already running the pinned install

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
