#!/usr/bin/env python3
"""judge_quality.py — Use Claude Opus to score benchmark responses.

For each result file in results/:
  - Read original task prompt + model response
  - Call claude -p with judge-prompt: rate quality, correctness, completeness
  - Parse JSON response, add scores to result file (atomic write)

Usage:
    python3 scripts/benchmark/judge_quality.py [--results-dir PATH] [--model opus]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Dict, Optional

BENCHMARK_DIR = Path(__file__).resolve().parent
RESULTS_DIR = BENCHMARK_DIR / "results"
PROMPTS_DIR = BENCHMARK_DIR / "prompts"

JUDGE_PROMPT_TEMPLATE = """Score this AI response on three dimensions. Be objective; you're evaluating output quality, not the model's internal reasoning.

Original task:
{task_prompt}

Response from {anon_label}:
{response}

Output ONLY a JSON object with exactly these fields:
{{
  "quality_score": <int 1-10>,
  "correctness": <true|false>,
  "completeness_score": <int 1-10>,
  "notable_issues": "<one sentence, or empty string>"
}}

No markdown, no prose, no code fences. Raw JSON only.
"""

_FALLBACK_SCORE = {
    "quality_score": 0,
    "correctness": False,
    "completeness_score": 0,
    "notable_issues": "judge failed to parse",
}


def anonymize_model_id(model_id: str) -> str:
    """Deterministic anonymized label so judge cannot recognize the model."""
    short_hash = hashlib.sha256(model_id.encode()).hexdigest()[:8]
    return f"X-anon-{short_hash}"


def _load_task_prompt(task_id: str, prompts_dir: Path) -> str:
    path = prompts_dir / f"{task_id}.txt"
    return path.read_text() if path.exists() else f"(prompt file not found: {task_id}.txt)"


def _call_judge(prompt: str, model: str, timeout: int) -> str:
    """Invoke claude -p with prompt on stdin (avoids ARG_MAX and process-table exposure)."""
    proc = subprocess.Popen(
        ["claude", "-p", "--model", model],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        stdout, stderr = proc.communicate(input=prompt, timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        raise
    except BrokenPipeError:
        raise RuntimeError("BrokenPipeError: claude process terminated before accepting prompt")
    if proc.returncode != 0:
        raise RuntimeError(f"claude returned {proc.returncode}: {(stderr or '')[:200]}")
    return stdout.strip()


def _parse_judge_response(raw: str) -> Dict:
    """Extract JSON object from judge output. Returns fallback dict on parse failure."""
    raw = raw.strip()
    # Strip markdown code fences if model wrapped it
    if raw.startswith("```"):
        lines = raw.splitlines()
        inner = [ln for ln in lines if not ln.startswith("```")]
        raw = "\n".join(inner).strip()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return dict(_FALLBACK_SCORE)

    score: Dict = {}
    score["quality_score"] = int(data.get("quality_score") or 0)
    score["correctness"] = bool(data.get("correctness", False))
    score["completeness_score"] = int(data.get("completeness_score") or 0)
    score["notable_issues"] = str(data.get("notable_issues") or "")
    return score


def judge_result_file(result_path: Path, prompts_dir: Path, model: str, timeout: int) -> Dict:
    """Score one result JSON file. Returns the score dict."""
    data = json.loads(result_path.read_text())
    task_id = data.get("task_id", "")
    model_id = data.get("model_id", "")
    response = data.get("response", "")

    task_prompt = _load_task_prompt(task_id, prompts_dir)
    anon_label = anonymize_model_id(model_id)
    judge_prompt = JUDGE_PROMPT_TEMPLATE.format(
        task_prompt=task_prompt,
        anon_label=anon_label,
        response=response or "(empty response)",
    )

    try:
        raw = _call_judge(judge_prompt, model, timeout)
        score = _parse_judge_response(raw)
    except Exception as exc:
        score = dict(_FALLBACK_SCORE)
        score["notable_issues"] = f"judge error: {exc}"

    data["judge_scores"] = score
    data["judge_model"] = model

    tmp_path = result_path.with_suffix(".tmp")
    tmp_path.write_text(json.dumps(data, indent=2))
    tmp_path.replace(result_path)

    return score


def main() -> int:
    parser = argparse.ArgumentParser(description="Judge benchmark responses via Claude Opus")
    parser.add_argument("--results-dir", default=None, help="Path to results directory")
    parser.add_argument("--model", default="opus", help="Claude model to use as judge")
    parser.add_argument("--timeout", type=int, default=120, help="Per-call timeout in seconds")
    parser.add_argument("--skip-judged", action="store_true", help="Skip files already containing judge_scores")
    args = parser.parse_args()

    results_dir = Path(args.results_dir) if args.results_dir else RESULTS_DIR
    if not results_dir.exists():
        print(f"Results directory not found: {results_dir}")
        return 1

    result_files = sorted(results_dir.glob("*.json"))
    if not result_files:
        print(f"No result files found in {results_dir}")
        return 0

    print(f"Judging {len(result_files)} result files with model={args.model}")

    errors = 0
    for i, path in enumerate(result_files, 1):
        if args.skip_judged:
            data = json.loads(path.read_text())
            if "judge_scores" in data:
                print(f"  [{i}/{len(result_files)}] skip (already judged): {path.name}")
                continue

        print(f"  [{i}/{len(result_files)}] {path.name}...")
        try:
            score = judge_result_file(path, PROMPTS_DIR, args.model, args.timeout)
            q = score["quality_score"]
            c = "correct" if score["correctness"] else "incorrect"
            comp = score["completeness_score"]
            print(f"    quality={q}/10 correctness={c} completeness={comp}/10")
        except Exception as exc:
            print(f"    ERROR: {exc}")
            errors += 1

    print(f"\nJudge complete: {len(result_files) - errors} ok, {errors} errors")
    return 0 if errors == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
