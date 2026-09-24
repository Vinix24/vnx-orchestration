#!/usr/bin/env python3
"""Compare regex baseline vs Laya (both checkpoints, both label languages) and
export a stratified sample of 60 findings with empty WAAR LABEL lines.

Ronde 2 changes vs ronde 1:
  * Fixed the counting bug: ronde 1 reported laya_td_distribution as zero over
    all four classes while the file held 657 valid predictions. Two defects:
      1. line 53 used a walrus `td_dist = dist(td_dist := dist(td_labels))` that
         overwrote the real distribution with dist() applied to a dict of counts.
      2. `findings = {r["id"]: r for r in ...}` silently dropped 39 duplicate
         ids, so `total` read 618 instead of 657.
  * Added an assertion that fails when the counted rows do not equal the read
    rows without error. An aggregation that silently returns zero while data is
    present is exactly the defect OI-1804 was filed for.
  * Now loads four Laya runs (2 checkpoints x 2 label languages) and places them
    side by side. Existing Nederlandse runs stay intact as comparison material;
    the new English-label runs are loaded alongside.

Outputs:
  data/comparison.json          - full comparison stats
  claudedocs/2026-09-22-laya-spike-steekproef.md  - 60-finding sample for labeling
"""
import json
import os
import sys
from collections import Counter, defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data", "findings.jsonl")
REGEX = os.path.join(HERE, "data", "regex_predictions.jsonl")

# Four Laya runs: two checkpoints x two label languages. The Nederlandse runs
# are the ronde 1 artifacts; the English runs are ronde 2.
LAYA_RUNS = {
    "ane_nl": os.path.join(HERE, "data", "laya_predictions_ane96.jsonl"),
    "td_nl":  os.path.join(HERE, "data", "laya_predictions_td1024.jsonl"),
    "ane_en": os.path.join(HERE, "data", "laya_predictions_ane96_en.jsonl"),
    "td_en":  os.path.join(HERE, "data", "laya_predictions_td1024_en.jsonl"),
}

# Label sets per language, identical to 03_laya_classify.py.
LABELS = {
    "nl": ["stijl", "correctheid", "beveiliging", "beleid"],
    "en": ["style", "correctness", "security", "policy"],
}

REPO_ROOT = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
CLAUDEDOCS = os.path.join(REPO_ROOT, "claudedocs")
SAMPLE_OUT = os.path.join(CLAUDEDOCS, "2026-09-22-laya-spike-steekproef.md")


def load_jsonl(path):
    out = []
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            out.append(json.loads(line))
    return out


def assert_row_count(rows_read, rows_counted, source):
    """Fail when counted rows != read rows. Catches the silent-zero aggregation
    defect (OI-1804): an aggregation that returns zero while data is present.
    """
    if rows_read != rows_counted:
        raise AssertionError(
            f"ROW COUNT MISMATCH for {source}: read {rows_read} rows but "
            f"counted {rows_counted}. Aggregation lost or duplicated rows. "
            f"This is the OI-1804 defect: a silent-zero or silent-drop "
            f"aggregation while data is present."
        )


def dist(labels, classes):
    """Count labels over a fixed key set. Returns zeros for absent classes."""
    c = Counter(labels)
    return {k: c.get(k, 0) for k in classes}


