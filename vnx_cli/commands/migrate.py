#!/usr/bin/env python3
"""vnx migrate — bootstrap or repair the runtime DB schema.

Applies the full migration chain to runtime_coordination.db and
quality_intelligence.db: v1-v10 base schema + runners 0017/0019/0020/0022/0024/0026.

Idempotent: migrations already at the target version are skipped.
Safe to run on existing installs — no data is deleted.

Use when:
  - After a fresh `vnx init` (handled automatically since vnx 1.0.5+)
  - After upgrading VNX to a version that adds new migration runners
  - To repair a partially-initialized project
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

from vnx_cli import _engine
from vnx_cli.commands.init_cmd import _anchor_tenant_marker, _bootstrap_runtime_dbs

#: Central store layout ``.../.vnx-data/<pid>/`` — anchors the tenant on the data path.
_DATA_PATH_PID_RE = re.compile(r"(?:^|/)\.vnx-data/([a-z0-9][a-z0-9._-]{0,63})/?$")

#: SQLite sidecars. The backup API folds their committed frames into the copy,
#: so they are never copied on their own (a lone -wal next to a fresh copy lies).
_SQLITE_SIDECAR_GLOBS = ("*-wal", "*-shm", "*-journal")

#: Suffixes that name a SQLite database; the file magic is the other signal.
_SQLITE_SUFFIXES = (".db", ".sqlite", ".sqlite3")

#: Migration record written next to the state DBs of a relocated store.
_RELOCATE_RECORD_NAME = "relocate-legacy.json"


class _RelocateRefused(RuntimeError):
    """A ``--relocate-legacy`` precondition failed; nothing was written (2: usage)."""

    def __init__(self, message: str, exit_code: int = 1) -> None:
        super().__init__(message)
        self.exit_code = exit_code


def _read_marker_pid(data_root: Path) -> "str | None":
    """First non-empty line of ``data_root/.vnx-project-id`` (the tenant marker)."""
    marker = data_root / ".vnx-project-id"
    if not marker.is_file():
        return None
    try:
        first = marker.read_text(encoding="utf-8").splitlines()[0].strip()
    except (OSError, IndexError):
        return None
    return first or None


def _reconcile_tenant_or_fail(data_root: Path, project_id: str) -> None:
    """FAIL-CLOSED tenant reconciliation before migrating (codex split-tenant fix).

    The CLI seeds pool_config with the CLI-derived ``project_id`` while the runner's
    fail-closed resolver stamps rows from the ``.vnx-project-id`` marker / VNX_PROJECT_ID
    / data-path anchor. If those DISAGREE, the store would be split-tenant (codex saw
    terminal_leases stamped 'otherproj' while pool_config was seeded 'myproject'). We
    refuse when any present source disagrees with the CLI-derived id rather than
    silently stamping one and seeding another.
    """
    sources = {
        "cli-derived": project_id,
        "marker": _read_marker_pid(data_root),
        "env:VNX_PROJECT_ID": (os.environ.get("VNX_PROJECT_ID") or "").strip() or None,
    }
    m = _DATA_PATH_PID_RE.search(str(data_root).rstrip("/"))
    if m:
        sources["data-path"] = m.group(1)
    present = {k: v for k, v in sources.items() if v}
    if len(set(present.values())) > 1:
        detail = ", ".join(f"{k}={v!r}" for k, v in present.items())
        raise RuntimeError(
            "refusing to migrate a split-tenant store: the tenant sources disagree "
            f"({detail}). Resolve .vnx-project-id / VNX_PROJECT_ID / the data-path so "
            "they all name one project before migrating (ADR-007 fail-closed)."
        )


def _run_future_system_pipeline(
    data_root: Path, project_id: str, *, tenant_stamp_fatal: bool = False, backup: bool = True
) -> None:
    """Drive the WHOLE future-system pipeline against the store at *data_root* (D4).

    The bootstrap chain above only reaches the numbered runners with a paired
    ``apply_NNNN.py`` (≤ v26). The Horizon schema (tracks.horizon, deliverables
    view) plus the ADR-007 dispatches repair, version reconciliation, the 0027→0031
    walk, and W1 tenant-stamping live in ``migrate_future_system.run()``, which was
    NEVER wired into ``vnx migrate`` — leaving pre-Horizon stores missing
    ``tracks.horizon`` (Bug A). This threads the EXACT canonical data root the CLI
    already resolved (the runner would otherwise re-derive
    ``<project_root>/.vnx-data`` — the D4 threading trap) and takes a VACUUM INTO
    backup first. ``tenant_stamp_fatal=False`` (the ``vnx migrate`` default) keeps a
    W1 failure non-fatal: the schema walk is committed first, so ``tracks.horizon``
    survives and the operator is warned. ``--relocate-legacy`` passes True (a store
    is published only when every step succeeded) and ``backup=False`` (the staging
    copy is itself the backup of an untouched source).
    """
    _engine.ensure_engine_on_path()
    scripts_dir = _engine.engine_root() / "scripts"
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))

    import migrate_future_system  # type: ignore

    # FAIL-CLOSED first: refuse to migrate if the CLI-derived tenant disagrees with an
    # existing marker / VNX_PROJECT_ID / the data-path anchor (no split-tenant store).
    _reconcile_tenant_or_fail(data_root, project_id)

    # Advisory pre-flight integrity check (Tier F, task #30): surface dangling FK edges
    # (the mission-control 22-dangling track_dependencies class) as a clear report BEFORE
    # the migration mutates, instead of a cryptic mid-migration constraint failure. Placed
    # right after the tenant gate and before ANY write (marker or DB), so strict mode aborts
    # before the store is touched at all. Advisory by default; VNX_MIGRATE_STRICT_FK=1 aborts
    # a violating store (the fleet sweep then skips it and continues). Fail-open: never block
    # a migration on the check itself.
    try:
        from store_integrity import preflight_or_report  # noqa: PLC0415

        preflight_or_report(data_root / "state" / "runtime_coordination.db", label=project_id)
    except ImportError:
        pass  # advisory tooling absent — never blocks the migration

    # Anchor the tenant on disk so the fail-closed resolver in the runner resolves THIS
    # store's project_id even for a layout the DB-path anchor can't cover. Never
    # overwrites an existing marker (reconciled above). The bootstrap already anchored
    # it when it ran; this keeps the pipeline correct on its own.
    _anchor_tenant_marker(data_root, project_id)

    # data_dir is threaded explicitly (D4 threading trap); no env mutation needed.
    # run_tenant_stamp=True (RE-ENABLED 2026-07-10): W1 tenant-stamping was disabled because
    # a normally-bootstrapped non-vnx-dev store carries a legacy ('vnx-dev', key) row AND an
    # authoritative (<pid>, key) row on a composite UNIQUE(project_id, key) (e.g.
    # execution_targets, pool_config), so Phase 2's 'vnx-dev'->pid restamp tripped the UNIQUE.
    # tenant_stamping._dedup_legacy_collisions now drops the stale legacy duplicate (the pid
    # row wins; children repoint via their own restamp) BEFORE the restamp. Verified on copies
    # of all 4 live central stores: W1 succeeds, foreign_key_check clean, only stale dual-seed
    # duplicates removed (no legitimate-data loss). tenant_stamp_fatal=False keeps a W1 failure
    # non-fatal (WARNING after the committed schema walk) as belt-and-suspenders.
    migrate_future_system.run(
        data_dir=data_root,
        tenant_stamp_fatal=tenant_stamp_fatal,
        run_tenant_stamp=True,
        backup=backup,
    )


# ===========================================================================
# vnx migrate --relocate-legacy — copy a pre-#1437 XDG store into the central
# store, and delete nothing
# ===========================================================================


def _readonly_uri(path: Path) -> str:
    """``file://`` URI that opens *path* read-only: the source is never written."""
    return path.resolve().as_uri() + "?mode=ro"


