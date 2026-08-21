#!/usr/bin/env python3
"""vnx update — version-flip for central VNX install.

Atomic symlink flip via os.replace() ensures no partial-swap window.
"""

import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from vnx_cli._reexec import PIN_FILE_NAME, _PIN_RE, _normalize_version

# Late import: vnx_version_ro lives in the engine tree (scripts/lib/), not in
# the pip CLI tree, so we import it inside the functions that need it rather
# than at module level.  This keeps the pip CLI importable without the engine
# on sys.path and without a try/except wrapper.
_VNX_VERSION_RO_MODULE = "vnx_version_ro"


VNX_GIT_REMOTE = "https://github.com/Vinix24/vnx-orchestration.git"
DEFAULT_KEEP_LAST = 3
DEFAULT_REGISTRY_PATH = Path.home() / ".vnx" / "projects.json"
PROTECTED_VERSIONS_FILE = "protected-versions"

# OI-1379: the fleet registry (source 1) only protects REGISTERED consumer
# projects. A project that pins a version via ``.vnx-version`` but was never
# registered in ~/.vnx/projects.json is invisible to _load_fleet_pins — its
# pin would otherwise be silently eligible for prune. VNX_PIN_SCAN_ROOTS bounds
# where the disk-scan protection source (4) looks for stray pins; default is
# the user's home directory, the one place every consumer project on this
# machine is expected to live under.
#
# Depth 8 is not arbitrary: the OI-1379 incident pin itself
# (~/Desktop/BUSINESS/clients/vincent/pacompany/build/pa-engine/.vnx-version)
# sits 7 directory levels below $HOME. A shallower default would silently miss
# the exact case this source exists to catch.
DEFAULT_PIN_SCAN_MAX_DEPTH = 8
_PIN_SCAN_SKIP_DIRS = frozenset({".git", "node_modules"})

_VERSION_RE = re.compile(r"^(edge|latest|v?\d+\.\d+\.\d+(?:-[\w.]+)?)$")

# Marker written into each central-install version dir. Mirrors
# install-central.sh::write_install_marker (99-110). Without it,
# _is_central_install() in vnx_paths.py misresolves a central install as a
# standalone dev checkout and collapses PROJECT_ROOT onto the shared code tree.
INSTALL_MODE_MARKER = ".vnx-install-mode"
INSTALL_MODE_VALUE = "central"


class ProtectionSetUnavailable(Exception):
    """The set of GC-protected versions could not be reliably determined.

    Raised instead of returning a partial protected set: pruning while blind
    to what is pinned is exactly the catastrophe GC-protect exists to prevent,
    so any read/parse failure of a protection source that EXISTS must surface,
    never be swallowed. Carries the offending ``source`` path and the
    underlying ``cause``.
    """

    def __init__(self, source, cause: BaseException):
        self.source = str(source)
        self.cause = cause
        super().__init__(f"cannot determine protected versions from {source}: {cause}")


def _resolve_root() -> Path:
    env_root = os.environ.get("VNX_HOME_ROOT")
    if env_root:
        return Path(env_root).expanduser().resolve()

    candidate = Path.home() / ".vnx-system"
    if candidate.is_dir():
        return candidate

    # Sandbox: central install doesn't exist yet
    return (Path.home() / ".vnx-system-test").expanduser().resolve()


def _resolve_audit_log() -> Path:
    """Resolve path for central install audit event log."""
    vnx_data = os.environ.get("VNX_DATA_DIR")
    if vnx_data:
        base = Path(vnx_data).expanduser().resolve()
    else:
        base = Path.home() / ".vnx-data"
    return base / "events" / "central_install.ndjson"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _emit_audit_event(
    event_type: str, fields: dict, audit_log: "Path | None" = None
) -> None:
    """Append an audit event to the central install NDJSON log with exclusive locking."""
    path = audit_log or _resolve_audit_log()
    record = {"event_type": event_type, "timestamp": _now_iso(), **fields}
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, separators=(",", ":")) + "\n"
    with open(path, "a", encoding="utf-8") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        f.write(line)


