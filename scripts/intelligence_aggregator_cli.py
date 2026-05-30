#!/usr/bin/env python3
"""CLI for cross-project intelligence aggregation — Wave 5 PR-5.4.

Usage:
    # Mine global patterns across registered projects:
    python3 scripts/intelligence_aggregator_cli.py mine --projects vnx-dev,project-a,mc

    # Get recommendations for a project:
    python3 scripts/intelligence_aggregator_cli.py recommend --target vnx-dev

    # Export global facet as JSON:
    python3 scripts/intelligence_aggregator_cli.py export --output .vnx-data/aggregator/global_facet.json

Project DB paths are resolved via VNX_AGGREGATOR_DB_DIR (default: ~/.vnx-aggregator/projects/)
with the layout: <dir>/<project_id>/quality_intelligence.db.
An explicit mapping can be provided via --db-map 'vnx-dev:/path/to/db,...'.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

# Allow running from project root without installation.
_SCRIPTS_LIB = Path(__file__).resolve().parent / "lib"
if str(_SCRIPTS_LIB) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_LIB))

from intelligence_aggregator import IntelligenceAggregator  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger(__name__)

_DEFAULT_AGGREGATOR_DIR = Path.home() / ".vnx-aggregator" / "projects"


def _resolve_db_paths(
    project_ids: list[str],
    db_map_str: str | None,
    base_dir: Path,
) -> dict[str, Path]:
    """Build {project_id: db_path} mapping.

    Precedence: --db-map entries > base_dir/<project_id>/quality_intelligence.db.
    """
    explicit: dict[str, Path] = {}
    if db_map_str:
        for entry in db_map_str.split(","):
            entry = entry.strip()
            if ":" not in entry:
                log.error("Invalid --db-map entry (expected 'id:/path'): %r", entry)
                sys.exit(1)
            pid, path = entry.split(":", 1)
            explicit[pid.strip()] = Path(path.strip()).expanduser()

    result: dict[str, Path] = {}
    for pid in project_ids:
        if pid in explicit:
            result[pid] = explicit[pid]
        else:
            result[pid] = base_dir / pid / "quality_intelligence.db"
    return result


def _parse_project_list(raw: str) -> list[str]:
    return [p.strip() for p in raw.split(",") if p.strip()]


def cmd_mine(args: argparse.Namespace) -> int:
    projects = _parse_project_list(args.projects)
    if not projects:
        log.error("--projects must name at least one project ID")
        return 1
    base_dir = Path(os.environ.get("VNX_AGGREGATOR_DB_DIR", str(_DEFAULT_AGGREGATOR_DIR)))
    db_paths = _resolve_db_paths(projects, args.db_map, base_dir)
    agg = IntelligenceAggregator(db_paths)
    patterns = agg.mine_global_patterns(
        min_projects=args.min_projects,
        min_confidence=args.min_confidence,
    )
    if not patterns:
        print("No global patterns found matching criteria.")
        return 0
    print(f"Found {len(patterns)} global pattern(s):\n")
    for p in patterns:
        proj_list = ", ".join(
            f"{pid}({cnt})" for pid, cnt in sorted(p.occurrences.items())
        )
        print(
            f"  [{p.pattern_id[:12]}] {p.pattern_family!r}\n"
            f"    total={p.total_occurrences}  conf={p.confidence:.3f}"
            f"  projects={proj_list}\n"
        )
    return 0


def cmd_recommend(args: argparse.Namespace) -> int:
    target = args.target.strip()
    if not target:
        log.error("--target must specify a project ID")
        return 1
    projects_raw = args.projects or target
    projects = _parse_project_list(projects_raw)
    if target not in projects:
        projects.append(target)
    base_dir = Path(os.environ.get("VNX_AGGREGATOR_DB_DIR", str(_DEFAULT_AGGREGATOR_DIR)))
    db_paths = _resolve_db_paths(projects, args.db_map, base_dir)
    agg = IntelligenceAggregator(db_paths)
    recs = agg.recommend_cross_project(target, max_recommendations=args.max)
    if not recs:
        print(f"No cross-project recommendations for '{target}'.")
        return 0
    print(f"Cross-project recommendations for '{target}' ({len(recs)}):\n")
    for r in recs:
        print(
            f"  [{r.pattern_id[:12]}] from={r.source_project}  conf={r.confidence:.3f}\n"
            f"    {r.rationale}\n"
        )
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    projects_raw = args.projects or ""
    projects = _parse_project_list(projects_raw)
    if not projects:
        log.error("--projects must name at least one project ID")
        return 1
    output = Path(args.output).expanduser()
    base_dir = Path(os.environ.get("VNX_AGGREGATOR_DB_DIR", str(_DEFAULT_AGGREGATOR_DIR)))
    db_paths = _resolve_db_paths(projects, args.db_map, base_dir)
    agg = IntelligenceAggregator(db_paths)
    agg.export_global_facet(output)
    print(f"Global facet exported to: {output}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="intelligence_aggregator_cli",
        description="Cross-project intelligence aggregation — VNX Wave 5 PR-5.4",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # mine
    mine_p = sub.add_parser("mine", help="Mine global patterns across projects")
    mine_p.add_argument("--projects", required=True, help="Comma-separated project IDs")
    mine_p.add_argument("--db-map", default=None, help="Explicit 'id:/path,...' overrides")
    mine_p.add_argument("--min-projects", type=int, default=2)
    mine_p.add_argument("--min-confidence", type=float, default=0.6)

    # recommend
    rec_p = sub.add_parser("recommend", help="Get recommendations for a target project")
    rec_p.add_argument("--target", required=True, help="Target project ID")
    rec_p.add_argument("--projects", default=None, help="Source project IDs (comma-separated)")
    rec_p.add_argument("--db-map", default=None)
    rec_p.add_argument("--max", type=int, default=5)

    # export
    exp_p = sub.add_parser("export", help="Export global facet as JSON")
    exp_p.add_argument("--projects", required=True)
    exp_p.add_argument("--output", required=True, help="Output JSON path")
    exp_p.add_argument("--db-map", default=None)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    dispatch = {"mine": cmd_mine, "recommend": cmd_recommend, "export": cmd_export}
    handler = dispatch.get(args.command)
    if handler is None:
        parser.print_help()
        return 1
    return handler(args)


if __name__ == "__main__":
    sys.exit(main())
