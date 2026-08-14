#!/usr/bin/env python3
"""Startup pin re-exec for the pip ``vnx`` console script.

Design-track ``pip-cli-honor-pin-via-reexec``. The pip CLI is API-coupled to
its engine (see the NOTE in ``vnx_cli/_engine.py::engine_root``), so honoring
a project's ``.vnx-version`` pin by swapping the engine root in-process can
crash a new CLI against an old engine. Instead, when the running install does
not satisfy the pin, this module re-execs a DIFFERENT install's ENTIRE tree
(its ``vnx_cli`` + its engine, consistent) BEFORE any engine code loads:
``python -m vnx_cli.main`` with that install's root on ``PYTHONPATH``.

Pin semantics (OI-1171 — floor, not freeze): ``.vnx-version`` names a MINIMUM
acceptable version, not an exact one. The running central install already
satisfies most pins most of the time (``current`` moves forward as the store
publishes new releases), so the common outcome is "run current, no re-exec at
all" — a project pinned to an older floor is pulled FORWARD automatically
instead of sitting frozen on it. Only when the running version is BELOW the
floor does this module act, and then in one of two ways:

* the exact floor version is still installed in the central store -> re-exec
  to it (this at least satisfies what the project declared it needs, even
  though it is not ``current``);
* it is not installed anywhere in the store -> loud failure (see below).

Escape hatch: ``.vnx-version-freeze``, found via the same walk as
``.vnx-version``, restores the OLD exact-pin behavior wholesale (re-exec to
that EXACT version, fully fail-open on any ambiguity) and takes priority over
the floor pin when present. This is the release-went-bad valve: floor
semantics make a bad release affect the whole fleet the moment it becomes
``current`` (every project's floor is instantly satisfied by it), so a
project that needs to sit out a bad release until it is fixed forward needs a
way to say "not that one" explicitly. A file (not an env var) so the decision
is committed, reviewable, and visible in ``git status`` — the same paradigm
``.vnx-version`` and ``.vnx-install-mode`` already use, rather than an
ambient shell var that can quietly outlive the incident it was set for.

Safety contract (shared binary — non-negotiable):

* Loop-guard: ``VNX_PIN_REEXECED`` is set to the version actually exec'd to
  (the floor pin's exact value, or the freeze value) before execv; a process
  that already re-exec'd to that exact value never re-execs again on it.
* Fail-open, with ONE deliberate exception: any ambiguity (unreadable/
  malformed pin, unresolvable interpreter, execv failure, a found-but-broken
  pinned install) logs a warning and continues with the CURRENT version — a
  pin/freeze problem must never break the CLI on its own account. The one
  exception is a floor genuinely UNMET with no installed version able to meet
  it: continuing silently there would recreate the exact bug this dispatch
  exists to close (a project quietly running older code than it declares it
  needs), so that ONE case raises ``SystemExit`` instead of warning — loud,
  with the exact remediation, never silent. The freeze escape hatch never
  raises this way: it is a deliberately soft, best-effort override, not a
  second guarantee to enforce.
* Dev checkouts never re-exec: the re-exec only fires when the RUNNING
  engine root carries the ``.vnx-install-mode=central`` marker written by
  install-central.sh. A dev checkout below the floor (or diverged from a
  freeze) is warned about, since it cannot be re-exec'd out of, but never
  blocked — a developer's local checkout is exempt from fleet version
  guarantees by construction.
* Rolling installs (``versions/edge``, ``versions/latest``) are exempt from
  floor enforcement the same way: their version identity is a dir NAME, not
  a comparable X.Y.Z, so "does this satisfy the floor" cannot be answered
  numerically. An operator who deliberately runs a rolling install has
  already opted out of pinned-version tracking.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from typing import List, Optional, Tuple

REEXEC_ENV_FLAG = "VNX_PIN_REEXECED"

PIN_FILE_NAME = ".vnx-version"
FREEZE_FILE_NAME = ".vnx-version-freeze"
INSTALL_MODE_MARKER = ".vnx-install-mode"
INSTALL_MODE_VALUE = "central"

# Same pin alphabet as the central-install shim (bin/vnx in ~/.vnx-system)
# and `vnx init --set-version`. Forbids '/' and shell metacharacters. Shared
# by both .vnx-version and .vnx-version-freeze — same grammar, same risk
# (both name a version dir under the central store).
_PIN_RE = re.compile(r"^[A-Za-z0-9._-]+$")

# X.Y.Z, optionally 'v'-prefixed, optionally with a '-suffix' (pre-release —
# e.g. the v1.0.0-rc1..rc9 tags this repo actually cut). The suffix is not
# given full semver precedence (rc1 vs rc2 are not ordered against each
# other) — it only needs to sort a pre-release BELOW its own final release
# (1.4.0-rc1 < 1.4.0), which is all floor comparison needs: no consumer pins
# to a specific -rc build.
_VERSION_NUM_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)(?:-(.+))?$")

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


def _parse_version(value: str) -> Optional[Tuple[int, int, int, int]]:
    """Parse an X.Y.Z(-suffix) version string into a comparable tuple.

    Returns ``None`` for anything that is not this shape — a rolling dir
    name (``edge``/``latest``), a malformed pin, or any other non-numeric
    identity. ``None`` means "not comparable", not "lowest possible
    version": callers must treat it as exempt from floor comparison, never
    as automatically failing one.

    The 4th slot ranks a final release (0) above any pre-release of the same
    X.Y.Z (-1) — e.g. ``1.4.0-rc1`` < ``1.4.0`` — without attempting full
    semver precedence between different pre-releases of the same version.
    """
    m = _VERSION_NUM_RE.match(value.strip())
    if not m:
        return None
    major, minor, patch, prerelease = m.groups()
    rank = 0 if prerelease is None else -1
    return (int(major), int(minor), int(patch), rank)


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


def _find_value_dir(start: Path, filename: str) -> Optional[Path]:
    """Walk UP from ``start`` looking for the nearest ancestor holding
    ``filename``. Returns that ancestor, or None when none of the
    directories walked has one.

    Same walk SHAPE as ``scripts/lib/vnx_paths.py::_project_id_from_marker``
    (check ``start``, then each parent in turn, first hit wins) — not
    imported from there, on purpose: that module pulls in ``vnx_ids`` and
    ``data_dir_guard`` (~8ms just to import, measured), and this check runs
    on the startup path of EVERY ``vnx`` invocation, including the common
    case of no pin at all, before this module has decided whether an engine
    bootstrap is even warranted. Re-implementing six lines of ``pathlib``
    here keeps that path cheap; other command modules already import
    lightweight symbols FROM ``_reexec`` rather than the reverse (see
    ``vnx_cli/commands/update.py`` and ``doctor.py``), so this direction of
    no-shared-import is the established boundary, not a new one.

    Bounded at the resolved home directory (exclusive) — deliberately
    NARROWER than the marker walk, which climbs unbounded to the filesystem
    root. The two markers carry different blast radii: a stray
    ``.vnx-project-id`` picked up from a shared ancestor mis-routes state
    storage for one project; a stray ``.vnx-version``/``.vnx-version-freeze``
    picked up the same way would silently pin the ENGINE CODE VERSION for
    every project a user runs ``vnx`` from underneath that ancestor — a
    home-directory-wide footgun, since nobody deliberately places a version
    pin above their own project root. git-toplevel was considered instead
    and rejected: resolving it means spawning a ``git`` subprocess on every
    invocation before we even know a pin exists, which the fail-fast design
    of this module (no engine bootstrap until a pin is confirmed) is built
    to avoid. When the home directory itself cannot be resolved (rare — a
    misconfigured environment) this falls open to the marker's unbounded
    behavior rather than refusing to search at all, matching the fail-open
    contract of this whole module.
    """
    try:
        home = Path.home().resolve()
    except (OSError, RuntimeError):
        home = None
    for ancestor in [start, *start.parents]:
        if home is not None and ancestor == home:
            break
        if (ancestor / filename).is_file():
            return ancestor
    return None


def _read_value_file(project_dir: Path, filename: str) -> Tuple[Optional[str], Optional[Path]]:
    """Return ``(value, holding_dir)``: the validated first line of the
    nearest ancestor's ``filename``, and the directory that held it.

    ``(None, None)`` when no usable value exists anywhere in the walk
    (absent everywhere within the boundary, empty, unreadable, malformed) —
    every one of those is a no-op outcome for the caller. Once an ancestor
    WITH the file is found, only that file is considered: an empty,
    unreadable, or malformed value there is terminal (warn + no-op), never a
    reason to keep climbing past it in search of a valid one further up.
    ``holding_dir`` is still ``None`` in that failure case — there is no
    honored value to attribute a directory to.
    """
    try:
        start = project_dir.expanduser().resolve()
    except OSError:
        return None, None
    holding_dir = _find_value_dir(start, filename)
    if holding_dir is None:
        return None, None
    value_file = holding_dir / filename
    try:
        lines = value_file.read_text(encoding="utf-8").splitlines()
        first = lines[0].strip() if lines else ""
    except OSError as exc:
        _warn(f"cannot read {value_file} ({exc}); running current version")
        return None, None
    if not first:
        return None, None
    if first in (".", "..") or not _PIN_RE.match(first):
        _warn(f"malformed value {first!r} in {value_file}; running current version")
        return None, None
    return first, holding_dir


def _read_pin(project_dir: Path) -> Tuple[Optional[str], Optional[Path]]:
    return _read_value_file(project_dir, PIN_FILE_NAME)


def _read_freeze(project_dir: Path) -> Tuple[Optional[str], Optional[Path]]:
    return _read_value_file(project_dir, FREEZE_FILE_NAME)


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


def _pin_candidate_names(pin: str) -> List[str]:
    """The dir names a *pin* could resolve to: as written, then the
    ``v``-prefixed / unprefixed spellings (the pin file and the dir name may
    differ on the decorative ``v``)."""
    candidates = [pin]
    norm = _normalize_version(pin)
    for alt in (f"v{norm}", norm):
        if alt and alt not in candidates:
            candidates.append(alt)
    return candidates


def _pin_dir_exists(versions_dir: Path, pin: str) -> bool:
    """True when SOME candidate dir for *pin* exists under *versions_dir* —
    regardless of whether ``_resolve_pinned_dir`` would actually accept it
    (e.g. a symlink escape). Distinguishes "genuinely not installed" from
    "installed but refused/broken" for the floor-violation path: only the
    former is the loud-failure case (see ``_maybe_reexec_pinned``)."""
    return any((versions_dir / name).is_dir() for name in _pin_candidate_names(pin))


def _resolve_pinned_dir(versions_dir: Path, pin: str) -> Optional[Path]:
    """Find the pinned install in the central store.

    Tries the pin as written, then the ``v``-prefixed / unprefixed spellings
    (the pin file and the dir name may differ on the decorative ``v``).
    Refuses any entry that resolves outside ``versions_dir`` (symlink escape),
    mirroring the shim's realpath guard.
    """
    for name in _pin_candidate_names(pin):
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


def _exec_pinned(pinned_dir: Path, target: str, argv: List[str]) -> None:
    """Re-exec into *pinned_dir* as a WHOLE install, targeting version
    *target* (used for the loop-guard flag and log messages only).

    Fail-open on any LOCAL problem with the re-exec itself (missing vnx_cli
    package, unresolvable interpreter, execv failure): warns and returns,
    letting the caller fall through to running current. Never raises — a
    found-but-broken install is a different, rarer failure class than a
    genuinely absent one, and degrades the same way the rest of this module
    does on ambiguity.
    """
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
    os.environ[REEXEC_ENV_FLAG] = target
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
        _warn(f"re-exec to {target!r} failed ({exc}); running current version")


def maybe_reexec_pinned(argv: Optional[List[str]] = None) -> None:
    """Honor a project's version floor (or freeze) by re-exec'ing a
    different central install when needed.

    Call as the FIRST thing ``main()`` does, before argparse dispatch. Either
    replaces the process (os.execv — never returns), returns to let the
    current version continue, or raises ``SystemExit`` for the one case that
    is not fail-open (an unmet floor with nothing installed to meet it — see
    the module docstring's safety contract). Any OTHER unexpected failure
    degrades to a warning: a pin/freeze problem must never break the CLI.
    """
    if argv is None:
        argv = sys.argv[1:]
    try:
        _maybe_reexec_pinned(argv)
    except SystemExit:
        raise  # the one deliberate non-fail-open case — let it propagate
    except Exception as exc:  # fail-open: a pin problem must never break the CLI
        _warn(f"pin re-exec check failed ({exc}); running current version")


def _warn_dev_checkout_below_floor(pin: str, pin_dir: Path, engine_root: Path) -> None:
    """Warn when a dev checkout is running a version below the floor a found
    pin declares. This is the one branch where a floor was actually LOCATED
    but no re-exec is possible (dev checkouts run whatever code tree they
    are), so without this the divergence between "floor requires >= X" and
    "actually running Y < X" was completely silent."""
    running = _running_version(engine_root)
    running_tuple = _parse_version(running) if running else None
    pin_tuple = _parse_version(pin)
    if running_tuple is not None and pin_tuple is not None and running_tuple >= pin_tuple:
        return  # satisfied — nothing to warn about
    _warn(
        f"floor {pin!r} (from {pin_dir / PIN_FILE_NAME}) is not satisfied by the "
        f"running version ({running or 'unknown'}), but this is a dev checkout (no "
        f"{INSTALL_MODE_MARKER}={INSTALL_MODE_VALUE} marker) so it cannot be honored "
        "by re-exec here; running current version"
    )


def _warn_dev_checkout_diverged_freeze(freeze: str, freeze_dir: Path, engine_root: Path) -> None:
    """Same as above, for the freeze escape hatch (exact-match semantics,
    not a floor): warns only when the running version actually differs from
    the frozen one."""
    running = _running_version(engine_root)
    if running is not None and _normalize_version(running) == _normalize_version(freeze):
        return
    _warn(
        f"freeze {freeze!r} (from {freeze_dir / FREEZE_FILE_NAME}) names a different "
        f"version than the one running ({running or 'unknown'}), but this is a dev "
        f"checkout (no {INSTALL_MODE_MARKER}={INSTALL_MODE_VALUE} marker) so it cannot "
        "be honored by re-exec here; running current version"
    )


def _apply_freeze(freeze: str, freeze_dir: Path, argv: List[str]) -> None:
    """Escape hatch: re-exec to the EXACT frozen version, old-style pin
    behavior, overriding the floor entirely. Fully fail-open — never raises
    SystemExit, unlike the floor path — because this is a deliberate,
    already-explicit operator override, not the default guarantee."""
    already = os.environ.get(REEXEC_ENV_FLAG, "").strip()
    if already and _normalize_version(already) == _normalize_version(freeze):
        return

    from vnx_cli import _engine

    engine_root = _engine.engine_root()
    if not _is_central_install(engine_root):
        _warn_dev_checkout_diverged_freeze(freeze, freeze_dir, engine_root)
        return

    versions_dir = _versions_dir(engine_root)
    pinned_dir = _resolve_pinned_dir(versions_dir, freeze)
    if pinned_dir is None:
        resolved = _resolved_current_version(engine_root)
        _warn(
            f"freeze {freeze!r} (from {freeze_dir / FREEZE_FILE_NAME}) is not "
            f"installed under {versions_dir}; running current version "
            f"({resolved or 'unknown'})"
        )
        return

    # Compare install IDENTITY (the resolved dir), not the VERSION string —
    # same OI-892 reasoning as the floor path: a rolling dir's VERSION file
    # is not authoritative and must never be trusted to mean "already there".
    try:
        running_is_frozen = engine_root.resolve() == pinned_dir.resolve()
    except OSError:
        running_is_frozen = False
    if running_is_frozen:
        return

    _exec_pinned(pinned_dir, freeze, argv)


def _maybe_reexec_pinned(argv: List[str]) -> None:
    project_dir = _resolve_project_dir(argv)

    # Escape hatch takes priority over the floor entirely — see module
    # docstring. Checked first so a project sitting out a bad release never
    # even evaluates the floor.
    freeze, freeze_dir = _read_freeze(project_dir)
    if freeze is not None:
        _apply_freeze(freeze, freeze_dir, argv)
        return

    pin, pin_dir = _read_pin(project_dir)
    if pin is None:
        return

    from vnx_cli import _engine

    engine_root = _engine.engine_root()
    if not _is_central_install(engine_root):
        _warn_dev_checkout_below_floor(pin, pin_dir, engine_root)
        return  # dev checkout / non-central install: never re-exec

    pin_tuple = _parse_version(pin)
    if pin_tuple is None:
        _warn(
            f"floor {pin!r} (from {pin_dir / PIN_FILE_NAME}) is not a recognized "
            "X.Y.Z version; cannot enforce as a floor. Running current version."
        )
        return

    running_version = _running_version(engine_root)
    running_tuple = _parse_version(running_version) if running_version else None
    if running_tuple is None:
        # Rolling install (edge/latest) or an unreadable VERSION file: not
        # numerically comparable to the floor. Exempt, not a violation —
        # see the module docstring.
        return

    if running_tuple >= pin_tuple:
        return  # current already satisfies the floor — the common case

    # FLOOR VIOLATION: the running version is below what the project
    # declares it needs.
    already = os.environ.get(REEXEC_ENV_FLAG, "").strip()
    if already and _normalize_version(already) == _normalize_version(pin):
        return  # already re-exec'd to this floor this process; never loop

    versions_dir = _versions_dir(engine_root)
    pinned_dir = _resolve_pinned_dir(versions_dir, pin)
    if pinned_dir is None:
        if _pin_dir_exists(versions_dir, pin):
            # A candidate dir for the pin exists but _resolve_pinned_dir
            # refused it (symlink escape) or _exec_pinned would find it
            # broken — _resolve_pinned_dir already warned. This is the
            # SOFTER failure class (found but unusable, not absent): fail
            # open like every other local-install problem in this module.
            return
        # The exact floor version is not installed anywhere in the store
        # either — there is no version this process could run that would
        # satisfy the operator's stated requirement. Silently continuing on
        # `current` would recreate precisely the bug this dispatch exists to
        # close (a project quietly running older code than it declares it
        # needs), so this is the ONE case in this module that does not fail
        # open: loud, with the exact remediation, instead of a warning.
        resolved = _resolved_current_version(engine_root)
        raise SystemExit(
            f"[vnx-reexec] ERROR: {pin_dir / PIN_FILE_NAME} requires >= {pin!r}, "
            f"but the running install is {running_version!r} "
            f"(current -> {resolved or 'unknown'}) and {pin!r} is not installed "
            f"under {versions_dir} either.\n"
            f"  Fix: publish/install {pin!r} into the central store "
            f"(`vnx release publish --tag v{_normalize_version(pin)}` on the fabric "
            f"repo), or lower the floor in {pin_dir / PIN_FILE_NAME}.\n"
            f"  Escape hatch: to run an older version anyway (e.g. a bad release), "
            f"write it to {pin_dir / FREEZE_FILE_NAME} instead — that is honored "
            "exactly, not as a floor."
        )

    _exec_pinned(pinned_dir, pin, argv)