def _iter_databases(root: Path) -> list[Path]:
    """Every ``*.db`` database below *root*, sorted (sidecars end otherwise)."""
    return sorted(
        Path(current) / name
        for current, _dirs, files in os.walk(root)
        for name in files
        if name.endswith(_SQLITE_SUFFIXES)
    )


def _copy_sqlite_database(src: Path, dst: Path) -> None:
    """Consistent copy through the SQLite backup API (committed WAL included)."""
    source_conn = sqlite3.connect(_readonly_uri(src), uri=True)
    dest_conn = sqlite3.connect(str(dst))
    try:
        source_conn.backup(dest_conn)
    finally:
        dest_conn.close()
        source_conn.close()


def _copy_store_consistent(source: Path, staging: Path) -> None:
    """Rule 3: SQLite DBs through the backup API, every other file a plain copy."""
    databases = _iter_databases(source)
    shutil.copytree(source, staging, dirs_exist_ok=True, ignore=shutil.ignore_patterns(
        *_SQLITE_SUFFIXES, *_SQLITE_SIDECAR_GLOBS))
    for db_path in databases:
        _copy_sqlite_database(db_path, staging / db_path.relative_to(source))


def _table_project_ids(db_path: Path) -> dict[str, set[str]]:
    """``{table: {project_id values}}`` for every table that carries a project_id."""
    stamps: dict[str, set[str]] = {}
    conn = sqlite3.connect(_readonly_uri(db_path), uri=True)
    try:
        names = [row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name NOT LIKE 'sqlite_%'")]
        for name in names:
            quoted = '"' + name.replace('"', '""') + '"'  # named by sqlite_master
            if "project_id" not in {row[1] for row in
                                    conn.execute(f"PRAGMA table_info({quoted})")}:
                continue
            values = {str(row[0]) for row in conn.execute(
                f"SELECT DISTINCT project_id FROM {quoted} "
                "WHERE project_id IS NOT NULL AND TRIM(project_id) <> ''")}
            if values:
                stamps[name] = values
    finally:
        conn.close()
    return stamps


def _source_stamps(source: Path) -> dict[str, set[str]]:
    """Every project_id stamp in the source DBs, keyed ``<db relpath>:<table>``."""
    return {
        f"{db.relative_to(source).as_posix()}:{table}": values
        for db in _iter_databases(source)
        for table, values in _table_project_ids(db).items()
    }


def _check_ownership(
    stamps: dict[str, set[str]], project_id: str, assume_owner: str | None
) -> None:
    """Rule 2: a foreign stamp refuses; no stamp at all needs ``--assume-owner``."""
    foreign = {key: sorted(values - {project_id})
               for key, values in stamps.items() if values - {project_id}}
    if foreign:
        detail = "; ".join(f"{key} -> {', '.join(v)}" for key, v in sorted(foreign.items()))
        raise _RelocateRefused(
            f"the legacy store is stamped with another project_id ({detail}); refusing "
            f"to hand it to --project-id {project_id!r}.")
    if not stamps and not (assume_owner or "").strip():
        raise _RelocateRefused(
            "the legacy store carries no project_id stamp (a pre-ADR-007 schema-v14 "
            f'store). Re-run with --assume-owner "<why this store is {project_id}>".')


def _refuse_ambient_tenant(project_id: str) -> None:
    """Refuse a relocation under an ambient data-root or tenant override."""
    for key in ("VNX_DATA_DIR", "VNX_STATE_DIR"):
        value = (os.environ.get(key) or "").strip()
        if value:
            raise _RelocateRefused(
                f"{key} is set ({value!r}); a relocation publishes into the central "
                f"store. Unset {key} and re-run.")
    env_pid = (os.environ.get("VNX_PROJECT_ID") or "").strip()
    if env_pid and env_pid != project_id:
        raise _RelocateRefused(
            f"VNX_PROJECT_ID is {env_pid!r} but --project-id is {project_id!r}; the "
            "pipeline resolves the tenant fail-closed and would refuse mid-run.")


def _refuse_symlinks(source: Path) -> None:
    """A legacy store is a plain tree: a symlink would bypass the copy guarantees.

    ``shutil.copytree`` follows directory symlinks while ``_iter_databases`` does not,
    so a linked directory's databases would be copied raw (their committed WAL frames
    lost) and a linked ``*.db`` would be listed and copied twice. Nothing is read yet,
    so the refusal itself writes nothing.
    """
    for current, dirs, files in os.walk(source, followlinks=False):
        for name in sorted(dirs) + sorted(files):
            path = Path(current) / name
            if path.is_symlink():
                relative = path.relative_to(source).as_posix()
                raise _RelocateRefused(
                    f"the legacy store holds a symlink at {relative}; a store must hold "
                    "no symlinks. Resolve or copy the linked content into the store "
                    "first, then re-run.", 2)


def _relocate_preflight(project_dir: Path, legacy_path, project_id: str) -> tuple[Path, Path]:
    """Rule 1: refuse before writing anything. Returns ``(source, target)``."""
    if not legacy_path:
        raise _RelocateRefused(
            "--legacy-path <dir> is required: there is no auto-discovery of the "
            "source store (a wrong guess would move another project's state).", 2)
    if not project_id:
        raise _RelocateRefused(
            "--project-id <pid> is required: it is the tenant the store is stamped with.", 2)
    _engine.ensure_engine_on_path()
    from vnx_ids import PROJECT_ID_RE

    if not PROJECT_ID_RE.match(project_id):
        raise _RelocateRefused(
            f"invalid --project-id {project_id!r}: must match {PROJECT_ID_RE.pattern}", 2)
    _refuse_ambient_tenant(project_id)
    import legacy_store_path
    import vnx_paths

    source = Path(legacy_path).expanduser().resolve()
    if not source.is_dir():
        raise _RelocateRefused(f"legacy store {source} is not a directory", 2)
    _refuse_symlinks(source)
    if not (source / legacy_store_path.RUNTIME_DB_RELPATH).is_file():
        raise _RelocateRefused(
            f"{source} holds no state/runtime_coordination.db — not a store (or only an "
            "empty skeleton), so there is nothing to relocate.")
    shadow = project_dir / ".vnx-data" / "state"
    if shadow.is_dir() and _iter_databases(shadow):
        raise _RelocateRefused(
            f"{project_dir} has its own repo-local store at {shadow.parent}; it would "
            "shadow the central target. Relocate or remove it first.")
    marker = _read_marker_pid(source)
    if (source / ".vnx-project-id").is_file() and not marker:
        raise _RelocateRefused(
            f"{source / '.vnx-project-id'} is empty or unreadable; refusing to guess "
            "the tenant it meant to name.")
    if marker and marker != project_id:
        raise _RelocateRefused(
            f"the legacy store's .vnx-project-id names {marker!r}, not {project_id!r}; "
            "refusing to relabel another project's store.")
    target = vnx_paths.resolve_project_data_root(project_id)
    if target.exists():
        raise _RelocateRefused(
            f"the central store {target} already exists; refusing to overwrite it. "
            "Move it aside first if the legacy copy is the one to keep.")
    return source, target


def _verify_integrity(root: Path) -> None:
    """``PRAGMA integrity_check`` must answer exactly ``ok`` for every database."""
    databases = _iter_databases(root)
    if not databases:
        raise RuntimeError(f"no SQLite database found under {root}")
    for db_path in databases:
        conn = sqlite3.connect(_readonly_uri(db_path), uri=True)
        try:
            rows = conn.execute("PRAGMA integrity_check").fetchall()
        finally:
            conn.close()
        if rows != [("ok",)]:
            raise RuntimeError(f"PRAGMA integrity_check on {db_path} returned {rows!r}")


def _upgrade_staging(staging: Path, project_id: str) -> None:
    """Rule 4: the full chain on the copy; every step fatal, then integrity."""
    _bootstrap_runtime_dbs(staging, project_id=project_id)
    _run_future_system_pipeline(staging, project_id, tenant_stamp_fatal=True, backup=False)
    db_path = staging / "state" / "runtime_coordination.db"
    if not db_path.is_file():
        raise RuntimeError(f"the upgrade left no {db_path}")
    conn = sqlite3.connect(_readonly_uri(db_path), uri=True)
    try:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(tracks)")}
    finally:
        conn.close()
    if "horizon" not in columns:
        raise RuntimeError(
            f"the pipeline did not reach the Horizon schema: tracks.horizon absent in {db_path}")
    _verify_integrity(staging)


def _write_relocate_record(
    staging: Path, source: Path, project_id: str,
    stamps: dict[str, set[str]], assume_owner: str | None,
) -> None:
    """Rule 2: the ownership assumption travels inside the published store."""
    assumed = not stamps
    record = {
        "schema": "vnx-relocate-legacy/1",
        "project_id": project_id,
        "relocated_from": str(source),
        "relocated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "owner_assumed": assumed,
        "owner_reason": (assume_owner or "").strip() if assumed else None,
        "source_stamps": {key: sorted(values) for key, values in sorted(stamps.items())},
    }
    path = staging / "state" / _RELOCATE_RECORD_NAME
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)  # atomic: the record lands whole or not at all


