"""reporter.py — emit CSV + markdown matrix from a list of CellScores."""
from __future__ import annotations

import csv
import statistics
from collections import defaultdict
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path


def write_raw_csv(scores: list, output_path: Path) -> None:
    """Per-cell row: lane, task, replication, all dimensions, cost, wallclock."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8", newline="") as f:
        if not scores:
            f.write("# no scores produced\n")
            return
        writer = csv.DictWriter(f, fieldnames=list(asdict(scores[0]).keys()))
        writer.writeheader()
        for s in scores:
            writer.writerow(asdict(s))


def write_summary_md(scores: list, output_path: Path, tasks_meta: dict) -> None:
    """Lane × tier matrix with median composite + N + cost."""
    by_lane_tier = defaultdict(list)
    for s in scores:
        tier = tasks_meta.get(s.task_id, {}).get("tier", "unknown")
        by_lane_tier[(s.lane_id, tier)].append(s)

    lanes = sorted({s.lane_id for s in scores})
    tiers = ["t1_trivial", "t2_medium", "t3_complex"]

    lines = []
    lines.append(f"# field-tests summary — {datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}")
    lines.append("")
    lines.append(f"Cells: {len(scores)} | Lanes: {len(lanes)} | Tasks: {len({s.task_id for s in scores})}")
    lines.append("")
    lines.append("## Composite score median per (lane, tier)")
    lines.append("")
    header = "| Lane | " + " | ".join(tiers) + " |"
    sep = "|---|" + "---|" * len(tiers)
    lines.append(header)
    lines.append(sep)
    for lane in lanes:
        row = [lane]
        for tier in tiers:
            cells = by_lane_tier.get((lane, tier), [])
            if not cells:
                row.append("—")
            else:
                med = statistics.median(c.composite for c in cells)
                row.append(f"{med:.2f} (N={len(cells)})")
        lines.append("| " + " | ".join(row) + " |")
    lines.append("")
    lines.append("## Cost per quality-point earned (USD / composite)")
    lines.append("")
    cost_lines = ["| Lane | Total cost USD | Total composite | $/point |", "|---|---:|---:|---:|"]
    for lane in lanes:
        lane_scores = [s for s in scores if s.lane_id == lane]
        total_cost = sum(s.cost_usd for s in lane_scores)
        total_comp = sum(s.composite for s in lane_scores)
        per_point = total_cost / total_comp if total_comp > 0 else 0
        cost_lines.append(f"| {lane} | {total_cost:.4f} | {total_comp:.2f} | {per_point:.5f} |")
    lines.extend(cost_lines)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_per_lane_md(scores: list, output_path: Path) -> None:
    """Narrative per lane: wins, losses, where it landed."""
    by_lane = defaultdict(list)
    for s in scores:
        by_lane[s.lane_id].append(s)

    lines = ["# per-lane narrative", ""]
    for lane in sorted(by_lane.keys()):
        cells = by_lane[lane]
        median_composite = statistics.median(c.composite for c in cells)
        total_cost = sum(c.cost_usd for c in cells)
        wins = [c for c in cells if c.composite >= 4.0]
        losses = [c for c in cells if c.composite <= 1.5]
        tps_vals = [c.tokens_per_second for c in cells if c.tokens_per_second > 0]
        tps_str = (
            f"median {statistics.median(tps_vals):.1f} tok/s (N={len(tps_vals)} measured)"
            if tps_vals else "n/a (no token data — subscription/unmeasured lane)"
        )
        lines.append(f"## {lane}")
        lines.append(f"- Cells: {len(cells)} | Median composite: {median_composite:.2f} | Total cost: ${total_cost:.4f}")
        lines.append(f"- Throughput: {tps_str}")
        lines.append(f"- Wins ({len(wins)}): {', '.join(sorted({c.task_id for c in wins})) or 'none'}")
        lines.append(f"- Losses ({len(losses)}): {', '.join(sorted({c.task_id for c in losses})) or 'none'}")
        lines.append("")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_methodology_md(
    output_path: Path, lanes: list, tasks: list, n_per_cell: int,
    started_at: str, finished_at: str,
) -> None:
    """Disclosure: what ran, how, with what limitations."""
    lines = [
        f"# methodology — field-tests run {started_at}",
        "",
        f"- Started: {started_at}",
        f"- Finished: {finished_at}",
        f"- Lanes: {len(lanes)} ({', '.join(lanes)})",
        f"- Tasks: {len(tasks)} ({', '.join(tasks)})",
        f"- Replications per cell: N={n_per_cell}",
        "",
        "## Scoring dimensions",
        "- correctness 0.40 (verify.py pass + partial-credit)",
        "- completeness 0.20 (expected files written)",
        "- cost_efficiency 0.15 (vs tier-expected max)",
        "- wallclock_efficiency 0.15 (vs deadline)",
        "- code_quality 0.10 (Opus LLM-judge)",
        "",
        "## Known limitations",
        "- LLM-judge is single-pass Opus; no inter-rater calibration",
        "- Cost from receipt YAML frontmatter; missing for some lanes → 0",
        "- Wallclock includes worker startup + git operations, not pure inference",
        "- N=3 per cell is variance-detection minimum, not statistical-significance",
        "- Tasks are derived from real production work but executed in isolated worktrees",
        "",
        "## Reproducibility",
        "- Same models.yaml + task seed/ + verify.py → reproducible within model-version drift",
        "- For model regression detection: diff this summary against prior month's run",
    ]
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
