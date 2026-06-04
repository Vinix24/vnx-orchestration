"""scorer.py — programmatic verify + LLM-judge for each (lane, task, replication) cell.

Each task folder contains a `verify.py` that must expose:
    def verify(workdir: Path, expected: dict) -> dict
        # returns: {"pass": bool, "evidence": str, "details": dict}

This runner imports + calls verify(), then optionally invokes an LLM-judge
(Opus) for the `code_quality` dimension that programmatic checks can't cover.

Composite score is weighted per scoring.yaml dimensions:
    correctness 0.40, completeness 0.20, cost_efficiency 0.15,
    wallclock_efficiency 0.15, code_quality 0.10
Each dimension scored 0-5, multiplied by weight, summed → composite 0-5.
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


REPO_ROOT = Path(__file__).resolve().parents[3]
JUDGE_MODEL = "claude-opus-4-8"
JUDGE_FALLBACK = "claude-opus-4-7"


@dataclass
class CellScore:
    lane_id: str
    task_id: str
    replication: int
    correctness: float        # 0-5
    completeness: float       # 0-5
    cost_efficiency: float    # 0-5
    wallclock_efficiency: float  # 0-5
    code_quality: float       # 0-5
    composite: float          # 0-5 weighted
    verify_evidence: str
    judge_reasoning: str
    cost_usd: float
    wallclock_seconds: float


def _load_verify_module(task_folder: Path):
    """Dynamically import the verify.py from a task folder."""
    verify_path = task_folder / "verify.py"
    if not verify_path.exists():
        raise FileNotFoundError(f"Missing verify.py at {verify_path}")
    spec = importlib.util.spec_from_file_location(
        f"verify_{task_folder.name}", verify_path,
    )
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load spec for {verify_path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    if not hasattr(mod, "verify"):
        raise AttributeError(f"verify.py at {verify_path} missing verify() function")
    return mod


def _compute_correctness(verify_result: dict) -> float:
    """Map verify pass/fail + partial-credit to 0-5."""
    if verify_result.get("pass") is True:
        return 5.0
    details = verify_result.get("details", {})
    if "pass_count" in details and "expected" in details:
        ratio = details["pass_count"] / max(details["expected"], 1)
        return round(5.0 * ratio, 2)
    return 0.0


def _compute_completeness(verify_result: dict, expected_files: list[str]) -> float:
    """Did the worker write all expected output files?"""
    if not expected_files:
        return 5.0 if verify_result.get("pass") else 2.5
    written = verify_result.get("details", {}).get("files_written", [])
    if not written:
        return 0.0
    ratio = len([f for f in expected_files if f in written]) / len(expected_files)
    return round(5.0 * ratio, 2)


def _compute_cost_efficiency(cost_usd: float, tier: str) -> float:
    """Cost-tier-aware: cheaper-than-expected is good."""
    expected_max = {"t1_trivial": 0.05, "t2_medium": 0.50, "t3_complex": 5.00}.get(tier, 1.0)
    if cost_usd == 0:
        return 5.0
    if cost_usd >= expected_max * 4:
        return 0.0
    return round(max(0.0, 5.0 * (1.0 - cost_usd / (expected_max * 4))), 2)


def _compute_wallclock_efficiency(wallclock_seconds: float, deadline_seconds: int) -> float:
    """Faster-than-deadline is good. Hitting deadline = 0."""
    if wallclock_seconds >= deadline_seconds:
        return 0.0
    return round(5.0 * (1.0 - wallclock_seconds / deadline_seconds), 2)


def _llm_judge_code_quality(
    instruction: str, worker_output: str, verify_evidence: str,
    expected_style: Optional[dict] = None,
) -> tuple[float, str]:
    """Spawn Opus subprocess to judge code_quality 0-5. Returns (score, reasoning)."""
    prompt = (
        f"You are scoring a worker's response to a benchmark task.\n\n"
        f"Task: {instruction[:1500]}\n\n"
        f"Worker output excerpt:\n{worker_output[:3000]}\n\n"
        f"Verify evidence: {verify_evidence}\n\n"
        f"Score 0-5 on CODE QUALITY only:\n"
        f"- Idiomatic patterns for the language/framework\n"
        f"- No dead code, no TODOs, no placeholders\n"
        f"- Reasonable function/file size\n"
        f"- Matches expected_style: {json.dumps(expected_style or {})}\n\n"
        f'Respond with ONLY this JSON line:\n'
        f'{{"code_quality": <0-5>, "reasoning": "<one sentence>"}}'
    )
    for model in (JUDGE_MODEL, JUDGE_FALLBACK):
        try:
            proc = subprocess.run(
                ["claude", "--print", "--model", model, prompt],
                capture_output=True, text=True, timeout=120, check=False,
            )
            if proc.returncode != 0:
                continue
            out = proc.stdout.strip()
            for line in out.splitlines():
                line = line.strip()
                if line.startswith("{") and "code_quality" in line:
                    try:
                        parsed = json.loads(line)
                        return float(parsed["code_quality"]), str(parsed.get("reasoning", ""))
                    except (json.JSONDecodeError, KeyError, ValueError):
                        continue
        except (subprocess.TimeoutExpired, FileNotFoundError):
            continue
    return 0.0, "judge_unavailable"


def _extract_cost_from_report(report_path: Path) -> float:
    """Parse cost_usd from report YAML frontmatter if present."""
    if not report_path or not report_path.exists():
        return 0.0
    try:
        content = report_path.read_text(encoding="utf-8", errors="ignore")
        for line in content.splitlines()[:30]:
            line = line.strip()
            if line.startswith("cost_usd:"):
                try:
                    return float(line.split(":", 1)[1].strip())
                except ValueError:
                    return 0.0
    except OSError:
        return 0.0
    return 0.0


def score_cell(
    dispatch_result, task_meta: dict, task_folder: Path,
    instruction: str, expected_files: list[str],
    expected_rubric: Optional[dict] = None, run_judge: bool = True,
) -> CellScore:
    """Score a completed dispatch against a task. Returns CellScore."""
    workdir = REPO_ROOT
    verify_mod = _load_verify_module(task_folder)
    try:
        verify_result = verify_mod.verify(workdir, task_meta)
    except Exception as exc:
        verify_result = {"pass": False, "evidence": f"verify-crash: {exc}", "details": {}}

    correctness = _compute_correctness(verify_result)
    completeness = _compute_completeness(verify_result, expected_files)

    cost_usd = _extract_cost_from_report(dispatch_result.report_path)
    cost_eff = _compute_cost_efficiency(cost_usd, task_meta.get("tier", ""))
    wall_eff = _compute_wallclock_efficiency(
        dispatch_result.wallclock_seconds, task_meta.get("deadline_seconds", 600),
    )

    code_q = 0.0
    judge_reasoning = "skipped"
    if run_judge and dispatch_result.report_path and dispatch_result.report_path.exists():
        report_body = dispatch_result.report_path.read_text(encoding="utf-8", errors="ignore")
        code_q, judge_reasoning = _llm_judge_code_quality(
            instruction=instruction,
            worker_output=report_body,
            verify_evidence=verify_result.get("evidence", ""),
            expected_style=(expected_rubric or {}).get("expected_style"),
        )

    composite = round(
        0.40 * correctness
        + 0.20 * completeness
        + 0.15 * cost_eff
        + 0.15 * wall_eff
        + 0.10 * code_q,
        2,
    )

    return CellScore(
        lane_id=dispatch_result.lane_id,
        task_id=dispatch_result.task_id,
        replication=dispatch_result.replication,
        correctness=correctness,
        completeness=completeness,
        cost_efficiency=cost_eff,
        wallclock_efficiency=wall_eff,
        code_quality=code_q,
        composite=composite,
        verify_evidence=verify_result.get("evidence", "")[:500],
        judge_reasoning=judge_reasoning[:300],
        cost_usd=cost_usd,
        wallclock_seconds=dispatch_result.wallclock_seconds,
    )
