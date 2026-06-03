#!/usr/bin/env python3
"""run_lane_bench.py — Smart-lanes benchmark runner.

Runs deterministic task suite across all provider lanes and tabulates
latency / cost / output-tokens / exit-code / quality per (lane, task) cell.

Quality scoring:
  - codegen: programmatic — extract function, exec, verify add(2,3)==5 → 0-5
  - review:  manual stub  — response saved verbatim for human scoring
  - docs:    manual stub  — response saved verbatim for human scoring

Outputs:
  - claudedocs/benchmarks-YYYYMMDD-HHMM.csv       raw per-run metrics
  - claudedocs/benchmarks-YYYYMMDD-HHMM-summary.md per-lane aggregates

Usage:
    python3 scripts/benchmarks/run_lane_bench.py --n 1
    python3 scripts/benchmarks/run_lane_bench.py --n 10 --lanes claude-sonnet,kimi
    python3 scripts/benchmarks/run_lane_bench.py --tasks codegen --parallel 6
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DISPATCH_SCRIPT = PROJECT_ROOT / "scripts" / "lib" / "provider_dispatch.py"
REPORT_DIR_LOCAL = PROJECT_ROOT / ".vnx-data" / "unified_reports"
REPORT_DIR_CENTRAL = Path.home() / ".vnx-data" / "vnx-dev" / "unified_reports"
CLAUDEDOCS_DIR = PROJECT_ROOT / "claudedocs"

# Lane behaviors differ: claude does real work (cost reflects it), kimi/deepseek
# answer inline. Benchmark captures that as-is. Quality scoring extracts the
# function/output from response text regardless of whether the worker also
# created files. No "no-tools" suffix — that broke response capture for Claude.
_RESPONSE_SUFFIX = (
    "\n\nIMPORTANT: Include the requested output VERBATIM in your response text "
    "(in a fenced code block where applicable), even if you also write it to a file. "
    "The benchmark scores your response text, not any files created."
)

TASKS: dict = {
    "codegen": {
        "instruction": (
            "Write a Python function named 'add' that takes two integers "
            "and returns their sum. Output only the function code in a "
            "Python code block (```python ... ```). No explanation, no tests."
        ),
        "role": "backend-developer",
        "quality_fn": "score_codegen",
    },
    "review": {
        "instruction": (
            "Review this Python code for exactly one bug:\n\n"
            "def divide(a, b):\n    return a/b\n\n"
            "Respond in exactly this format: 'Bug: <one-line description>'. "
            "Nothing else, no preamble, no other lines."
        ),
        "role": "quality-engineer",
        "quality_fn": "score_review",
    },
    "docs": {
        "instruction": (
            "Write a one-line docstring for a Python function called 'add' "
            "that returns the sum of two integers. Output only the docstring "
            "between triple double-quotes. No code, no commentary."
        ),
        "role": "technical-writer",
        "quality_fn": "score_docs",
    },
}

LANES: dict = {
    "claude-sonnet":    {"provider": "claude",            "model": "sonnet"},
    "claude-haiku":     {"provider": "claude",            "model": "haiku"},
    "deepseek-harness": {"provider": "deepseek-harness",  "model": "deepseek-v4-flash"},
    "litellm-ds-pro":   {"provider": "litellm:deepseek",  "model": "deepseek-v4-pro"},
    "kimi":             {"provider": "kimi",              "model": "kimi-k2-0905"},
    "gemini":           {"provider": "gemini",            "model": "gemini-2.5-flash"},
    "local-gemma":      {"provider": "local-gemma",       "model": "gemma-4b-local"},
}


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def fire_dispatch(lane_id: str, task_id: str, run_idx: int) -> dict:
    """Fire one provider_dispatch.py run. Returns metrics dict."""
    lane = LANES[lane_id]
    task = TASKS[task_id]
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dispatch_id = f"{stamp}-bench-{_slug(lane_id)}-{_slug(task_id)}-r{run_idx}"
    terminal_id = f"bench-{_slug(lane_id)}-{_slug(task_id)}-r{run_idx}"

    cmd = [
        "python3", str(DISPATCH_SCRIPT),
        "--provider", lane["provider"],
        "--model", lane["model"],
        "--terminal-id", terminal_id,
        "--dispatch-id", dispatch_id,
        "--role", task["role"],
        "--instruction", task["instruction"],
        "--no-auto-commit",
    ]
    t0 = time.time()
    try:
        proc = subprocess.run(
            cmd, cwd=PROJECT_ROOT, capture_output=True, text=True, timeout=300,
        )
        wall_seconds = time.time() - t0
        exit_code = proc.returncode
        stderr = proc.stderr[-500:] if proc.stderr else ""
    except subprocess.TimeoutExpired:
        wall_seconds = time.time() - t0
        exit_code = 124
        stderr = "timeout after 300s"

    central_path = REPORT_DIR_CENTRAL / f"{dispatch_id}.md"
    local_path = REPORT_DIR_LOCAL / f"{dispatch_id}.md"
    receipt = parse_receipt(central_path) if central_path.exists() else parse_receipt(local_path)
    response_text = extract_response(central_path) if central_path.exists() else ""
    if not response_text or "(no response captured)" in response_text:
        response_text = extract_response(local_path) if local_path.exists() else response_text
    quality = globals()[task["quality_fn"]](response_text)

    return {
        "lane": lane_id,
        "task": task_id,
        "run": run_idx,
        "dispatch_id": dispatch_id,
        "wall_seconds": round(wall_seconds, 2),
        "exit_code": exit_code,
        "provider": receipt.get("provider", ""),
        "model": receipt.get("model", ""),
        "report_duration": receipt.get("duration_seconds", ""),
        "input_tokens": receipt.get("input_tokens", ""),
        "output_tokens": receipt.get("output_tokens", ""),
        "cost_usd": receipt.get("cost_usd", ""),
        "quality_score": quality,
        "stderr_tail": stderr.replace("\n", " | "),
        "response_preview": response_text[:200].replace("\n", " "),
    }


def parse_receipt(report_path: Path) -> dict:
    """Read frontmatter YAML from report, return metrics dict."""
    if not report_path.exists():
        return {}
    text = report_path.read_text()
    m = re.match(r"^---\n(.*?)\n---\n", text, re.DOTALL)
    if not m:
        return {}
    frontmatter = m.group(1)
    result = {}
    for line in frontmatter.splitlines():
        if ":" in line and not line.startswith(" "):
            k, _, v = line.partition(":")
            result[k.strip()] = v.strip()
        elif line.strip().startswith(("input:", "output:", "cache_read:")):
            k, _, v = line.partition(":")
            result[f"{k.strip()}_tokens"] = v.strip()
    return result


def extract_response(report_path: Path) -> str:
    """Extract the worker's response section from the report."""
    text = report_path.read_text()
    m = re.search(r"## Response\s*\n(.*?)(?:\n## |\Z)", text, re.DOTALL)
    if m:
        return m.group(1).strip()
    m = re.search(r"---\n.*?\n---\n(.*?)(?:\n## |\Z)", text, re.DOTALL)
    return m.group(1).strip() if m else ""


