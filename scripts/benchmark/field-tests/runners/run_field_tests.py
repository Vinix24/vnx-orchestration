"""run_field_tests.py — orchestrate a field-tests benchmark run.

Iterates lanes × tasks × replications, dispatches each cell via lane_adapter,
scores via scorer, writes results to results/<timestamp>/.

Usage:
    python3 run_field_tests.py                            # full matrix
    python3 run_field_tests.py --tier t1_trivial          # one tier
    python3 run_field_tests.py --task 01_yaml_config_refactor --n 1   # smoke
    python3 run_field_tests.py --lane claude-sonnet-4-6 --task 04_scorer_task

Concurrency: parallel-N via --parallel (default 6, capped at active T-pool).
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
FIELD_TESTS = HERE.parent
REPO_ROOT = FIELD_TESTS.parents[2]

sys.path.insert(0, str(HERE))
from lane_adapter import dispatch as lane_dispatch, load_lanes  # noqa: E402
from scorer import score_cell  # noqa: E402
from reporter import (  # noqa: E402
    write_raw_csv, write_summary_md, write_per_lane_md, write_methodology_md,
)


def load_tasks_config() -> dict:
    cfg_path = FIELD_TESTS / "tasks.yaml"
    return yaml.safe_load(cfg_path.read_text(encoding="utf-8"))


def filter_cells(
    cfg: dict,
    lane_filter: list[str] | None,
    tier_filter: list[str] | None,
    task_filter: list[str] | None,
    n_override: int | None,
) -> list[tuple]:
    """Return list of (lane_dict, task_meta, replication) tuples to run."""
    tiers_cfg = cfg["tiers"]
    tasks_by_id = {t["id"]: t for t in cfg["tasks"]}
    models_yaml = REPO_ROOT / "scripts" / "benchmark" / "models.yaml"

    cells = []
    for task in cfg["tasks"]:
        if task_filter and task["id"] not in task_filter:
            continue
        tier = task["tier"]
        if tier_filter and tier not in tier_filter:
            continue
        tier_cfg = tiers_cfg.get(tier, {})
        n = n_override if n_override is not None else tier_cfg.get("n_replications", 1)
        lane_ids = tier_cfg.get("lanes", [])
        if lane_filter:
            lane_ids = [lid for lid in lane_ids if lid in lane_filter]
        if not lane_ids:
            continue
        lanes = load_lanes(models_yaml, lane_ids)
        for lane in lanes:
            for rep in range(1, n + 1):
                cells.append((lane, task, rep))
    return cells


def run_one_cell(lane: dict, task: dict, rep: int, run_judge: bool) -> dict:
    """Dispatch + score one cell. Returns dict with dispatch + score."""
    task_folder = FIELD_TESTS / task["folder"]
    instruction_path = task_folder / "instruction.md"
    if not instruction_path.exists():
        return {
            "error": f"missing instruction.md at {instruction_path}",
            "lane_id": lane["id"], "task_id": task["id"], "replication": rep,
        }

    instruction = instruction_path.read_text(encoding="utf-8")
    seed_dir = task_folder / "seed"
    dispatch_paths = str(seed_dir.relative_to(REPO_ROOT)) if seed_dir.exists() else ""

    result = lane_dispatch(
        lane=lane, task_id=task["id"], replication=rep,
        instruction=instruction, dispatch_paths=dispatch_paths,
        deadline_seconds=task.get("deadline_seconds", 600),
    )

    expected_files = []
    expected_rubric = None
    expected_path = task_folder / "expected.json"
    if expected_path.exists():
        rubric = json.loads(expected_path.read_text(encoding="utf-8"))
        expected_files = rubric.get("expected_files", [])
        expected_rubric = rubric

    score = score_cell(
        dispatch_result=result, task_meta=task, task_folder=task_folder,
        instruction=instruction, expected_files=expected_files,
        expected_rubric=expected_rubric, run_judge=run_judge,
    )
    return {"dispatch": result, "score": score}


def main() -> int:
    parser = argparse.ArgumentParser(description="Run field-tests benchmark suite")
    parser.add_argument("--lane", action="append", default=None, help="filter to lane id(s)")
    parser.add_argument("--tier", action="append", default=None, help="filter to tier(s): t1_trivial t2_medium t3_complex")
    parser.add_argument("--task", action="append", default=None, help="filter to task id(s)")
    parser.add_argument("--n", type=int, default=None, help="override replications per cell")
    parser.add_argument("--parallel", type=int, default=6, help="concurrent dispatches (default 6)")
    parser.add_argument("--no-judge", action="store_true", help="skip LLM-judge step")
    parser.add_argument("--results-dir", type=Path, default=None, help="override results dir")
    args = parser.parse_args()

    cfg = load_tasks_config()
    cells = filter_cells(cfg, args.lane, args.tier, args.task, args.n)
    if not cells:
        print("No cells to run after filtering.", file=sys.stderr)
        return 2

    started_at = datetime.now(timezone.utc)
    results_dir = args.results_dir or (
        FIELD_TESTS / "results" / started_at.strftime("%Y-%m-%dT%H-%M-%SZ")
    )
    results_dir.mkdir(parents=True, exist_ok=True)
    print(f"[run_field_tests] {len(cells)} cells → {results_dir}", file=sys.stderr)

    scores = []
    failures = []
    run_judge = not args.no_judge

    with ThreadPoolExecutor(max_workers=args.parallel) as pool:
        futures = {
            pool.submit(run_one_cell, lane, task, rep, run_judge): (lane["id"], task["id"], rep)
            for lane, task, rep in cells
        }
        for fut in as_completed(futures):
            key = futures[fut]
            try:
                res = fut.result()
                if "error" in res:
                    failures.append({"key": key, "error": res["error"]})
                    print(f"  ✗ {key}: {res['error']}", file=sys.stderr)
                    continue
                scores.append(res["score"])
                print(
                    f"  ✓ {key} composite={res['score'].composite:.2f} "
                    f"cost=${res['score'].cost_usd:.4f} wall={res['score'].wallclock_seconds:.1f}s",
                    file=sys.stderr,
                )
            except Exception as exc:
                failures.append({"key": key, "error": f"crash: {exc}"})
                print(f"  ✗ {key}: crash {exc}", file=sys.stderr)

    finished_at = datetime.now(timezone.utc)
    write_raw_csv(scores, results_dir / "raw.csv")
    write_summary_md(scores, results_dir / "summary.md", {t["id"]: t for t in cfg["tasks"]})
    write_per_lane_md(scores, results_dir / "per-lane.md")
    write_methodology_md(
        results_dir / "methodology.md",
        lanes=sorted({s.lane_id for s in scores}),
        tasks=sorted({s.task_id for s in scores}),
        n_per_cell=args.n if args.n is not None else 0,
        started_at=started_at.isoformat(),
        finished_at=finished_at.isoformat(),
    )

    if failures:
        (results_dir / "failures.json").write_text(
            json.dumps(failures, indent=2), encoding="utf-8",
        )

    print(
        f"[run_field_tests] done: {len(scores)} scored, {len(failures)} failed, "
        f"results at {results_dir}",
        file=sys.stderr,
    )
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
