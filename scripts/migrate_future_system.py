#!/usr/bin/env python3
"""migrate_future_system.py — one-shot idempotent migration to seed the track layer.

Steps:
  1. Apply schemas/migrations/0022_track_layer.sql
  2. Seed 6 tracks from claudedocs/VNX-MASTER-ROADMAP-2026-05-28.md §1 table
  3. Tag existing dispatches by PR-cluster prefix → track FK
  4. Set next_up=1 on first queued track with all dependencies done/active
  5. Emit migration receipt + track_created events into coordination_events

Idempotency: tracks already present by ID are skipped (source_hash in metadata_json).
Re-running is safe and produces no duplicate rows.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Bootstrap sys.path so lib modules resolve regardless of cwd
# ---------------------------------------------------------------------------

_HERE = Path(__file__).resolve().parent
_LIB = _HERE / "lib"
_SCHEMAS = _HERE.parent / "schemas"
_MIGRATIONS = _SCHEMAS / "migrations"

if str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))

from project_root import resolve_project_root, resolve_state_dir
import schema_migration


# ---------------------------------------------------------------------------
# PR-cluster → track-ID mapping (dispatch.tag logic)
# ---------------------------------------------------------------------------

PR_CLUSTER_TO_TRACK: list[tuple[str, str]] = [
    ("PR-HYG-", "track-02"),
    ("PR-DOC-", "track-05"),
    ("PR-OBS-", "track-03"),
    ("PR-ROUTE-", "track-04"),
    ("PR-TMUX-", "track-01"),
    ("PR-LANE-", "track-01"),
    ("PR-START-", "track-02"),
    ("PR-QUAL-", "track-02"),
    ("PR-MON-", "track-05"),
    ("PR-FUT-", "track-06"),
    ("PR-PIP-", "track-02"),
]


def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


def _source_hash(track_id: str, title: str, goal_state: str) -> str:
    content = f"{track_id}|{title}|{goal_state}"
    return hashlib.sha256(content.encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Step 0: PRAGMA pre-flight — guard against schema drift before rebuild
# ---------------------------------------------------------------------------

def _assert_dispatches_schema_intact(conn: sqlite3.Connection) -> None:
    cols = {row[1] for row in conn.execute("PRAGMA table_info('dispatches')")}
    expected = {'id', 'dispatch_id', 'project_id', 'state', 'terminal_id', 'track', 'priority',
                'pr_ref', 'gate', 'attempt_count', 'bundle_path', 'created_at', 'updated_at',
                'expires_after', 'metadata_json'}
    missing = expected - cols
    if missing:
        raise RuntimeError(
            f'dispatches schema missing expected columns: {missing}. Refusing rebuild.'
        )


# Register PRAGMA pre-flight for 0022: any call to apply_script_if_below(22, ...)
# triggers the column assertion, even when invoked outside of run().
schema_migration.register_preflight(22, _assert_dispatches_schema_intact)


# ---------------------------------------------------------------------------
# Step 1: apply migration SQL
# ---------------------------------------------------------------------------

def apply_migration(conn: sqlite3.Connection, project_root: Path) -> None:
    migration_path = _MIGRATIONS / "0022_track_layer.sql"
    if not migration_path.exists():
        raise FileNotFoundError(f"Migration not found: {migration_path}")

    sql = migration_path.read_text(encoding="utf-8")

    current_version = schema_migration.get_user_version(conn)
    if current_version >= 22:
        print(f"  [skip] migration 0022 already applied (user_version={current_version})")
        return

    _assert_dispatches_schema_intact(conn)
    print("  [apply] migration 0022_track_layer.sql ...")
    schema_migration.apply_script_if_below(conn, 22, sql)
    print(f"  [ok]    user_version → {schema_migration.get_user_version(conn)}")


def apply_migration_0023(conn: sqlite3.Connection, project_root: Path) -> None:
    """Apply 0023_dispatches_fk.sql — adds tracks FK after seed+tag (Option A).

    Pre-condition: tracks seeded + orphaned track refs nullified.
    """
    migration_path = _MIGRATIONS / "0023_dispatches_fk.sql"
    if not migration_path.exists():
        raise FileNotFoundError(f"Migration not found: {migration_path}")

    sql = migration_path.read_text(encoding="utf-8")

    current_version = schema_migration.get_user_version(conn)
    if current_version >= 23:
        print(f"  [skip] migration 0023 already applied (user_version={current_version})")
        return

    print("  [apply] migration 0023_dispatches_fk.sql ...")
    schema_migration.apply_script_if_below(conn, 23, sql)
    print(f"  [ok]    user_version → {schema_migration.get_user_version(conn)}")


# ---------------------------------------------------------------------------
# Step 2: parse master-roadmap tracks table
# ---------------------------------------------------------------------------

ROADMAP_FILE = "claudedocs/VNX-MASTER-ROADMAP-2026-05-28.md"

# Track phases derived from roadmap narrative (section "2. Phase — ACTIVE")
_TRACK_PHASES: dict[str, str] = {
    "track-01": "active",
    "track-02": "active",
    "track-03": "queued",
    "track-04": "queued",
    "track-05": "queued",
    "track-06": "parked",
}

_TRACK_SORT_ORDER: dict[str, int] = {
    "track-01": 1,
    "track-02": 2,
    "track-03": 3,
    "track-04": 4,
    "track-05": 5,
    "track-06": 6,
}

_TRACK_PRIORITY: dict[str, str] = {
    "track-01": "high",
    "track-02": "high",
    "track-03": "medium",
    "track-04": "medium",
    "track-05": "medium",
    "track-06": "low",
}

_TRACK_PR_REF: dict[str, str] = {
    "track-01": "PR-TMUX-2,PR-LANE-WIRE-1,PR-LANE-DEFAULT-FLIP",
    "track-02": "PR-HYG-1,PR-HYG-2,PR-HYG-3,PR-HYG-4,PR-DOC-1,PR-DOC-README",
    "track-03": "PR-OBS-1,PR-OBS-3,PR-OBS-4,PR-OBS-5",
    "track-04": "PR-ROUTE-1,PR-ROUTE-2,PR-ROUTE-3",
    "track-05": "PR-MON-1,PR-MON-2,PR-QUAL-1",
    "track-06": "PR-FUT-1,PR-FUT-2,PR-FUT-3",
}


def _strip_md_bold(text: str) -> str:
    return re.sub(r"\*\*(.+?)\*\*", r"\1", text).strip()


def _parse_tracks_from_roadmap(project_root: Path) -> list[dict]:
    roadmap_path = project_root / ROADMAP_FILE
    if not roadmap_path.exists():
        raise FileNotFoundError(
            f"Master-roadmap not found: {roadmap_path}\n"
            "Cannot seed tracks without source of truth. Halting."
        )

    text = roadmap_path.read_text(encoding="utf-8")

    # Find the "Feature-tracks" table in section "1."
    section_match = re.search(
        r"##\s+1\.\s+Feature-tracks.*?(?=\n##\s+\d+\.)",
        text,
        re.DOTALL,
    )
    if not section_match:
        raise ValueError(
            f"Could not find '## 1. Feature-tracks' section in {roadmap_path}. "
            "Roadmap format may have changed. Halting."
        )

    section = section_match.group(0)

    # Extract table rows: | **track-NN** | **Title** | Goal-state |
    row_re = re.compile(
        r"\|\s*\*\*?(track-\d+)\*\*?\s*\|\s*\*\*?(.+?)\*\*?\s*\|\s*(.+?)\s*\|",
        re.MULTILINE,
    )

    tracks = []
    for m in row_re.finditer(section):
        track_id = m.group(1).strip()
        title = _strip_md_bold(m.group(2)).strip()
        goal_state = _strip_md_bold(m.group(3)).strip()

        if not track_id.startswith("track-"):
            continue

        tracks.append({
            "track_id": track_id,
            "title": title,
            "goal_state": goal_state,
            "phase": _TRACK_PHASES.get(track_id, "queued"),
            "sort_order": _TRACK_SORT_ORDER.get(track_id, 99),
            "priority": _TRACK_PRIORITY.get(track_id),
            "pr_ref": _TRACK_PR_REF.get(track_id),
            "project_id": "vnx-dev",
            "source_hash": _source_hash(track_id, title, goal_state),
        })

    if len(tracks) == 0:
        raise ValueError(
            f"Parsed 0 tracks from {roadmap_path}. "
            "Table format may have changed. Halting."
        )

    if len(tracks) < 6:
        raise ValueError(
            f"Expected 6 tracks, parsed {len(tracks)}. "
            "Table may be incomplete. Halting."
        )

    return tracks


def seed_tracks(conn: sqlite3.Connection, tracks: list[dict]) -> list[str]:
    """Insert tracks; skip rows that already exist with same source_hash."""
    inserted: list[str] = []
    skipped: list[str] = []
    now = _now_utc()

    for t in tracks:
        row = conn.execute(
            "SELECT metadata_json FROM tracks WHERE track_id = ?", (t["track_id"],)
        ).fetchone()

        if row:
            try:
                meta = json.loads(row[0] or "{}")
                if meta.get("source_hash") == t["source_hash"]:
                    skipped.append(t["track_id"])
                    continue
                # source_hash differs → update title/goal/pr_ref but preserve phase
                conn.execute(
                    """
                    UPDATE tracks SET title = ?, goal_state = ?, pr_ref = ?,
                        metadata_json = ?
                    WHERE track_id = ?
                    """,
                    (
                        t["title"], t["goal_state"], t.get("pr_ref"),
                        json.dumps({"source_hash": t["source_hash"]}),
                        t["track_id"],
                    ),
                )
                inserted.append(t["track_id"])
                continue
            except (json.JSONDecodeError, TypeError):
                skipped.append(t["track_id"])
                continue

        conn.execute(
            """
            INSERT INTO tracks (
                track_id, title, goal_state, phase, next_up, sort_order, priority,
                pr_ref, project_id, created_at, phase_changed_at, metadata_json
            ) VALUES (?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                t["track_id"],
                t["title"],
                t["goal_state"],
                t["phase"],
                t["sort_order"],
                t.get("priority"),
                t.get("pr_ref"),
                t["project_id"],
                now,
                now,
                json.dumps({"source_hash": t["source_hash"]}),
            ),
        )
        inserted.append(t["track_id"])

    if skipped:
        print(f"  [skip] already seeded: {', '.join(skipped)}")
    if inserted:
        print(f"  [ok]   seeded tracks: {', '.join(inserted)}")

    return inserted


