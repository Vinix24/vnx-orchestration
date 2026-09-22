#!/usr/bin/env python3
"""Extract all blocking + advisory findings from review-gate results into JSONL.

Read-only over the gate-results store. Writes scripts/spikes/laya_finding_triage/data/findings.jsonl
and prints token-length statistics.

The review-gates store is the central per-project data directory
(``~/.vnx-data/<project_id>/state/review_gates/results``), resolved via the
``scripts/lib`` helpers (``resolve_project_id`` + ``resolve_central_data_dir``)
rather than a hardcoded path. That keeps the spike portable across machines
and projects: the project_id is read from the ``.vnx-project-id`` marker or
git remote, and ``VNX_PROJECT_ID`` can override it (issue #225).
"""
import glob
import json
import os
import statistics
import sys
from pathlib import Path

# Resolve scripts/lib via the project root so the helpers are importable
# regardless of the current working directory (issue #225).
_HERE = Path(__file__).resolve()
_LIB_DIR = _HERE.parents[2] / "lib"
if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))

from vnx_paths import resolve_central_data_dir, resolve_project_id  # noqa: E402

_PROJECT_ID = resolve_project_id()
_DATA_DIR = resolve_central_data_dir(_PROJECT_ID)
RESULTS_DIR = _DATA_DIR / "state" / "review_gates" / "results"
OUT = _HERE.parent / "data" / "findings.jsonl"
os.makedirs(OUT.parent, exist_ok=True)


def approx_tokens(text):
    """Rough token estimate: 0.75 words + 0.25 punctuation/chars heuristic.

    The dispatch cites ~39 tokens at 30 words median, i.e. ~1.3 tokens/word.
    We use the standard 1.3 tokens/word proxy and report it explicitly.
    """
    words = text.split()
    return int(len(words) * 1.3)


def main():
    files = sorted(glob.glob(os.path.join(RESULTS_DIR, "*.json")))
    rows = []
    for f in files:
        with open(f) as fh:
            d = json.load(fh)
        pr_id = d.get("pr_id") or os.path.basename(f).replace(".json", "")
        gate = d.get("gate", os.path.basename(f))
        for severity, key in (("blocking", "blocking_findings"), ("advisory", "advisory_findings")):
            findings = d.get(key, []) or []
            for i, item in enumerate(findings):
                if isinstance(item, dict):
                    msg = item.get("message", "")
                    sev = item.get("severity", severity)
                else:
                    msg = str(item)
                    sev = severity
                if not msg.strip():
                    continue
                rows.append({
                    "id": f"{pr_id}#{severity}#{i}",
                    "pr_id": pr_id,
                    "gate": gate,
                    "severity_tier": severity,
                    "severity": sev,
                    "message": msg,
                    "tokens": approx_tokens(msg),
                    "words": len(msg.split()),
                })

    with open(OUT, "w") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")

    print(f"Extracted {len(rows)} findings -> {OUT}")
    by_tier = {}
    for r in rows:
        by_tier.setdefault(r["severity_tier"], 0)
        by_tier[r["severity_tier"]] += 1
    print("by tier:", by_tier)

    tokens = sorted(r["tokens"] for r in rows)
    words = sorted(r["words"] for r in rows)

    def pct(vals, p):
        if not vals:
            return 0
        k = (len(vals) - 1) * p / 100
        f = int(k)
        c = min(f + 1, len(vals) - 1)
        if f == c:
            return vals[f]
        return vals[f] + (vals[c] - vals[f]) * (k - f)

    print(f"words:  median={statistics.median(words):.1f} p90={pct(words,90):.0f} p99={pct(words,99):.0f} min={words[0]} max={words[-1]}")
    print(f"tokens:  median={statistics.median(tokens):.1f} p90={pct(tokens,90):.0f} p99={pct(tokens,99):.0f} min={tokens[0]} max={tokens[-1]}")

    # fit analysis
    fit96 = sum(1 for t in tokens if t <= 70)   # ~26 tokens reserve for question+options
    fit1024 = sum(1 for t in tokens if t <= 1024)
    print(f"fit <=70 tokens (96 budget minus ~26 overhead): {fit96}/{len(rows)} ({100*fit96/len(rows):.1f}%)")
    print(f"fit <=1024 tokens: {fit1024}/{len(rows)} ({100*fit1024/len(rows):.1f}%)")


if __name__ == "__main__":
    sys.exit(main())