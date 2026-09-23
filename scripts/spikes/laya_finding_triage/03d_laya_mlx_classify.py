#!/usr/bin/env python3
"""Classify all findings with the native-MLX Laya checkpoint — ronde 4 (1024, definitions).

Ronde 4 heft precies de grens op die het negatieve resultaat van ronde 3 bevatte.
De ronde-3-arm draaide op de CoreML ANE-port met een vast 96-token budget, gecompileerd
voor de Neural Engine. Bij 96 tokens passen de bevinding en de klassedefinities niet
samen in de prompt, dus de definities moesten weg. Dat is de confound: de 96 is geen
eigenschap van Laya maar van die ene port. Het negatieve resultaat (4% juist bij 85%
zekerheid) meet dus deels de port en niet de aanpak.

Deze arm draait de native-MLX conversie `aac6fef/laya-multilingual-mlx` in plaats van
de CoreML-port. De RL-agent-config draagt ``max_len = 1024`` en ``head_max_len = 256``
(tegen 96 op de ANE-port). Dat geeft ruimte voor de volledige klassedefinities bij
elke bevinding. De dataset is 665 bevindingen, mediaan 39 tokens, p99 117, max 143;
met de definities erbij hoort alles ruim binnen 1024 te passen. Valt een aanroep buiten
1024 of buiten de 256 van de head, dan boek dat zichtbaar en val niet stil terug.

Exact dezelfde uitvoersleutels als ``03b_laya_classify_with_definitions.py`` (zie
``LAYA_KEYS``), gelezen uit de code en niet gegokt, zodat ``04_compare_and_sample.py``
en ``05_ground_truth_check.py`` ongewijzigd blijven werken.

Twee dingen die deze arm verder levert boven ronde 3:

  1. Tokenverbruik per aanroep. Het script boekt ``input_tokens`` per voorspelling en
     rapporteert hoeveel van de 1024 (en van de 256 head) er werkelijk gebruikt wordt,
     plus of een aanroep de grens raakte (truncated).
  2. Kalibratie, niet alleen juistheid. De vorige arm faalde op de combinatie 85%
     zekerheid bij 4% juistheid. Dit script levert een betrouwbaarheidscurve (bin op
     zekerheid, werkelijke juistheid per bin) plus een ECE, met dezelfde binning als
     ``03c_jev_classify.py`` op branch ``dispatch/20260922-jev-arm-finding-triage``,
     zodat de drie armen op dezelfde schaal liggen. De curve vereist ground-truth
     labels; zijn die afwezig, dan schrijven we een ``no_labels``-status en geen
     verzonnen labels (zie ``CALIB_OUT``).

Let op de API-vorm, getoetst tegen het pakket (``laya_mlx`` 0.2.0):

  - ``criteria`` voor een ``choice``-vraag accepteert zowel een lijst labels als een
    dict ``{label: beschrijving}``. De dict-vorm neemt de beschrijving als de
    optie-tekst mee in de prompt, wat precies is hoe de volledige definities meegaan.
    De dispatch stelt een lijst voor, maar een lijst labels draagt geen definities;
    de dict-vorm is dus de juiste lezing van "stuur de volledige klassedefinities
    mee". Getoetst: ``render_options`` in ``laya_mlx.common`` geeft
    ``"label: beschrijving"`` per optie bij dict-criteria, en alleen het label bij
    list-criteria.
  - ``confidence`` is de normalized Shannon-entropy ``1 - H(p) / log(k)`` (zie
    ``confidence_from_probs``), niet de maximale kans. Dat is een andere maat dan de
    argmax-kans, en dat onderscheid staat in het rapport.

Usage: 03d_laya_mlx_classify.py <model_id_or_dir> <out_name> [nl|en]
  Default label language is nl. ``model_id_or_dir`` is een HF-repo-id (dan wordt de
  cache gebruikt) of een lokaal pad.
"""
import json
import os
import sys
import time
import statistics

import laya_mlx as laya

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data", "findings.jsonl")
LABELS_PATH = os.path.join(HERE, "data", "ground_truth_labels.jsonl")
CALIB_OUT = os.path.join(HERE, "data", "laya_mlx_calibration.json")