# ---------------------------------------------------------------------------
# Step 3: tag existing dispatches with track FK
# ---------------------------------------------------------------------------

def _nullify_orphaned_track_refs(conn: sqlite3.Connection) -> int:
    """NULL out dispatches.track values that don't match any seeded track.

    Must run after seed_tracks and before apply_migration_0023. Prevents FK
    violations from pre-existing dispatch rows with unknown track values.
    """
    result = conn.execute(
        "UPDATE dispatches SET track = NULL "
        "WHERE track IS NOT NULL AND track NOT IN (SELECT track_id FROM tracks)"
    )
    count = result.rowcount
    if count:
        print(f"  [warn]  nullified {count} orphaned track ref(s) before FK enforcement")
    return count


def tag_dispatches(conn: sqlite3.Connection) -> int:
    """Update dispatches.track based on PR-cluster prefix matching."""
    try:
        rows = conn.execute(
            "SELECT dispatch_id, pr_ref, metadata_json FROM dispatches "
            "WHERE track IS NULL OR track = ''"
        ).fetchall()
    except sqlite3.OperationalError:
        return 0

    tagged = 0
    for row in rows:
        dispatch_id = row[0]
        pr_ref = row[1] or ""

        # Also check metadata_json for feature or pr keys
        try:
            meta = json.loads(row[2] or "{}")
            meta_pr = meta.get("pr_ref") or meta.get("pr") or meta.get("feature") or ""
        except (json.JSONDecodeError, TypeError):
            meta_pr = ""

        candidate = pr_ref or meta_pr

        for prefix, track_id in PR_CLUSTER_TO_TRACK:
            if candidate.upper().startswith(prefix.upper()):
                conn.execute(
                    "UPDATE dispatches SET track = ? WHERE dispatch_id = ?",
                    (track_id, dispatch_id),
                )
                tagged += 1
                break

    if tagged:
        print(f"  [ok]   tagged {tagged} dispatch(es) with track FK")
    return tagged


