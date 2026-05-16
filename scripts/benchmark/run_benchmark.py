#!/usr/bin/env python3
"""run_benchmark.py — Execute benchmark suite across N models x M tasks.

Usage:
    python3 scripts/benchmark/run_benchmark.py [--models MODEL_IDS] [--tasks TASK_IDS] [--output PATH]
    python3 scripts/benchmark/run_benchmark.py --dry-run

For each (model, task):
  - Dispatch via provider_dispatch.py
  - Wait for completion
  - Read receipt from the receipts NDJSON log
  - Read unified report
  - Capture: duration_seconds, token_usage, cost_usd, response_text
  - Save to results/{model_id}__{task_id}.json

INFRASTRUCTURE ONLY — does not execute benchmarks by itself without explicit --run flag.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import time
import sys
from pathlib import Path
from typing import Dict, List, Optional

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
BENCHMARK_DIR = Path(__file__).resolve().parent
PROMPTS_DIR = BENCHMARK_DIR / "prompts"
RESULTS_DIR = BENCHMARK_DIR / "results"


def load_models(models_yaml: Optional[Path] = None) -> List[Dict]:
    path = models_yaml or (BENCHMARK_DIR / "models.yaml")
    config = yaml.safe_load(path.read_text())
    return config["models"]


def load_tasks(prompts_dir: Optional[Path] = None) -> List[Dict]:
    source = prompts_dir or PROMPTS_DIR
    tasks = []
    for prompt_file in sorted(source.glob("*.txt")):
        tasks.append({
            "id": prompt_file.stem,
            "prompt": prompt_file.read_text(),
        })
    return tasks


def run_single(model: Dict, task: Dict, dispatch_id: str) -> Dict:
    """Dispatch single (model, task) pair. Return result dict."""
    start = time.time()
    cmd = [
        "python3",
        str(REPO_ROOT / "scripts" / "lib" / "provider_dispatch.py"),
        "--provider", model["provider"],
        "--terminal-id", "T2",
        "--dispatch-id", dispatch_id,
        "--role", "backend-developer",
        "--instruction", task["prompt"],
    ]
    if model.get("model_arg") and model["provider"] == "claude":
        cmd.extend(["--model", model["model_arg"]])

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
    except BrokenPipeError:
        return _error_result(model, task, dispatch_id, "BrokenPipeError from provider", time.time() - start)

    duration = time.time() - start

    receipts_path = REPO_ROOT / ".vnx-data" / "receipts" / "t0_receipts.ndjson"
    receipt = _read_last_receipt(receipts_path, dispatch_id)

    report_path = REPO_ROOT / ".vnx-data" / "unified_reports" / f"{dispatch_id}_report.md"
    report_text = report_path.read_text() if report_path.exists() else ""

    return {
        "model_id": model["id"],
        "task_id": task["id"],
        "dispatch_id": dispatch_id,
        "duration_seconds": round(duration, 3),
        "exit_code": proc.returncode,
        "stderr": (proc.stderr or "")[:500],
        "receipt": receipt,
        "response": _extract_response_from_report(report_text),
        "cost_usd": _compute_cost_usd(model, receipt),
    }


def _error_result(model: Dict, task: Dict, dispatch_id: str, error: str, duration: float) -> Dict:
    return {
        "model_id": model["id"],
        "task_id": task["id"],
        "dispatch_id": dispatch_id,
        "duration_seconds": round(duration, 3),
        "exit_code": -1,
        "stderr": error,
        "receipt": {},
        "response": "",
        "cost_usd": None,
    }


def _read_last_receipt(path: Path, dispatch_id: str) -> Dict:
    if not path.exists():
        return {}
    for line in reversed(path.read_text().splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
            if record.get("dispatch_id") == dispatch_id:
                return record
        except json.JSONDecodeError:
            continue
    return {}


def _extract_response_from_report(report: str) -> str:
    if "## Response" not in report:
        return ""
    parts = report.split("## Response", 1)
    if len(parts) < 2:
        return ""
    tail = parts[1]
    response = tail.split("\n## ", 1)[0] if "\n## " in tail else tail
    return response.strip()


def _compute_cost_usd(model: Dict, receipt: Dict) -> Optional[float]:
    """Estimate cost from receipt token counts + model pricing."""
    input_tokens = receipt.get("input_tokens", 0) or 0
    output_tokens = receipt.get("output_tokens", 0) or 0
    if not input_tokens and not output_tokens:
        return None
    cost_in = (input_tokens / 1_000_000) * model.get("cost_input_mtok", 0)
    cost_out = (output_tokens / 1_000_000) * model.get("cost_output_mtok", 0)
    return round(cost_in + cost_out, 6)


def _filter_models(models: List[Dict], selector: str) -> List[Dict]:
    if selector == "all":
        return models
    wanted = {m.strip() for m in selector.split(",")}
    return [m for m in models if m["id"] in wanted]


def _filter_tasks(tasks: List[Dict], selector: str) -> List[Dict]:
    if selector == "all":
        return tasks
    wanted = {t.strip() for t in selector.split(",")}
    return [t for t in tasks if t["id"] in wanted]


def main() -> int:
    parser = argparse.ArgumentParser(description="VNX benchmark runner — 9 models x 7 tasks")
    parser.add_argument("--models", default="all", help="Comma-separated model IDs or 'all'")
    parser.add_argument("--tasks", default="all", help="Comma-separated task IDs or 'all'")
    parser.add_argument("--output", default=None, help="Override output directory")
    parser.add_argument("--dry-run", action="store_true", help="List dispatches without executing")
    parser.add_argument("--run", action="store_true", help="Actually execute benchmarks")
    args = parser.parse_args()

    if not args.dry_run and not args.run:
        print("Pass --dry-run to list planned dispatches, or --run to execute.")
        print("No benchmarks executed (infrastructure-only mode).")
        return 0

    output_dir = Path(args.output) if args.output else RESULTS_DIR
    output_dir.mkdir(parents=True, exist_ok=True)

    models = _filter_models(load_models(), args.models)
    tasks = _filter_tasks(load_tasks(), args.tasks)

    total = len(models) * len(tasks)
    print(f"Benchmark plan: {len(models)} models x {len(tasks)} tasks = {total} dispatches")

    if args.dry_run:
        for m in models:
            for t in tasks:
                print(f"  dispatch: {m['id']} x {t['id']}")
        return 0

    completed = 0
    errors = 0
    for model in models:
        for task in tasks:
            ts = int(time.time())
            dispatch_id = f"bench-{model['id']}-{task['id']}-{ts}"
            print(f"[{completed + 1}/{total}] {model['id']} x {task['id']}...")
            try:
                result = run_single(model, task, dispatch_id)
                result_path = output_dir / f"{model['id']}__{task['id']}.json"
                result_path.write_text(json.dumps(result, indent=2))
                cost = result.get("cost_usd")
                dur = result["duration_seconds"]
                cost_str = f"${cost:.4f}" if cost is not None else "?"
                print(f"  ok {dur:.1f}s cost={cost_str}")
            except subprocess.TimeoutExpired:
                print(f"  TIMEOUT after 900s")
                errors += 1
            except Exception as exc:
                print(f"  ERROR: {exc}")
                errors += 1
            completed += 1

    print(f"\nDone: {completed - errors}/{total} succeeded, {errors} errors.")
    return 0 if errors == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
