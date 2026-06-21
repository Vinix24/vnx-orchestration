#!/usr/bin/env python3
"""export_routing_matrix.py — refresh the smart-router matrix from the field-tests benchmark.

Reads every `scripts/benchmark/field-tests/results/*/raw.csv`, aggregates the
measured t1-t6 cells per (router task_class, model), and emits a routing matrix
in the schema `smart_router.py` reads (`routing_by_task` -> task_class ->
{min_quality_tier, candidates:[{model_id, composite_score, cost_usd_per_call,
avg_duration_seconds, runner, launch_success_rate, n}]}).

Honesty rules (operator's 100%-fair bar):
  * composite_score is the mean of GENUINE attempts only — cells with
    wallclock < REAL_RUN_S are immediate-exits (rate-limit / launch-fail), not
    capability, and are excluded from the score (counted against launch rate).
  * launch_success_rate = produced cells / attempted cells, so a lane that
    launches 1/3 of the time (codex) carries the penalty as data, not a hidden
    low score (the old file stored DNFs as composite 1.0 — that bug is removed).
  * runner is first-class: GLM via the flat runner and GLM via the claude
    harness are DIFFERENT rows, so the "GLM flat-runner trap" is representable.
  * benchmark composite is 0-5; the router scale is 0-10, so scores are x2.

Writes to a SIDE file by default (review before swapping the live config).
"""
from __future__ import annotations

import argparse
import csv
import glob
import statistics
from collections import defaultdict
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
RESULTS = HERE / "field-tests" / "results"
LIVE = HERE.parent / "lib" / "providers" / "routing_recommendations.yaml"

REAL_RUN_S = 5.0  # below this a cell is an immediate-exit, not a genuine attempt

# benchmark task_id -> router task_class (explicit, judgment documented in the design doc)
TASK_CLASS = {
    "01_yaml_config_refactor": "03_refactoring",
    "05_extractor_subclass": "03_refactoring",
    "02_rls_policy": "01_code_generation",
    "03_dotenv_script": "01_code_generation",
    "04_scorer_task": "01_code_generation",
    "07_state_machine_sse": "01_code_generation",
    "08_ssrf_async_fetch": "01_code_generation",
    "t4_01_path_sandbox": "01_code_generation",
    "06_flaky_mock_fix": "05_debugging",
    "09_mock_introspection_trap": "05_debugging",
    "t4_02_subtle_bugfix": "05_debugging",
    "t5_01_planted_review": "02_code_review",
    "t6_01_seocrawler_review": "02_code_review",
    "t6_02_salescopilot_review": "02_code_review",
    "t5_02_agent_engine_design": "06_design",
}

# benchmark lane -> (router model_id, runner). harness vs flat is the routing dimension.
LANE = {
    "claude-opus-4-8": ("claude-opus-4-8", "subscription"),
    "claude-opus-4-7": ("claude-opus-4-7", "subscription"),
    "claude-opus-4-6": ("claude-opus-4-6", "subscription"),
    "claude-sonnet-4-6": ("claude-sonnet-4-6", "subscription"),
    "codex-gpt-5-5": ("codex-gpt-5-5", "codex-cli"),
    "codex-gpt-5-4": ("codex-gpt-5-4", "codex-cli"),
    "kimi-k2-7-code": ("kimi-k2-7-code", "kimi-cli"),
    "glm-5": ("glm-5", "flat-runner"),
    "glm-5-1": ("glm-5-1", "flat-runner"),
    "glm-5-2": ("glm-5-2", "flat-runner"),
    "glm-5-2-harness": ("glm-5-2", "claude-harness"),
    "deepseek-v4-flash-harness": ("deepseek-v4-flash", "claude-harness"),
    "deepseek-v4-pro-harness": ("deepseek-v4-pro", "claude-harness"),
    # kimi-k2-6 deliberately dropped (superseded by kimi-k2-7-code)
}


def _quality_tier(score10: float) -> int:
    if score10 >= 7.5:
        return 3
    if score10 >= 5.0:
        return 2
    return 1