# ---------------------------------------------------------------------------
# Step 4: set next_up=1
# ---------------------------------------------------------------------------

def set_initial_next_up(conn: sqlite3.Connection) -> None:
    """Set next_up=1 on the first queued track whose dependencies are all done/active."""
    # Clear stale next_up flags
    conn.execute("UPDATE tracks SET next_up = 0 WHERE next_up = 1")

    # Get all queued tracks ordered by sort_order
    queued = conn.execute(
        "SELECT track_id FROM tracks WHERE phase = 'queued' AND project_id = 'vnx-dev' "
        "ORDER BY sort_order ASC, track_id ASC"
    ).fetchall()

    for (track_id,) in queued:
        deps = conn.execute(
            "SELECT to_track_id FROM track_dependencies WHERE from_track_id = ? AND kind = 'hard'",
            (track_id,),
        ).fetchall()

        all_satisfied = True
        for (dep_id,) in deps:
            dep_row = conn.execute("SELECT phase FROM tracks WHERE track_id = ?", (dep_id,)).fetchone()
            if not dep_row or dep_row[0] not in ("done", "active"):
                all_satisfied = False
                break

        if all_satisfied:
            conn.execute("UPDATE tracks SET next_up = 1 WHERE track_id = ?", (track_id,))
            print(f"  [ok]   next_up=1 on {track_id}")
            break


