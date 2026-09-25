#!/usr/bin/env python3
"""Ground-truth check: de 70-regel functie-drempel is stijl, per constructie.

Ronde 3 legt de falsificatie vast die de ronde beslist. Een bevinding die
letterlijk zegt dat een functie over de 70-regel-drempel gaat is stijl, geen
correctheid en geen beveiliging. Wie dat fout classificeert faalt op een geval
waarvan de waarheid vaststaat.

Selectie (letterlijk uit de dispatch):
  r'70[- ]line|70 executable|function size|oversized'

De dispatch noemt 110; de letterlijke regex matcht 124 (zie Analysis in het
rapport). Beide aantallen worden gerapporteerd, het oordeel staat op de groep
die de regex selecteert.

Voor drie methoden over die groep:
  1. nieuwe run ronde 3 (ane96 met definities in de instructie)
  2. bestaande ronde-2 ANE-EN-run (zonder definities)
  3. regex-baseline

Per methode:
  - aantal capaciteitsfouten
  - van de rest: per klasse hoeveel
  - percentage juist (style / stijl)
  - mediane zekerheid op die groep

Falsificatie, vooraf vastgelegd: het haalt het als de nieuwe run BOVEN 69%
uitkomt op de zekere gevallen. Daaronder of gelijk is falen.

Usage:
  05_ground_truth_check.py
    --new-run data/laya_predictions_ane96_en_defs.jsonl
    --ronde2-run data/laya_predictions_ane96_en.jsonl
    --regex data/regex_predictions.jsonl
    --findings data/findings.jsonl
  (defaults point at these files; override voor andere runs)

Prints een tabel en het falsificatie-oordeel. Schrijft ook
data/ground_truth_check.json voor het rapport.
"""
import argparse
import collections
import json
import os
import re
import statistics

HERE = os.path.dirname(os.path.abspath(__file__))

# De letterlijke selectie-regex uit de dispatch.
GT_RE = re.compile(r"70[- ]line|70 executable|function size|oversized", re.IGNORECASE)

# De subgroep die T0 mat op 76/110 = 69%: zonder de kale "function size"-term.
# Bevindingen die alleen "function size" noemen zonder 70 of oversized zijn niet
# per constructie stijl, dus die horen niet in de zekerheids-groep. Deze regex
# is de 110-groep. Beide worden gerapporteerd.
GT_RE_STRICT = re.compile(r"70[- ]line|70 executable|oversized", re.IGNORECASE)

LABELS_EN = ["style", "correctness", "security", "policy"]
LABELS_NL = ["stijl", "correctheid", "beveiliging", "beleid"]


def pct(vals, p):
    if not vals:
        return 0.0
    k = (len(vals) - 1) * p / 100
    f = int(k)
    c = min(f + 1, len(vals) - 1)
    if f == c:
        return vals[f]
    return vals[f] + (vals[c] - vals[f]) * (k - f)


def load_jsonl(path):
    out = []
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            out.append(json.loads(line))
    return out


def select_gt(rows, regex):
    return [r for r in rows if regex.search(r["message"])]


def laya_stats(gt, preds_by_id, correct_label):
    """Compute stats for a Laya run over the GT group.

    correct_label is the label that counts as 'juist' for this run's language:
    'style' for EN runs, 'stijl' for NL runs.
    """
    cap = 0
    rest = collections.Counter()
    confs = []
    missing = 0
    for r in gt:
        p = preds_by_id.get(r["id"])
        if p is None:
            missing += 1
            continue
        if p.get("error") == "CAPACITY_ERROR" or p.get("laya_choice") is None:
            cap += 1
            continue
        rest[p["laya_choice"]] += 1
        if p.get("confidence") is not None:
            confs.append(p["confidence"])
    total = len(gt)
    non_cap = total - cap
    correct = rest.get(correct_label, 0)
    pct_total = 100 * correct / total if total else 0.0
    pct_noncap = 100 * correct / non_cap if non_cap else 0.0
    median_conf = round(pct(sorted(confs), 50), 4) if confs else None
    return {
        "total": total,
        "capacity_errors": cap,
        "missing": missing,
        "non_cap": non_cap,
        "label_counts": dict(rest),
        "correct_label": correct_label,
        "correct": correct,
        "pct_correct_of_total": round(pct_total, 2),
        "pct_correct_of_non_cap": round(pct_noncap, 2),
        "median_confidence": median_conf,
    }


def regex_stats(gt, regex_by_id, correct_label="stijl"):
    rest = collections.Counter()
    missing = 0
    for r in gt:
        lab = regex_by_id.get(r["id"])
        if lab is None:
            missing += 1
            continue
        rest[lab] += 1
    total = len(gt)
    correct = rest.get(correct_label, 0)
    return {
        "total": total,
        "missing": missing,
        "label_counts": dict(rest),
        "correct_label": correct_label,
        "correct": correct,
        "pct_correct_of_total": round(100 * correct / total if total else 0.0, 2),
    }


def falsification_verdict(pct_correct, threshold=69.0):
    """Letterlijk uit de dispatch: boven 69% is slagen, daaronder of gelijk is falen."""
    if pct_correct > threshold:
        return "PASS"
    return "FAIL"