def _print_relocate_success(target: Path, source: Path, project_dir: Path) -> None:
    print(f"\nRelocated into the central store: {target}\n  source left untouched: {source}\n")
    print("Verify it:\n  vnx doctor --project-dir " + shlex.quote(str(project_dir)) + "\n")
    print("The legacy copy is NOT removed. Remove it yourself once the check passes:")
    print(f"  rm -rf -- {shlex.quote(str(source))}")


def relocate_legacy(args) -> int:
    """``vnx migrate --relocate-legacy``: copy a legacy store into the central store.

    The source is opened read-only and never modified: a failure before the
    publish rename leaves the staging dir in place (its path is printed) and no
    target. Removal of the source stays the operator's own command.
    """
    project_dir = Path(getattr(args, "project_dir", ".")).expanduser().resolve()
    project_id = (getattr(args, "project_id", None) or "").strip()
    assume_owner = getattr(args, "assume_owner", None)
    try:
        source, target = _relocate_preflight(
            project_dir, getattr(args, "legacy_path", None), project_id
        )
        stamps = _source_stamps(source)
        _check_ownership(stamps, project_id, assume_owner)
    except _RelocateRefused as exc:
        print(f"\n  error: {exc}", file=sys.stderr)
        return exc.exit_code
    except Exception as exc:  # unreadable/invalid source: still nothing written
        print(f"\n  error: cannot read the legacy store: {exc}", file=sys.stderr)
        return 1

    print(f"Relocating legacy store {source}\n                 into {target}")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = target.parent / f".staging-{project_id}-{stamp}"
    staged_store = staging / project_id
    staged_store.mkdir(parents=True)
    # The marker goes in BEFORE the bootstrap: the fail-closed resolver reads a DB
    # path as <...>/.vnx-data/<pid>/state/..., so the staged store sits one level
    # below the staging dir — nothing there parses the staging name as a tenant.
    (staged_store / ".vnx-project-id").write_text(project_id + "\n", encoding="utf-8")
    try:
        _copy_store_consistent(source, staged_store)
        _upgrade_staging(staged_store, project_id)
        _write_relocate_record(staged_store, source, project_id, stamps, assume_owner)
        if target.exists():  # appeared while staging: never overwrite it
            raise RuntimeError(f"{target} appeared during the relocation")
        os.rename(staged_store, target)  # rule 5: one rename publishes the store
    except Exception as exc:  # every step is fatal; the staging dir is kept
        print(f"\n  error: relocation failed: {exc}", file=sys.stderr)
        print(f"  staging dir kept for inspection: {staging}", file=sys.stderr)
        print(f"  central store NOT created: {target}", file=sys.stderr)
        return 1
    try:
        os.rmdir(staging)  # our empty scratch dir; the store already moved out
    except OSError:  # vnx-silent-except: an empty scratch dir is not a failure
        pass
    _print_relocate_success(target, source, project_dir)
    return 0


