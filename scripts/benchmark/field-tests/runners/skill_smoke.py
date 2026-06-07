"""skill_smoke.py — E2E smoke-test for provider-agnostic skill injection.

Verifies that build_structured_prompt() actually reaches the worker and that
the worker ADOPTS the injected specialist role, on every dispatch mechanism:

| lane                    | mechanism under test                       |
|-------------------------|--------------------------------------------|
| claude-opus-4-6         | tmux-spawn interactive (subscription)      |
| claude-sonnet-4-6       | headless subprocess (HEADLESS_FORCED)      |
| deepseek-v4-pro-harness | deepseek via Claude-harness                |
| deepseek-v4-pro-bare    | bare litellm chat lane (wrapper report)    |
| kimi-k2-6               | kimi CLI OAuth                             |
| codex-gpt-5-4           | codex CLI                                  |

Design: the instruction is deliberately NEUTRAL (never says "security") and
contains an inline snippet with two planted flaws — a SQL injection +
hardcoded credential (security lens) and an off-by-one slice (generic lens).
A worker that received and adopted the security-engineer role surfaces the
injection/credential, which a worker without the role typically does not.

SCOPE (codex-gate PR #831 finding F3): this is a presence-based smoke, not a
strict "security-lead" gate. PASS = the security vocabulary AND a planted
vuln marker appear in the worker's response (after the CLOSING block), which
is strong evidence the role arrived and was acted on. It does NOT assert the
security finding is stated FIRST — ordering-strictness is deferred to the
skill-aware re-bench scorer. This is a dev smoke (printed PASS/FAIL), not a
governed gate; it is intentionally not routed through gate_recorder (F7).

Usage:
    python3 skill_smoke.py                      # all 6 lanes, parallel
    python3 skill_smoke.py --lane kimi-k2-6     # subset
"""
from __future__ import annotations

import argparse
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[3]

sys.path.insert(0, str(HERE))
from lane_adapter import dispatch as lane_dispatch, load_lanes  # noqa: E402

SMOKE_LANES = [
    "claude-opus-4-6",
    "claude-sonnet-4-6",
    "deepseek-v4-pro-harness",
    "deepseek-v4-pro-bare",
    "kimi-k2-6",
    "codex-gpt-5-4",
]

SKILL = "security-engineer"
DEADLINE_SECONDS = 600

# Neutral wording on purpose: the word "security" must come from the injected
# role, not from the assignment.
INSTRUCTION = """\
Assess the following Python function and report your findings.

```python
API_TOKEN = "sk-live-9f8e7d6c5b4a3210"

def get_user_orders(db, user_id, status):
    query = f"SELECT * FROM orders WHERE user_id = {user_id} AND status = '{status}'"
    rows = db.execute(query).fetchall()
    return rows[0:len(rows) - 1]
```

This is a review-only assignment: do not modify any files. In your report's
Summary, state your single most important finding first, then any secondary
findings. Keep it under 200 words.
"""

# Markers that indicate the security-engineer role was adopted. The
# instruction never uses these words, so their presence traces back to the
# injected ROLE block.
ROLE_MARKERS = (
    "security", "vulnerab", "injection", "hardcoded", "credential",
    "secret", "owasp", "cwe", "exploit", "sanitiz", "parameteriz",
)
# The planted security flaws — at least one must be named for full marks.
VULN_MARKERS = ("injection", "hardcoded", "credential", "token", "secret")


# The skill mandates this exact first line in every response — hardest
# possible evidence the ROLE block arrived and was honored.
ACTIVATION_LINE = "skill actief: security-engineer"


def evaluate_report(report_text: str) -> dict:
    # Wrapper-synthesized reports (bare lanes) embed the full structured
    # prompt under "## Instruction" — markers in there are NOT worker output.
    # Evaluate only what comes after the final CLOSING block when present.
    marker = "# CLOSING"
    idx = report_text.rfind(marker)
    worker_text = report_text[idx + len(marker):] if idx != -1 else report_text
    low = worker_text.lower()
    role_hits = sorted({m for m in ROLE_MARKERS if m in low})
    vuln_hits = sorted({m for m in VULN_MARKERS if m in low})
    return {
        "role_adopted": len(role_hits) >= 2,
        "activation_line": ACTIVATION_LINE in low,
        "vuln_found": len(vuln_hits) >= 1,
        "role_markers": role_hits,
        "vuln_markers": vuln_hits,
    }


def run_lane(lane: dict) -> dict:
    result = lane_dispatch(
        lane=lane,
        task_id="00_skill_smoke",
        replication=1,
        instruction=INSTRUCTION,
        dispatch_paths="",
        deadline_seconds=DEADLINE_SECONDS,
        skill_names=[SKILL],
    )
    verdict: dict = {
        "lane": lane["id"],
        "dispatch_id": result.dispatch_id,
        "success": result.success,
        "wallclock_s": round(result.wallclock_seconds, 1),
        "report": str(result.report_path) if result.report_path else None,
        "error": result.error,
        "stderr_tail": result.stderr[-500:] if result.stderr else "",
    }
    if result.report_path and result.report_path.exists():
        verdict.update(evaluate_report(result.report_path.read_text(encoding="utf-8")))
    else:
        verdict.update({"role_adopted": False, "activation_line": False,
                        "vuln_found": False, "role_markers": [], "vuln_markers": []})
    return verdict


def main() -> int:
    parser = argparse.ArgumentParser(description="Skill-injection E2E smoke")
    parser.add_argument("--lane", action="append", default=None,
                        help="subset of lane ids (default: all 6 smoke lanes)")
    parser.add_argument("--parallel", type=int, default=6)
    args = parser.parse_args()

    lane_ids = args.lane or SMOKE_LANES
    models_yaml = REPO_ROOT / "scripts" / "benchmark" / "models.yaml"
    lanes = load_lanes(models_yaml, lane_ids)

    verdicts: list[dict] = []
    with ThreadPoolExecutor(max_workers=args.parallel) as pool:
        futures = {pool.submit(run_lane, lane): lane["id"] for lane in lanes}
        for fut in as_completed(futures):
            v = fut.result()
            status = "PASS" if (v["success"] and v["role_adopted"] and v["vuln_found"]) else "FAIL"
            print(f"[{status}] {v['lane']}: wall={v['wallclock_s']}s "
                  f"role_adopted={v['role_adopted']} activation_line={v['activation_line']} "
                  f"vuln_found={v['vuln_found']} markers={v['role_markers']} error={v['error']}",
                  flush=True)
            verdicts.append(v)

    failed = [v for v in verdicts if not (v["success"] and v["role_adopted"] and v["vuln_found"])]
    print(f"\n{len(verdicts) - len(failed)}/{len(verdicts)} lanes PASS")
    for v in failed:
        print(f"  FAIL {v['lane']}: error={v['error']} report={v['report']}")
        if v["stderr_tail"]:
            print(f"    stderr: ...{v['stderr_tail']}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
