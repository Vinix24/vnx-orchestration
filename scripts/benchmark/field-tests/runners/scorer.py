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


REPO_ROOT = Path(__file__).resolve().parents[4]
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
    input_tokens: int = 0
    output_tokens: int = 0
    tokens_per_second: float = 0.0   # output_tokens / wallclock (effective throughput)


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


def _build_judge_prompt(
    instruction: str, worker_output: str, verify_evidence: str,
    expected_style: Optional[dict] = None,
) -> str:
    """Single prompt for all judge providers — kept identical for fair cross-comparison."""
    return (
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


def _parse_judge_json(out: str) -> Optional[tuple[float, str]]:
    """Extract (score, reasoning) from a judge's stdout. Returns None on parse-fail."""
    for line in out.strip().splitlines():
        line = line.strip()
        if line.startswith("{") and "code_quality" in line:
            try:
                parsed = json.loads(line)
                return float(parsed["code_quality"]), str(parsed.get("reasoning", ""))
            except (json.JSONDecodeError, KeyError, ValueError):
                continue
    return None


def _judge_claude(prompt: str) -> Optional[tuple[float, str]]:
    """Opus subprocess judge. Returns (score, reasoning) or None on all-fallbacks-failed."""
    for model in (JUDGE_MODEL, JUDGE_FALLBACK):
        try:
            proc = subprocess.run(
                ["claude", "--print", "--model", model, prompt],
                capture_output=True, text=True, timeout=120, check=False,
            )
            if proc.returncode != 0:
                continue
            result = _parse_judge_json(proc.stdout)
            if result is not None:
                return result
        except (subprocess.TimeoutExpired, FileNotFoundError):
            continue
    return None


def _judge_kimi(prompt: str) -> Optional[tuple[float, str]]:
    """Kimi CLI subprocess judge (cross-model verification of claude-judge)."""
    try:
        proc = subprocess.run(
            ["kimi", "--print", "--prompt", prompt],
            capture_output=True, text=True, timeout=120, check=False,
        )
        if proc.returncode != 0:
            # Common transient failures: quota exhausted, auth expired.
            # Logged but not raised — panel falls back to claude-only.
            return None
        return _parse_judge_json(proc.stdout)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None


def _llm_judge_code_quality(
    instruction: str, worker_output: str, verify_evidence: str,
    expected_style: Optional[dict] = None,
) -> tuple[float, str]:
    """Cross-provider judge panel. Returns (avg_score, combined_reasoning).

    Runs claude (Opus) + kimi in parallel; averages scores; flags disagreement.
    Falls back to single-judge if one provider unavailable. If both fail,
    returns 0.0 with "judge_unavailable" so the scorer can distinguish from
    a real "0/5" verdict.

    Disagreement threshold: score-spread > 1.5 between judges. Flagged in
    reasoning so the operator can spot-check on aggregate review.
    """
    prompt = _build_judge_prompt(instruction, worker_output, verify_evidence, expected_style)

    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=2) as pool:
        f_claude = pool.submit(_judge_claude, prompt)
        f_kimi = pool.submit(_judge_kimi, prompt)
        claude_result = f_claude.result()
        kimi_result = f_kimi.result()

    scores: list[float] = []
    reasonings: list[str] = []
    if claude_result is not None:
        scores.append(claude_result[0])
        reasonings.append(f"opus={claude_result[0]:.1f}: {claude_result[1]}")
    if kimi_result is not None:
        scores.append(kimi_result[0])
        reasonings.append(f"kimi={kimi_result[0]:.1f}: {kimi_result[1]}")

    if not scores:
        return 0.0, "judge_unavailable (both opus and kimi failed)"

    avg_score = round(sum(scores) / len(scores), 2)
    disagreement_flag = ""
    if len(scores) == 2 and abs(scores[0] - scores[1]) > 1.5:
        disagreement_flag = f" [DISAGREEMENT spread={abs(scores[0] - scores[1]):.1f}]"

    combined = " | ".join(reasonings) + disagreement_flag
    return avg_score, combined


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