def _validate_version_name(target: str) -> str:
    """Validate and return a safe version name.

    Allowed: edge, latest, vX.Y.Z, X.Y.Z, vX.Y.Z-suffix (alphanumeric/./-)
    Raises ValueError on path traversal, shell metacharacters, or invalid format.
    """
    if not _VERSION_RE.match(target):
        raise ValueError(f"invalid version name: {target!r}")
    return target


def _list_version_dirs(root: Path) -> list:
    versions_dir = root / "versions"
    if not versions_dir.is_dir():
        return []
    return sorted(
        [d for d in versions_dir.iterdir() if d.is_dir()],
        key=lambda d: d.stat().st_mtime,
    )


def _current_target(root: Path):
    current = root / "current"
    if current.is_symlink():
        try:
            return current.resolve()
        except OSError:
            return None
    return None


def _fetch_version(
    root: Path, target: str, dry_run: bool, audit_log: "Path | None" = None
) -> Path:
    # Belt-and-suspenders: validate before every path join regardless of call site
    _validate_version_name(target)

    versions_dir = root / "versions"
    target_dir = versions_dir / target

    if dry_run:
        print(f"[dry-run] Would clone/pull {VNX_GIT_REMOTE} -> {target_dir}")
        return target_dir

    versions_dir.mkdir(parents=True, exist_ok=True)

    if target_dir.is_dir():
        print(f"Pulling {target} in {target_dir}...")
        from vnx_cli import _engine as _eng2
        _eng2.ensure_engine_on_path()
        from vnx_version_ro import writeable_version_dir as _wvd
        with _wvd(target_dir):
            subprocess.run(
                ["git", "-C", str(target_dir), "pull", "--ff-only"],
                check=True,
            )
            # Strip and marker happen inside the context so the dir is
            # writable for both.  _write_install_marker has its own inner
            # context manager that is a no-op when already writable.
            _strip_tenant_marker(target_dir)
            _write_install_marker(target_dir, audit_log=audit_log)
    else:
        ref = "main" if target == "edge" else target
        print(f"Cloning {VNX_GIT_REMOTE} (ref={ref}) -> {target_dir}...")
        subprocess.run(
            ["git", "clone", "--branch", ref, "--depth", "1",
             VNX_GIT_REMOTE, str(target_dir)],
            check=True,
        )
        _strip_tenant_marker(target_dir)
        _write_install_marker(target_dir, audit_log=audit_log)
        # Lock the freshly cloned pinned version dir (edge stays writable).
        from vnx_cli import _engine as _eng3
        _eng3.ensure_engine_on_path()
        from vnx_version_ro import make_readonly as _mkro
        _mkro(target_dir)

    return target_dir


def _strip_tenant_marker(version_dir: Path) -> None:
    """Remove `.vnx-project-id` from an installed engine version dir (tenant-neutral)."""
    marker = version_dir / ".vnx-project-id"
    try:
        if marker.is_file():
            marker.unlink()
            print(f"Stripped stray tenant marker: {marker}")
    except OSError as exc:
        print(f"[warn] could not strip tenant marker {marker}: {exc}")


def _git_toplevel(path: Path) -> "Path | None":
    """Mirror of vnx_paths._git_toplevel — the git toplevel of ``path``, or None."""
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