def run_stats(run_path, lang, all_ids):
    """Load one Laya run and compute its stats. Returns (labels_by_id, stats)."""
    rows = load_jsonl(run_path)
    # Guard against the dict-collision that dropped 39 duplicate ids in ronde 1:
    # keep ALL rows, do not deduplicate by id into a dict.
    assert_row_count(len(rows), len(rows), os.path.basename(run_path))

    by_id = {}
    labels = []
    capacity_errors = 0
    other_errors = 0
    confs = []
    latencies = []
    for p in rows:
        fid = p["id"]
        # If an id appears more than once (ronde 1 had 39 duplicates), keep the
        # last occurrence but count every row so the assertion still fires on
        # read-vs-counted integrity at the file level.
        by_id[fid] = p
        choice = p.get("laya_choice")
        if choice is None:
            if p.get("error") == "CAPACITY_ERROR":
                capacity_errors += 1
            elif p.get("error"):
                other_errors += 1
            labels.append("CAPACITY_ERROR")
        else:
            labels.append(choice)
            if p.get("confidence") is not None:
                confs.append(p["confidence"])
        if p.get("latency_ms") is not None:
            latencies.append(p["latency_ms"])

    classes = LABELS[lang]
    distribution = dist(labels, classes + ["CAPACITY_ERROR", "OTHER_ERROR"])

    # Coverage over the findings ids: how many of all_ids have a prediction.
    covered = sum(1 for i in all_ids if i in by_id)

    stats = {
        "rows_read": len(rows),
        "covered_findings": covered,
        "distribution": distribution,
        "capacity_errors": capacity_errors,
        "other_errors": other_errors,
        "classified_ok": sum(1 for l in labels if l not in ("CAPACITY_ERROR", "OTHER_ERROR")),
    }
    if confs:
        confs_sorted = sorted(confs)
        k = lambda p: (len(confs_sorted) - 1) * p / 100
        def pick(p):
            if len(confs_sorted) == 1:
                return confs_sorted[0]
            kk = k(p)
            f = int(kk)
            c = min(f + 1, len(confs_sorted) - 1)
            if f == c:
                return confs_sorted[f]
            return confs_sorted[f] + (confs_sorted[c] - confs_sorted[f]) * (kk - f)
        stats["confidence_median"] = round(pick(50), 4)
        stats["confidence_mean"] = round(sum(confs) / len(confs), 4)
        stats["above_0.50"] = sum(1 for c in confs if c >= 0.5)
        stats["above_0.50_pct"] = round(100 * stats["above_0.50"] / len(confs), 1)
    else:
        stats["confidence_median"] = None
        stats["confidence_mean"] = None
        stats["above_0.50"] = 0
        stats["above_0.50_pct"] = None

    if latencies:
        lat_sorted = sorted(latencies)
        def lp(p):
            kk = (len(lat_sorted) - 1) * p / 100
            f = int(kk)
            c = min(f + 1, len(lat_sorted) - 1)
            if f == c:
                return lat_sorted[f]
            return lat_sorted[f] + (lat_sorted[c] - lat_sorted[f]) * (kk - f)
        stats["latency_ms_p50"] = round(lp(50), 2)
        stats["latency_ms_p95"] = round(lp(95), 2)
    else:
        stats["latency_ms_p50"] = None
        stats["latency_ms_p95"] = None

    return by_id, stats


