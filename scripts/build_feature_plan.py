#!/usr/bin/env python3
"""Generate FEATURE_PLAN.md from dispatch_register.ndjson + gh pr list + ROADMAP.yaml.

Sources (in priority order):
  1. dispatch_register.ndjson  — canonical feature/PR lifecycle events
  2. gh pr list --state merged — supplementary merged-PR evidence
  3. ROADMAP.yaml              — planned features not yet in register

Usage:
    python3 scripts/build_feature_plan.py [--output PATH] [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

_REPO_ROOT = Path(__file__).resolve().parents[1]
_AUTOGEN_HEADER = "<!-- AUTO-GENERATED — DO NOT EDIT — see scripts/build_feature_plan.py -->"
_FEATURE_RE = re.compile(r"\bF(\d+)\b", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Source readers
# ---------------------------------------------------------------------------

def _register_path() -> Path:
    state_dir = os.environ.get("VNX_STATE_DIR")
    if state_dir:
        return Path(state_dir) / "dispatch_register.ndjson"
    data_dir = os.environ.get("VNX_DATA_DIR")
    if data_dir and os.environ.get("VNX_DATA_DIR_EXPLICIT") == "1":
        return Path(data_dir) / "state" / "dispatch_register.ndjson"
    return _REPO_ROOT / ".vnx-data" / "state" / "dispatch_register.ndjson"


def read_register_events(state_dir: Optional[Path] = None) -> list[dict]:
    """Read dispatch_register.ndjson; returns [] on any failure."""
    path = (Path(state_dir) / "dispatch_register.ndjson") if state_dir else _register_path()
    if not path.exists():
        return []
    events: list[dict] = []
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    try:
                        events.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
    except Exception:
        pass
    return events


def fetch_merged_prs(limit: int = 100) -> list[dict]:
    """Run gh pr list --state merged; returns [] on any failure."""
    try:
        result = subprocess.run(
            [
                "gh", "pr", "list", "--state", "merged",
                "--limit", str(limit),
                "--json", "number,title,mergedAt",
            ],
            capture_output=True, text=True, timeout=20,
        )
        if result.returncode != 0:
            return []
        return json.loads(result.stdout) or []
    except Exception:
        return []


def load_roadmap(roadmap_path: Optional[Path] = None) -> list[dict]:
    """Load planned features from ROADMAP.yaml; returns [] on any failure."""
    path = roadmap_path or (_REPO_ROOT / "ROADMAP.yaml")
    if not path.exists():
        return []
    try:
        import yaml  # type: ignore[import]
        with path.open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        return data.get("features") or []
    except ImportError:
        pass
    except Exception:
        return []
    # Fallback: minimal regex-based parser for the simple ROADMAP.yaml structure
    try:
        text = path.read_text(encoding="utf-8")
        features: list[dict] = []
        current: dict = {}
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("- feature_id:"):
                if current:
                    features.append(current)
                current = {"feature_id": stripped.split(":", 1)[1].strip()}
            elif ":" in stripped and current:
                k, _, v = stripped.partition(":")
                k = k.strip().lstrip("-").strip()
                v = v.strip()
                if k and v and k not in current:
                    current[k] = v
        if current:
            features.append(current)
        return features
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Feature aggregation
# ---------------------------------------------------------------------------

def _extract_fnum(s: str) -> Optional[int]:
    m = _FEATURE_RE.search(s or "")
    return int(m.group(1)) if m else None


def _build_feature_sections(
    register_events: list[dict],
    merged_prs: list[dict],
    roadmap_features: list[dict],
) -> dict:
    """Aggregate sources into {active, completed, planned} feature lists."""
    # fnum → {prs: {pr_num: "merged"|"open"}, merged_set, latest_ts}
    features: dict[int, dict] = {}

    def _get_or_create(fnum: int) -> dict:
        if fnum not in features:
            features[fnum] = {"prs": {}, "merged_set": set(), "latest_ts": ""}
        return features[fnum]

    # --- Register events ---
    for ev in register_events:
        feature_id = ev.get("feature_id") or ""
        dispatch_id = ev.get("dispatch_id") or ""
        pr_number = ev.get("pr_number")
        event = ev.get("event") or ""
        ts = ev.get("timestamp") or ""

        fnum = _extract_fnum(feature_id) or _extract_fnum(dispatch_id)
        if fnum is None:
            continue

        feat = _get_or_create(fnum)
        if ts > feat["latest_ts"]:
            feat["latest_ts"] = ts

        if pr_number is not None:
            if event == "pr_merged":
                feat["prs"][pr_number] = "merged"
                feat["merged_set"].add(pr_number)
            elif event == "pr_opened":
                feat["prs"].setdefault(pr_number, "open")

    # --- gh merged PRs ---
    for pr in merged_prs:
        fnum = _extract_fnum(pr.get("title") or "")
        if fnum is None:
            continue
        pr_num = pr["number"]
        merged_at = pr.get("mergedAt") or ""
        feat = _get_or_create(fnum)
        feat["prs"][pr_num] = "merged"
        feat["merged_set"].add(pr_num)
        if merged_at > feat["latest_ts"]:
            feat["latest_ts"] = merged_at

    # --- Classify active vs completed ---
    known_fnums = set(features.keys())
    active: list[dict] = []
    completed: list[dict] = []

    for fnum in sorted(features.keys()):
        feat = features[fnum]
        all_prs = feat["prs"]
        if not all_prs:
            continue
        merged = sorted(feat["merged_set"])
        open_prs = sorted(set(all_prs.keys()) - feat["merged_set"])
        if open_prs:
            active.append({"fnum": fnum, "merged_prs": merged, "open_prs": open_prs})
        else:
            completed.append({"fnum": fnum, "merged_prs": merged})

    # --- Planned from ROADMAP (skip features already seen in register/gh) ---
    planned: list[dict] = []
    for rf in roadmap_features:
        fid = rf.get("feature_id") or ""
        title = rf.get("title") or ""
        status = rf.get("status") or "planned"
        fnum = _extract_fnum(fid) or _extract_fnum(title)
        if fnum is not None and fnum in known_fnums:
            continue
        planned.append({"fnum": fnum, "feature_id": fid, "title": title, "status": status})

    return {"active": active, "completed": completed, "planned": planned}


def _group_consecutive(fnums: list[int]) -> list[tuple[int, int]]:
    if not fnums:
        return []
    groups: list[tuple[int, int]] = []
    start = prev = fnums[0]
    for n in fnums[1:]:
        if n == prev + 1:
            prev = n
        else:
            groups.append((start, prev))
            start = prev = n
    groups.append((start, prev))
    return groups


def _pr_label(pr_nums: list[int], idx: int) -> str:
    return f"PR-{idx + 1} (#{pr_nums[idx]})"


# ---------------------------------------------------------------------------
# Markdown renderer
# ---------------------------------------------------------------------------

def generate_feature_plan(
    register_events: list[dict],
    merged_prs: list[dict],
    roadmap_features: list[dict],
    now: Optional[datetime] = None,
) -> str:
    """Generate FEATURE_PLAN.md content from the three sources."""
    if now is None:
        now = datetime.now(timezone.utc)

    sections = _build_feature_sections(register_events, merged_prs, roadmap_features)
    lines: list[str] = [
        _AUTOGEN_HEADER,
        "",
        "# VNX Feature Plan",
        f"**Last updated**: {now.isoformat()}",
        "",
    ]

    # --- Active features ---
    lines.append("## Active features")
    if sections["active"]:
        for feat in sections["active"]:
            all_pr_nums = sorted(feat["merged_prs"] + feat["open_prs"])
            lines.append(f"\n### F{feat['fnum']}")
            for i, pr_num in enumerate(all_pr_nums):
                status = "merged" if pr_num in feat["merged_prs"] else "in flight"
                lines.append(f"- {_pr_label(all_pr_nums, i)}: {status}")
    else:
        lines.append("\n_No active features._")

    # --- Completed ---
    lines.append("\n## Completed")
    if sections["completed"]:
        completed_by_fnum = {f["fnum"]: f for f in sections["completed"]}
        fnums_sorted = sorted(completed_by_fnum.keys())
        for start, end in _group_consecutive(fnums_sorted):
            if start == end:
                feat = completed_by_fnum[start]
                pr_str = " + ".join(f"#{n}" for n in feat["merged_prs"])
                lines.append(f"\n### F{start}")
                lines.append(f"All PRs merged. ({pr_str})")
            else:
                all_prs: list[int] = []
                for fnum in range(start, end + 1):
                    if fnum in completed_by_fnum:
                        all_prs.extend(completed_by_fnum[fnum]["merged_prs"])
                all_prs.sort()
                first_three = " + ".join(f"#{n}" for n in all_prs[:3])
                suffix = f" + {len(all_prs) - 3} more" if len(all_prs) > 3 else ""
                lines.append(f"\n### F{start}–F{end}")
                lines.append(f"All PRs merged. ({first_three}{suffix})")
    else:
        lines.append("\n_No completed features found in register or PR history._")

    # --- Planned ---
    lines.append("\n## Planned (from ROADMAP.yaml)")
    if sections["planned"]:
        for feat in sections["planned"]:
            fnum = feat.get("fnum")
            title = feat.get("title") or feat.get("feature_id") or "unknown"
            label = f"F{fnum}" if fnum is not None else feat.get("feature_id", "?")
            lines.append(f"\n### {label} — {title}")
            lines.append(f"Status: {feat.get('status', 'planned')}")
    else:
        lines.append("\n_No planned features in ROADMAP.yaml._")

    lines.append("")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def write_feature_plan(
    output_path: Optional[Path] = None,
    dry_run: bool = False,
    state_dir: Optional[Path] = None,
) -> str:
    """Read all sources, generate content, write to output_path. Returns content."""
    if output_path is None:
        output_path = _REPO_ROOT / "FEATURE_PLAN.md"

    register_events = read_register_events(state_dir=state_dir)
    merged_prs = fetch_merged_prs()
    roadmap_features = load_roadmap()

    content = generate_feature_plan(register_events, merged_prs, roadmap_features)

    if not dry_run:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(content, encoding="utf-8")

    return content


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Generate FEATURE_PLAN.md from dispatch register, gh PRs, and ROADMAP."
    )
    parser.add_argument("--output", default=None, help="Output path (default: FEATURE_PLAN.md)")
    parser.add_argument("--dry-run", action="store_true", help="Print to stdout, do not write")
    args = parser.parse_args()

    content = write_feature_plan(
        output_path=Path(args.output) if args.output else None,
        dry_run=args.dry_run,
    )
    if args.dry_run:
        sys.stdout.write(content)
    return 0


if __name__ == "__main__":
    sys.exit(main())