# Exact de sleutels die 03b op elke regel van laya_predictions_*.jsonl schrijft.
# Niet afwijken: 04/05 lezen deze met .get() en de vergelijking moet appels op
# appels zijn. Gekopieerd uit 03c_jev_classify.py (LAYA_KEYS), dat ze uit 03b las.
LAYA_KEYS = (
    "id",
    "laya_choice",
    "confidence",
    "probabilities",
    "latency_ms",
    "input_tokens",
    "label_lang",
    "instruction",
    "error",
)

# De volledige klassedefinities. Dit is de variabele die ronde 3 moest weglaten om
# binnen het 96-token ANE-budget te passen, en die ronde 4 wel kan meenemen dankzij
# de 1024 van de native-MLX config. De definitietekst trekt de intentie uit de
# regex-patronen in 02_regex_baseline.py en de INSTRUCTION-tekst uit 03b, zodat
# alle armen dezelfde definitie van de klassen krijgen. Identiek aan de rubrieken
# in 03c_jev_classify.py, opdat de drie armen op dezelfde schaal liggen.
CATEGORY_INSTRUCTION = (
    "Classify this code review finding into the single best category. "
    "Choose the category whose rubric best matches what the finding is about."
)

CRITERIA = {
    "stijl": (
        "Style and maintainability: function size (70-line threshold), naming, "
        "comments and docstrings, lint (flake8/pylint/ruff), unused imports, "
        "dead code, magic numbers, trailing whitespace, type hints and return "
        "annotations."
    ),
    "correctheid": (
        "Correctness: logic errors, race conditions, atomicity, deadlocks, "
        "concurrency bugs, off-by-one errors, silent failures (swallowed or "
        "discarded exceptions), wrong outcome."
    ),
    "beveiliging": (
        "Security: injection, path traversal, yolo/hardcoded secrets, "
        "credentials, api keys, passwords, token leaks, fail-open behaviour, "
        "shell=True, eval(), os.system, unsafe deserialization, xss/csrf/sqli, "
        "privilege escalation."
    ),
    "beleid": (
        "Policy and governance: ADR or architecture-decision-record violations, "
        "contract breaches, unlodged mutations, governance, audit trail and "
        "provenance requirements."
    ),
}

# Twee labeltalen over dezelfde vier klassen, identiek aan 03/03b. De labeltaal is
# de enige die verandert; de definitie-tekst (hierboven) blijft voor beide talen
# dezelfde, want de definities zijn taalonafhankelijk Engels en de labels
# verschuiven met de gekozen taal. Bij "en" vervangen we de Nederlandse labels
# door de Engelse in de criteria-dict, zodat het model Engelse labels teruggeeft.
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


def build_criteria(lang):
    """Bouw de criteria-dict voor de gekozen labeltaal.

    De definitie-tekst blijft identiek; alleen de label-sleutels schuiven met de
    taal. Bij 'nl' staan de Nederlandse labels (stijl/correctheid/beveiliging/
    beleid); bij 'en' de Engelse (style/correctness/security/policy).
    """
    classes = LABELS[lang]
    source_keys = list(CRITERIA.keys())
    return {cls: CRITERIA[src] for cls, src in zip(classes, source_keys)}


def load_jsonl(path):
    rows = []
    if not os.path.exists(path):
        return rows
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def load_ground_truth_labels(path=LABELS_PATH):
    """Lees optionele ground-truth labels: {id: true_label}."""
    rows = load_jsonl(path)
    out = {}
    for r in rows:
        if "id" in r and "true_label" in r:
            out[r["id"]] = r["true_label"]
    return out


def write_jsonl_atomic(path, rows):
    """Schrijf JSONL atomisch: naar <path>.tmp in dezelfde map, dan os.replace.

    Een open(path, "w") kan bij onderbreking een afgekapt bestand achterlaten.
    os.replace is atomisch binnen hetzelfde bestandssysteem, en omdat de tmp in
    dezelfde map staat als het doel is dat hier gewaarborgd.
    """
    tmp = f"{path}.tmp"
    with open(tmp, "w") as fh:
        for p in rows:
            fh.write(json.dumps(p, ensure_ascii=False) + "\n")
    os.replace(tmp, path)