def _iter_central_project_stores() -> list[tuple[Path, str]]:
    """Return ``[(data_root, project_id)]`` for every central per-project store.

    A central store lives at ``<data_home>/<project_id>/state/runtime_coordination.db``
    where <data_home> is ``$VNX_DATA_HOME`` or ``~/.vnx-data``. Only directory names
    matching the project-id grammar (``^[a-z][a-z0-9-]{1,31}$``) are considered, so a
    stray non-store dir cannot be swept and no path-traversal name is honoured.
    """
    import re as _re

    pid_re = _re.compile(r"^[a-z][a-z0-9-]{1,31}$")
    data_home = Path(os.environ.get("VNX_DATA_HOME") or (Path.home() / ".vnx-data"))
    if not data_home.is_dir():
        return []
    stores: list[tuple[Path, str]] = []
    for child in sorted(data_home.iterdir()):
        if not child.is_dir() or not pid_re.match(child.name):
            continue
        if (child / "state" / "runtime_coordination.db").exists():
            stores.append((child, child.name))
    return stores


def migrate_all_central_stores() -> int:
    """Fleet-sync: run the future-system pipeline on every central store (D4).

    Invoked after ``vnx update`` flips the engine version so no per-project store is
    left half-migrated behind a newer engine. Best-effort per store: a failure on one
    store is logged and the sweep continues (never aborts the whole fleet). Each store
    gets a VACUUM INTO backup and non-fatal W1. Returns the count that migrated cleanly.
    """
    stores = _iter_central_project_stores()
    if not stores:
        print("  no central per-project stores found — nothing to migrate.")
        return 0
    ok = 0
    for data_root, project_id in stores:
        print(f"\n[fleet-migrate] {project_id} — {data_root}")
        try:
            _run_future_system_pipeline(data_root, project_id)
            ok += 1
        except Exception as exc:
            print(
                f"  [fleet-migrate][WARN] {project_id}: migration failed: {exc}",
                file=sys.stderr,
            )
    print(f"\n[fleet-migrate] {ok}/{len(stores)} central stores migrated cleanly.")
    return ok


