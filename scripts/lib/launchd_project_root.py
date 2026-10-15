#!/usr/bin/env python3
"""launchd_project_root.py: the project checkout a per-project launchd job runs against (OI-1629 B).

``reload_plist.sh`` calls this for a template that declares ``${VNX_PROJECT_ROOT}``. The
checkout comes from the operator registry (``~/.vnx/projects.json``) through
``forge_project_target.project_checkout_path``, the lookup the install guard uses, so there is
no second registry reader. An explicit root is accepted only when its ``.vnx-project-id``
equals the project id. The engine root is never a fallback.

CLI: ``python3 launchd_project_root.py <project_id> [explicit_root]`` prints the checkout on
stdout (exit 0); on a refusal it prints the reason on stderr and exits 1.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

_LIB_DIR = Path(__file__).resolve().parent
if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))


class ProjectRootRefused(Exception):
    pass


def resolve_project_root(project_id: str, explicit_root: Optional[str] = None) -> Path:
    """The checkout for ``project_id``; raises ``ProjectRootRefused`` with the reason."""
    from forge_project_target import checkout_project_id, project_checkout_path

    if not project_id:
        raise ProjectRootRefused("no project id: the checkout is looked up per project id")

    if explicit_root:
        root = Path(explicit_root).expanduser()
        found = checkout_project_id(root) if root.is_dir() else ""
        if found != project_id:
            raise ProjectRootRefused(
                f"VNX_PROJECT_ROOT={explicit_root} is the checkout of project "
                f"{found or '(no .vnx-project-id)'!r}, not of {project_id!r}"
            )
        return root.resolve()

    registered = project_checkout_path(project_id)
    if registered is None:
        registry = Path.home() / ".vnx" / "projects.json"
        raise ProjectRootRefused(
            f"project {project_id!r} is not in the registry {registry} (or its path is gone); "
            "register it first. Nothing written"
        )
    return registered.resolve()


def main(argv: Optional[list] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) not in (1, 2):
        print("usage: launchd_project_root.py <project_id> [explicit_root]", file=sys.stderr)
        return 2
    try:
        root = resolve_project_root(args[0], args[1] if len(args) == 2 else None)
    except ProjectRootRefused as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(root)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