# ---------------------------------------------------------------------------
# Step 5: emit coordination events
# ---------------------------------------------------------------------------

def emit_events(conn: sqlite3.Connection, inserted_track_ids: list[str]) -> None:
    """Emit track_created events for each newly seeded track."""
    has_project_id = any(
        row[1] == "project_id"
        for row in conn.execute("PRAGMA table_info(coordination_events)").fetchall()
    )

    now = _now_utc()
    for track_id in inserted_track_ids:
        event_id = str(uuid.uuid4())
        if has_project_id:
            conn.execute(
                """
                INSERT INTO coordination_events
                    (event_id, event_type, entity_type, entity_id,
                     actor, metadata_json, occurred_at, project_id)
                VALUES (?, 'track_created', 'track', ?, 'system', '{}', ?, 'vnx-dev')
                """,
                (event_id, track_id, now),
            )
        else:
            conn.execute(
                """
                INSERT INTO coordination_events
                    (event_id, event_type, entity_type, entity_id,
                     actor, metadata_json, occurred_at)
                VALUES (?, 'track_created', 'track', ?, 'system', '{}', ?)
                """,
                (event_id, track_id, now),
            )

    if inserted_track_ids:
        print(f"  [ok]   emitted {len(inserted_track_ids)} track_created event(s)")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run(project_root: Path | None = None) -> None:
    if project_root is None:
        project_root = resolve_project_root(__file__)

    state_dir = project_root / ".vnx-data" / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    db_path = state_dir / "runtime_coordination.db"

    if not db_path.exists():
        raise FileNotFoundError(
            f"runtime_coordination.db not found at {db_path}\n"
            "Run `vnx init` or initialize the schema first."
        )

    print(f"\nVNX migrate_future_system — db: {db_path}")

    conn = sqlite3.connect(str(db_path), timeout=30.0)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")

    try:
        # Step 0: assert schema is intact before any rebuild
        _assert_dispatches_schema_intact(conn)

        # Step 1: apply 0022 — creates track tables; dispatches rebuilt WITHOUT track FK
        apply_migration(conn, project_root)
        conn.commit()

        # Step 2: parse roadmap and seed tracks
        tracks = _parse_tracks_from_roadmap(project_root)
        print(f"\n  Parsed {len(tracks)} track(s) from roadmap")

        inserted_ids = seed_tracks(conn, tracks)

        # Step 3: tag existing dispatches by PR-cluster prefix
        tagged = tag_dispatches(conn)

        # Step 3b: nullify orphaned track refs that weren't mapped (FK safety for 0023)
        _nullify_orphaned_track_refs(conn)

        # emit_events BEFORE commit — if emit fails, seed rolls back (ADR-005)
        emit_events(conn, inserted_ids)
        conn.commit()

        # Step 4: apply 0023 — adds dispatches.track FK now that tracks are seeded
        apply_migration_0023(conn, project_root)
        conn.commit()

        # Step 5
        set_initial_next_up(conn)
        conn.commit()
        print(f"\n  Migration complete. {len(tracks)} track(s) in DB.\n")

    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    try:
        run()
    except Exception as exc:
        print(f"\n  [ERROR] {exc}", file=sys.stderr)
        sys.exit(1)