def print_table_row(name, stats, is_regex=False):
    print(f"\n--- {name} ---")
    print(f"  total in GT group: {stats['total']}")
    if is_regex:
        print(f"  missing predictions: {stats.get('missing', 0)}")
        print(f"  label counts: {stats['label_counts']}")
        print(f"  correct ({stats['correct_label']}): {stats['correct']}")
        print(f"  pct correct of total: {stats['pct_correct_of_total']}%")
        return
    print(f"  capacity errors: {stats['capacity_errors']}")
    if stats.get("missing"):
        print(f"  missing predictions: {stats['missing']}")
    print(f"  non-cap (classified ok): {stats['non_cap']}")
    print(f"  label counts: {stats['label_counts']}")
    print(f"  correct ({stats['correct_label']}): {stats['correct']}")
    print(f"  pct correct of total: {stats['pct_correct_of_total']}%")
    print(f"  pct correct of non-cap: {stats['pct_correct_of_non_cap']}%")
    print(f"  median confidence: {stats['median_confidence']}")


def main():
    ap = argparse.ArgumentParser(description="Ground-truth check ronde 3.")
    ap.add_argument("--findings", default=os.path.join(HERE, "data", "findings.jsonl"))
    ap.add_argument("--new-run", default=os.path.join(HERE, "data", "laya_predictions_ane96_en_defs.jsonl"))
    ap.add_argument("--ronde2-run", default=os.path.join(HERE, "data", "laya_predictions_ane96_en.jsonl"))
    ap.add_argument("--regex", default=os.path.join(HERE, "data", "regex_predictions.jsonl"))
    ap.add_argument("--threshold", type=float, default=69.0,
                    help="Falsificatie-drempel in procenten. Boven = slagen.")
    args = ap.parse_args()

    rows = load_jsonl(args.findings)

    # Twee groepen: de letterlijke dispatch-regex (124) en de strikte 110-groep.
    gt_literal = select_gt(rows, GT_RE)
    gt_strict = select_gt(rows, GT_RE_STRICT)

    print("=" * 70)
    print("Ground-truth check — ronde 3 (definities in de instructie)")
    print("=" * 70)
    print(f"findings: {args.findings}")
    print(f"new run:  {args.new_run}")
    print(f"ronde2:   {args.ronde2_run}")
    print(f"regex:    {args.regex}")
    print(f"falsification threshold: > {args.threshold}% = PASS")
    print()
    print(f"GT group (letterlijke dispatch-regex): {len(gt_literal)}")
    print(f"GT group (strijkte 110-groep, zonder kale 'function size'): {len(gt_strict)}")
    print("(T0 mat 76/110 = 69% op de strikte groep; bevestig of weerleg hieronder.)")

    # Laad voorspellingen.
    new_rows = load_jsonl(args.new_run) if os.path.exists(args.new_run) else []
    ronde2_rows = load_jsonl(args.ronde2_run) if os.path.exists(args.ronde2_run) else []
    regex_rows = load_jsonl(args.regex) if os.path.exists(args.regex) else []

    new_by_id = {p["id"]: p for p in new_rows}
    ronde2_by_id = {p["id"]: p for p in ronde2_rows}
    regex_by_id = {p["id"]: p["regex_label"] for p in regex_rows}

    # Bepaal label-taal van de nieuwe run (uit het eerste pred-veld).
    new_lang = "en"
    if new_rows:
        new_lang = new_rows[0].get("label_lang", "en")

    results = {}
    for group_name, gt, threshold in [
        ("literal (124)", gt_literal, args.threshold),
        ("strict-110", gt_strict, args.threshold),
    ]:
        print("\n" + "=" * 70)
        print(f"GT group: {group_name}  (n={len(gt)})")
        print("=" * 70)

        # Regex (taalonafhankelijk; regex_labels zijn NL: stijl).
        rgx = regex_stats(gt, regex_by_id, correct_label="stijl")
        print_table_row(f"regex baseline (stijl = juist)", rgx, is_regex=True)

        # Ronde-2 run (EN labels, dus style = juist).
        if ronde2_by_id:
            r2 = laya_stats(gt, ronde2_by_id, correct_label="style")
            print_table_row("ronde-2 ANE-EN (style = juist)", r2)
        else:
            r2 = None
            print("\n--- ronde-2 ANE-EN: bestand niet gevonden ---")

        # Nieuwe run.
        if new_by_id:
            new_correct = "style" if new_lang == "en" else "stijl"
            nw = laya_stats(gt, new_by_id, correct_label=new_correct)
            print_table_row(f"ronde-3 nieuw (ANE, {new_lang}, {new_correct} = juist)", nw)
            verdict = falsification_verdict(nw["pct_correct_of_total"], threshold)
            print()
            print(f"  FALSIFICATIE: {nw['pct_correct_of_total']}% {'>' if nw['pct_correct_of_total'] > threshold else '<='} {threshold}% => {verdict}")
            print(f"  (boven {threshold}% = slagen; daaronder of gelijk = falen)")
            nw["verdict"] = verdict
            nw["threshold"] = threshold
        else:
            nw = None
            print("\n--- ronde-3 nieuw: bestand niet gevonden ---")

        results[group_name] = {
            "n": len(gt),
            "regex": rgx,
            "ronde2": r2,
            "new_run": nw,
        }

    out_path = os.path.join(HERE, "data", "ground_truth_check.json")
    with open(out_path, "w") as fh:
        json.dump(results, fh, indent=2, ensure_ascii=False)
    print(f"\nGround-truth check geschreven naar {out_path}")


if __name__ == "__main__":
    main()
