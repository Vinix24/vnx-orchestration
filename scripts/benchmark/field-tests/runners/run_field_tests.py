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
import subprocess
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
    cfg = yaml.safe_load((FIELD_TESTS / "tasks.yaml").read_text(encoding="utf-8"))
    # Optional gitignored overlay for INTERNAL tasks (e.g. the t6 real-codebase-review
    # tier, whose instruction/verify reference proprietary repos and must not be
    # committed/published). Merges its tiers + tasks on top of the public config.
    local = FIELD_TESTS / "tasks.local.yaml"
    if local.exists():
        lcfg = yaml.safe_load(local.read_text(encoding="utf-8")) or {}
        cfg.setdefault("tiers", {}).update(lcfg.get("tiers", {}))
        seen = {t["id"] for t in cfg.get("tasks", [])}
        cfg.setdefault("tasks", []).extend(
            t for t in lcfg.get("tasks", []) if t["id"] not in seen
        )
    return cfg


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


def run_one_cell(
    lane: dict, task: dict, rep: int, run_judge: bool,
    deadline_override: int | None = None,
) -> dict:
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
    # Always pass the conventional seed-rel path. When the dir exists the worker gets
    # the materialized seed; when it does NOT (from-scratch tasks like t3 07/08 that
    # build everything themselves) the harness starts the worker in an empty cell and
    # plants the SEED_REL symlink, so verify.py's `workdir / SEED_REL` resolves either way.
    dispatch_paths = str(seed_dir.relative_to(REPO_ROOT))

    deadline = deadline_override if deadline_override is not None else task.get("deadline_seconds", 600)
    # Skill-binding: tasks.yaml may declare `skill: <name>` or `skills: [a, b]`.
    # Provider-agnostic plain-prepend handled in lane_adapter.dispatch().
    raw_skills = task.get("skill") or task.get("skills") or []
    skill_names = [raw_skills] if isinstance(raw_skills, str) else list(raw_skills)
    result = lane_dispatch(
        lane=lane, task_id=task["id"], replication=rep,
        instruction=instruction, dispatch_paths=dispatch_paths,
        deadline_seconds=deadline,
        skill_names=skill_names,
    )

    expected_files = []
    expected_rubric = None
    expected_path = task_folder / "expected.json"
    if expected_path.exists():
        rubric = json.loads(expected_path.read_text(encoding="utf-8"))
        expected_files = rubric.get("expected_files", [])
        expected_rubric = rubric

    try:
        score = score_cell(
            dispatch_result=result, task_meta=task, task_folder=task_folder,
            instruction=instruction, expected_files=expected_files,
            expected_rubric=expected_rubric, run_judge=run_judge,
        )
    finally:
        # lane_adapter may have created a temp checkout purely for scoring
        # (tmux cell whose worktree was reaped but whose branch survived).
        # F8 (PR #831, deferred 1.0.1): a removal failure here is logged but
        # not surfaced as a structured result — a leaked worktree is possible.
        if result.scoring_worktree is not None:
            rm = subprocess.run(
                ["git", "-C", str(REPO_ROOT), "worktree", "remove", "--force",
                 str(result.scoring_worktree)],
                capture_output=True, text=True, check=False,
            )
            if rm.returncode != 0:
                print(
                    f"  ⚠ scoring-worktree removal failed for {result.scoring_worktree}: "
                    f"{(rm.stderr or '').strip()[:200]}",
                    file=sys.stderr,
                )
    return {"dispatch": result, "score": score}


