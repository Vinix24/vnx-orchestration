#!/usr/bin/env python3
"""vnx_snapshot.py — VNX project-state snapshot, restore, and quiesce-check."""

from __future__ import annotations

import argparse
import sqlite3
import subprocess
import sys
import tarfile
import time
import warnings
from datetime import datetime, timezone
from pathlib import Path


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _snapshots_dir() -> Path:
    d = Path.home() / "vnx-snapshots"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _project_slug(project: Path) -> str:
    return project.resolve().name.lower().replace(" ", "-")


def _tarball_stem(tarball: Path) -> str:
    name = tarball.name
    if name.endswith(".tar.gz"):
        return name[: -len(".tar.gz")]
    return tarball.stem


# ---------------------------------------------------------------------------
# snapshot
# ---------------------------------------------------------------------------

def do_snapshot(project_path: str) -> int:
    project = Path(project_path).resolve()
    vnx_data = project / ".vnx-data"
    if not vnx_data.is_dir():
        print(f"ERROR: .vnx-data not found at {vnx_data}", file=sys.stderr)
        return 1

    slug = _project_slug(project)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    out_dir = _snapshots_dir()
    tarball = out_dir / f"{slug}-{ts}.tar.gz"

    with tarfile.open(tarball, "w:gz") as tf:
        tf.add(vnx_data, arcname=".vnx-data")
    print(f"snapshot: {tarball}")

    db_path = vnx_data / "state" / "runtime_coordination.db"
    if db_path.is_file():
        sql_file = out_dir / f"{slug}-runtime-{ts}.sql"
        con = sqlite3.connect(str(db_path))
        try:
            with open(sql_file, "w", encoding="utf-8") as fh:
                for line in con.iterdump():
                    fh.write(line + "\n")
        finally:
            con.close()
        print(f"db-dump:  {sql_file}")

    return 0


# ---------------------------------------------------------------------------
# restore
# ---------------------------------------------------------------------------

def do_restore(tarball_str: str, target_str: str | None, force: bool) -> int:
    tarball = Path(tarball_str).resolve()
    if not tarball.is_file():
        print(f"ERROR: tarball not found: {tarball}", file=sys.stderr)
        return 1

    if not tarfile.is_tarfile(str(tarball)):
        print(f"ERROR: not a valid tar archive: {tarball}", file=sys.stderr)
        return 1

    with tarfile.open(tarball, "r:gz") as tf:
        names = tf.getnames()

    has_vnx_data = any(n == ".vnx-data" or n.startswith(".vnx-data/") for n in names)
    if not has_vnx_data:
        print("ERROR: tarball does not contain .vnx-data/ at root", file=sys.stderr)
        return 1

    target = Path(target_str).resolve() if target_str else tarball.parent
    existing = target / ".vnx-data"

    if existing.is_dir() and not force:
        if sys.stdin.isatty():
            resp = input(f".vnx-data already exists at {target}. Overwrite? [y/N] ").strip().lower()
            if resp != "y":
                print("Aborted.")
                return 1
        else:
            print(
                f"ERROR: .vnx-data already exists at {target}. Use --force to overwrite.",
                file=sys.stderr,
            )
            return 1
        import shutil
        shutil.rmtree(existing)
    elif existing.is_dir() and force:
        import shutil
        shutil.rmtree(existing)

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        with tarfile.open(tarball, "r:gz") as tf:
            tf.extractall(str(target))
    print(f"restored: {existing}")

    # Offer to restore companion SQL dump
    stem = _tarball_stem(tarball)
    # timestamp = last 15 chars of stem (YYYYMMDD-HHMMSS)
    ts_suffix = stem[-15:] if len(stem) >= 15 else ""
    if ts_suffix:
        candidates = sorted(tarball.parent.glob("*.sql"))
        candidates = [s for s in candidates if ts_suffix in s.name]
        if candidates:
            sql_file = candidates[0]
            if sys.stdin.isatty():
                resp = input(f"Restore runtime DB from {sql_file.name}? [y/N] ").strip().lower()
                restore_db = resp == "y"
            else:
                restore_db = False
            if restore_db:
                db_target = existing / "state" / "runtime_coordination.db"
                _restore_sqlite_from_sql(sql_file, db_target)
                print(f"db-restored: {db_target}")

    return 0