def score_codegen(response: str) -> int:
    """Extract Python function, exec, verify add(2,3)==5. Return 0-5."""
    if not response:
        return 0
    m = re.search(r"```(?:python)?\n(.*?)```", response, re.DOTALL)
    code = m.group(1).strip() if m else response.strip()
    if not code or "def add" not in code:
        return 0
    try:
        ns: dict = {}
        exec(code, ns)
    except SyntaxError:
        return 1
    except Exception:
        return 2
    if "add" not in ns:
        return 2
    try:
        if ns["add"](2, 3) == 5 and ns["add"](-1, 1) == 0:
            return 5
        return 3
    except Exception:
        return 2


def score_review(response: str) -> int:
    """Heuristic for review task: format compliance + bug-correctness keyword."""
    if not response:
        return 0
    r = response.strip().lower()
    has_format = r.startswith("bug:") or "bug:" in r.split("\n")[0]
    keywords_for_real_bug = ("zero", "division", "divid", "divide by 0", "/0", "zerodivision")
    has_real_bug = any(kw in r for kw in keywords_for_real_bug)
    if has_format and has_real_bug:
        return 5
    if has_real_bug:
        return 3
    if has_format:
        return 2
    return 1


def score_docs(response: str) -> int:
    """Heuristic for docs task: triple-quoted single-line docstring."""
    if not response:
        return 0
    r = response.strip()
    has_triple = '"""' in r
    lines = [ln for ln in r.split("\n") if ln.strip() and '"""' not in ln]
    is_oneline = len(lines) <= 1
    mentions_sum = any(kw in r.lower() for kw in ("sum", "add", "integer", "return"))
    if has_triple and is_oneline and mentions_sum:
        return 5
    if has_triple and mentions_sum:
        return 3
    if mentions_sum:
        return 2
    return 1


def run_benchmark(args: argparse.Namespace) -> list:
    """Main loop: lanes × tasks × N runs, optionally parallel."""
    selected_lanes = (
        [l.strip() for l in args.lanes.split(",")] if args.lanes else list(LANES.keys())
    )
    selected_tasks = (
        [t.strip() for t in args.tasks.split(",")] if args.tasks else list(TASKS.keys())
    )
    unknown_lanes = [l for l in selected_lanes if l not in LANES]
    unknown_tasks = [t for t in selected_tasks if t not in TASKS]
    if unknown_lanes:
        sys.exit(f"Unknown lanes: {unknown_lanes}. Available: {list(LANES.keys())}")
    if unknown_tasks:
        sys.exit(f"Unknown tasks: {unknown_tasks}. Available: {list(TASKS.keys())}")

    jobs = [
        (lane, task, run)
        for lane in selected_lanes
        for task in selected_tasks
        for run in range(1, args.n + 1)
    ]
    print(f"[bench] {len(jobs)} dispatches: {len(selected_lanes)} lanes × "
          f"{len(selected_tasks)} tasks × N={args.n}, parallel={args.parallel}",
          file=sys.stderr)

    results: list = []
    with ThreadPoolExecutor(max_workers=args.parallel) as pool:
        futures = {pool.submit(fire_dispatch, l, t, r): (l, t, r) for l, t, r in jobs}
        for fut in as_completed(futures):
            l, t, r = futures[fut]
            try:
                res = fut.result()
                results.append(res)
                tag = "OK " if res["exit_code"] == 0 else "ERR"
                print(f"[bench] {tag} {l}/{t}/r{r} "
                      f"q={res['quality_score']} t={res['wall_seconds']}s "
                      f"exit={res['exit_code']}", file=sys.stderr)
            except Exception as e:
                print(f"[bench] CRASH {l}/{t}/r{r}: {e}", file=sys.stderr)
                results.append({"lane": l, "task": t, "run": r, "error": str(e)})
    return results