def _load_dnf_cells_from_csv(
    csv_path: Path,
    deadlines: dict[str, int] | None = None,
) -> set[tuple[str, str, int]]:
    """Read a prior raw.csv and return (lane, task, rep) tuples that are DNF/invalid.

    DNF criteria (must match scorer.py + lane_adapter.py policy):
      - verify_evidence starts with "DNF:" (scorer marked it as failed dispatch)
      - wallclock at/near the task's own deadline AND scored < 4.5 (hang-at-deadline)
      - report missing in both candidate dirs (immediate-exit no-report cell)

    Note: short wallclock alone is NOT enough — Kimi can legitimately finish in 2-3s
    if the worker is fast and the task is bounded. The discriminator is report-presence.
    """
    import csv as _csv
    from lane_adapter import REPORT_DIR_CANDIDATES  # local import to avoid cycle
    deadlines = deadlines or {}
    dnf: set[tuple[str, str, int]] = set()
    with csv_path.open(encoding="utf-8") as fh:
        reader = _csv.DictReader(fh)
        for row in reader:
            try:
                wall = float(row["wallclock_seconds"])
                comp = float(row["composite"])
                ev = (row.get("verify_evidence") or "").lower()
            except (KeyError, ValueError):
                continue

            # Hard signals.
            if ev.startswith("dnf:"):
                dnf.add((row["lane_id"], row["task_id"], int(row["replication"])))
                continue
            # Hang-at-deadline: wallclock within 90% of the task's OWN deadline
            # AND composite < 4.5. A fixed wall>5000s cutoff wrongly re-ran
            # legitimate long T3 cells that finished well inside their
            # 10,800/14,400s deadlines (codex-gate PR #831 finding). The
            # 2026-06-04 opus-4-7 T3-08 hang (10806s vs 10800s deadline,
            # composite 3.75) is still caught.
            deadline = deadlines.get(row["task_id"])
            if deadline and wall >= 0.9 * deadline and comp < 4.5:
                dnf.add((row["lane_id"], row["task_id"], int(row["replication"])))
                continue

            # Soft signal: report-missing for any cell with wallclock < 5s.
            # Check filesystem for both report-naming conventions in both candidate dirs.
            # KNOWN LIMITATION (F5, PR #831, deferred 1.0.1): the prefix match
            # below has a per-cell timestamp suffix, so ANY historical report
            # for this lane/task/rep counts as "present" — a short failed cell
            # whose only report is from an earlier run is wrongly excluded from
            # retry. Fix is to match the row's own dispatch_id; deferred.
            if wall < 5.0:
                prefix = f"bench-{row['lane_id']}-{row['task_id']}-r{row['replication']}-"
                found = False
                for d in REPORT_DIR_CANDIDATES:
                    if not d.exists():
                        continue
                    for p in d.iterdir():
                        if p.name.startswith(prefix) and (
                            p.name.endswith(".md") or p.name.endswith("_report.md")
                        ):
                            found = True
                            break
                    if found:
                        break
                if not found:
                    dnf.add((row["lane_id"], row["task_id"], int(row["replication"])))
    return dnf


def _load_prior_scores(
    csv_path: Path, exclude: set[tuple[str, str, int]],
) -> list:
    """Load non-excluded rows from a prior raw.csv as CellScore objects.

    Used by --retry-from to merge the prior run's healthy rows with the
    retried cells, so the output is a complete consolidated result instead
    of retried-rows-only (codex-gate PR #831 finding).
    """
    import csv as _csv
    from scorer import CellScore  # local import to avoid cycle
    prior: list = []
    with csv_path.open(encoding="utf-8") as fh:
        for row in _csv.DictReader(fh):
            try:
                key = (row["lane_id"], row["task_id"], int(row["replication"]))
            except (KeyError, ValueError):
                continue
            if key in exclude:
                continue
            prior.append(CellScore(
                lane_id=row["lane_id"], task_id=row["task_id"],
                replication=int(row["replication"]),
                correctness=float(row["correctness"]),
                completeness=float(row["completeness"]),
                cost_efficiency=float(row["cost_efficiency"]),
                wallclock_efficiency=float(row["wallclock_efficiency"]),
                code_quality=float(row["code_quality"]),
                composite=float(row["composite"]),
                verify_evidence=row.get("verify_evidence", ""),
                judge_reasoning=row.get("judge_reasoning", ""),
                cost_usd=float(row["cost_usd"]),
                wallclock_seconds=float(row["wallclock_seconds"]),
            ))
    return prior


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run field-tests benchmark suite")
    parser.add_argument("--lane", action="append", default=None, help="filter to lane id(s)")
    parser.add_argument("--tier", action="append", default=None, help="filter to tier(s): t1_trivial t2_medium t3_complex")
    parser.add_argument("--task", action="append", default=None, help="filter to task id(s)")
    parser.add_argument("--n", type=int, default=None, help="override replications per cell")
    parser.add_argument("--parallel", type=int, default=6, help="concurrent dispatches (default 6)")
    parser.add_argument("--no-judge", action="store_true", help="skip LLM-judge step")
    parser.add_argument("--results-dir", type=Path, default=None, help="override results dir")
    parser.add_argument(
        "--retry-from", type=Path, default=None,
        help="re-run only DNF cells from a prior raw.csv; output merges the "
             "prior run's healthy rows with the retried cells",
    )
    parser.add_argument(
        "--claude-serial", action="store_true",
        help="serialize claude lanes (parallel=1 for claude, --parallel for others) "
             "to avoid subscription rate-limit cliff observed in 2026-06-04 bench",
    )
    parser.add_argument(
        "--max-retries", type=int, default=2,
        help="if a cell DNFs, retry up to N more times with exponential back-off (default 2)",
    )
    parser.add_argument(
        "--deadline-override", type=int, default=None,
        help="override task deadline_seconds (e.g. 1800 to fail-fast on retry-run)",
    )
    return parser