def vnx_migrate(args) -> int:
    """Run the full migration chain against the project's runtime DBs."""
    if getattr(args, "relocate_legacy", False):
        return relocate_legacy(args)
    project_dir = Path(getattr(args, "project_dir", ".")).resolve()

    # Resolve data root via canonical chain (explicit > VNX_DATA_HOME > default).
    # ensure_engine_on_path is called inside resolve_data_root.
    try:
        data_root = _engine.resolve_data_root(project_dir)
    except Exception as exc:
        print(f"  error: cannot resolve data root: {exc}", file=sys.stderr)
        return 1

    # Derive project_id so the bootstrap seeds the correct pool_config row.
    project_id = _engine.derive_project_id(project_dir)

    # FAIL-CLOSED FIRST GATE (codex round-2 ordering fix): reconcile the tenant BEFORE
    # bootstrap so a split-tenant store is refused with ZERO rows created or seeded —
    # bootstrap seeds pool_config with the CLI-derived id, which must never happen on a
    # store whose marker/env/data-path names a different tenant.
    try:
        _reconcile_tenant_or_fail(data_root, project_id)
    except Exception as exc:
        print(f"\n  error: {exc}", file=sys.stderr)
        return 1

    print(f"Migrating VNX runtime databases at: {data_root}")

    try:
        _bootstrap_runtime_dbs(data_root, project_id=project_id)
    except Exception as exc:
        print(f"\n  error: migration failed: {exc}", file=sys.stderr)
        return 1

    # D4: drive the future-system pipeline (0027→0031 + Horizon + W1) so
    # tracks.horizon actually lands. Without this, vnx migrate stops at v26.
    try:
        _run_future_system_pipeline(data_root, project_id)
    except Exception as exc:
        print(f"\n  error: future-system migration failed: {exc}", file=sys.stderr)
        return 1

    print()
    print("Migration complete.")
    return 0
