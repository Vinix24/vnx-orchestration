#!/usr/bin/env python3
"""launchd_install_guard.py: one refusal rule for every launchd install (OI-1942).

A launchd job bakes ``VNX_HOME`` (the engine root) into its ProgramArguments. A job
installed from a root that later disappears (a reaped worktree, a bench clone under
``/private/tmp``) keeps firing at a path that is gone, and because launchd keys on
the Label it also replaces the operator's job of the same name.

``vnx init`` (``init_cmd._install_launchd_agent``) and ``reload_plist.sh`` both call
:func:`refusal_reason` on the engine root that becomes ``VNX_HOME``. A root passes
only when it is one of:

  1. a central install: ``<root>/.vnx-install-mode`` holds ``central``;
  2. the packaged engine dir (``<site-packages>/vnx_orchestration`` next to ``vnx_cli``);
  3. a primary checkout registered in ``~/.vnx/projects.json``: the registry path of
     the id in ``<root>/.vnx-project-id`` resolves to exactly this root.

A root under ``.vnx-data/worktrees/`` and a linked git worktree (``<root>/.git`` is a
file) are refused before any of the three is considered. ``.vnx-project-id`` is
tracked in git, so a clone carries its origin's id; only path equality refuses it.

The registry is read through ``forge_project_target.project_checkout_path`` (no second
reader), at call time, so ``Path.home`` / ``HOME`` overrides are honoured.

CLI: ``python3 launchd_install_guard.py <engine_root>`` exits 0 when allowed, 1 with
the reason on stderr when refused.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

_LIB_DIR = Path(__file__).resolve().parent
if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))

INSTALL_MODE_MARKER = ".vnx-install-mode"


def _under_worktrees(root: Path) -> bool:
    parts = root.parts
    return any(
        part == ".vnx-data" and i + 1 < len(parts) and parts[i + 1] == "worktrees"
        for i, part in enumerate(parts)
    )


def _is_central_install(root: Path) -> bool:
    try:
        return (root / INSTALL_MODE_MARKER).read_text(encoding="utf-8").strip() == "central"
    except OSError:
        return False


def _is_packaged_engine(root: Path) -> bool:
    return (
        root.name == "vnx_orchestration"
        and (root / "scripts").is_dir()
        and (root.parent / "vnx_cli").is_dir()
    )


def _is_registered_primary_checkout(root: Path) -> bool:
    from forge_project_target import checkout_project_id, project_checkout_path

    project_id = checkout_project_id(root)
    if not project_id:
        return False
    registered = project_checkout_path(project_id)
    if registered is None:
        return False
    try:
        return registered.resolve() == root
    except OSError:
        return False


def refusal_reason(engine_root) -> Optional[str]:
    """Why a launchd job must not be installed from ``engine_root``; None when allowed."""
    try:
        root = Path(engine_root).expanduser().resolve()
    except (OSError, RuntimeError) as exc:
        return f"engine root {engine_root!s} cannot be resolved ({exc})"

    if _under_worktrees(root):
        return (
            f"engine root is under .vnx-data/worktrees/ ({root}); a worktree is reaped "
            "and the job would point at a path that is gone"
        )
    if (root / ".git").is_file():
        return f"engine root {root} is a linked git worktree; install from the primary checkout"
    if _is_central_install(root) or _is_packaged_engine(root):
        return None
    if _is_registered_primary_checkout(root):
        return None
    return (
        f"engine root {root} is not a central install, not the packaged engine and not a "
        "primary checkout registered in ~/.vnx/projects.json under its own .vnx-project-id; "
        "an unregistered clone would replace the operator's job and vanish with the clone"
    )


def main(argv: Optional[list] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1:
        print("usage: launchd_install_guard.py <engine_root>", file=sys.stderr)
        return 2
    reason = refusal_reason(args[0])
    if reason is None:
        return 0
    print(f"REFUSING launchd install: {reason}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