def _write_install_marker(version_dir: Path, audit_log: "Path | None" = None) -> None:
    """Atomically write `.vnx-install-mode` = `central` into version_dir.

    Reuses the codebase's shared atomic-write helper (scripts/lib/atomic_io.py)
    instead of a bespoke tmp+os.replace implementation.

    Emits a `central_install_marker_written` NDJSON audit event (ADR-005) —
    this is an install-state mutation like the symlink flip and prune it
    accompanies, and must leave the same kind of audit trace.
    """
    from vnx_cli import _engine
    _engine.ensure_engine_on_path()
    from atomic_io import atomic_write_text
    from vnx_version_ro import writeable_version_dir

    with writeable_version_dir(version_dir):
        atomic_write_text(version_dir / INSTALL_MODE_MARKER, f"{INSTALL_MODE_VALUE}\n")
        _emit_audit_event(
            "central_install_marker_written",
            {"version_dir": str(version_dir)},
            audit_log=audit_log,
        )


def _is_under_versions(root: Path, version_dir: Path) -> bool:
    """True when ``version_dir`` lives directly under ``<root>/versions/`` —
    the central-install layout install-central.sh lays down
    (``TARGET_DIR/versions/<version>/``).
    """
    try:
        return version_dir.resolve().parent == (root / "versions").resolve()
    except OSError:
        return False


def _ensure_install_marker(
    root: Path, version_dir: "Path | None", audit_log: "Path | None" = None
) -> None:
    """Self-heal: back-fill the marker on an already-installed, marker-less
    central version dir.

    Guarded by two conditions:

    1. Ownership: ``version_dir`` must live directly under ``<root>/versions/``
       (the central-install layout). A standalone dev checkout that happens to
       resolve as the active `current` target or a rollback/flip target is
       NOT under ``versions/`` and must never be stamped — every git repo is
       its own git-toplevel, so relying on that check alone would let a plain
       dev checkout be mis-stamped `central`, the inverse of the bug this
       marker exists to prevent.
    2. Git-toplevel: mirrors the condition `vnx_paths._is_central_install()`
       uses, kept as an additional guard on top of the ownership check above.
    """
    if version_dir is None or not version_dir.is_dir():
        return
    marker = version_dir / INSTALL_MODE_MARKER
    try:
        if marker.is_file() and marker.read_text(encoding="utf-8").strip() == INSTALL_MODE_VALUE:
            return
    except OSError:
        return
    if not _is_under_versions(root, version_dir):
        return
    if _git_toplevel(version_dir) != version_dir:
        return
    _write_install_marker(version_dir, audit_log=audit_log)
    print(f"Repaired missing install-mode marker: {marker}")


def _atomic_symlink_flip(
    root: Path, target_dir: Path, dry_run: bool, audit_log: "Path | None" = None
) -> None:
    current = root / "current"

    if dry_run:
        print(f"[dry-run] Would flip symlink: {current} -> {target_dir}")
        return

    # Self-heal: whatever becomes active must carry the marker, even when this
    # flip target was not freshly fetched (e.g. a --rollback to an older
    # version cloned before this fix shipped).
    _ensure_install_marker(root, target_dir, audit_log=audit_log)

    from_version = _current_target(root)
    from_name = from_version.name if from_version else None
    to_name = target_dir.name

    root.mkdir(parents=True, exist_ok=True)
    tmp_link = root / "current.tmp"

    if tmp_link.is_symlink() or tmp_link.exists():
        tmp_link.unlink(missing_ok=True)

    tmp_link.symlink_to(target_dir)

    _emit_audit_event(
        "central_install_update",
        {"from_version": from_name, "to_version": to_name, "success": False, "phase": "before_flip"},
        audit_log=audit_log,
    )

    os.replace(tmp_link, current)

    _emit_audit_event(
        "central_install_update",
        {"from_version": from_name, "to_version": to_name, "success": True, "phase": "after_flip"},
        audit_log=audit_log,
    )

    print(f"Activated: {current} -> {target_dir}")