def write_csv(results: list, path: Path) -> None:
    if not results:
        return
    fieldnames = [
        "lane", "task", "run", "dispatch_id", "exit_code",
        "wall_seconds", "report_duration", "quality_score",
        "provider", "model", "input_tokens", "output_tokens", "cost_usd",
        "stderr_tail", "response_preview",
    ]
    with path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(results)


def write_summary(results: list, path: Path, args: argparse.Namespace) -> None:
    by_lane: dict = {}
    for r in results:
        if "error" in r:
            continue
        by_lane.setdefault(r["lane"], []).append(r)

    lines = [
        f"# Smart-Lanes Benchmark — {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        "",
        f"Runs: {len(results)} total ({len(by_lane)} lanes × N={args.n})",
        "",
        "## Per-lane aggregates",
        "",
        "| Lane | Runs | OK | Avg wall (s) | Avg report-dur (s) | Avg quality | Total cost ($) |",
        "|---|---|---|---|---|---|---|",
    ]
    for lane in sorted(by_lane.keys()):
        runs = by_lane[lane]
        ok = sum(1 for r in runs if r["exit_code"] == 0)
        avg_wall = sum(r["wall_seconds"] for r in runs) / len(runs)
        durs = [float(r["report_duration"]) for r in runs if r.get("report_duration")]
        avg_dur = (sum(durs) / len(durs)) if durs else 0.0
        avg_q = sum(r["quality_score"] for r in runs) / len(runs)
        costs = [float(r["cost_usd"]) for r in runs
                 if r.get("cost_usd") not in (None, "", "0.0")]
        total_cost = sum(costs)
        lines.append(
            f"| {lane} | {len(runs)} | {ok}/{len(runs)} | "
            f"{avg_wall:.1f} | {avg_dur:.1f} | {avg_q:.1f} | {total_cost:.5f} |"
        )

    lines += ["", "## Per-(lane, task) quality matrix", ""]
    tasks_present = sorted({r["task"] for r in results if "error" not in r})
    header = "| Lane | " + " | ".join(tasks_present) + " |"
    sep = "|---|" + "|".join("---" for _ in tasks_present) + "|"
    lines.append(header)
    lines.append(sep)
    for lane in sorted(by_lane.keys()):
        cells = []
        for t in tasks_present:
            qs = [r["quality_score"] for r in by_lane[lane] if r["task"] == t]
            cells.append(f"{(sum(qs)/len(qs)):.1f}" if qs else "-")
        lines.append(f"| {lane} | " + " | ".join(cells) + " |")

    lines += [
        "",
        "## Notes",
        "",
        "- Quality scoring: codegen=programmatic (exec + verify), "
        "review/docs=heuristic (format + keyword).",
        "- Manual review of response_preview in CSV recommended for review/docs cells.",
        "- Cost field empty/0 indicates provider did not report cost in receipt.",
    ]
    path.write_text("\n".join(lines) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description="Smart-lanes benchmark runner")
    parser.add_argument("--n", type=int, default=1,
                        help="Runs per (lane, task) cell (default 1)")
    parser.add_argument("--lanes", default="",
                        help=f"Comma-separated subset of {list(LANES.keys())}")
    parser.add_argument("--tasks", default="",
                        help=f"Comma-separated subset of {list(TASKS.keys())}")
    parser.add_argument("--parallel", type=int, default=6,
                        help="Max parallel dispatches (default 6)")
    parser.add_argument("--output-prefix", default="",
                        help="Override filename prefix")
    args = parser.parse_args()

    results = run_benchmark(args)
    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    prefix = args.output_prefix or f"benchmarks-{stamp}"
    csv_path = CLAUDEDOCS_DIR / f"{prefix}.csv"
    md_path = CLAUDEDOCS_DIR / f"{prefix}-summary.md"
    write_csv(results, csv_path)
    write_summary(results, md_path, args)
    print(f"[bench] wrote {csv_path} ({len(results)} rows)", file=sys.stderr)
    print(f"[bench] wrote {md_path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
