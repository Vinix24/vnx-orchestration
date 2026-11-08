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
import json
import logging
import os
import shutil
import subprocess
from datetime import datetime, timezone
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

NATIVE_DIR = ".claude/skills"
RECORD_DIR = "agent_skill_materializations"
RECORD_SCHEMA = 1
STATUSES = frozenset({"ok", "partial", "error", "skipped"})
REASONS = frozenset({
    "no_role", "no_declared_set", "no_cwd", "no_state_dir", "no_dispatch_id",
    "no_project_id", "not_isolated", "not_ignored", "already_native",
    "source_missing", "copy_failed:<Class>", "not_owned:<slug>", "path_outside_worktree",
})


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
        role_dir = _safe_error_dir(wt, role)
        if role_dir is None:
            return
        atomic_write_json(role_dir / MANIFEST_NAME, {"schema": MANIFEST_SCHEMA, **result})
    except Exception as exc:  # fail-open: the reason is already in the WARNING
        logger.warning("could not write error manifest: %s", type(exc).__name__)


def _checked_base(wt: Path) -> Path:
    """The shadow dir: not a symlink and, once it exists, resolving inside the worktree."""
    base = wt / SHADOW_DIR
    if base.is_symlink():
        raise ShadowError("shadow_dir_symlink")
    if base.exists() and not _inside(Path(os.path.realpath(base)), Path(os.path.realpath(wt))):
        raise ShadowError("shadow_dir_outside_worktree")
    return base


def _safe_error_dir(wt: Path, role: str) -> Path | None:
    """The role dir for an error manifest, or None when any component is unsafe."""
    try:
        base = _checked_base(wt)
    except ShadowError:
        return None
    role_dir = base / role
    if role_dir.is_symlink() or (role_dir.exists() and not role_dir.is_dir()):
        return None
    base.mkdir(exist_ok=True)
    role_dir.mkdir(exist_ok=True)
    return role_dir


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
    base = _checked_base(wt)
    base.mkdir(exist_ok=True)
    real_base = Path(os.path.realpath(base))
    role_dir = base / role
    if role_dir.is_symlink():
        raise ShadowError("role_dir_symlink")
    if role_dir.exists():
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


def materialize_skill_set(
    role: str | None,
    *,
    worktree: Path | str | None,
    provider: str,
    mode: str,
    dispatch_id: str | None,
    state_dir: Path | str | None,
    project_id: str | None = None,
) -> dict[str, Any]:
    """Make *role*'s declared skill set visible to *provider* (ADR-059 decision 13).

    ``shadow`` delegates to :func:`materialize_role_skills`. ``on`` with ``claude``
    copies to ``<worktree>/.claude/skills/<slug>/``, writes one record per attempt
    and returns ``status`` in :data:`STATUSES`. Anything else is ``skipped`` without
    a write (``off``, or a provider other than claude). Never reads the environment
    and never raises: the dispatch runs on.
    """
    if mode == "shadow":
        return _shadow_delegate(worktree, role)
    if mode != "on":
        return {"status": "skipped", "reason": "layout_off"}
    if provider != "claude":
        return {"status": "skipped", "reason": "provider_unsupported"}
    ids = {"role": role, "provider": provider, "dispatch_id": dispatch_id, "project_id": project_id,
           "worktree": str(worktree) if worktree else None}
    try:
        result = _native_claude(role, worktree, dispatch_id, state_dir, project_id)
    except Exception as exc:  # fail-open: materialisation never stops a dispatch
        result = _outcome("error", f"copy_failed:{type(exc).__name__}")
    if result["status"] != "ok":
        logger.warning("agent skill materialisation for role %r: %s", role, result.get("reason"))
    result["record"] = _write_record(state_dir, ids, result)
    return result


def _shadow_delegate(worktree: Path | str | None, role: str | None) -> dict[str, Any]:
    if not role or worktree is None:
        return {"status": "skipped", "reason": "no_role" if not role else "no_cwd"}
    from agent_resolver import resolve_agent

    config = resolve_agent(role, worktree, engine_root=Path(__file__).resolve().parents[2])
    if config is None:
        logger.warning("agent skill shadow for role %r: no config", role)
        return {"status": "skipped", "reason": "no_role"}
    if not config.skills:
        return {"status": "skipped", "reason": "no_declared_set"}
    return materialize_role_skills(worktree, role, config.skills)


def _outcome(status: str, reason: str | None = None, skills: list | None = None) -> dict[str, Any]:
    return {"status": status, "reason": reason, "skills": skills or []}


