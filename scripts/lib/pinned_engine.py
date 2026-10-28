#!/usr/bin/env python3
"""pinned_engine.py: start a launchd job on its project's pinned engine (fleet PR-3).

A launchd job bakes its engine root (``VNX_HOME``) in at install time, so before this
module a job kept running whatever tree ``reload_plist.sh`` was run from, whatever the
project's ``.vnx-version`` said. :func:`resolve_engine` answers what a start needs: which
tree may this job use for ``project_id``? The checkout comes from the operator registry
through :func:`launchd_project_root.resolve_project_root`, and the pin is read by
``vnx_cli._reexec``, so neither the registry nor the pin rules get a second copy.

No pin -> the declared home (unchanged dev-checkout behaviour). A pin that resolves under
the declared store's ``versions/`` -> that tree. A pin that does not resolve (not
installed, unreadable, malformed, escaping) raises :class:`PinnedEngineRefused`; the CLI
exits 78 (EX_CONFIG) with ONE stderr line naming project, pin and the directory searched.
Never a silent fallback.

Every launchd start resolves again: a run in progress is never switched -- it finishes on
the tree it started from -- and only the NEXT start picks up a changed pin. Resolving at
run time is the point: a forgotten reload would otherwise run an old tree silently.

CLI: ``python3 pinned_engine.py <project_id> <declared_home> [relative_script]`` prints
the engine root, or exits 78 with one line. ``relative_script`` is the launcher's check
on the chosen tree: a pin older than the script is refused naming pin and missing path,
never patched from another tree.
"""

from __future__ import annotations

import sys
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Optional

_LIB_DIR = Path(__file__).resolve().parent
if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))

# The engine root carries ``scripts/`` and, in a dev checkout, ``vnx_cli/``; a packaged
# install ships the engine trees under ``vnx_orchestration/`` next to ``vnx_cli/``, one
# level higher. Same bootstrap as scripts/lib/pin_drift.py; the file check keeps an
# unrelated ``vnx_cli`` off ``sys.path``.
_ENGINE_ROOT = Path(__file__).resolve().parent.parents[1]
for _root in (_ENGINE_ROOT, _ENGINE_ROOT.parent):
    if (_root / "vnx_cli" / "_reexec.py").is_file():
        if str(_root) not in sys.path:
            sys.path.insert(0, str(_root))
        break

import vnx_cli._reexec as _reexec

from launchd_project_root import ProjectRootRefused, resolve_project_root

EX_CONFIG = 78
EX_USAGE = 64

SOURCE_PIN = "pin"
SOURCE_DECLARED = "declared"


class PinnedEngineRefused(Exception):
    """This start must be refused; ``str(exc)`` is the single stderr line."""


@dataclass(frozen=True)
class EngineChoice:
    """The tree a start may use, and where that choice came from."""

    engine_root: Path
    source: str  # SOURCE_PIN | SOURCE_DECLARED
    pin: Optional[str] = None


@contextmanager
def _quiet_pin_reader() -> Iterator[None]:
    """Silence ``vnx_cli._reexec``'s warnings while a pin is read.

    ``_read_pin``/``_resolve_pinned_dir`` are written for the CLI's FAIL-OPEN startup
    path: they warn ("running current version") and carry on. Here the same condition is
    fail-CLOSED, so their warning would contradict the refusal and break the one-line
    stderr contract. Only the warning hook is swapped; the pin rules stay in ``_reexec``.
    """
    original = _reexec._warn
    _reexec._warn = lambda message: None
    try:
        yield
    finally:
        _reexec._warn = original


def resolve_engine(project_id: str, declared_home) -> EngineChoice:
    """The engine root a start of ``project_id`` must use; raises rather than falls back."""
    declared = Path(declared_home).expanduser().resolve()
    try:
        checkout = resolve_project_root(project_id)
    except ProjectRootRefused as exc:
        raise PinnedEngineRefused(f"project {project_id!r}: {exc}") from exc

    versions_dir = _reexec._versions_dir(declared)
    with _quiet_pin_reader():
        pin_dir = _reexec._find_pin_dir(checkout)
        if pin_dir is None:
            return EngineChoice(declared, SOURCE_DECLARED, None)
        pin, _ = _reexec._read_pin(checkout)
        if pin is None:
            raise PinnedEngineRefused(
                f"project {project_id!r} has an unreadable or invalid pin in "
                f"{pin_dir / _reexec.PIN_FILE_NAME}; looked for it under {versions_dir}"
            )
        pinned_root = _reexec._resolve_pinned_dir(versions_dir, pin)
    if pinned_root is None:
        raise PinnedEngineRefused(
            f"project {project_id!r} pins {pin!r}, but nothing under {versions_dir} "
            "answers it (not installed, or it escapes the versions root)"
        )
    return EngineChoice(pinned_root, SOURCE_PIN, pin)


def _require_script(choice: EngineChoice, project_id: str, relative_script: str) -> None:
    """Refuse when the chosen tree lacks ``relative_script`` (a pin older than the script)."""
    missing = choice.engine_root / relative_script
    if missing.is_file():
        return
    named = "no pin, declared engine" if choice.pin is None else f"pin {choice.pin!r}"
    raise PinnedEngineRefused(
        f"project {project_id!r} ({named}) resolves to a tree without {missing}; "
        "refusing to fall back to another tree"
    )


def main(argv: Optional[list] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) not in (2, 3):
        print("usage: pinned_engine.py <project_id> <declared_home> [relative_script]",
              file=sys.stderr)
        return EX_USAGE
    project_id, declared_home = args[0], args[1]
    try:
        choice = resolve_engine(project_id, declared_home)
        if len(args) == 3:
            _require_script(choice, project_id, args[2])
    except PinnedEngineRefused as exc:
        print(f"pinned_engine: {exc}", file=sys.stderr)
        return EX_CONFIG
    print(choice.engine_root)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