def _run_with_retry(lane, task, rep, args, run_judge):
    """Run one cell with up to args.max_retries retries on DNF.

    Success discriminator is report-presence + non-zero composite. Wallclock
    is deliberately NOT part of the condition: fast lanes legitimately finish
    bounded tasks in 2-3s, and a >=5s floor re-ran those healthy cells
    (codex-gate PR #831 finding).

    KNOWN LIMITATION (F4, PR #831, deferred 1.0.1): headless/provider lanes
    write to the shared main checkout; a retry after a partial failed attempt
    does not reset that checkout, so leftover edits could inflate the retry
    score. tmux lanes are unaffected (each dispatch gets a fresh worktree).
    Until the reset lands, prefer tmux lanes for retry-sensitive cells.
    """
    attempt = 0
    last_res = None
    while attempt <= args.max_retries:
        res = run_one_cell(lane, task, rep, run_judge, deadline_override=args.deadline_override)
        if "error" in res:
            return res
        score = res["score"]
        if score.composite > 0.0 and res["dispatch"].report_path is not None:
            return res
        attempt += 1
        if attempt > args.max_retries:
            return res
        back_off = min(60 * (2 ** (attempt - 1)), 600)
        print(
            f"  ↻ ({lane['id']},{task['id']},r{rep}) DNF (wall={score.wallclock_seconds:.1f}s "
            f"comp={score.composite:.2f}); retry {attempt}/{args.max_retries} after {back_off}s",
            file=sys.stderr,
        )
        time.sleep(back_off)
        last_res = res
    return last_res


def _run_cells(cells, args, run_judge) -> tuple[list, list]:
    """Dispatch all cells (claude-serial aware); returns (scores, failures)."""
    scores: list = []
    failures: list = []

    def _drain_pool(pool, cells_to_run):
        futures = {
            pool.submit(_run_with_retry, lane, task, rep, args, run_judge):
                (lane["id"], task["id"], rep)
            for lane, task, rep in cells_to_run
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

    # Partition if --claude-serial: claude lanes run sequentially (parallel=1)
    # to avoid the subscription rate-limit hit observed on 2026-06-04.
    claude_cells = [c for c in cells if c[0]["provider"] == "claude"]
    other_cells = [c for c in cells if c[0]["provider"] != "claude"]
    if args.claude_serial and claude_cells:
        print(
            f"[run_field_tests] --claude-serial: running {len(claude_cells)} claude cells "
            f"sequentially, then {len(other_cells)} other cells at parallel={args.parallel}",
            file=sys.stderr,
        )
        with ThreadPoolExecutor(max_workers=1) as pool:
            _drain_pool(pool, claude_cells)
        with ThreadPoolExecutor(max_workers=args.parallel) as pool:
            _drain_pool(pool, other_cells)
    else:
        with ThreadPoolExecutor(max_workers=args.parallel) as pool:
            _drain_pool(pool, cells)
    return scores, failures


def _write_outputs(scores, failures, results_dir, cfg, args, started_at) -> None:
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


def main() -> int:
    args = _build_parser().parse_args()

    cfg = load_tasks_config()
    cells = filter_cells(cfg, args.lane, args.tier, args.task, args.n)

    # Retry-from mode: re-run only prior DNF cells; merge with healthy rows later.
    retry_csv: Path | None = None
    dnf_filter: set[tuple[str, str, int]] | None = None
    if args.retry_from:
        retry_csv = args.retry_from.resolve()
        if not retry_csv.exists():
            print(f"--retry-from: file not found: {retry_csv}", file=sys.stderr)
            return 2
        deadlines = {t["id"]: t.get("deadline_seconds", 600) for t in cfg["tasks"]}
        dnf_filter = _load_dnf_cells_from_csv(retry_csv, deadlines)
        if not dnf_filter:
            print(f"--retry-from: no DNF cells found in {retry_csv}", file=sys.stderr)
            return 0
        cells = [
            (lane, task, rep) for (lane, task, rep) in cells
            if (lane["id"], task["id"], rep) in dnf_filter
        ]
        print(
            f"[run_field_tests] --retry-from {retry_csv.name}: {len(cells)} DNF cells to re-run",
            file=sys.stderr,
        )

    if not cells:
        print("No cells to run after filtering.", file=sys.stderr)
        return 2

    started_at = datetime.now(timezone.utc)
    results_dir = args.results_dir or (
        FIELD_TESTS / "results" / started_at.strftime("%Y-%m-%dT%H-%M-%SZ")
    )
    results_dir.mkdir(parents=True, exist_ok=True)
    print(f"[run_field_tests] {len(cells)} cells → {results_dir}", file=sys.stderr)

    scores, failures = _run_cells(cells, args, run_judge=not args.no_judge)

    # Consolidate: a retry-run output must contain the full matrix, not only
    # the retried rows (codex-gate PR #831 finding).
    if retry_csv is not None and dnf_filter:
        prior = _load_prior_scores(retry_csv, exclude=dnf_filter)
        print(
            f"[run_field_tests] --retry-from: merged {len(prior)} healthy prior rows "
            f"with {len(scores)} retried rows",
            file=sys.stderr,
        )
        scores = prior + scores

    _write_outputs(scores, failures, results_dir, cfg, args, started_at)

    print(
        f"[run_field_tests] done: {len(scores)} scored, {len(failures)} failed, "
        f"results at {results_dir}",
        file=sys.stderr,
    )
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