def _iter_pin_values(text: str) -> list:
    """Yield validated pin values from newline/comma separated text.

    Blank lines and ``#`` comments are ignored; values that violate the pin
    alphabet (same regex as the re-exec keystone and the shim) are skipped
    with a warning instead of aborting the whole protection sweep.
    """
    values = []
    for raw in re.split(r"[\n,]", text):
        value = raw.strip()
        if not value or value.startswith("#"):
            continue
        if value in (".", "..") or not _PIN_RE.match(value):
            print(f"[warn] ignoring malformed protected version: {value!r}", file=sys.stderr)
            continue
        values.append(value)
    return values


def _load_fleet_pins(registry_path: "Path | None" = None) -> dict:
    """Collect ``{normalized_pin: {sources}}`` from the fleet project registry.

    Reads the same ``~/.vnx/projects.json`` registry the fabric already uses
    (scripts/commands/registry.sh, scripts/lib/vnx_identity.py) and harvests
    each registered project's ``.vnx-version`` pin. A pruned pinned version
    silently degrades that project's ``vnx`` to ``current`` (the re-exec
    keystone fails open), so every pin found here protects its version dir.
    """
    if registry_path is None:
        env_registry = os.environ.get("VNX_PROJECT_REGISTRY")
        registry_path = (
            Path(env_registry).expanduser() if env_registry else DEFAULT_REGISTRY_PATH
        )
    protected: dict = {}
    try:
        text = registry_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        # Absent registry legitimately means "no pins from this source".
        return protected
    except (OSError, UnicodeDecodeError) as exc:
        # A registry that EXISTS but cannot be read => fail CLOSED.
        raise ProtectionSetUnavailable(registry_path, exc) from exc
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ProtectionSetUnavailable(registry_path, exc) from exc
    for entry in data.get("projects", []) or []:
        project_path = entry.get("path")
        if not project_path:
            continue
        project_dir = Path(project_path).expanduser()
        pin_file = project_dir / PIN_FILE_NAME
        try:
            if not pin_file.is_file():
                continue
            lines = pin_file.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            # Raced removal between stat and read = absent, not a failure.
            continue
        except (OSError, UnicodeDecodeError) as exc:
            # A pin file that EXISTS but cannot be read => fail CLOSED: its
            # pin must not silently become eligible for deletion.
            raise ProtectionSetUnavailable(pin_file, exc) from exc
        pin = lines[0].strip() if lines else ""
        if not pin or pin in (".", "..") or not _PIN_RE.match(pin):
            continue
        protected.setdefault(_normalize_version(pin), set()).add(
            f"pinned by project {project_dir}"
        )
    return protected


def _resolve_pin_scan_roots(scan_roots: "str | list | None" = None) -> list:
    """Resolve the configured disk-scan roots for stray ``.vnx-version`` pins.

    ``VNX_PIN_SCAN_ROOTS`` is a ``:``-separated list of directories (mirrors
    ``PATH`` convention). Defaults to the user's home directory when unset.
    Blank entries are skipped; each surviving entry is ``~``-expanded but not
    required to exist (a missing root legitimately means "no pins from this
    source", handled by the caller).
    """
    if scan_roots is None:
        env_val = os.environ.get("VNX_PIN_SCAN_ROOTS")
        scan_roots = env_val if env_val is not None else str(Path.home())
    raw_parts = scan_roots.split(":") if isinstance(scan_roots, str) else list(scan_roots)
    out = []
    for part in raw_parts:
        part = (part or "").strip()
        if not part:
            continue
        out.append(Path(part).expanduser())
    return out


