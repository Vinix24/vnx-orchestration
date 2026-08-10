#!/usr/bin/env python3
"""Shared path resolver for VNX Python scripts.

Allows environment overrides while defaulting to dist/runtime-relative paths.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import warnings
from pathlib import Path
from typing import Dict, Optional

# Self-bootstrap: ensure scripts/lib is on sys.path so sibling imports work
# regardless of whether the caller set up the repo root or lib dir.
import sys as _sys
_lib = str(Path(__file__).resolve().parent)
if _lib not in _sys.path:
    _sys.path.insert(0, _lib)

# Single source of truth — do not redefine; import from vnx_ids.
from vnx_ids import PROJECT_ID_RE as _PROJECT_ID_RE

import data_dir_guard

log = logging.getLogger(__name__)


def _resolve_overrides_dir(project_root: Path) -> Optional[Path]:
    """Return project_root/.vnx-overrides if it exists as a directory, else None."""
    candidate = project_root / ".vnx-overrides"
    if candidate.is_dir():
        return candidate
    return None


def _resolve_packaged_vnx_home() -> Optional[Path]:
    """Resolve VNX_HOME for a pip-installed (site-packages) layout.

    In an installed wheel the engine ships under the ``vnx_orchestration``
    namespace package (PR-PIP-REPACKAGE), so this module sits at
    ``<site-packages>/vnx_orchestration/scripts/lib/vnx_paths.py`` with
    ``schemas/``, ``skills/``, etc. as siblings of ``scripts/`` inside
    ``vnx_orchestration/``. Because the walk is relative to this file's own
    location, the same three-parent walk also resolves a legacy top-level wheel
    (``<site-packages>/scripts/lib/vnx_paths.py``); the ``schemas/`` + ``scripts/``
    presence check confirms whichever layout produced the install.

    Returns None for a dev checkout or editable install so the existing
    ``__file__``-walk / git-based resolution stays in control. Detection keys on
    the module living under a ``site-packages``/``dist-packages`` root, which a
    source checkout never does.
    """
    here = Path(__file__).resolve()
    if not any(part in ("site-packages", "dist-packages") for part in here.parts):
        return None
    # scripts/lib/vnx_paths.py -> scripts/lib -> scripts -> engine root
    # (= <site-packages>/vnx_orchestration in a namespaced wheel).
    engine_root = here.parent.parent.parent
    if (engine_root / "schemas").is_dir() and (engine_root / "scripts").is_dir():
        return engine_root
    return None


def _resolve_vnx_home() -> Path:
    vnx_home = os.environ.get("VNX_HOME")
    if vnx_home:
        return Path(vnx_home).expanduser().resolve()

    vnx_bin = os.environ.get("VNX_BIN") or os.environ.get("VNX_EXECUTABLE")
    if vnx_bin:
        return Path(vnx_bin).expanduser().resolve().parent.parent

    # Packaged install: resolve the engine root from the installed layout
    # before falling back to the dev-checkout walk below.
    packaged = _resolve_packaged_vnx_home()
    if packaged is not None:
        return packaged

    here = Path(__file__).resolve()
    # scripts/lib/vnx_paths.py -> scripts/lib -> scripts -> VNX_HOME
    if here.parent.name == "lib":
        return here.parent.parent.parent
    return here.parent.parent


def _is_embedded_layout(vnx_home: Path) -> bool:
    return vnx_home.name == "vnx-system" and vnx_home.parent.name == ".claude"


def _git_toplevel(path: Path) -> Path | None:
    try:
        output = subprocess.check_output(
            ["git", "-C", str(path), "rev-parse", "--show-toplevel"],
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None
    if not output:
        return None
    return Path(output).expanduser().resolve()


def _git_common_root(path: Path) -> Path | None:
    try:
        output = subprocess.check_output(
            ["git", "-C", str(path), "rev-parse", "--path-format=absolute", "--git-common-dir"],
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None
    if not output:
        return None
    common_dir = Path(output).expanduser().resolve()
    return common_dir.parent if common_dir.name == ".git" else common_dir


def _vnx_project_root_override(vnx_home: Path) -> Path | None:
    """Return the resolved VNX_PROJECT_ROOT override if it is a usable directory.

    VNX_PROJECT_ROOT is the explicit override exported by the central-install
    shim. It wins over any heuristic but is ignored when it points at VNX_HOME
    (mis-detection) or at a non-directory.
    """
    raw = os.environ.get("VNX_PROJECT_ROOT")
    if not raw:
        return None
    candidate = Path(raw).expanduser().resolve()
    if candidate.is_dir() and candidate != vnx_home:
        return candidate
    return None


def _is_central_install(vnx_home: Path) -> bool:
    """True when VNX_HOME is a standalone git repo serving as a central install.

    Central install = the VNX code tree is shared (e.g. ~/.vnx-system/versions/<v>)
    and the operator runs from their own project. Detected *only* via the
    ``.vnx-install-mode`` marker file (content ``central``) written by
    install-central.sh.

    The earlier CWD-git-root-mismatch heuristic was removed: a git worktree of
    vnx-orchestration itself produces a CWD git root that differs from VNX_HOME,
    which mis-fired the heuristic and collapsed PROJECT_ROOT onto the parent repo
    (issue #225 / PR-WAVE4-1 CI regression). The marker is unambiguous, so a
    standalone dev checkout or worktree is correctly treated as non-central.
    """
    if _git_toplevel(vnx_home) != vnx_home:
        return False
    marker = vnx_home / ".vnx-install-mode"
    if marker.is_file():
        try:
            return marker.read_text(encoding="utf-8").strip() == "central"
        except OSError:
            return False
    return False


def _default_project_root(vnx_home: Path) -> Path:
    if _is_embedded_layout(vnx_home):
        return vnx_home.parent.parent.resolve()

    # Explicit override exported by the central-install shim (belt-and-suspenders).
    override = _vnx_project_root_override(vnx_home)
    if override is not None:
        return override

    git_root = _git_toplevel(vnx_home)
    if git_root == vnx_home:
        if _is_central_install(vnx_home):
            cwd_git_root = _git_toplevel(Path.cwd())
            resolved = cwd_git_root if cwd_git_root else Path.cwd().resolve()
            # Safety: never collapse PROJECT_ROOT to filesystem root.
            if resolved == Path(resolved.anchor):
                return vnx_home.resolve()
            return resolved
        # Standalone dev checkout: runtime/bootstrap stay local to the repo checkout.
        return vnx_home.resolve()

    return vnx_home.parent.resolve()


def _default_canonical_root(vnx_home: Path) -> Path:
    if _is_embedded_layout(vnx_home):
        return vnx_home.resolve()

    # Explicit override: intelligence follows the project's git root.
    override = _vnx_project_root_override(vnx_home)
    if override is not None:
        return _git_toplevel(override) or override

    git_root = _git_toplevel(vnx_home)
    if git_root == vnx_home:
        if _is_central_install(vnx_home):
            project_root = _default_project_root(vnx_home)
            return _git_toplevel(project_root) or project_root
        return _git_common_root(vnx_home) or vnx_home.resolve()
    return vnx_home.resolve()


def _resolve_project_root(vnx_home: Path) -> Path:
    default_root = _default_project_root(vnx_home)

    # Explicit shim override takes precedence over inherited PROJECT_ROOT, so
    # direct Python callers honor it even when PROJECT_ROOT was not exported (EC-2).
    override = _vnx_project_root_override(vnx_home)
    if override is not None:
        return override

    project_root_env = os.environ.get("PROJECT_ROOT")
    if project_root_env:
        candidate = Path(project_root_env).expanduser().resolve()
        if candidate == default_root:
            return candidate

    return default_root


def _project_id_from_marker(project_root: Path) -> Optional[str]:
    """Read a validated project_id from the nearest ``.vnx-project-id`` marker.

    Walks up from project_root (and honors the ``VNX_PROJECT_ID`` env-var first)
    looking for ``.vnx-project-id``; returns the validated first line. Unlike the
    full identity chain this needs no operator_id, so a freshly ``vnx init``-ed
    project (which writes only ``.vnx-project-id``) still resolves a project_id
    for state-root purposes. Returns None when no valid id is found.
    """
    env_pid = os.environ.get("VNX_PROJECT_ID")
    if env_pid and _PROJECT_ID_RE.match(env_pid.strip()):
        return env_pid.strip()
    try:
        start = Path(project_root).expanduser().resolve()
    except OSError:
        return None
    for ancestor in [start, *start.parents]:
        marker = ancestor / ".vnx-project-id"
        if not marker.is_file():
            continue
        try:
            first_line = marker.read_text(encoding="utf-8").splitlines()[0].strip()
        except (OSError, IndexError):
            return None
        if _PROJECT_ID_RE.match(first_line):
            return first_line
        return None
    return None


def _resolve_state_project_id(project_root: Path) -> Optional[str]:
    """Best-effort project_id for state-root resolution (never raises).

    Resolution order:
      1. Canonical identity chain via ``vnx_identity.try_resolve_identity``
         (env > .vnx-project-id file > registry; requires operator+project).
      2. Lenient ``.vnx-project-id`` marker / ``VNX_PROJECT_ID`` env lookup,
         which needs no operator_id — so a fresh ``vnx init`` project resolves.
      3. ADR-007 git-remote fallback (``git remote get-url origin``), scoped to
         the given project_root only. This keeps the data-dir resolution
         consistent with the horizon/pool CLIs and the data_dir_guard fallback:
         a bare repo whose only identity signal is its origin remote still
         resolves a project_id, so the state root defaults CENTRAL
         (``~/.vnx-data/<pid>``) instead of falling back to the repo-local
         ``<project>/.vnx-data`` (OI-897b). Only reached when the identity
         chain and marker both yield nothing, so repos that carry a marker or
         a registry entry are unaffected.

    Returns None when no validated project_id is available, so
    _resolve_state_root applies its collision-safe project-local fallback
    instead of guessing a shared id.
    """
    try:
        from vnx_identity import try_resolve_identity
        identity = try_resolve_identity(cwd=project_root)
    except Exception:  # pragma: no cover - non-raising contract, belt-and-suspenders
        identity = None
    if identity is not None:
        pid = getattr(identity, "project_id", None)
        if pid and _PROJECT_ID_RE.match(pid):
            return pid
    pid = _project_id_from_marker(project_root)
    if pid:
        return pid
    return _project_id_from_git_remote(project_root)


def _project_id_from_git_remote(project_root: Path) -> Optional[str]:
    """Derive a validated project_id from ``git remote get-url origin``.

    Scoped strictly to ``project_root`` — unlike ``project_root.resolve_project_id``
    this never consults the CWD, so resolving a bare repo does not leak a
    marker/id from wherever the caller happens to be running.
    """
    try:
        out = subprocess.check_output(
            ["git", "-C", str(project_root), "remote", "get-url", "origin"],
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        return None
    if not out:
        return None
    name = out.rstrip("/").split("/")[-1]
    if name.endswith(".git"):
        name = name[:-4]
    if name and _PROJECT_ID_RE.match(name):
        return name
    return None


class TestIsolationGuardError(RuntimeError):
    """Raised when a write targets the real central store while under pytest.

    Distinct RuntimeError subclass so best-effort write wrappers — the
    central-mirror drain in append_receipt_internals.payload,
    dispatch_govern.ensure_receipt, dual_writer's never-raises API — can
    re-raise it instead of swallowing it as a routine I/O failure: a
    test-isolation violation must FAIL the test, not queue as retryable
    debt or degrade to a log warning (OI-1043).
    """


def refuse_real_central_store_write_under_pytest(resolved: Path) -> None:
    """Fail loud when code is ABOUT TO WRITE under the real central store
    while running under pytest (w19c / test-store-isolation class guard).

    Call this from write surfaces — a manager class's ``__init__``, a
    ``write_*`` function — right before any file/dir gets created, NOT from
    generic path resolvers. ``vnx_paths.resolve_paths()`` /
    ``_resolve_state_root()`` are pure computations used by plenty of
    legitimate read-only tests that inspect resolution logic without ever
    touching disk (e.g. ``tests/test_path_resolution_regression.py`` calling
    ``resolve_paths()`` with a deliberately clean env to assert on shape, not
    on where it points) — those must keep resolving to wherever production
    would, even ``~/.vnx-data/vnx-dev``, without failing. Only an imminent
    WRITE into that path is the actual hazard.

    ``_resolve_state_root``'s branch 3 ("existing central install — keep
    resolving to ``~/.vnx-data/<id>``") is correct for production, but a
    landmine when something is about to write there during THIS repo's own
    test suite: vnx-orchestration IS a real, governed central-store project
    (``~/.vnx-data/vnx-dev``), so a test that loses its isolation — a
    stripped ``VNX_DATA_DIR_EXPLICIT``, a leaked ``VNX_PROJECT_ID``, a
    subprocess with a cleaned env — silently resolves right back to that
    live store. That is exactly how ``tests/test_pr_dispatch_integration.py``
    wrote real dispatch-staging files into production governance state, and
    how ``vnx_mode.write_mode()``'s resolver fallback can flip the live
    ``mode.json`` from operator to starter, closing the governance door for
    ``vnx dispatch``.

    Deliberately checks the ACTUAL resolved value rather than requiring
    ``VNX_DATA_DIR_EXPLICIT=1`` (contrast with the precedent in
    ``build_t0_state._pytest_db_isolation_guard`` /
    ``migrate_future_system._pytest_db_isolation_guard``): callers of this
    function have their own legitimate no-explicit-flag paths (fresh-install
    / project-local fallbacks all resolve safely without it), so the
    flag itself is not the invariant. Landing a WRITE in the real
    ``~/.vnx-data`` is.

    Production is unaffected: pytest is never in ``sys.modules`` outside a
    test run.
    """
    if os.environ.get("PYTEST_CURRENT_TEST") is None and "pytest" not in _sys.modules:
        return
    real_home_vnx_data = (Path(os.path.expanduser("~")) / ".vnx-data").resolve()
    resolved = resolved.resolve()
    sep = os.sep
    if str(resolved) == str(real_home_vnx_data) or str(resolved).startswith(
        str(real_home_vnx_data) + sep
    ):
        raise TestIsolationGuardError(
            f"[TEST ISOLATION GUARD] about to write under the real central "
            f"store '{resolved}' while running under pytest. A test lost its "
            "isolation. Set VNX_DATA_DIR_EXPLICIT=1 with a tmp_path-based "
            "VNX_DATA_DIR before this code runs, or ensure the "
            "tests/conftest.py _vnx_data_dir_isolation autouse fixture is "
            "active for this test."
        )


def refuse_real_launch_agents_write_under_pytest(dest_dir: Path) -> None:
    """Fail loud when code is ABOUT TO WRITE under the real
    ``~/Library/LaunchAgents`` while running under pytest
    (OI-1117 / launchd-test-isolation class guard).

    Call this from write surfaces right before any plist or file gets
    created in ``~/Library/LaunchAgents``. The guard is placed at the write
    surface, not inside a generic path resolver: the LaunchAgents dir is a
    legitimate production write target outside of tests; only an imminent
    WRITE to it during pytest is the isolation hazard.

    Production is unaffected: pytest is never in ``sys.modules`` outside a
    test run.

    Pattern mirrors ``refuse_real_central_store_write_under_pytest`` — the
    two guards share the same shape so the class of test-isolation guard is
    recognisable as a single idiom across the codebase.
    """
    if os.environ.get("PYTEST_CURRENT_TEST") is None and "pytest" not in _sys.modules:
        return
    real_la = (
        Path(os.path.expanduser("~")) / "Library" / "LaunchAgents"
    ).resolve()
    resolved = dest_dir.resolve()
    sep = os.sep
    if str(resolved) == str(real_la) or str(resolved).startswith(
        str(real_la) + sep
    ):
        raise TestIsolationGuardError(
            f"[TEST ISOLATION GUARD] about to write under the real "
            f"LaunchAgents dir '{resolved}' while running under pytest. "
            "A test lost its isolation. Mock Path.home to a tmp_path-based "
            "fake home before calling code that installs launchd plists, "
            "or monkeypatch subprocess.run to intercept launchctl calls."
        )


def _resolve_state_root(project_id: Optional[str], project_root: Path) -> Path:
    """Resolve the VNX runtime data root (the ``.vnx-data`` equivalent).

    Ordered resolution — first applicable wins:
      1. ``VNX_DATA_DIR_EXPLICIT=1`` + ``VNX_DATA_DIR``  — explicit override
         (worktree isolation, CI, tests rely on this).
      2. ``VNX_DATA_HOME`` + project_id  — ``$VNX_DATA_HOME/<project_id>``.
      3. ``~/.vnx-data/<project_id>`` *if it already exists*  — keep resolving
         existing central installs to their current location.
      4. ``<project_root>/.vnx-data`` *if it already exists*  — keep resolving
         existing dev checkouts / pre-migration installs in place.
      5. Fresh install — ``~/.vnx-data/<project_id>``, the single canonical
         central data directory for all installs (parity with
         ``resolve_central_data_dir``).

    The existence-gated legacy branches (3, 4) are checked *before* the fresh
    default so that the existing dev checkouts and central installs keep
    resolving to where their state already lives (per PR-PIP-2: "breek de
    bestaande dev-checkout/central resolutie NIET"). A fresh install has
    neither legacy dir and lands on the canonical central dir — same path
    as ``resolve_central_data_dir``, so the data-dir guard never warns on a
    freshly-initialised project (OI-1055).

    Collision-safety: a per-project directory is only ever formed from a
    *resolved* project_id. When project_id is None we never substitute a shared
    default id (which would collide every project into one dir); we fall back to
    the legacy project-local ``<project_root>/.vnx-data`` instead. No guessing.
    """
    # 1. Explicit override — highest precedence.
    explicit_flag = os.environ.get("VNX_DATA_DIR_EXPLICIT") == "1"
    explicit_val = os.environ.get("VNX_DATA_DIR")
    if explicit_flag and explicit_val:
        return Path(explicit_val).expanduser().resolve()

    pid = project_id if (project_id and _PROJECT_ID_RE.match(project_id)) else None
    local = project_root / ".vnx-data"

    # 2. VNX_DATA_HOME — operator-chosen data home, per-project subdir.
    data_home = os.environ.get("VNX_DATA_HOME")
    if data_home and pid:
        return (Path(data_home).expanduser() / pid).resolve()

    # 3. Existing central install — keep resolving to ~/.vnx-data/<id>.
    if pid:
        central = Path.home() / ".vnx-data" / pid
        if central.is_dir():
            return central.resolve()

    # 4. Existing dev checkout / pre-migration install — keep project-local dir.
    if local.is_dir():
        return local.resolve()

    # 5. Fresh install: central per-project data directory.
    if pid:
        return (Path.home() / ".vnx-data" / pid).resolve()

    # Collision-safety: no resolvable project_id and no existing layout — never
    # guess a shared id. Stay project-local rather than collide projects.
    return local.resolve()


def resolve_data_root(project_root) -> Path:
    """Public: resolve the VNX runtime data root for an explicit project_root.

    Honors the same ordered resolution as :func:`resolve_paths` (explicit
    override > ``VNX_DATA_HOME`` > existing ``~/.vnx-data/<id>`` > existing
    project-local ``.vnx-data`` > fresh default), but anchored on the *given*
    project_root rather than the env/VNX_HOME-resolved one. The project_id is
    resolved leniently from that root (env, ``.vnx-project-id`` marker, or
    identity chain). Used by the pip console-script commands (``vnx_cli``)
    which operate on a ``--project-dir`` argument instead of the ambient repo.

    Collision-safe: an unresolvable project_id never collapses to a shared
    default; resolution falls back to the project-local ``.vnx-data`` instead.
    """
    project_root = Path(project_root).expanduser().resolve()
    pid = _resolve_state_project_id(project_root)
    data_dir = _resolve_state_root(pid, project_root)
    data_dir_guard.check_data_dir_project_id_guard(data_dir, pid)
    return data_dir


def resolve_paths() -> Dict[str, str]:
    vnx_home = _resolve_vnx_home()
    project_root = _resolve_project_root(vnx_home)
    canonical_root = Path(
        os.environ.get("VNX_CANONICAL_ROOT") or _default_canonical_root(vnx_home)
    ).expanduser().resolve()

    _explicit_flag = os.environ.get("VNX_DATA_DIR_EXPLICIT") == "1"
    _explicit_val = os.environ.get("VNX_DATA_DIR")
    if _explicit_val and not _explicit_flag:
        warnings.warn(
            f"VNX_DATA_DIR env-var set ({_explicit_val}) but "
            "VNX_DATA_DIR_EXPLICIT=1 is required for it to be honored. "
            "Ignoring and using the resolved state root. "
            "See https://github.com/Vinix24/vnx-orchestration/issues/225",
            DeprecationWarning,
            stacklevel=2,
        )
    _state_project_id = _resolve_state_project_id(project_root)
    vnx_data_dir = _resolve_state_root(_state_project_id, project_root)
    data_dir_guard.check_data_dir_project_id_guard(vnx_data_dir, _state_project_id)

    paths = {
        "VNX_HOME": str(vnx_home),
        "PROJECT_ROOT": str(project_root),
        "VNX_CANONICAL_ROOT": str(canonical_root),
        "VNX_DATA_DIR": str(vnx_data_dir),
        "VNX_STATE_DIR": str(Path(os.environ.get("VNX_STATE_DIR") or (vnx_data_dir / "state")).expanduser()),
        "VNX_DISPATCH_DIR": str(Path(os.environ.get("VNX_DISPATCH_DIR") or (vnx_data_dir / "dispatches")).expanduser()),
        "VNX_LOGS_DIR": str(Path(os.environ.get("VNX_LOGS_DIR") or (vnx_data_dir / "logs")).expanduser()),
        "VNX_PIDS_DIR": str(Path(os.environ.get("VNX_PIDS_DIR") or (vnx_data_dir / "pids")).expanduser()),
        "VNX_LOCKS_DIR": str(Path(os.environ.get("VNX_LOCKS_DIR") or (vnx_data_dir / "locks")).expanduser()),
        "VNX_SOCKETS_DIR": str(Path(os.environ.get("VNX_SOCKETS_DIR") or (vnx_data_dir / "sockets")).expanduser()),
        "VNX_REPORTS_DIR": str(Path(os.environ.get("VNX_REPORTS_DIR") or (vnx_data_dir / "unified_reports")).expanduser()),
        "VNX_DB_DIR": str(Path(os.environ.get("VNX_DB_DIR") or (vnx_data_dir / "database")).expanduser()),
    }

    reports_dir = Path(paths["VNX_REPORTS_DIR"])
    paths["VNX_HEADLESS_REPORTS_DIR"] = str(
        Path(os.environ.get("VNX_HEADLESS_REPORTS_DIR") or (reports_dir / "headless")).expanduser()
    )

    # Git-tracked intelligence directory (portable across worktrees)
    paths["VNX_INTELLIGENCE_DIR"] = str(
        Path(os.environ.get("VNX_INTELLIGENCE_DIR") or (canonical_root / ".vnx-intelligence")).expanduser().resolve()
    )

    if "VNX_SKILLS_DIR" in os.environ:
        paths["VNX_SKILLS_DIR"] = os.environ["VNX_SKILLS_DIR"]
    else:
        # Resolver order: .vnx-overrides/skills > .claude/skills > VNX_HOME/skills
        overrides_dir = _resolve_overrides_dir(project_root)
        overrides_skills = overrides_dir / "skills" if overrides_dir is not None else None
        if overrides_skills is not None and overrides_skills.is_dir():
            paths["VNX_SKILLS_DIR"] = str(overrides_skills)
        else:
            claude_skills = project_root / ".claude" / "skills"
            if claude_skills.is_dir():
                paths["VNX_SKILLS_DIR"] = str(claude_skills)
            else:
                paths["VNX_SKILLS_DIR"] = str(vnx_home / "skills")

    return paths


def ensure_env() -> Dict[str, str]:
    """Populate os.environ with any missing VNX path defaults."""
    paths = resolve_paths()
    for key, value in paths.items():
        os.environ.setdefault(key, value)
    return paths


def project_id_from_state_dir(state_dir: Path) -> str:
    """Best-effort derive a project_id from a state dir path.

    Supports both:
    - central paths: ``~/.vnx-data/<project_id>/state``
    - repo-local paths with a nearby ``.vnx-project-id`` file, such as
      ``<repo>/.vnx-data/state``

    Returns an empty string when no valid project_id can be derived.
    """
    try:
        resolved = Path(state_dir).expanduser().resolve()
    except Exception:
        return ""

    try:
        vnx_data = (Path.home() / ".vnx-data").resolve()
        if resolved.name == "state" and resolved.parent.parent == vnx_data:
            candidate = resolved.parent.name.strip()
            if _PROJECT_ID_RE.match(candidate):
                return candidate
    except OSError as e:
        log.debug("Failed to resolve vnx-data path: %s", e)

    for ancestor in [resolved, *resolved.parents]:
        project_file = ancestor / ".vnx-project-id"
        if not project_file.is_file():
            continue
        try:
            first_line = project_file.read_text(encoding="utf-8").splitlines()[0].strip()
        except (OSError, IndexError):
            return ""
        if _PROJECT_ID_RE.match(first_line):
            return first_line
        return ""

    return ""


def resolve_project_id() -> Optional[str]:
    """Return the resolved project_id for the current VNX context (best-effort, never raises).

    Resolution order: identity chain > VNX_PROJECT_ID env > .vnx-project-id marker > None.
    Used by nightly pipeline and CLI tools that need project_id without a full dispatch.
    ADR-007: project_id is required on all cross-project operations.
    """
    vnx_home = _resolve_vnx_home()
    project_root = _resolve_project_root(vnx_home)
    return _resolve_state_project_id(project_root)


def resolve_state_dir(project_root: "Path | None" = None) -> Path:
    """Return the VNX state directory.

    When project_root is supplied, derives the state dir from that root
    (project_root / '.vnx-data' / 'state') without reading any env var.

    When project_root is None, returns VNX_STATE_DIR from resolve_paths().
    """
    if project_root is not None:
        return (Path(project_root) / ".vnx-data" / "state").resolve()
    paths = resolve_paths()
    return Path(paths["VNX_STATE_DIR"])


def resolve_worker_state_dir(terminal_id: str, vnx_data_dir: "Path | None" = None) -> Path:
    """Return ``.vnx-data/workers/<terminal_id>/`` — per-worker isolated state directory.

    Creates the directory on demand (exist_ok=True). When vnx_data_dir is None,
    derives it from resolve_paths()["VNX_DATA_DIR"].

    Raises:
        ValueError: if terminal_id is empty or contains path-traversal characters.
    """
    if not terminal_id or not terminal_id.strip():
        raise ValueError("terminal_id must be non-empty")
    clean = terminal_id.strip()
    if "/" in clean or "\\" in clean or ".." in clean:
        raise ValueError(
            f"terminal_id must not contain path separators or '..': {terminal_id!r}"
        )
    if vnx_data_dir is None:
        vnx_data_dir = Path(resolve_paths()["VNX_DATA_DIR"])
    worker_dir = vnx_data_dir / "workers" / clean
    os.makedirs(worker_dir, exist_ok=True)
    return worker_dir.resolve()


def resolve_central_data_dir(project_id: str) -> Path:
    """Return ``~/.vnx-data/<project_id>/`` — the central per-project data directory.

    Used by Phase 6 P3 dual-write paths and the envelope re-stamper.

    Raises:
        ValueError: if project_id is empty or does not match ^[a-z][a-z0-9-]{1,31}$.
            Rejects dots, slashes, leading dashes, uppercase, and all special chars
            to prevent path-traversal escaping the ~/.vnx-data sandbox.
    """
    if not project_id:
        raise ValueError("project_id must be non-empty")
    if not _PROJECT_ID_RE.match(project_id):
        raise ValueError(
            f"project_id must match ^[a-z][a-z0-9-]{{1,31}}$ "
            f"(no dots, slashes, leading dashes, or special chars): {project_id!r}"
        )
    return Path.home() / ".vnx-data" / project_id


_SKILL_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def _validate_skill_name(skill_name: str) -> str:
    """Validate skill_name is a safe bare name with no path traversal.

    Raises:
        ValueError: on empty string, path separators, dots, or any char
                    outside [A-Za-z0-9_-].
    """
    if not skill_name:
        raise ValueError(f"invalid skill name: {skill_name!r}")
    if not _SKILL_NAME_RE.match(skill_name):
        raise ValueError(f"invalid skill name: {skill_name!r}")
    return skill_name


def _confine_skill_path(resolved: Path, skill_root: Path) -> None:
    """Raise ValueError if resolved path escapes skill_root."""
    root_str = str(skill_root.resolve()) + os.sep
    if not str(resolved).startswith(root_str):
        raise ValueError(f"resolved path escapes skill root: {resolved}")


def get_skill_path(skill_name: str, project_root: Optional[Path] = None) -> Path:
    """Return the resolved Path for a named skill directory.

    Resolution order:
    1. project_root/.vnx-overrides/skills/<skill_name>/  (if project_root supplied)
    2. VNX_HOME/skills/<skill_name>/

    Raises:
        ValueError: if skill_name fails validation or resolved path escapes skill root.
        FileNotFoundError: if the skill directory is not found in any location.
    """
    skill_name = _validate_skill_name(skill_name)

    if project_root is not None:
        overrides_dir = _resolve_overrides_dir(Path(project_root))
        if overrides_dir is not None:
            skill_root = overrides_dir / "skills"
            override_skill = skill_root / skill_name
            resolved = override_skill.resolve()
            _confine_skill_path(resolved, skill_root)
            if override_skill.is_dir():
                return resolved

    vnx_home = _resolve_vnx_home()
    skill_root = vnx_home / "skills"
    central_skill = skill_root / skill_name
    resolved = central_skill.resolve()
    _confine_skill_path(resolved, skill_root)
    if central_skill.is_dir():
        return resolved

    raise FileNotFoundError(
        f"Skill {skill_name!r} not found in overrides or central VNX_HOME ({vnx_home})"
    )


if __name__ == "__main__":
    # Print resolved paths for quick diagnostics
    resolved = ensure_env()
    for key in sorted(resolved.keys()):
        print(f"{key}={resolved[key]}")
