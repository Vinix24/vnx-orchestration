#!/usr/bin/env python3
"""analyze_results.py — Aggregate benchmark results into report + routing recommendations.

Output:
  - claudedocs/benchmark-model-comparison.md  (data tables + insights)
  - scripts/lib/providers/routing_recommendations.yaml  (cost-routing input data)

Usage:
    python3 scripts/benchmark/analyze_results.py [--results-dir PATH] [--output-dir PATH]
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import tempfile
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
BENCHMARK_DIR = Path(__file__).resolve().parent
RESULTS_DIR = BENCHMARK_DIR / "results"
CLAUDEDOCS_DIR = REPO_ROOT / "claudedocs"
ROUTING_OUTPUT = REPO_ROOT / "scripts" / "lib" / "providers" / "routing_recommendations.yaml"


def load_results(results_dir: Path) -> List[Dict]:
    records = []
    skipped = []
    for path in sorted(results_dir.glob("*.json")):
        try:
            records.append(json.loads(path.read_text()))
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("analyze: skipping %s: %s", path, e)
            skipped.append(str(path))
            continue
    if skipped:
        print(
            f"WARNING: skipped {len(skipped)} unreadable result files: {skipped[:3]}{'...' if len(skipped) > 3 else ''}",
            file=sys.stderr,
        )
    return records


def _score(record: Dict) -> Optional[float]:
    scores = (record.get("judge_scores") or {})
    q = scores.get("quality_score")
    comp = scores.get("completeness_score")
    if q is None or comp is None:
        return None
    return round((q + comp) / 2, 2)


def _cost(record: Dict) -> Optional[float]:
    return record.get("cost_usd")


def _duration(record: Dict) -> float:
    return record.get("duration_seconds") or 0.0


def build_task_summary(records: List[Dict]) -> Dict[str, Dict[str, Dict]]:
    """Build nested dict: task_id -> model_id -> {score, cost, duration}."""
    summary: Dict[str, Dict[str, Dict]] = defaultdict(dict)
    for r in records:
        task_id = r.get("task_id", "unknown")
        model_id = r.get("model_id", "unknown")
        summary[task_id][model_id] = {
            "score": _score(r),
            "cost_usd": _cost(r),
            "duration_seconds": _duration(r),
            "correctness": (r.get("judge_scores") or {}).get("correctness"),
        }
    return summary


def build_pareto_frontier(records: List[Dict]) -> List[Dict]:
    """Compute cost-quality Pareto frontier across all (model, task) pairs."""
    points = []
    for r in records:
        s = _score(r)
        c = _cost(r)
        if s is None or c is None:
            continue
        points.append({"model_id": r["model_id"], "task_id": r["task_id"], "score": s, "cost_usd": c})

    pareto = []
    for p in points:
        dominated = any(
            other["score"] >= p["score"] and other["cost_usd"] <= p["cost_usd"] and other is not p
            for other in points
        )
        if not dominated:
            pareto.append(p)

    return sorted(pareto, key=lambda x: x["cost_usd"])


def build_routing_recommendations(summary: Dict[str, Dict[str, Dict]]) -> Dict:
    """For each task class, rank models by composite score and cost."""
    routing: Dict[str, List[Dict]] = {}
    for task_id, models in summary.items():
        ranked = []
        for model_id, metrics in models.items():
            ranked.append({
                "model_id": model_id,
                "composite_score": metrics["score"],
                "cost_usd_per_call": metrics["cost_usd"],
                "avg_duration_seconds": metrics["duration_seconds"],
            })
        # Sort: highest score first; tie-break by lowest cost
        ranked.sort(key=lambda x: (-(x["composite_score"] or 0), (x["cost_usd_per_call"] or float("inf"))))
        routing[task_id] = ranked
    return {"routing_by_task": routing}


def _md_table(headers: List[str], rows: List[List[str]]) -> str:
    widths = [max(len(h), max((len(str(r[i])) for r in rows), default=0)) for i, h in enumerate(headers)]
    sep = "| " + " | ".join("-" * w for w in widths) + " |"
    header_row = "| " + " | ".join(h.ljust(widths[i]) for i, h in enumerate(headers)) + " |"
    data_rows = ["| " + " | ".join(str(r[i]).ljust(widths[i]) for i in range(len(headers))) + " |" for r in rows]
    return "\n".join([header_row, sep] + data_rows)


def render_markdown_report(records: List[Dict], summary: Dict, pareto: List[Dict]) -> str:
    lines = [
        "# Benchmark Model Comparison Report",
        "",
        f"Generated from {len(records)} benchmark results.",
        "",
        "## Per-Task Rankings",
        "",
    ]

    for task_id in sorted(summary):
        models_data = summary[task_id]
        lines.append(f"### {task_id}")
        headers = ["Model", "Score", "Correctness", "Cost USD", "Duration (s)"]
        rows = []
        for model_id in sorted(models_data, key=lambda m: -(models_data[m]["score"] or 0)):
            m = models_data[model_id]
            rows.append([
                model_id,
                str(m["score"] or "N/A"),
                "yes" if m["correctness"] else ("no" if m["correctness"] is False else "N/A"),
                f"{m['cost_usd']:.4f}" if m["cost_usd"] is not None else "N/A",
                f"{m['duration_seconds']:.1f}",
            ])
        lines.append(_md_table(headers, rows))
        lines.append("")

    lines += [
        "## Cost-Quality Pareto Frontier",
        "",
        "Models where no other model is both cheaper AND better quality.",
        "",
    ]
    if pareto:
        headers = ["Model", "Task", "Score", "Cost USD"]
        rows = [[p["model_id"], p["task_id"], str(p["score"]), f"{p['cost_usd']:.4f}"] for p in pareto]
        lines.append(_md_table(headers, rows))
    else:
        lines.append("No Pareto data (missing judge scores or cost data).")
    lines.append("")

    all_scores: Dict[str, List[float]] = defaultdict(list)
    all_costs: Dict[str, List[float]] = defaultdict(list)
    for r in records:
        s = _score(r)
        c = _cost(r)
        mid = r.get("model_id", "unknown")
        if s is not None:
            all_scores[mid].append(s)
        if c is not None:
            all_costs[mid].append(c)

    lines += ["## Overall Model Summary", ""]
    headers = ["Model", "Avg Score", "Avg Cost USD", "Tasks Scored"]
    rows = []
    for model_id in sorted(all_scores):
        avg_score = round(sum(all_scores[model_id]) / len(all_scores[model_id]), 2)
        costs = all_costs.get(model_id, [])
        avg_cost = round(sum(costs) / len(costs), 4) if costs else None
        rows.append([
            model_id,
            str(avg_score),
            f"{avg_cost:.4f}" if avg_cost is not None else "N/A",
            str(len(all_scores[model_id])),
        ])
    rows.sort(key=lambda r: -float(r[1]))
    lines.append(_md_table(headers, rows))
    lines.append("")

    return "\n".join(lines)


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(content)
    tmp.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser(description="Aggregate benchmark results into report + routing YAML")
    parser.add_argument("--results-dir", default=None)
    parser.add_argument("--output-dir", default=None, help="Override claudedocs output directory")
    args = parser.parse_args()

    results_dir = Path(args.results_dir) if args.results_dir else RESULTS_DIR
    output_dir = Path(args.output_dir) if args.output_dir else CLAUDEDOCS_DIR

    records = load_results(results_dir)
    if not records:
        print(f"No result files found in {results_dir}")
        return 0

    print(f"Loaded {len(records)} results")

    summary = build_task_summary(records)
    pareto = build_pareto_frontier(records)
    routing = build_routing_recommendations(summary)
    report_md = render_markdown_report(records, summary, pareto)

    report_path = output_dir / "benchmark-model-comparison.md"
    _atomic_write(report_path, report_md)
    print(f"Report written to {report_path}")

    routing_path = ROUTING_OUTPUT
    _atomic_write(routing_path, yaml.dump(routing, default_flow_style=False, allow_unicode=True))
    print(f"Routing recommendations written to {routing_path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
