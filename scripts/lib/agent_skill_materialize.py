"""Shadow copy of an agent role's skills (ADR-050 contract 5, agents-als-map PR-2a).

Under ``VNX_AGENT_LAYOUT=shadow`` the skills a role declares in
``agents/<role>/config.yaml`` are copied to
``<worktree>/.vnx-agent-skills/<role>/<slug>/`` with a ``manifest.json`` (path,
length, sha256 per file). No harness discovers that directory, git ignores it,
and it goes away with the worktree.

The shadow fails open: any error gives one WARNING and ``status: error`` in the
result (and in ``manifest.json`` once the ignore check has passed). Nothing here
raises to the caller. Under ``off`` nothing happens and no directory is made.
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Iterable

import agent_layout
from agent_resolver import _is_safe_agent_name
from atomic_io import atomic_write_json
from skill_prefix import SKILL_TREES, resolve_skill_dir, skill_search_paths

logger = logging.getLogger(__name__)

SHADOW_DIR = ".vnx-agent-skills"
MANIFEST_NAME = "manifest.json"
MANIFEST_SCHEMA = 1


class ShadowError(Exception):
    """A refusal with a stable reason string; caught inside the module."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def materialize_role_skills(
    worktree: Path | str,
    role: str,
    slugs: Iterable[str],
    source_root: Path | str | None = None,
) -> dict[str, Any]:
    """Copy *slugs* for *role* into the worktree's shadow directory.

    Returns ``{"status": "skipped"|"ok"|"error", ...}``. Never raises.
    ``source_root`` is the tree holding ``skills/`` (default: the worktree).
    """
    if not agent_layout.is_shadow():
        return {"status": "skipped", "reason": "layout_off"}
    wt = Path(worktree)
    try:
        return _materialize(wt, role, list(slugs), Path(source_root) if source_root else wt)
    except ShadowError as exc:
        return _fail(wt, role, exc.reason)
    except Exception as exc:  # fail-open: the shadow never stops a dispatch
        return _fail(wt, role, f"unexpected:{type(exc).__name__}")


def _fail(wt: Path, role: str, reason: str) -> dict[str, Any]:
    logger.warning("agent skill shadow for role %r: %s", role, reason)
    result: dict[str, Any] = {"status": "error", "reason": reason, "role": role}
    if reason != "not_ignored":
        _write_error_manifest(wt, role, result)
    return result


def _write_error_manifest(wt: Path, role: str, result: dict[str, Any]) -> None:
    """Best effort: record the reason, but only for a safe role under an ignored dir."""
    try:
        if not _is_safe_agent_name(role) or not _is_ignored(wt):
            return
        role_dir = wt / SHADOW_DIR / role
        role_dir.mkdir(parents=True, exist_ok=True)
        atomic_write_json(role_dir / MANIFEST_NAME, {"schema": MANIFEST_SCHEMA, **result})
    except Exception as exc:  # fail-open: the reason is already in the WARNING
        logger.warning("could not write error manifest: %s", type(exc).__name__)


def _is_ignored(wt: Path) -> bool:
    """True only when git positively reports the shadow dir as ignored (rc 0)."""
    proc = subprocess.run(
        ["git", "-C", str(wt), "check-ignore", "-q", f"{SHADOW_DIR}/x"],
        capture_output=True,
        check=False,
    )
    return proc.returncode == 0


def _materialize(wt: Path, role: str, slugs: list[str], source_root: Path) -> dict[str, Any]:
    if not _is_ignored(wt):
        raise ShadowError("not_ignored")
    if not _is_safe_agent_name(role):
        raise ShadowError(f"role_refused:{role}")
    sources = [(slug, _vet_slug(source_root, slug)) for slug in slugs]
    role_dir = _reset_role_dir(wt, role)
    entries = [_copy_skill(role_dir, source_root, slug, src) for slug, src in sources]
    manifest = {"schema": MANIFEST_SCHEMA, "status": "ok", "role": role, "skills": entries}
    atomic_write_json(role_dir / MANIFEST_NAME, manifest)
    return {"status": "ok", "role": role, "dest": str(role_dir), "skills": entries}


def _vet_slug(source_root: Path, slug: str) -> Path:
    """Return the real skill dir for *slug*, or refuse it."""
    if not isinstance(slug, str) or not skill_search_paths(source_root, slug):
        raise ShadowError(f"slug_refused:{slug}")
    src = resolve_skill_dir(source_root, slug)
    if src is None:
        raise ShadowError(f"slug_unknown:{slug}")
    real_src = Path(os.path.realpath(src))
    if not any(_inside(real_src, Path(os.path.realpath(source_root / t))) for t in SKILL_TREES):
        raise ShadowError(f"slug_outside_skill_trees:{slug}")
    return real_src


def _inside(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _reset_role_dir(wt: Path, role: str) -> Path:
    """Remove a leftover ``<role>`` dir (only that one) and recreate it empty."""
    base = wt / SHADOW_DIR
    base.mkdir(exist_ok=True)
    real_base = Path(os.path.realpath(base))
    if not _inside(real_base, Path(os.path.realpath(wt))):
        raise ShadowError("shadow_dir_outside_worktree")
    role_dir = base / role
    if role_dir.is_symlink():
        role_dir.unlink()  # drops the link itself, never what it points at
    elif role_dir.exists():
        if not _inside(Path(os.path.realpath(role_dir)), real_base):
            raise ShadowError("leftover_outside_worktree")
        shutil.rmtree(role_dir)
    role_dir.mkdir()
    return role_dir


def _skill_files(src: Path) -> list[Path]:
    """Regular files under *src* (sorted); a symlink must stay inside *src*."""
    files: list[Path] = []
    for dirpath, _dirs, names in os.walk(src):
        for name in names:
            path = Path(dirpath) / name
            if not _inside(Path(os.path.realpath(path)), src):
                raise ShadowError(f"symlink_outside_skill:{path.relative_to(src).as_posix()}")
            if path.is_file():
                files.append(path)
    return sorted(files)


def _copy_skill(role_dir: Path, source_root: Path, slug: str, src: Path) -> dict[str, Any]:
    dest = role_dir / slug
    files = []
    for path in _skill_files(src):
        rel = path.relative_to(src)
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
        data = target.read_bytes()
        files.append(
            {"path": rel.as_posix(), "length": len(data), "sha256": hashlib.sha256(data).hexdigest()}
        )
    real_root = Path(os.path.realpath(source_root))
    return {"slug": slug, "source_dir": src.relative_to(real_root).as_posix(), "files": files}