def _native_claude(role, worktree, dispatch_id, state_dir, project_id) -> dict[str, Any]:
    for value, reason in ((role, "no_role"), (worktree, "no_cwd"), (state_dir, "no_state_dir"),
                          (dispatch_id, "no_dispatch_id"), (project_id, "no_project_id")):
        if not value:
            return _outcome("skipped", reason)
    wt = Path(worktree)
    if not _worktree_is_isolated(wt):
        return _outcome("skipped", "path_outside_worktree")
    from agent_resolver import resolve_agent

    config = resolve_agent(role, wt, engine_root=Path(__file__).resolve().parents[2])
    if config is None:
        return _outcome("skipped", "no_role")
    if not config.skills:
        return _outcome("skipped", "no_declared_set")
    return _copy_native(wt, config.skills)


def _worktree_is_isolated(wt: Path) -> bool:
    from dispatch_worktree_isolation import resolve_consumer_project_root

    real = wt.resolve()
    if real == resolve_consumer_project_root():
        return False
    return not any(_inside(real, (Path.home() / d).resolve()) for d in (".claude", ".codex"))


def _copy_native(wt: Path, slugs: list[str]) -> dict[str, Any]:
    base = wt / NATIVE_DIR
    if base.exists() and not _inside(Path(os.path.realpath(base)), Path(os.path.realpath(wt))):
        return _outcome("skipped", "path_outside_worktree")
    pending = [s for s in slugs if skill_search_paths(wt, s) and not os.path.lexists(base / s / "SKILL.md")]
    if any(not _dest_ignored(wt, s) for s in pending):
        return _outcome("skipped", "not_ignored")
    entries: list[dict[str, Any]] = []
    failure: str | None = None
    for slug in slugs:
        try:
            entries.append(_native_entry(wt, base, slug, slug in pending))
        except ShadowError as exc:
            failure = failure or ("source_missing" if exc.reason.startswith("slug_") else exc.reason)
        except Exception as exc:  # fail-open per skill: the next slug still gets its copy
            failure = failure or f"copy_failed:{type(exc).__name__}"
    copied = any(e["ownership"] == "materialized" for e in entries)
    if failure:
        return _outcome("partial" if entries else "error", failure, entries)
    return _outcome("ok", None if copied else "already_native", entries)


def _dest_ignored(wt: Path, slug: str) -> bool:
    proc = subprocess.run(["git", "-C", str(wt), "check-ignore", "-q", f"{NATIVE_DIR}/{slug}/SKILL.md"],
                          capture_output=True, check=False)
    return proc.returncode == 0


def _native_entry(wt: Path, base: Path, slug: str, to_copy: bool) -> dict[str, Any]:
    if to_copy:
        src = _vet_slug(wt, slug)
        base.mkdir(parents=True, exist_ok=True)
        _copy_skill(base, wt, slug, src)
    elif not skill_search_paths(wt, slug):
        raise ShadowError(f"slug_refused:{slug}")
    data = (base / slug / "SKILL.md").read_bytes()
    return {"slug": slug, "dest": f"{NATIVE_DIR}/{slug}", "sha256": hashlib.sha256(data).hexdigest(),
            "bytes": len(data), "ownership": "materialized" if to_copy else "native"}


def _write_record(state_dir, ids: dict[str, Any], result: dict[str, Any]) -> str | None:
    """One file per attempt, ``n`` chosen with ``O_CREAT|O_EXCL``; best effort."""
    dispatch_id = ids["dispatch_id"]
    if not state_dir or not dispatch_id or dispatch_id != Path(str(dispatch_id)).name:
        return None
    skills = result["skills"]
    body = {"schema": RECORD_SCHEMA, "status": result["status"], "reason": result["reason"], **ids,
            "session_id": None, "captured_at": datetime.now(timezone.utc).isoformat(), "skills": skills,
            "manifest_sha256": hashlib.sha256(json.dumps(skills, sort_keys=True).encode()).hexdigest()}
    try:
        folder = Path(state_dir) / RECORD_DIR / dispatch_id
        folder.mkdir(parents=True, exist_ok=True)
        attempt = 1
        while True:
            path = folder / f"{ids['provider']}.attempt-{attempt}.json"
            try:
                fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
                break
            except FileExistsError:
                attempt += 1
        with os.fdopen(fd, "w", encoding="utf-8") as handle:  # vnx-atomic-write: O_EXCL create is the exclusivity
            json.dump({**body, "attempt": attempt}, handle, indent=2)
        return str(path)
    except Exception as exc:  # fail-open: the reason is already in the WARNING
        logger.warning("could not write materialisation record: %s", type(exc).__name__)
        return None