def _scan_root_for_pins(root: Path, max_depth: int) -> dict:
    """Walk ``root`` (bounded depth) for ``.vnx-version`` pin files.

    Returns ``{normalized_pin: {reasons}}`` for this root only. Symlinks and
    ``.git``/``node_modules`` directories are never descended into — they
    cannot hold a legitimate ancestor pin and would otherwise let a vendored
    tree or a symlink cycle blow up the walk. ``max_depth`` bounds how many
    directory levels below ``root`` (root itself is depth 0) are visited, so a
    broad default root like ``$HOME`` cannot turn every prune into a full
    filesystem walk.

    An ABSENT root is not a failure (mirrors the registry/file sources: "does
    not exist" means "no pins from this source"). A root that EXISTS but
    cannot be listed (permission denied, race) raises ``OSError`` — the caller
    converts that into ``ProtectionSetUnavailable``: pruning while blind to a
    scan root we could not read is exactly the catastrophe this mechanism
    exists to prevent.
    """
    protected: dict = {}
    if not root.is_dir():
        return protected

    def _walk(dirpath: Path, depth: int) -> None:
        try:
            entries = list(os.scandir(dirpath))
        except OSError:
            if dirpath == root:
                raise
            # A subdirectory that turns unreadable mid-walk (permission
            # oddity, race) is skipped — only the configured ROOT itself is a
            # fail-closed condition; the walk should not accumulate one flaky
            # subtree into a whole-prune abort.
            return
        for entry in entries:
            try:
                if entry.is_symlink():
                    continue
                is_dir = entry.is_dir(follow_symlinks=False)
            except OSError:
                continue
            if is_dir:
                if entry.name in _PIN_SCAN_SKIP_DIRS:
                    continue
                if depth < max_depth:
                    _walk(Path(entry.path), depth + 1)
                continue
            if entry.name != PIN_FILE_NAME:
                continue
            pin_path = Path(entry.path)
            try:
                lines = pin_path.read_text(encoding="utf-8").splitlines()
            except (OSError, UnicodeDecodeError):
                continue
            pin = lines[0].strip() if lines else ""
            if not pin or pin in (".", "..") or not _PIN_RE.match(pin):
                continue
            protected.setdefault(_normalize_version(pin), set()).add(
                f"pin scan: {pin_path}"
            )

    _walk(root.resolve(), 0)
    return protected


def _scan_disk_for_pins(
    scan_roots: "str | list | None" = None,
    max_depth: int = DEFAULT_PIN_SCAN_MAX_DEPTH,
) -> dict:
    """Union of ``.vnx-version`` pins found by scanning configured disk roots.

    Fourth protected-version source (OI-1379). See ``_scan_root_for_pins`` for
    the walk semantics and ``_resolve_pin_scan_roots`` for root configuration.
    """
    protected: dict = {}
    for root in _resolve_pin_scan_roots(scan_roots):
        try:
            root_protected = _scan_root_for_pins(root, max_depth)
        except OSError as exc:
            raise ProtectionSetUnavailable(root, exc) from exc
        for norm, reasons in root_protected.items():
            protected.setdefault(norm, set()).update(reasons)
    return protected


def _collect_protected_versions(
    root: Path,
    protect_pins: "str | list | None" = None,
    registry_path: "Path | None" = None,
    protected_file: "Path | None" = None,
    pin_scan_roots: "str | list | None" = None,
) -> dict:
    """Union of all version-protection sources: ``{normalized_pin: {reasons}}``.

    Sources (all optional, all unioned):

    1. Fleet registry: every registered consumer project's ``.vnx-version``.
    2. ``<root>/protected-versions``: operator-curated newline file.
    3. ``--protect-pins``: explicit comma-separated CLI argument.
    4. Disk scan (``VNX_PIN_SCAN_ROOTS``): ``.vnx-version`` pins belonging to
       consumer projects that were never registered in source 1 (OI-1379).

    Normalization matches ``vnx_cli._reexec._normalize_version`` so a pin of
    ``1.3.0`` protects the version dir ``v1.3.0`` (and vice versa).
    """
    protected = _load_fleet_pins(registry_path)

    for norm, reasons in _scan_disk_for_pins(pin_scan_roots).items():
        protected.setdefault(norm, set()).update(reasons)

    pfile = protected_file or (root / PROTECTED_VERSIONS_FILE)
    if pfile.is_file():
        try:
            text = pfile.read_text(encoding="utf-8")
        except FileNotFoundError:
            # Raced removal between stat and read = absent, not a failure.
            text = None
        except (OSError, UnicodeDecodeError) as exc:
            # An operator protection file that EXISTS but cannot be read =>
            # fail CLOSED: the versions it protects must not be pruned blind.
            raise ProtectionSetUnavailable(pfile, exc) from exc
        if text is not None:
            for value in _iter_pin_values(text):
                protected.setdefault(_normalize_version(value), set()).add(
                    f"listed in {pfile}"
                )

    if protect_pins:
        text = protect_pins if isinstance(protect_pins, str) else ",".join(protect_pins)
        for value in _iter_pin_values(text):
            protected.setdefault(_normalize_version(value), set()).add("--protect-pins")

    return protected


