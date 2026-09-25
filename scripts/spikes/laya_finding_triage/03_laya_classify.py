#!/usr/bin/env python3
"""Classify all findings with a Laya Core ML checkpoint.

One `choice` question per finding, four classes. The label language is selectable
so the taalconfound from ronde 1 can be removed: ronde 1 gave Nederlandse labels
(`stijl`, `correctheid`, `beveiliging`, `beleid`) to both checkpoints, but
`laya-td` (convaiinnovations/laya-typed-decisions) is English-only. This script
lets you run both label sets.

Label sets (the four real classes, unchanged across languages):
  nl: stijl, correctheid, beveiliging, beleid
  en: style, correctness, security, policy

Instruction text stays English and identical for both languages, per the
ronde 2 dispatch. Only the criteria labels change.

  stijl / style       - function size, naming, comments, lint, unused import
  correctheid / correctness - logic, race, atomicity, wrong outcome
  beveiliging / security - injection, traversal, secrets, permissions, fail-open
  beleid / policy     - ADR violation, unlodged mutation, contract breach

For the ANE 96-token variant, findings whose question+options+finding exceed the
budget raise a capacity error; we record them as 'CAPACITY_ERROR' and skip.

Usage: 03_laya_classify.py <model_dir> <out_name> [nl|en]
  Default label language is nl (preserves the ronde 1 runs verbatim).
"""
import json
import os
import sys
import time
import statistics

import laya_coreml as laya

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data", "findings.jsonl")

INSTRUCTION = "Classify this code review finding into the single best category."

# Two label sets over the same four real classes. The label language is the only
# thing that changes; the instruction text stays English for both.
LABELS = {
    "nl": ["stijl", "correctheid", "beveiliging", "beleid"],
    "en": ["style", "correctness", "security", "policy"],
}


def pct(vals, p):
    if not vals:
        return 0.0
    k = (len(vals) - 1) * p / 100
    f = int(k)
    c = min(f + 1, len(vals) - 1)
    if f == c:
        return vals[f]
    return vals[f] + (vals[c] - vals[f]) * (k - f)


def main():
    if len(sys.argv) < 3 or len(sys.argv) > 4:
        print("usage: 03_laya_classify.py <model_dir> <out_name> [nl|en]")
        sys.exit(2)
    model_dir = sys.argv[1]
    out_name = sys.argv[2]
    lang = sys.argv[3] if len(sys.argv) == 4 else "nl"
    if lang not in LABELS:
        print(f"unknown label language {lang!r}; use one of: {', '.join(LABELS)}")
        sys.exit(2)
    classes = LABELS[lang]

    # Record the label language and set in the output so downstream scripts and
    # the report can distinguish runs unambiguously.
    out = os.path.join(HERE, "data", f"laya_predictions_{out_name}.jsonl")

    rows = []
    with open(DATA) as fh:
        for line in fh:
            rows.append(json.loads(line))

    questions = {
        "c": {
            "type": "choice",
            "instructions": INSTRUCTION,
            "criteria": classes,
        }
    }

    print(f"Loading model from {model_dir} ...")
    print(f"Label language: {lang}  classes: {classes}")
    print(f"Instruction: {INSTRUCTION!r}")
    t0 = time.perf_counter()
    agent = laya.load(model_dir)
    t1 = time.perf_counter()
    load_time = t1 - t0
    print(f"Load time: {load_time:.2f}s")

    # warmup call
    try:
        agent.predict(rows[0]["message"], questions)
    except Exception as e:
        print(f"warmup error (ok to ignore): {e}")

    preds = []
    latencies = []
    capacity_errors = 0
    other_errors = 0

    for i, r in enumerate(rows):
        state = r["message"]
        t2 = time.perf_counter()
        try:
            res = agent.predict(state, questions)
            t3 = time.perf_counter()
            dt = t3 - t2
            latencies.append(dt)
            ans = res["answers"]["c"]
            preds.append({
                "id": r["id"],
                "laya_choice": ans.get("choice"),
                "confidence": ans.get("confidence"),
                "probabilities": ans.get("probabilities"),
                "latency_ms": round(dt * 1000, 3),
                "input_tokens": res.get("usage", {}).get("input_tokens"),
                "label_lang": lang,
                "error": None,
            })
        except Exception as e:
            t3 = time.perf_counter()
            msg = str(e)
            kind = "CAPACITY_ERROR" if ("capacity" in msg.lower() or "token" in msg.lower() or "length" in msg.lower() or "too many" in msg.lower() or "budget" in msg.lower()) else "OTHER_ERROR"
            if kind == "CAPACITY_ERROR":
                capacity_errors += 1
            else:
                other_errors += 1
            preds.append({
                "id": r["id"],
                "laya_choice": None,
                "confidence": None,
                "probabilities": None,
                "latency_ms": round((t3 - t2) * 1000, 3),
                "input_tokens": None,
                "label_lang": lang,
                "error": kind,
            })

        if (i + 1) % 100 == 0:
            print(f"  {i+1}/{len(rows)} done (cap_err={capacity_errors}, other_err={other_errors})")

    with open(out, "w") as fh:
        for p in preds:
            fh.write(json.dumps(p, ensure_ascii=False) + "\n")

    print(f"\n=== {out_name} results -> {out} ===")
    print(f"label_lang: {lang}")
    print(f"classes: {classes}")
    print(f"instruction: {INSTRUCTION!r}")
    print(f"load_time_s: {load_time:.2f}")
    print(f"capacity_errors: {capacity_errors}/{len(rows)} ({100*capacity_errors/len(rows):.1f}%)")
    print(f"other_errors: {other_errors}/{len(rows)} ({100*other_errors/len(rows):.1f}%)")
    if latencies:
        lat_sorted = sorted(latencies)
        print(f"latency_ms P50: {pct(lat_sorted,50)*1000:.2f}  P95: {pct(lat_sorted,95)*1000:.2f}  mean: {statistics.mean(latencies)*1000:.2f}  max: {max(latencies)*1000:.2f}")

    from collections import Counter
    ok = [p for p in preds if p["laya_choice"]]
    dist = Counter(p["laya_choice"] for p in ok)
    print("class distribution (successful predictions):")
    for k in classes:
        c = dist.get(k, 0)
        denom = len(ok) if ok else 1
        print(f"  {k:14s}: {c:4d}  ({100*c/denom:5.1f}%)")

    # confidence stats over successful predictions
    confs = [p["confidence"] for p in ok if p["confidence"] is not None]
    if confs:
        confs_sorted = sorted(confs)
        print(f"highest-prob median: {pct(confs_sorted,50):.4f}  mean: {statistics.mean(confs):.4f}")
        above_05 = sum(1 for c in confs if c >= 0.5)
        print(f"highest-prob >= 0.50: {above_05}/{len(confs)} ({100*above_05/len(confs):.1f}%)")
    low_conf = sum(1 for p in ok if p["confidence"] is not None and p["confidence"] < 0.5)
    denom = len(ok) if ok else 1
    print(f"low-confidence (<0.5): {low_conf}/{len(ok)} ({100*low_conf/denom:.1f}%)")


if __name__ == "__main__":
    main()