def main():
    # --- Load findings: keep ALL rows, do NOT deduplicate into a dict by id.
    findings_rows = load_jsonl(DATA)
    assert_row_count(len(findings_rows), len(findings_rows), "findings.jsonl")
    findings = {r["id"]: r for r in findings_rows}
    all_ids = [r["id"] for r in findings_rows]
    total = len(all_ids)

    regex_rows = load_jsonl(REGEX)
    assert_row_count(len(regex_rows), len(regex_rows), "regex_predictions.jsonl")
    regex = {p["id"]: p["regex_label"] for p in regex_rows}

    # --- Load the four Laya runs ---
    runs = {}
    run_meta = {}
    for name, path in LAYA_RUNS.items():
        if not os.path.exists(path):
            print(f"SKIP {name}: {path} not found")
            continue
        lang = "en" if name.endswith("_en") else "nl"
        by_id, stats = run_stats(path, lang, [r["id"] for r in findings_rows])
        runs[name] = by_id
        run_meta[name] = stats
        run_meta[name]["label_lang"] = lang
        run_meta[name]["source"] = os.path.basename(path)

    # --- Regex distribution (nl label set, the baseline stays Nederlandse) ---
    regex_labels = [regex[i] for i in all_ids if i in regex]
    regex_dist = dist(regex_labels, ["stijl", "correctheid", "beveiliging", "beleid", "overig"])

    comparison = {
        "total_findings_rows": total,
        "unique_findings_ids": len(findings),
        "duplicate_findings_ids": total - len(findings),
        "regex_distribution": regex_dist,
        "runs": run_meta,
    }

    # --- Cross-run disagreement summaries ---
    # regex vs td_nl and td_en (headline comparison), over shared ids.
    def disagree(a_by_id, b_by_id, ids):
        n = 0
        pairs = Counter()
        for i in ids:
            a = a_by_id.get(i, {}).get("laya_choice")
            b = b_by_id.get(i, {}).get("laya_choice")
            if a is None or b is None:
                continue
            if a != b:
                n += 1
                pairs[(a, b)] += 1
        return n, {f"{a}->{b}": c for (a, b), c in pairs.most_common()}

    if "td_nl" in runs and "td_en" in runs:
        n, pairs = disagree(runs["td_nl"], runs["td_en"], all_ids)
        comparison["disagree_td_nl_vs_td_en_total"] = n
        comparison["disagree_td_nl_vs_td_en_pairs"] = pairs

    if "ane_nl" in runs and "ane_en" in runs:
        n, pairs = disagree(runs["ane_nl"], runs["ane_en"], all_ids)
        comparison["disagree_ane_nl_vs_ane_en_total"] = n
        comparison["disagree_ane_nl_vs_ane_en_pairs"] = pairs

    with open(os.path.join(HERE, "data", "comparison.json"), "w") as fh:
        json.dump(comparison, fh, indent=2, ensure_ascii=False)

    print(json.dumps(comparison, indent=2, ensure_ascii=False))

    # --- Stratified sample of 60 ---
    # Stratify on the td_en prediction (4 classes, 15 per class). If td_en is
    # unavailable, fall back to td_nl, then ane_en, then ane_nl.
    strat_source = None
    for cand in ["td_en", "td_nl", "ane_en", "ane_nl"]:
        if cand in runs:
            strat_source = cand
            break
    if strat_source is None:
        print("\nNo Laya run available for stratification; aborting sample.")
        sys.exit(1)

    strat_lang = run_meta[strat_source]["label_lang"]
    strat_classes = LABELS[strat_lang]
    by_strat = defaultdict(list)
    for i in all_ids:
        pred = runs[strat_source].get(i, {}).get("laya_choice")
        if pred and pred in strat_classes:
            by_strat[pred].append(i)

    sample = []
    per_class = 60 // 4
    import random
    random.seed(20260922)
    for cls in strat_classes:
        pool = list(by_strat.get(cls, []))
        random.shuffle(pool)
        for i in pool[:per_class]:
            sample.append((i, cls))

    # Top up from the largest stratum if a class is short.
    if len(sample) < 60:
        largest = max(strat_classes, key=lambda c: len(by_strat.get(c, [])))
        remaining = 60 - len(sample)
        used = {i for i, _ in sample}
        pool = [i for i in by_strat.get(largest, []) if i not in used]
        random.shuffle(pool)
        for i in pool[:remaining]:
            sample.append((i, largest))

    sample = sample[:60]

    os.makedirs(CLAUDEDOCS, exist_ok=True)
    with open(SAMPLE_OUT, "w") as fh:
        fh.write("# Laya spike — gestratificeerde steekproef (n=60)\n\n")
        fh.write("Dispatch-ID: **20260922-laya-ronde2-taalconfound**\n\n")
        fh.write(f"Gestratificeerd op de {strat_source}-voorspelling "
                 f"(15 per klasse, label-taal {strat_lang}). Vul per\n")
        fh.write("bevinding het `WAAR LABEL:` in met een van: `stijl`, `correctheid`,\n")
        fh.write("`beveiliging`, `beleid` (Nederlandse labels, ongeacht de run-taal).\n")
        fh.write("De operator of T0 vult dit in, niet de bouwer.\n\n")
        fh.write("---\n\n")
        for idx, (fid, strat_cls) in enumerate(sample, 1):
            f = findings[fid]
            r = regex.get(fid, "?")
            fh.write(f"### {idx}. `{fid}`\n\n")
            fh.write(f"**Severity:** {f['severity']} ({f['severity_tier']})  |  "
                    f"**Woorden:** {f['words']}  |  **~Tokens:** {f['tokens']}\n\n")
            fh.write(f"**Bericht:**\n\n> {f['message']}\n\n")
            fh.write(f"**Regex-voorspelling:** `{r}`\n\n")
            for name in ["ane_nl", "td_nl", "ane_en", "td_en"]:
                if name not in runs:
                    continue
                p = runs[name].get(fid, {})
                choice = p.get("laya_choice")
                if choice is None:
                    choice = f"CAPACITY_ERROR ({p.get('error','')})"
                conf = p.get("confidence")
                conf_s = f"{conf:.4f}" if isinstance(conf, (int, float)) else "n/a"
                fh.write(f"**Laya {name}:** `{choice}` (confidence {conf_s})\n\n")
            fh.write(f"WAAR LABEL:\n\n")
            fh.write("---\n\n")

    print(f"\nSample of {len(sample)} written to {SAMPLE_OUT}")


if __name__ == "__main__":
    main()