def _restore_sqlite_from_sql(sql_file: Path, db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if db_path.is_file():
        db_path.unlink()
    con = sqlite3.connect(str(db_path))
    try:
        sql = sql_file.read_text(encoding="utf-8")
        con.executescript(sql)
        con.commit()
    finally:
        con.close()


# ---------------------------------------------------------------------------
# quiesce-check  (read-only — never mutates state)
# ---------------------------------------------------------------------------

def do_quiesce_check(project_path: str) -> int:
    project = Path(project_path).resolve()
    vnx_data = project / ".vnx-data"
    failures: list[str] = []

    # 1. active dispatches — empty OR all older than 1 hour
    active_dir = vnx_data / "dispatches" / "active"
    if active_dir.is_dir():
        now = time.time()
        recent = [
            f for f in active_dir.glob("*.md")
            if (now - f.stat().st_mtime) < 3600
        ]
        if recent:
            failures.append(
                f"active-dispatches: {len(recent)} dispatch(es) younger than 1h"
            )

    # 2. held leases in runtime_coordination.db
    db_path = vnx_data / "state" / "runtime_coordination.db"
    if db_path.is_file():
        try:
            con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            try:
                cur = con.execute(
                    "SELECT COUNT(*) FROM terminal_leases WHERE state='leased'"
                )
                count = cur.fetchone()[0]
                if count > 0:
                    failures.append(f"held-leases: {count} terminal(s) in leased state")
            except sqlite3.OperationalError:
                pass  # table doesn't exist in fresh DB — not a failure
            finally:
                con.close()
        except Exception as exc:
            failures.append(f"db-error: cannot read runtime_coordination.db: {exc}")

    # 3. in-flight gates (request without result)
    requests_dir = vnx_data / "state" / "review_gates" / "requests"
    results_dir = vnx_data / "state" / "review_gates" / "results"
    if requests_dir.is_dir():
        request_ids = {f.stem for f in requests_dir.glob("*.json")}
        result_ids = {f.stem for f in results_dir.glob("*.json")} if results_dir.is_dir() else set()
        pending = request_ids - result_ids
        if pending:
            failures.append(
                f"in-flight-gates: {len(pending)} gate(s) without result: "
                + ", ".join(sorted(pending))
            )

    # 4. uncommitted git changes
    git_result = subprocess.run(
        ["git", "-C", str(project), "status", "--porcelain"],
        capture_output=True,
        text=True,
    )
    if git_result.returncode == 0 and git_result.stdout.strip():
        failures.append("uncommitted-changes: worktree has uncommitted modifications")

    if failures:
        for reason in failures:
            print(f"NOT QUIESCENT: {reason}")
        return 1

    print("QUIESCENT: project is safe to migrate")
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        prog="vnx_snapshot",
        description="VNX project-state snapshot/restore/quiesce tools",
    )
    sub = parser.add_subparsers(dest="command")

    p_snap = sub.add_parser("snapshot", help="Snapshot .vnx-data to ~/vnx-snapshots/")
    p_snap.add_argument(
        "project_path", nargs="?", default=".", help="Project path (default: cwd)"
    )

    p_restore = sub.add_parser("restore", help="Restore .vnx-data from tarball")
    p_restore.add_argument("tarball", help="Path to .tar.gz snapshot tarball")
    p_restore.add_argument("--target", help="Target directory (default: tarball parent)")
    p_restore.add_argument("--force", action="store_true", help="Overwrite without prompting")

    p_quiesce = sub.add_parser("quiesce-check", help="Check if project is safe to migrate")
    p_quiesce.add_argument(
        "project_path", nargs="?", default=".", help="Project path (default: cwd)"
    )

    args = parser.parse_args()

    if args.command == "snapshot":
        return do_snapshot(args.project_path)
    if args.command == "restore":
        return do_restore(args.tarball, args.target, args.force)
    if args.command == "quiesce-check":
        return do_quiesce_check(args.project_path)

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
