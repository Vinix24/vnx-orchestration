#!/usr/bin/env python3
"""Generate PR_QUEUE.md from ROADMAP.yaml.

Reads features[].pr_queue entries from ROADMAP.yaml and materializes
a status view grouped by outcome (merged, queued, planned).

Usage:
    python3 scripts/build_pr_queue.py [--output PATH] [--dry-run] [--check]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_AUTOGEN_HEADER = "<!-- AUTO-GENERATED — DO NOT EDIT — see scripts/build_pr_queue.py -->"


def load_roadmap() -> dict:
    try:
        import yaml  # type: ignore[import]
    except ImportError:
        print("ERROR: PyYAML required — pip install pyyaml", file=sys.stderr)
        sys.exit(1)
    path = _REPO_ROOT / "ROADMAP.yaml"
    with path.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def _build_output(data: dict) -> str:
    launch = data.get("launch_state") or {}
    features: list[dict] = data.get("features") or []

    merged: list[tuple[str, str, str]] = []
    queued: list[tuple[str, str, str, str]] = []

    for feat in features:
        for pr in feat.get("pr_queue") or []:
            pr_id = pr.get("pr_id", "")
            title = pr.get("title", "")
            risk = pr.get("risk_class", "")
            status = pr.get("status", "")
            if status == "merged":
                merged.append((pr_id, title, feat.get("feature_id", "")))
            elif status in ("queued", "in_progress"):
                queued.append((pr_id, title, risk, feat.get("feature_id", "")))

    lines: list[str] = [
        _AUTOGEN_HEADER,
        "",
        "# PR Queue (DERIVED VIEW — generated example)",
        "",
        "> Generated from the repo-root `ROADMAP.yaml` (`launch_state` + `features[].pr_queue`),",
        "> which is a generic example of the roadmap format — the live roadmap is the",
        "> maintainer's tracks database (`vnx objective`). Live PR state: `gh pr list`.",
        "> Do not hand-edit this file.",
        "",
        "## Progress Overview",
        f"Launch status: **{launch.get('status', 'unknown')}** (version {launch.get('version', '?')})",
        f"Last verified: {launch.get('last_verified', '?')} against {launch.get('verified_against', '?')}",
        f"Merged launch PRs: {len(merged)} | Queued: {len(queued)}",
        "",
        "## Status",
        "",
    ]

    if merged:
        lines.append("### Merged")
        for pr_id, title, feat_id in merged:
            lines.append(f"- {pr_id} — {title} [feature={feat_id}]")
        lines.append("")

    if queued:
        lines.append("### Queued / In Progress")
        for pr_id, title, risk, feat_id in queued:
            lines.append(f"- {pr_id} — {title} [risk={risk}, feature={feat_id}]")
        lines.append("")

    if not merged and not queued:
        lines.append("_No PRs in queue._")
        lines.append("")

    blockers = launch.get("launch_blockers") or []
    if blockers:
        lines.append("## Remaining Launch Blockers")
        for b in blockers:
            lines.append(f"- {b}")
        lines.append("")

    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate PR_QUEUE.md from ROADMAP.yaml.")
    parser.add_argument("--output", default=None, help="Output path (default: PR_QUEUE.md)")
    parser.add_argument("--dry-run", action="store_true", help="Print to stdout, do not write")
    parser.add_argument("--check", action="store_true", help="Fail if committed file is stale")
    args = parser.parse_args()

    data = load_roadmap()
    output = _build_output(data)

    out_path = Path(args.output) if args.output else _REPO_ROOT / "PR_QUEUE.md"

    if args.dry_run:
        print(output)
        return

    if args.check:
        if not out_path.exists():
            print(f"FAIL: {out_path} does not exist", file=sys.stderr)
            sys.exit(1)
        committed = out_path.read_text(encoding="utf-8")
        if committed != output:
            print(f"FAIL: {out_path} is stale — re-run scripts/build_pr_queue.py", file=sys.stderr)
            sys.exit(1)
        print(f"OK: {out_path} is current")
        return

    out_path.write_text(output, encoding="utf-8")
    print(f"Written: {out_path}")


if __name__ == "__main__":
    main()