def _iter_site_packages_dirs() -> list:
    """Candidate site-packages roots to scan for pip-install pointers.

    Covers the interpreter's global + user site-packages, plus any ``sys.path``
    entry whose basename is ``site-packages``/``dist-packages`` (venvs, extra
    prefixes). Deduplicated, resolved, best-effort — an import failure returns
    an empty list so a prune never fails on an exotic interpreter.
    """
    try:
        import site as _site

        candidates = list(_site.getsitepackages())
        user = _site.getusersitepackages()
        if user:
            candidates.append(user)
    except Exception:  # vnx-silent-except: site introspection is best-effort
        candidates = []
    for entry in sys.path:
        if Path(entry).name in ("site-packages", "dist-packages"):
            candidates.append(entry)
    seen = set()
    out = []
    for raw in candidates:
        try:
            resolved = str(Path(raw).expanduser().resolve())
        except OSError:
            continue
        if resolved not in seen:
            seen.add(resolved)
            out.append(Path(resolved))
    return out


_PATH_TOKEN_RE = re.compile(r"/[^\s'\"()<>\[\]{}]+")


def _text_references_dir(text: str, version_dir: Path) -> bool:
    """True when ``text`` mentions a path that resolves to ``version_dir``.

    Handles raw absolute paths, ``~/``-forms and ``file://`` URLs, and tolerates
    symlink aliasing (macOS ``/tmp`` -> ``/private/tmp``): the comparison is
    done on ``Path.resolve()`` of both sides, never on the raw string.
    """
    vdir = version_dir.resolve()
    try:
        from urllib.parse import unquote, urlparse

        for m in re.finditer(r"file://([^\s'\"()<>\[\]{}]+)", text):
            raw = unquote(urlparse(m.group(1)).path)
            try:
                if Path(raw).expanduser().resolve() == vdir:
                    return True
            except OSError:
                continue
    except ImportError:
        pass
    for m in _PATH_TOKEN_RE.finditer(text):
        token = m.group(0)
        if token == "/" or ".." in token:
            continue
        try:
            if Path(token).expanduser().resolve() == vdir:
                return True
        except OSError:
            continue
    return False


