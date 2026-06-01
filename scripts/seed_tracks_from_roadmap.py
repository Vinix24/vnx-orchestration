#!/usr/bin/env python3
"""seed_tracks_from_roadmap.py — project authored ROADMAP.yaml into the tracks DB.

One-directional projection: ROADMAP.yaml `features[]` (the SINGLE authored
planning surface) -> `tracks` rows via the tracks.py DAL. Phase 1 of the
planning layer (NO-NODE model): TRACK = ROADMAP feature, one row per feature.

Idempotent + dry-run default:
  python3 scripts/seed_tracks_from_roadmap.py            # DRY-RUN — prints plan, no writes
  python3 scripts/seed_tracks_from_roadmap.py --apply    # writes rows + emits track events
  python3 scripts/seed_tracks_from_roadmap.py --apply --project-id vnx-dev
  python3 scripts/seed_tracks_from_roadmap.py --roadmap path/to/ROADMAP.yaml --state-dir /tmp/s

Drift handling (load-bearing):
  - row absent           -> created (in --apply)
  - row present, equal   -> unchanged (no-op)
  - row present, differs -> updated (authored fields only; NEVER overwrites phase)
  - row present, status<>phase drift -> reported (phase_drift), NOT auto-resolved
  - row in DB, gone from ROADMAP -> reported as orphan (never auto-deleted)

Mapping (ROADMAP feature -> tracks row):
  feature_id      -> track_id          (stable identity)
  title           -> title
  notes/synth     -> goal_state         (synthesize "<feature_id> done" if absent)
  status          -> phase              (declared; create-only, see below)
  milestone+order -> sort_order
  risk_class      -> priority           (high->P1, else P2)
  pr_queue        -> pr_ref + metadata_json.pr_queue
  depends_on      -> track_dependencies (kind=hard, derivation_source=manual)
  milestone       -> horizon            (1.0->now/next, 1.x->later)

The seeder NEVER writes ROADMAP.yaml and NEVER runs --apply against the live DB
on your behalf. Tests drive it against a temp DB + sample ROADMAP.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Optional

import yaml

# ---------------------------------------------------------------------------
# Bootstrap sys.path so lib modules resolve regardless of cwd
# ---------------------------------------------------------------------------

_HERE = Path(__file__).resolve().parent
_LIB = _HERE / "lib"
if str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))

import tracks as tracks_lib  # noqa: E402

# ROADMAP status -> declared phase. shipped-dark counts as done (it shipped;
# "dark" is a runtime-exposure flag, not a planning phase).
_STATUS_TO_PHASE = {
    "done": "done",
    "shipped-dark": "done",
    "in-progress": "active",
    "in_progress": "active",
    "active": "active",
    "planned": "queued",
    "queued": "queued",
    "blocked": "parked",
    "parked": "parked",
}

# Milestone rank for stable sort_order (lower = earlier).
_MILESTONE_RANK = {
    "1.0": 0,
    "1.0.1": 1,
    "1.1": 2,
    "1.2": 3,
    "1.x": 9,
}


def _milestone_rank(milestone: Optional[str]) -> int:
    return _MILESTONE_RANK.get(str(milestone), 5)


def _horizon_for(milestone: Optional[str], status: Optional[str]) -> str:
    """Derive the strategic horizon band from milestone + status.

    1.0 features are the launch focus -> 'now' if not yet done, else 'next'.
    1.0.x / 1.1 -> 'next'. 1.x and anything later -> 'later'.
    """
    m = str(milestone)
    phase = _STATUS_TO_PHASE.get(str(status), "queued")
    if m == "1.0":
        return "next" if phase == "done" else "now"
    if m in ("1.0.1", "1.1", "1.2"):
        return "next"
    return "later"


def _priority_for(risk_class: Optional[str]) -> str:
    return "P1" if str(risk_class).lower() == "high" else "P2"


def _goal_state_for(feature: dict[str, Any]) -> str:
    """Synthesize goal_state if the feature has no usable notes.

    Tolerates the 0022 NOT NULL vs 0024 nullable goal_state divergence by always
    producing a non-empty string.
    """
    notes = feature.get("notes")
    if isinstance(notes, str) and notes.strip():
        return notes.strip().splitlines()[0].strip()
    return f"{feature.get('feature_id', 'feature')} done"


def _pr_ref_for(feature: dict[str, Any]) -> Optional[str]:
    """Pick a representative pr_ref: first open (non-merged) else last in queue."""
    queue = feature.get("pr_queue") or []
    if not isinstance(queue, list) or not queue:
        return None
    for item in queue:
        if isinstance(item, dict) and item.get("status") not in ("merged", "closed"):
            return item.get("pr_id")
    last = queue[-1]
    return last.get("pr_id") if isinstance(last, dict) else None


def _metadata_for(feature: dict[str, Any]) -> str:
    queue = feature.get("pr_queue") or []
    meta: dict[str, Any] = {
        "milestone": feature.get("milestone"),
        "roadmap_status": feature.get("status"),
        "risk_class": feature.get("risk_class"),
    }
    if isinstance(queue, list) and queue:
        meta["pr_queue"] = [
            item.get("pr_id") for item in queue if isinstance(item, dict) and item.get("pr_id")
        ]
    return json.dumps(meta, sort_keys=True)


def _desired_row(feature: dict[str, Any], index: int) -> dict[str, Any]:
    """Build the authored-derived field set for one ROADMAP feature."""
    feature_id = feature.get("feature_id")
    milestone = feature.get("milestone")
    status = feature.get("status")
    return {
        "track_id": feature_id,
        "title": feature.get("title") or feature_id,
        "goal_state": _goal_state_for(feature),
        "phase": _STATUS_TO_PHASE.get(str(status), "queued"),
        "priority": _priority_for(feature.get("risk_class")),
        "sort_order": _milestone_rank(milestone) * 1000 + index,
        "horizon": _horizon_for(milestone, status),
        "pr_ref": _pr_ref_for(feature),
        "metadata_json": _metadata_for(feature),
        "depends_on": list(feature.get("depends_on") or []),
    }


def _row_matches(existing: dict[str, Any], desired: dict[str, Any]) -> bool:
    """True if the existing track already reflects the authored fields.

    Compares only the authored-derived fields (NOT phase — phase drift is
    reported separately, never auto-synced). Also deliberately excludes
    `depends_on`: dependencies live in a separate table and are always
    reconciled unconditionally (via _seed_dependencies), even for rows that
    are otherwise unchanged. This prevents dependency-only changes from being
    silently ignored.
    """
    for col in ("title", "goal_state", "priority", "sort_order", "horizon", "pr_ref", "metadata_json"):
        if (existing.get(col) or None) != (desired.get(col) or None):
            return False
    return True


def _load_roadmap(roadmap_path: Path) -> list[dict[str, Any]]:
    data = yaml.safe_load(roadmap_path.read_text(encoding="utf-8")) or {}
    features = data.get("features") or []
    if not isinstance(features, list):
        raise ValueError(f"ROADMAP.yaml `features` is not a list in {roadmap_path}")
    return [f for f in features if isinstance(f, dict) and f.get("feature_id")]


def seed(
    state_dir: str | Path,
    roadmap_path: str | Path,
    project_id: str,
    *,
    apply: bool = False,
) -> dict[str, Any]:
    """Run the seed. Returns a drift report dict (counts + per-track records).

    Pure projection: when apply=False, nothing is written.
    """
    roadmap_path = Path(roadmap_path)
    features = _load_roadmap(roadmap_path)

    report: dict[str, Any] = {
        "apply": apply,
        "project_id": project_id,
        "roadmap_path": str(roadmap_path),
        "created": [],
        "updated": [],
        "unchanged": [],
        "phase_drift": [],
        "orphan": [],
    }

    seen_ids: set[str] = set()

    for index, feature in enumerate(features):
        desired = _desired_row(feature, index)
        track_id = desired["track_id"]
        seen_ids.add(track_id)

        existing = tracks_lib.get_track(state_dir, track_id, project_id)

        if existing is None:
            if apply:
                tracks_lib.create_track(
                    state_dir,
                    track_id,
                    project_id,
                    desired["title"],
                    desired["goal_state"],
                    phase=desired["phase"],
                    sort_order=desired["sort_order"],
                    priority=desired["priority"],
                    pr_ref=desired["pr_ref"],
                    metadata_json=desired["metadata_json"],
                    horizon=desired["horizon"],
                )
                _seed_dependencies(state_dir, track_id, project_id, desired["depends_on"])
            report["created"].append(track_id)
            continue

        # Existing row: detect phase drift (declared status divergence) separately.
        if existing.get("phase") != desired["phase"]:
            report["phase_drift"].append({
                "track_id": track_id,
                "db_phase": existing.get("phase"),
                "roadmap_phase": desired["phase"],
            })

        if _row_matches(existing, desired):
            if apply:
                _seed_dependencies(state_dir, track_id, project_id, desired["depends_on"])
            report["unchanged"].append(track_id)
            continue

        if apply:
            tracks_lib.update_authored_fields(
                state_dir,
                track_id,
                project_id,
                title=desired["title"],
                goal_state=desired["goal_state"],
                priority=desired["priority"],
                sort_order=desired["sort_order"],
                horizon=desired["horizon"],
                pr_ref=desired["pr_ref"],
                metadata_json=desired["metadata_json"],
            )
            _seed_dependencies(state_dir, track_id, project_id, desired["depends_on"])
        report["updated"].append(track_id)

    # Orphans: in DB but no longer authored in ROADMAP.
    for row in tracks_lib.list_tracks(state_dir, project_id):
        if row["track_id"] not in seen_ids:
            report["orphan"].append(row["track_id"])

    report["summary"] = {
        "created": len(report["created"]),
        "updated": len(report["updated"]),
        "unchanged": len(report["unchanged"]),
        "phase_drift": len(report["phase_drift"]),
        "orphan": len(report["orphan"]),
    }
    return report


def _seed_dependencies(
    state_dir: str | Path,
    track_id: str,
    project_id: str,
    depends_on: list[str],
) -> None:
    """Add manual hard-dependency edges. INSERT OR REPLACE makes this idempotent.

    Skips dependency targets not yet present in the tracks table (forward refs
    are tolerated — the seeder is single-pass and the FK requires the target row).
    """
    for dep_id in depends_on:
        if tracks_lib.get_track(state_dir, dep_id, project_id) is None:
            continue
        tracks_lib.add_dependency(
            state_dir,
            track_id,
            project_id,
            dep_id,
            project_id,
            kind="hard",
            derivation_source="manual",
            confidence=1.0,
        )


def _print_report(report: dict[str, Any]) -> None:
    mode = "APPLY" if report["apply"] else "DRY-RUN (no writes)"
    s = report["summary"]
    print(f"\nVNX seed_tracks_from_roadmap — {mode}")
    print(f"  project_id : {report['project_id']}")
    print(f"  roadmap    : {report['roadmap_path']}")
    print(
        f"  created={s['created']}  updated={s['updated']}  unchanged={s['unchanged']}"
        f"  phase_drift={s['phase_drift']}  orphan={s['orphan']}"
    )
    if report["created"]:
        print(f"  + created : {', '.join(report['created'])}")
    if report["updated"]:
        print(f"  ~ updated : {', '.join(report['updated'])}")
    if report["phase_drift"]:
        print("  ! phase drift (declared status differs — reported, NOT auto-synced):")
        for d in report["phase_drift"]:
            print(f"      {d['track_id']}: db={d['db_phase']} roadmap={d['roadmap_phase']}")
    if report["orphan"]:
        print(f"  ? orphan (in DB, gone from ROADMAP — not deleted): {', '.join(report['orphan'])}")
    if not report["apply"]:
        print("  (dry-run: re-run with --apply to write)")
    print()


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Seed tracks from authored ROADMAP.yaml (dry-run default).")
    parser.add_argument("--apply", action="store_true", help="write rows (default: dry-run, no writes)")
    parser.add_argument("--project-id", default=os.environ.get("VNX_PROJECT_ID", "vnx-dev"))
    parser.add_argument("--roadmap", default=None, help="path to ROADMAP.yaml (default: project root)")
    parser.add_argument("--state-dir", default=os.environ.get("VNX_STATE_DIR", ""),
                        help="state dir containing runtime_coordination.db")
    parser.add_argument("--report", default=None, help="write the JSON drift report to this path")
    args = parser.parse_args(argv)

    if args.state_dir:
        state_dir = Path(args.state_dir)
    else:
        from project_root import resolve_project_root
        state_dir = resolve_project_root(__file__) / ".vnx-data" / "state"

    if args.roadmap:
        roadmap_path = Path(args.roadmap)
    else:
        from project_root import resolve_project_root
        roadmap_path = resolve_project_root(__file__) / "ROADMAP.yaml"

    report = seed(state_dir, roadmap_path, args.project_id, apply=args.apply)
    _print_report(report)

    if args.report:
        Path(args.report).write_text(json.dumps(report, indent=2), encoding="utf-8")

    return 0


if __name__ == "__main__":
    sys.exit(main())