def aggregate():
    # (task_class, model_id, runner) -> {scores:[genuine composites], attempts, produced, costs, durations}
    cells = defaultdict(lambda: {"scores": [], "attempts": 0, "produced": 0, "costs": [], "durs": []})
    for f in glob.glob(str(RESULTS / "*" / "raw.csv")):
        try:
            for r in csv.DictReader(open(f)):
                task = r.get("task_id", "").strip()
                lane = r.get("lane_id", "").strip()
                if task not in TASK_CLASS or lane not in LANE:
                    continue
                tc = TASK_CLASS[task]
                model_id, runner = LANE[lane]
                try:
                    comp = float(r["composite"]); wall = float(r["wallclock_seconds"])
                except (KeyError, ValueError):
                    continue
                key = (tc, model_id, runner)
                cells[key]["attempts"] += 1
                # A genuine attempt RAN (wall>=5s). Its composite counts even when 0
                # (capability-fail), so the score is honest "how good when it runs".
                # Immediate-exits (wall<5s) are infra, not capability: they only drag
                # launch_success_rate, never the score.
                if wall >= REAL_RUN_S:
                    cells[key]["produced"] += 1
                    cells[key]["scores"].append(comp)
                    cells[key]["durs"].append(wall)
                    try:
                        c = float(r.get("cost_usd", "") or 0)
                        if c > 0:
                            cells[key]["costs"].append(c)
                    except ValueError:
                        pass
        except Exception:
            pass
    return cells


def build(cells):
    by_class = defaultdict(list)
    for (tc, model_id, runner), d in cells.items():
        if not d["scores"]:
            continue  # no genuine run -> no capability evidence; omit (don't store a DNF as a score)
        score10 = round(statistics.mean(d["scores"]) * 2, 2)  # 0-5 -> 0-10
        cand = {
            "model_id": model_id,
            "runner": runner,
            "composite_score": score10,
            "quality_tier": _quality_tier(score10),
            "cost_usd_per_call": round(statistics.mean(d["costs"]), 4) if d["costs"] else None,
            "avg_duration_seconds": round(statistics.mean(d["durs"]), 1),
            "launch_success_rate": round(d["produced"] / d["attempts"], 2) if d["attempts"] else 0.0,
            "n": d["produced"],
        }
        by_class[tc].append(cand)
    out = {"routing_by_task": {}, "_meta": {
        "source": "field-tests t1-t6 benchmark (export_routing_matrix.py)",
        "scale": "composite_score 0-10 (benchmark 0-5 x2)",
        "honesty": "score = genuine attempts (wall>=5s, capability-0s included); launch_success_rate carries reliability; runner is first-class",
        "scope": (
            "WORKER lane routing ONLY. A model's score here = how well it PRODUCES a "
            "scored deliverable. It is NOT the gate-model policy: gate-panel composition "
            "(adversarial defect-finding, e.g. codex 'almost always finds something' on "
            "PR-4/PR-9) is a SEPARATE PM-skill policy driven by proven defect-recall + "
            "family diversity. A low worker score NEVER removes a model from the gate role."
        ),
    }}
    for tc in sorted(by_class):
        cands = sorted(by_class[tc], key=lambda c: -c["composite_score"])
        floor = 3 if tc in ("02_code_review", "06_design") else 2 if tc in ("05_debugging",) else None
        block = {"candidates": cands}
        if floor:
            block["min_quality_tier"] = floor
        out["routing_by_task"][tc] = block
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(LIVE) + ".refresh", help="side file (default: *.refresh; review before swap)")
    ap.add_argument("--apply", action="store_true", help="write the LIVE routing_recommendations.yaml directly (use after review)")
    args = ap.parse_args()
    cells = aggregate()
    matrix = build(cells)
    target = str(LIVE) if args.apply else args.out
    with open(target, "w") as fh:
        yaml.safe_dump(matrix, fh, sort_keys=False, default_flow_style=False)
    n_cls = len(matrix["routing_by_task"])
    n_cand = sum(len(v["candidates"]) for v in matrix["routing_by_task"].values())
    print(f"wrote {target}: {n_cls} task-classes, {n_cand} candidates")


if __name__ == "__main__":
    main()
