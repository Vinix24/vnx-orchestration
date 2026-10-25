#!/usr/bin/env python3
"""How far a project's ``.vnx-version`` pin sits behind the store's ``current``.

``vnx doctor`` names a pin that is not honored ("pins 1.7.5 but active is
v1.7.6") but not the DISTANCE. Measured 24-10: the store held 16 releases
(``v1.5.0`` ... ``v1.7.6``) with ``current -> v1.7.6`` while ``mission-control``
pinned ``1.7.5`` -- one release behind. That count is all this module computes:
read-only, stdlib only, no CLI, no caller and no write in this PR.

The decorative-``v`` normalizer and the rolling (``edge``/``latest``) dir names
(OI-892) live in ``vnx_cli/_reexec.py`` and are imported here, never copied.
``vnx_cli/commands/update.py``'s ``_list_version_dirs`` is deliberately NOT
reused: it sorts by mtime and includes ``versions/.vnx-data``. A pin audit needs
release ORDER -- ``major.minor.patch`` plus an optional pre-release, where
``-rc9`` sorts before its release. ``packaging`` would do that but is not a
declared dependency, hence the local comparator.

Verdicts: ``current`` is the pin ``current`` resolves to (``behind = 0``; a pin
AHEAD lands here too, with a ``note`` -- the install exists and is not behind,
and a negative distance is an amount nobody can act on); ``lagging`` is an
existing release dir with ``behind = N >= 1`` releases after it up to and
including ``current``, or ``behind = None`` when ``current`` itself is not a
release (rolling dir, dangling link) -- unknown, never ``0``, which would report
a lagging pin as green; ``non-release`` is an existing dir that is rolling or
not a version (never ``dead``, never ``0``); ``dead`` means no dir for the pin
exists at all.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import List, NamedTuple, Optional, Tuple

# The engine root carries ``scripts/`` and, in a dev checkout, ``vnx_cli/``; a
# packaged install ships the engine trees under ``vnx_orchestration/`` next to
# ``vnx_cli/``, one level higher. Whichever sibling holds ``_reexec.py`` goes on
# ``sys.path``; the file check keeps an unrelated ``vnx_cli`` off it.
_ENGINE_ROOT = Path(__file__).resolve().parent.parents[1]
for _root in (_ENGINE_ROOT, _ENGINE_ROOT.parent):
    if (_root / "vnx_cli" / "_reexec.py").is_file():
        if str(_root) not in sys.path:
            sys.path.insert(0, str(_root))
        break

from vnx_cli._reexec import _ROLLING_DIR_RE, _normalize_version

VERDICT_CURRENT = "current"
VERDICT_LAGGING = "lagging"
VERDICT_NON_RELEASE = "non-release"
VERDICT_DEAD = "dead"

# ``major.minor.patch`` plus an optional ``-pre.release`` tail. A name that does
# not parse is not a release: guessing one into existence is how ``.vnx-data``
# got counted as a version in the first place.
_VERSION_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z]+(?:[.-][0-9A-Za-z]+)*))?$")

# ``release_rank``: 0 for a pre-release, 1 for the release itself, so
# ``1.0.0-rc9`` sorts before ``1.0.0`` with no special-casing at the call site.
_VersionKey = Tuple[int, int, int, int, Tuple[Tuple[int, object], ...]]


class PinDrift(NamedTuple):
    """One pin against one store: ``pin`` normalized, ``current`` as resolved
    (``None`` when it is not a release), and ``behind`` ``None`` wherever there
    is no release distance to report."""

    pin: str
    current: Optional[str]
    behind: Optional[int]
    verdict: str
    note: Optional[str] = None


def _prerelease_key(pre: str) -> Tuple[Tuple[int, object], ...]:
    """Semver pre-release precedence: numeric identifiers compare numerically
    (``rc9 < rc10``, which string order gets wrong) and sort before alphanumeric
    ones; a shorter list sorts first."""
    key: List[Tuple[int, object]] = []
    for identifier in pre.split("."):
        if identifier.isdigit():
            key.append((0, int(identifier)))
        else:
            key.append((1, identifier))
    return tuple(key)


def _parse_version(name: str) -> Optional[_VersionKey]:
    """Sort key for a version dir NAME, or ``None`` when it is not a version."""
    match = _VERSION_RE.match(_normalize_version(name))
    if match is None:
        return None
    major, minor, patch, pre = match.groups()
    if pre is None:
        return (int(major), int(minor), int(patch), 1, ())
    return (int(major), int(minor), int(patch), 0, _prerelease_key(pre))


def load_store(versions_dir: Path) -> List[str]:
    """The released versions in a store's ``versions/`` dir, in semver order.

    Normalized and deduplicated -- ``v1.5.0`` and ``1.5.0`` are one release, and
    counting both would inflate every ``behind`` by one. Skipped: dot-dirs
    (``.vnx-data`` is runtime state, never a version), rolling dirs, entries
    that are not dirs, and names that do not parse. A missing or unreadable
    ``versions/`` yields an empty list.
    """
    try:
        entries = list(versions_dir.iterdir())
    except OSError:
        return []
    found = {}
    for entry in entries:
        name = entry.name
        if name.startswith(".") or _ROLLING_DIR_RE.match(name):
            continue
        try:
            if not entry.is_dir():
                continue
        except OSError:
            continue
        key = _parse_version(name)
        if key is not None:
            found.setdefault(_normalize_version(name), key)
    return sorted(found, key=found.__getitem__)


def current_version(store_root: Path) -> Optional[str]:
    """The released version the store's ``current`` symlink points at.

    Read from the link TARGET, never inferred from the highest dir in the store:
    a store whose highest dir is newer than ``current`` (a fetch that was never
    activated) would otherwise report a version nobody runs. ``None`` when
    ``current`` is missing, is not a symlink, dangles, is not a dir, or resolves
    to a rolling dir or a non-version name -- each of those means what runs is
    not a released version, so a distance to it is unknown, not zero.
    """
    try:
        current = store_root / "current"
        if not current.is_symlink():
            return None
        target = current.resolve()
    except OSError:
        return None
    if not target.is_dir():
        return None
    name = target.name
    if _ROLLING_DIR_RE.match(name) or _parse_version(name) is None:
        return None
    return _normalize_version(name)


def _pinned_dir(versions_dir: Path, pin: str) -> Optional[str]:
    """The dir NAME in ``versions/`` that the pin names, or ``None``.

    Tried as written, then with a ``v`` added and with one stripped -- the three
    spellings the startup re-exec resolves, since the pin file and the dir name
    may disagree on the decorative ``v``. A candidate resolving outside
    ``versions/`` (a ``..`` pin, an absolute path, an escaping symlink) is
    skipped rather than followed.
    """
    normalized = _normalize_version(pin)
    candidates = [pin]
    for alternative in (f"v{normalized}", normalized):
        if alternative and alternative not in candidates:
            candidates.append(alternative)
    try:
        root = versions_dir.resolve()
    except OSError:
        return None
    for name in candidates:
        candidate = versions_dir / name
        try:
            if not candidate.is_dir() or candidate.resolve().parent != root:
                continue
        except OSError:
            continue
        return name
    return None


def classify_pin(pin: str, store_root: Path) -> PinDrift:
    """Measure one pin against one store. Pure read; nothing is written."""
    versions_dir = store_root / "versions"
    current = current_version(store_root)
    normalized = _normalize_version(pin)
    pinned_name = _pinned_dir(versions_dir, pin)
    if pinned_name is None:
        return PinDrift(normalized, current, None, VERDICT_DEAD)
    pinned_key = _parse_version(pinned_name)
    if pinned_key is None or _ROLLING_DIR_RE.match(pinned_name):
        return PinDrift(normalized, current, None, VERDICT_NON_RELEASE)

    # ``current`` unresolvable to a release: the pin is a release dir that is
    # not what runs. The distance is unknown, so it must not be 0 (green) and
    # must not be a count over a store ``current`` does not name (fiction).
    current_key = _parse_version(current) if current is not None else None
    if current_key is None:
        return PinDrift(
            normalized,
            current,
            None,
            VERDICT_LAGGING,
            note=f"pin {normalized} is a release but current does not resolve to one; distance unknown",
        )

    if pinned_key >= current_key:
        note = None
        if pinned_key > current_key:
            note = f"pin {normalized} is ahead of current {current}"
        return PinDrift(normalized, current, 0, VERDICT_CURRENT, note)

    # Releases strictly after the pin up to and including current. ``current``
    # is counted even when absent from the listing (a symlink into a dir the
    # listing skips), so ``lagging`` can never carry ``behind = 0``.
    released = {
        key
        for key in (_parse_version(v) for v in load_store(versions_dir))
        if key is not None
    }
    released.add(current_key)
    behind = sum(1 for key in released if pinned_key < key <= current_key)
    return PinDrift(normalized, current, behind, VERDICT_LAGGING)