def _pip_install_references_to(version_dir: Path) -> list:
    """Reasons a pip install in the current interpreter maps into ``version_dir``.

    Scans site-packages for the three shapes a pip install leaves behind when it
    targets a version dir:
      1. ``<pkg>-*.dist-info/direct_url.json`` — pip records ``url`` pointing at
         the install source; an editable install keeps the ORIGINAL source dir
         (OI-912: the global ``vnx_cli`` console script was ``pip install -e
         versions/edge`` and ``direct_url.json`` still named ``versions/edge``
         after the dir was pruned).
      2. ``*.pth`` files whose content mentions the dir (path-based installs).
      3. ``__editable__*.py`` finder modules whose MAPPING mentions the dir.

    Pruning a dir any of these reference breaks the install silently — that is
    the exact silent-catastrophe the GC-protect protection set exists to avoid.
    """
    reasons = []
    for site in _iter_site_packages_dirs():
        if not site.is_dir():
            continue
        # 1. dist-info direct_url.json
        try:
            dist_infos = list(site.glob("*.dist-info"))
        except OSError:
            dist_infos = []
        for di in dist_infos:
            direct_url = di / "direct_url.json"
            if not direct_url.is_file():
                continue
            try:
                data = json.loads(direct_url.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                continue
            url = (data or {}).get("url", "") or ""
            if _text_references_dir(url, version_dir):
                reasons.append(f"pip install {di.name} -> {url}")
        # 2. + 3. .pth files and __editable__ finder modules
        try:
            pth_files = list(site.glob("*.pth")) + list(site.glob("__editable__*.py"))
        except OSError:
            continue
        for pth in pth_files:
            try:
                text = pth.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            if _text_references_dir(text, version_dir):
                reasons.append(f"pip editable {pth.name} references {version_dir}")
    return reasons


def _symlink_references_to(root: Path, version_dir: Path) -> list:
    """Reasons a symlink under the central install root points into ``version_dir``.

    Walks the central install tree (``~/.vnx-system``) WITHOUT following symlinks
    and records any symlink whose resolved target is the version dir or sits
    inside it. ``current`` is handled separately by ``_prune_old_versions`` (it
    is never a prune candidate), but a bin/ shim or a legacy alias symlink that
    points at a version dir must protect it too (OI-912).
    """
    reasons = []
    vdir = version_dir.resolve()
    try:
        for dirpath, dirnames, filenames in os.walk(root):
            for name in list(dirnames) + list(filenames):
                entry = Path(dirpath) / name
                try:
                    if not entry.is_symlink():
                        continue
                    target = entry.resolve()
                    if target == vdir or vdir in target.parents:
                        reasons.append(f"symlink {entry} -> {target}")
                except OSError:
                    continue
    except OSError:
        pass
    return reasons


def _prune_old_versions(
    root: Path,
    keep_last: int,
    dry_run: bool,
    audit_log: "Path | None" = None,
    protect_pins: "str | list | None" = None,
    registry_path: "Path | None" = None,
    pin_scan_roots: "str | list | None" = None,
) -> None:
    versions = _list_version_dirs(root)
    current = _current_target(root)

    # FAIL-CLOSED: if the protected set cannot be reliably determined, prune
    # NOTHING. Pruning while blind to what is pinned is the exact catastrophe
    # GC-protect exists to prevent.
    try:
        protected = _collect_protected_versions(
            root,
            protect_pins=protect_pins,
            registry_path=registry_path,
            pin_scan_roots=pin_scan_roots,
        )
    except ProtectionSetUnavailable as exc:
        print(
            f"[warn] GC prune ABORTED (fail-closed): {exc}. "
            "No versions were deleted.",
            file=sys.stderr,
        )
        if not dry_run:
            _emit_audit_event(
                "central_install_prune_aborted",
                {
                    "reason": str(exc),
                    "source": exc.source,
                    "keep_last_N": keep_last,
                },
                audit_log=audit_log,
            )
        return

    # Keep the newest keep_last dirs; prune anything older, skip current
    if len(versions) <= keep_last:
        return

    to_prune = versions[: len(versions) - keep_last]
    for version_dir in to_prune:
        if current and version_dir.resolve() == current.resolve():
            continue
        reasons = set(protected.get(_normalize_version(version_dir.name), ()))
        # OI-912: a pip-install or symlink that maps into this version dir is a
        # live reference — pruning it silently breaks that install (the global
        # vnx_cli console script died with ModuleNotFoundError when update
        # removed versions/edge). Fail-loud is too blunt (the update to the NEW
        # version is fine); protect the referenced dir with a clear message.
        reasons.update(_pip_install_references_to(version_dir))
        reasons.update(_symlink_references_to(root, version_dir))
        reasons = sorted(reasons)
        if reasons:
            reason_text = "; ".join(reasons)
            if dry_run:
                print(f"[dry-run] Would protect from prune: {version_dir} ({reason_text})")
            else:
                _emit_audit_event(
                    "central_install_prune_protected",
                    {
                        "protected_version": version_dir.name,
                        "keep_last_N": keep_last,
                        "reasons": reasons,
                    },
                    audit_log=audit_log,
                )
                print(f"Protected from prune: {version_dir} ({reason_text})")
            continue
        if dry_run:
            print(f"[dry-run] Would prune: {version_dir}")
        else:
            _emit_audit_event(
                "central_install_prune",
                {"pruned_version": version_dir.name, "keep_last_N": keep_last},
                audit_log=audit_log,
            )
            print(f"Pruning: {version_dir}")
            from vnx_cli import _engine as _eng4
            _eng4.ensure_engine_on_path()
            from vnx_version_ro import make_writable as _mkw
            _mkw(version_dir)
            shutil.rmtree(version_dir)


def _do_rollback(root: Path, dry_run: bool) -> int:
    versions = _list_version_dirs(root)
    current = _current_target(root)

    if current is None:
        print("Error: no current symlink — nothing to roll back.", file=sys.stderr)
        return 1

    previous = [v for v in reversed(versions) if v.resolve() != current.resolve()]
    if not previous:
        print("Error: no previous version available for rollback.", file=sys.stderr)
        return 1

    prev_dir = previous[0]
    if dry_run:
        print(f"[dry-run] Would rollback current -> {prev_dir}")
        return 0

    _atomic_symlink_flip(root, prev_dir, dry_run=False)
    print(f"Rolled back to: {prev_dir.name}")
    return 0


def vnx_update(args) -> int:
    target: "str | None" = getattr(args, "to_version", None)
    keep_last: int = getattr(args, "keep_last", DEFAULT_KEEP_LAST)
    dry_run: bool = getattr(args, "dry_run", False)
    rollback: bool = getattr(args, "rollback", False)
    protect_pins = getattr(args, "protect_pins", None)

    root = _resolve_root()

    if dry_run:
        print(f"[dry-run] VNX_HOME_ROOT: {root}")
    else:
        # One-time repair: an already-installed central version (e.g. `edge`,
        # fetched before this fix shipped) may be active but marker-less. Heal
        # it on this and every subsequent `vnx update` invocation, regardless
        # of what --to/--rollback target is requested below.
        _ensure_install_marker(root, _current_target(root))

    if rollback:
        return _do_rollback(root, dry_run=dry_run)

    if not target:
        print("Error: --to <version> is required (or use --rollback).", file=sys.stderr)
        return 1

    try:
        target = _validate_version_name(target)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    try:
        target_dir = _fetch_version(root, target, dry_run=dry_run)
        _atomic_symlink_flip(root, target_dir, dry_run=dry_run)
        _prune_old_versions(
            root, keep_last=keep_last, dry_run=dry_run, protect_pins=protect_pins
        )
    except FileNotFoundError:
        print("Error: git executable not found in PATH", file=sys.stderr)
        return 1
    except subprocess.CalledProcessError as exc:
        print(f"Error: git operation failed: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"Error: OS error during update: {exc}", file=sys.stderr)
        return 1

    if dry_run:
        print(f"[dry-run] Update to '{target}' would succeed.")
        print("[dry-run] Would migrate all central per-project stores to the new schema.")
        return 0

    print(f"VNX updated to '{target}'.")

    # D4 fleet-sync: after flipping the engine version, migrate every central
    # per-project store so none is left half-migrated behind the newer engine.
    # Best-effort — a per-store failure is logged, never aborts the update.
    try:
        print("\nMigrating central per-project stores to the new schema ...")
        from vnx_cli.commands.migrate import migrate_all_central_stores
        migrate_all_central_stores()
    except Exception as exc:
        print(f"  warning: fleet store migration sweep failed: {exc}", file=sys.stderr)

    return 0