def _extract_tokens_from_report(report_path: Path) -> "tuple[int, int]":
    """Parse (input_tokens, output_tokens) from the report frontmatter token_usage block.

    Frontmatter shape:
        token_usage:
          input: 1234
          output: 567
    Returns (0, 0) when absent or unparseable (e.g. subscription lanes that report no
    usage). A 0 here means "not measured", not "zero work".
    """
    if not report_path or not report_path.exists():
        return 0, 0

    def _safe_int(s: str) -> int:
        try:
            return int(float(s.strip()))
        except ValueError:
            return 0

    try:
        lines = report_path.read_text(encoding="utf-8", errors="ignore").splitlines()[:40]
    except OSError:
        return 0, 0
    in_block = False
    inp = out = 0
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("token_usage:"):
            in_block = True
            continue
        if in_block:
            if stripped.startswith("input:"):
                inp = _safe_int(stripped.split(":", 1)[1])
            elif stripped.startswith("output:"):
                out = _safe_int(stripped.split(":", 1)[1])
            elif stripped and not line.startswith((" ", "\t")):
                break  # dedent to a new top-level key → left the token_usage block
    return inp, out


def score_cell(
    dispatch_result, task_meta: dict, task_folder: Path,
    instruction: str, expected_files: list[str],
    expected_rubric: Optional[dict] = None, run_judge: bool = True,
) -> CellScore:
    """Score a completed dispatch against a task. Returns CellScore.

    Dispatch-failure handling: if the dispatcher itself failed (success=False),
    record a DNF cell with composite=0. Without this guard, verify.py would run
    against the seed/ reference solution and award partial credit (3.50 baseline)
    for cells where the worker did literally nothing — inflating failed cells
    above the no-show-deserves-zero line.
    """
    # VNX_BENCH_SCORE_DELIVERABLE_ON_FAILURE: some harness lanes (glm-harness) produce a
    # CORRECT deliverable but the claude CLI exits rc=1 (GLM doesn't close the agentic loop
    # cleanly), so dispatch_result.success is False even though the work is done. When set,
    # fall through to verify the on-disk deliverable instead of recording an automatic DNF.
    # SAFE for from-scratch / new-file tasks (a no-op leaves no deliverable → verify scores 0);
    # for seed-based tasks verify checks the actual change, not the untouched seed.
    _score_on_failure = os.environ.get("VNX_BENCH_SCORE_DELIVERABLE_ON_FAILURE") == "1"
    if not dispatch_result.success and not _score_on_failure:
        return CellScore(
            lane_id=dispatch_result.lane_id,
            task_id=dispatch_result.task_id,
            replication=dispatch_result.replication,
            correctness=0.0,
            completeness=0.0,
            cost_efficiency=0.0,
            wallclock_efficiency=0.0,
            code_quality=0.0,
            composite=0.0,
            verify_evidence=f"DNF: {dispatch_result.error or 'dispatch_failed'}"[:500],
            judge_reasoning="skipped (dispatch failed)",
            cost_usd=0.0,
            wallclock_seconds=dispatch_result.wallclock_seconds,
        )

    # Verify in the checkout the worker actually wrote to. Headless/provider
    # lanes work in the repo root; tmux lanes provide their (preserved or
    # branch-restored) worktree via DispatchResult.workdir. Scoring the repo
    # root for a tmux cell measures the wrong checkout (codex-gate PR #831).
    workdir = getattr(dispatch_result, "workdir", None) or REPO_ROOT
    verify_mod = _load_verify_module(task_folder)
    try:
        verify_result = verify_mod.verify(workdir, task_meta)
    except Exception as exc:
        verify_result = {"pass": False, "evidence": f"verify-crash: {exc}", "details": {}}

    correctness = _compute_correctness(verify_result)
    completeness = _compute_completeness(verify_result, expected_files)

    cost_usd = _extract_cost_from_report(dispatch_result.report_path)
    cost_eff = _compute_cost_efficiency(cost_usd, task_meta.get("tier", ""))
    input_tokens, output_tokens = _extract_tokens_from_report(dispatch_result.report_path)
    _wall = dispatch_result.wallclock_seconds
    tokens_per_second = (
        round(output_tokens / _wall, 2) if _wall > 0 and output_tokens else 0.0
    )
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
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        tokens_per_second=tokens_per_second,
    )