def write_json_atomic(path, obj):
    """Schrijf een JSON-object atomisch: naar <path>.tmp, dan os.replace.

    Zelfde reden als write_jsonl_atomic: geen afgekapt bestand bij onderbreking.
    """
    tmp = f"{path}.tmp"
    with open(tmp, "w") as fh:
        json.dump(obj, fh, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


def compute_calibration(predictions, labels_by_id, n_bins=10):
    """Betrouwbaarheidscurve + ECE over de gelabelde voorspellingen.

    Zelfde binning als 03c_jev_classify.py: bin voorspellingen op confidence
    (0..1 in n_bins gelijke breedte). Per bin: aantal, gemiddelde zekerheid,
    werkelijke juistheid. ECE is het gewogen gemiddelde van
    |mean_conf - mean_acc| over de bins.

    Laya faalde in ronde 3 op precies dit punt: 85% zekerheid bij 4% juistheid. Een
    gekalibreerd model heeft per bin mean_conf ~= mean_acc, dus een lage ECE.

    ``confidence`` hier is de Laya-entropy-maat (1 - H/log(k)), niet de
    argmax-kans. Dat is de zekerheid die het model zelf publiceert, dus de
    kalibratie moet daartegen worden gemeten.

    ``predictions``: lijst van dicts met ``id``, ``confidence`` (float of None),
    ``laya_choice``, ``error`` (None voor succes).
    ``labels_by_id``: {id: true_label}. Alleen voorspellingen met label en met
    confidence dragen bij.
    """
    bins = [{"lo": i / n_bins, "hi": (i + 1) / n_bins,
             "count": 0, "conf_sum": 0.0, "acc_sum": 0} for i in range(n_bins)]
    used = 0
    for p in predictions:
        if p.get("error"):
            continue
        if p.get("laya_choice") is None:
            continue
        conf = p.get("confidence")
        if conf is None:
            continue
        true = labels_by_id.get(p["id"])
        if true is None:
            continue
        correct = 1 if p["laya_choice"] == true else 0
        idx = min(int(conf * n_bins), n_bins - 1)
        b = bins[idx]
        b["count"] += 1
        b["conf_sum"] += conf
        b["acc_sum"] += correct
        used += 1

    ece = 0.0
    curve = []
    for i, b in enumerate(bins):
        if b["count"] == 0:
            curve.append({"bin": i, "range": [b["lo"], b["hi"]], "count": 0,
                          "mean_confidence": None, "mean_accuracy": None})
            continue
        mean_conf = b["conf_sum"] / b["count"]
        mean_acc = b["acc_sum"] / b["count"]
        curve.append({"bin": i, "range": [b["lo"], b["hi"]], "count": b["count"],
                      "mean_confidence": round(mean_conf, 4),
                      "mean_accuracy": round(mean_acc, 4)})
        if used > 0:
            ece += (b["count"] / used) * abs(mean_conf - mean_acc)

    overall = None
    if used > 0:
        total_correct = sum(b["acc_sum"] for b in bins)
        overall = round(total_correct / used, 4)
    return {
        "n_labeled_predictions": used,
        "n_bins": n_bins,
        "overall_accuracy": overall,
        "ece": round(ece, 4),
        "confidence_measure": "normalized_shannon_entropy_1_minus_H_over_log_k",
        "curve": curve,
    }


def main():
    if len(sys.argv) < 3 or len(sys.argv) > 4:
        print("usage: 03d_laya_mlx_classify.py <model_id_or_dir> <out_name> [nl|en]")
        sys.exit(2)
    model_id = sys.argv[1]
    out_name = sys.argv[2]
    lang = sys.argv[3] if len(sys.argv) == 4 else "nl"
    if lang not in LABELS:
        print(f"unknown label language {lang!r}; use one of: {', '.join(LABELS)}")
        sys.exit(2)
    classes = LABELS[lang]
    criteria = build_criteria(lang)

    if not os.path.exists(DATA):
        print(f"findings.jsonl niet gevonden op {DATA}.")
        print("Draai eerst 01_extract_findings.py.")
        return 2

    rows = load_jsonl(DATA)
    if not rows:
        print(f"geen bevindingen in {DATA}.")
        return 2

    # Uitvoerpad: de dispatch noemt expliciet data/laya_mlx_predictions.jsonl als
    # de primaire uitvoer. De NL-run (default) schrijft exact dat bestand; de
    # EN-run schrijft een eigen naam zodat de twee talen elkaar niet overschrijven
    # (dat deed de oude {out_name}-vorm wel: beide runs op dezelfde out_name
    # overschreven de NL-uitvoer met de EN-uitvoer). out_name blijft beschikbaar
    # om de run in de rapportage te identificeren.
    if lang == "en":
        out = os.path.join(HERE, "data", "laya_mlx_predictions_en.jsonl")
    else:
        out = os.path.join(HERE, "data", "laya_mlx_predictions.jsonl")

    questions = {
        "c": {
            "type": "choice",
            "instructions": CATEGORY_INSTRUCTION,
            "criteria": criteria,
        }
    }

    print(f"Loading model from {model_id} ...")
    print(f"Label language: {lang}  classes: {classes}")
    print(f"Instruction: {CATEGORY_INSTRUCTION!r}")
    t0 = time.perf_counter()
    agent = laya.load(model_id)
    t1 = time.perf_counter()
    load_time = t1 - t0
    max_len = agent.cfg.get("max_len", 1024)
    head_max_len = agent.cfg.get("head_max_len", 256)
    print(f"Load time: {load_time:.2f}s")
    print(f"cfg max_len={max_len} head_max_len={head_max_len}")
    print(f"temperature={agent.temperature}")

    # warmup call (zelfde vorm als 03b)
    try:
        agent.predict(rows[0]["message"], questions)
    except Exception as e:
        print(f"warmup error (ok to ignore): {e}")

    preds = []
    latencies = []
    input_tokens = []
    truncated_at_max = 0
    head_capacity_errors = 0
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
            tok = res.get("usage", {}).get("input_tokens")
            if tok is not None:
                input_tokens.append(tok)
                # Truncatie-signaal: effectief tokens op de max_len-grens betekent
                # dat de state is afgeknipt. laya_mlx trunkeert stil naar max_len;
                # we vlaggen dit zichtbaar in plaats van het te negeren.
                if tok >= max_len:
                    truncated_at_max += 1
            preds.append({
                "id": r["id"],
                "laya_choice": ans.get("choice"),
                "confidence": ans.get("confidence"),
                "probabilities": ans.get("probabilities"),
                "latency_ms": round(dt * 1000, 3),
                "input_tokens": tok,
                "label_lang": lang,
                "instruction": CATEGORY_INSTRUCTION,
                "error": None,
            })
        except Exception as e:
            t3 = time.perf_counter()
            msg = str(e)
            # laya_mlx raised alleen als het head-budget de opties niet past
            # (ValueError "too many options for the token budget"). Met 4 opties
            # en head_max_len=256 hoort dat niet te gebeuren, maar we boek het
            # apart van overige fouten voor het rapport.
            kind = ("HEAD_CAPACITY_ERROR"
                    if "too many options" in msg.lower() or "token budget" in msg.lower()
                    else "CAPACITY_ERROR" if any(s in msg.lower() for s in
                       ("capacity", "token", "length", "too many", "budget"))
                    else "OTHER_ERROR")
            if kind == "HEAD_CAPACITY_ERROR":
                head_capacity_errors += 1
            elif kind == "CAPACITY_ERROR":
                head_capacity_errors += 1
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
                "instruction": CATEGORY_INSTRUCTION,
                "error": kind,
            })

        if (i + 1) % 100 == 0:
            print(f"  {i+1}/{len(rows)} done (trunc={truncated_at_max}, "
                  f"head_cap={head_capacity_errors}, other_err={other_errors})")

    os.makedirs(os.path.dirname(out), exist_ok=True)
    write_jsonl_atomic(out, preds)

    print(f"\n=== {out_name} results -> {out} ===")
    print(f"label_lang: {lang}")
    print(f"classes: {classes}")
    print(f"instruction: {CATEGORY_INSTRUCTION!r}")
    print(f"criteria: dict-form with full definitions (label: rubric)")
    print(f"load_time_s: {load_time:.2f}")
    print(f"max_len: {max_len}  head_max_len: {head_max_len}")
    print(f"truncated_at_max_len: {truncated_at_max}/{len(rows)} "
          f"({100*truncated_at_max/len(rows):.1f}%)")
    print(f"head_capacity_errors: {head_capacity_errors}/{len(rows)} "
          f"({100*head_capacity_errors/len(rows):.1f}%)")
    print(f"other_errors: {other_errors}/{len(rows)} "
          f"({100*other_errors/len(rows):.1f}%)")

    if input_tokens:
        tok_sorted = sorted(input_tokens)
        print(f"input_tokens: min={tok_sorted[0]} median={pct(tok_sorted,50):.0f} "
              f"p90={pct(tok_sorted,90):.0f} p99={pct(tok_sorted,99):.0f} "
              f"max={tok_sorted[-1]}  (of {max_len})")
        at_head = sum(1 for t in input_tokens if t >= head_max_len)
        print(f"input_tokens >= head_max_len ({head_max_len}): {at_head}/{len(input_tokens)} "
              f"({100*at_head/len(input_tokens):.1f}%)")
    if latencies:
        lat_sorted = sorted(latencies)
        print(f"latency_ms P50: {pct(lat_sorted,50)*1000:.2f}  "
              f"P95: {pct(lat_sorted,95)*1000:.2f}  "
              f"mean: {statistics.mean(latencies)*1000:.2f}  "
              f"max: {max(latencies)*1000:.2f}")

    from collections import Counter
    ok = [p for p in preds if p["laya_choice"]]
    dist = Counter(p["laya_choice"] for p in ok)
    print("class distribution (successful predictions):")
    for k in classes:
        c = dist.get(k, 0)
        denom = len(ok) if ok else 1
        print(f"  {k:14s}: {c:4d}  ({100*c/denom:5.1f}%)")

    confs = [p["confidence"] for p in ok if p["confidence"] is not None]
    if confs:
        confs_sorted = sorted(confs)
        print(f"confidence (entropy) median: {pct(confs_sorted,50):.4f}  "
              f"mean: {statistics.mean(confs):.4f}")
        above_05 = sum(1 for c in confs if c >= 0.5)
        print(f"confidence >= 0.50: {above_05}/{len(confs)} "
              f"({100*above_05/len(confs):.1f}%)")
    low_conf = sum(1 for p in ok if p["confidence"] is not None and p["confidence"] < 0.5)
    denom = len(ok) if ok else 1
    print(f"low-confidence (<0.5): {low_conf}/{len(ok)} ({100*low_conf/denom:.1f}%)")

    # Kalibratie: alleen tegen ground-truth labels. Geen labels => geen curve,
    # en geen verzonnen labels. Zelfde pad als 03c_jev_classify.py.
    labels = load_ground_truth_labels()
    if labels:
        cal = compute_calibration(preds, labels)
        write_json_atomic(CALIB_OUT, cal)
        print(f"\ncalibration -> {CALIB_OUT}")
        print(f"  confidence measure: {cal['confidence_measure']}")
        print(f"  labeled predictions: {cal['n_labeled_predictions']}")
        print(f"  overall accuracy: {cal['overall_accuracy']}")
        print(f"  ECE: {cal['ece']}")
        print("  reliability curve:")
        for b in cal["curve"]:
            if b["count"] > 0:
                print(f"    bin {b['bin']} [{b['range'][0]:.1f}-{b['range'][1]:.1f}]: "
                      f"n={b['count']} conf={b['mean_confidence']} acc={b['mean_accuracy']}")
    else:
        print(f"\nkalibratie overgeslagen: geen ground_truth_labels.jsonl gevonden.")
        print(f"  Plaats {LABELS_PATH} met {{id, true_label}} per regel om de")
        print(f"  betrouwbaarheidscurve + ECE te laten draaien. Geen labels, geen curve.")
        write_json_atomic(CALIB_OUT, {"status": "no_labels",
                       "n_predictions": len(preds),
                       "confidence_measure": "normalized_shannon_entropy_1_minus_H_over_log_k",
                       "note": "Plaats ground_truth_labels.jsonl om ECE te rekenen."})

    if other_errors:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
