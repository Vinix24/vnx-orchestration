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

import os
import re
import sys
from pathlib import Path

from vnx_cli import _engine
from vnx_cli.commands.init_cmd import _bootstrap_runtime_dbs

#: Central store layout ``.../.vnx-data/<pid>/`` — anchors the tenant on the data path.
_DATA_PATH_PID_RE = re.compile(r"(?:^|/)\.vnx-data/([a-z0-9][a-z0-9._-]{0,63})/?$")


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


def _run_future_system_pipeline(data_root: Path, project_id: str) -> None:
    """Drive the WHOLE future-system pipeline against the store at *data_root* (D4).

    The bootstrap chain above only reaches the numbered runners with a paired
    ``apply_NNNN.py`` (≤ v26). The Horizon schema (tracks.horizon, deliverables
    view) plus the ADR-007 dispatches repair, version reconciliation, the 0027→0031
    walk, and W1 tenant-stamping live in ``migrate_future_system.run()``, which was
    NEVER wired into ``vnx migrate`` — leaving pre-Horizon stores missing
    ``tracks.horizon`` (Bug A). This threads the EXACT canonical data root the CLI
    already resolved (the runner would otherwise re-derive
    ``<project_root>/.vnx-data`` — the D4 threading trap) and takes a VACUUM INTO
    backup first. A W1 tenant-stamping failure is non-fatal here: the schema walk is
    committed before W1 runs, so the store keeps ``tracks.horizon`` and the operator
    is warned rather than losing the Horizon schema.
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

    # Anchor the tenant on disk (not via a global env mutation that would leak across
    # a fleet sweep or a shared test process): write the canonical .vnx-project-id
    # marker in the data root when absent, so the fail-closed resolver in the runner
    # resolves THIS store's project_id even for a non-central (XDG/local) layout the
    # DB-path anchor can't cover. Never overwrites an existing marker (reconciled above).
    marker = data_root / ".vnx-project-id"
    if not marker.exists():
        try:
            marker.write_text(project_id + "\n", encoding="utf-8")
        except OSError:
            pass  # advisory — central stores still resolve via the DB-path anchor

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
        tenant_stamp_fatal=False,
        run_tenant_stamp=True,
        backup=True,
    )


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
    project_dir = Path(getattr(args, "project_dir", ".")).resolve()

    # Resolve data root via canonical chain (explicit > VNX_DATA_HOME > XDG).
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
