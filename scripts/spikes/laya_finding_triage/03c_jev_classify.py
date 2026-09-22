#!/usr/bin/env python3
"""JEV arm for the finding-triage spike — appels met appels naast de Laya-arm.

Zelfde corpus, zelfde vijf klassen als `02_regex_baseline.py`, en exact dezelfde
uitvoersleutels als `03b_laya_classify_with_definitions.py` produceert, zodat
`04_compare_and_sample.py` en `05_ground_truth_check.py` de uitvoer ongewijzigd
kunnen lezen. De vorm is uit `03b` gelezen, niet gegokt (zie ``LAYA_KEYS``).

Waarom deze arm er is: Laya faalde niet op juistheid alleen maar op de combinatie
85% zekerheid bij 4% juistheid. JEV (TypeSafe systemone) claimt gekalibreerde
kansen, en heeft 32k context in plaats van 96 tokens. Dat laat twee dingen toe
die Laya niet kon:

  1. De volledige rubrieken in ``criteria`` (Laya moest ze weglaten om binnen 96
     tokens te blijven). Zowel de regex-intentie uit `02` als de INSTRUCTION
     uit `03b` zit erin, zodat beide armen dezelfde definitie van de klassen
     krijgen.
  2. Een betrouwbaarheidscurve + ECE, zodat JEV's kalibratie-claim toetsbaar is
     en niet alleen beweerd.

De API (gemeten 22-09 uit docs.typesafe.ai/api)::

    POST https://api.typesafe.ai/v1/systemone
    Authorization: Bearer <sleutel>
    Content-Type: application/json
    {"state": "<finding>", "model": "jev-latest",
     "questions": {"categorie": {"type": "choice", "instructions": "...",
                                 "criteria": {"stijl": "...", ...}},
                   "defect": {"type": "noul", "instructions": "...",
                              "criteria": {"ja": "...", "nee": "..."}}}}

De leverancier raadt aan alle vragen in één aanroep te zetten (fan-out); ze
draaien parallel en kosten nauwelijks extra tijd. Het tweede signaal
(echt defect of ruis) wordt apart geboekt in ``data/jev_defect_signals.jsonl``
zodat ``jev_predictions.jsonl`` schema-identiek aan Laya blijft.

De response-vorm (answers gedefinieerd per vraag-sleutel, ``usage`` met
``input_tokens``) is de aanname die deze script maakt op basis van de
vraagstructuur; de parser staat in één functie (``parse_response``) zodat een
afwijkende live-vorm een één-functie-wijziging is. De unit-test legt de vorm
vast.

Geen sleutel => duidelijke melding + exit non-zero. Geen fallback-provider, en
geen verzonnen voorspellingen om de pijplijn groen te krijgen.
"""
import json
import os
import sys
import time
import statistics
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data", "findings.jsonl")
OUT = os.path.join(HERE, "data", "jev_predictions.jsonl")
DEFECT_OUT = os.path.join(HERE, "data", "jev_defect_signals.jsonl")
CALIB_OUT = os.path.join(HERE, "data", "jev_calibration.json")
LABELS_PATH = os.path.join(HERE, "data", "ground_truth_labels.jsonl")

API_URL = "https://api.typesafe.ai/v1/systemone"
MODEL = "jev-latest"
REQUEST_TIMEOUT = 60

# Exact de sleutels die 03b op elke regel van laya_predictions_*.jsonl schrijft.
# Niet afwijken: 04/05 lezen deze met .get() en de vergelijking moet appels op
# appels zijn.
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

# De vijf klassen, identiek aan 02_regex_baseline.py (inclusief de expliciete
# restbak `overig`). De rubriekbeschrijvingen trekken de intentie uit de
# regex-patronen in 02 en de INSTRUCTION-tekst uit 03b, zodat JEV en Laya
# dezelfde definitie van de klassen krijgen. JEV's 32k context laat deze volledig
# staan; Laya moest ze om het 96-token budget weglaten.
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
    "overig": (
        "Anything that does not fit the four categories above. Explicit rest "
        "bin, NOT silently labelled as correctness."
    ),
}

# Tweede signaal in dezelfde aanroep (fan-out): echt defect of ruis. Types: noul
# is ja/nee. Apart geboekt in jev_defect_signals.jsonl.
DEFECT_INSTRUCTION = (
    "Is this an actual defect that warrants action, or noise (stylistic "
    "preference, false positive, or not actionable)?"
)
DEFECT_CRITERIA = {
    "ja": "A real defect that warrants action.",
    "nee": "Noise: stylistic preference, false positive, or not actionable.",
}

CLASSES = list(CRITERIA.keys())


def pct(vals, p):
    if not vals:
        return 0.0
    k = (len(vals) - 1) * p / 100
    f = int(k)
    c = min(f + 1, len(vals) - 1)
    if f == c:
        return vals[f]
    return vals[f] + (vals[c] - vals[f]) * (k - f)


def approx_tokens(text):
    """Zelfde proxy als 01_extract_findings.py: 1.3 tokens/woord."""
    return int(len(text.split()) * 1.3)


def build_request_payload(state):
    """Bouw de systemone-request. Fan-out: categorie (choice) + defect (noul)."""
    return {
        "state": state,
        "model": MODEL,
        "questions": {
            "categorie": {
                "type": "choice",
                "instructions": CATEGORY_INSTRUCTION,
                "criteria": CRITERIA,
            },
            "defect": {
                "type": "noul",
                "instructions": DEFECT_INSTRUCTION,
                "criteria": DEFECT_CRITERIA,
            },
        },
    }


def parse_response(body):
    """Vertaal een systemone-response naar een vast racket.

    Aanname (gebaseerd op de vraagstructuur): ``answers`` is een map van
    vraag-sleutel naar antwoord; een choice-antwoord draagt ``choice``,
    ``confidence`` en ``probabilities``; een noul-antwoord draagt ``choice``
    (ja/nee) en ``confidence``. ``usage.input_tokens`` is optioneel.

    Eén functie, zodat een afwijkende live-vorm hier gecorrigeerd wordt.
    """
    if not isinstance(body, dict):
        raise ValueError(f"response body is not an object: {type(body).__name__}")
    answers = body.get("answers", {})
    if not isinstance(answers, dict):
        raise ValueError(f"answers is not an object: {type(answers).__name__}")
    cat = answers.get("categorie", {}) or {}
    defect = answers.get("defect", {}) or {}
    usage = body.get("usage", {}) or {}
    return {
        "categorie": {
            "choice": cat.get("choice"),
            "confidence": cat.get("confidence"),
            "probabilities": cat.get("probabilities"),
        },
        "defect": {
            "choice": defect.get("choice"),
            "confidence": defect.get("confidence"),
            "probabilities": defect.get("probabilities"),
        },
        "input_tokens": usage.get("input_tokens"),
    }


def default_http_post(url, headers, payload, timeout):
    """Stdlib POST met retry op 429/5xx. Geeft de response-body als dict."""
    data = json.dumps(payload).encode("utf-8")
    last_err = None
    for attempt in range(4):
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8")
                return json.loads(raw)
        except urllib.error.HTTPError as e:
            last_err = e
            if e.code == 429 or 500 <= e.code < 600:
                wait = min(2 ** attempt, 8)
                time.sleep(wait)
                continue
            body = e.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"HTTP {e.code} from {url}: {body}") from e
        except (urllib.error.URLError, TimeoutError) as e:
            last_err = e
            wait = min(2 ** attempt, 8)
            time.sleep(wait)
            continue
    raise RuntimeError(f"request failed after retries: {last_err}")


def classify_finding(state, api_key, http_post=default_http_post):
    """Eén bevinding door JEV. Geeft (category_pred, defect_pred, latency_ms, input_tokens, error)."""
    payload = build_request_payload(state)
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    t0 = time.perf_counter()
    try:
        body = http_post(API_URL, headers, payload, REQUEST_TIMEOUT)
    except Exception as e:
        dt = (time.perf_counter() - t0) * 1000
        return None, None, round(dt, 3), None, f"HTTP_ERROR: {e}"
    dt = (time.perf_counter() - t0) * 1000
    try:
        parsed = parse_response(body)
    except Exception as e:
        return None, None, round(dt, 3), None, f"PARSE_ERROR: {e}"
    cat = parsed["categorie"]
    defect = parsed["defect"]
    return cat, defect, round(dt, 3), parsed["input_tokens"], None


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


def compute_calibration(predictions, labels_by_id, n_bins=10):
    """Betrouwbaarheidscurve + ECE over de gelabelde voorspellingen.

    Bin voorspellingen op confidence (0..1 in n_bins gelijke breedte). Per bin:
    aantal, gemiddelde zekerheid, werkelijke juistheid. ECE is het
    gewogen gemiddelde van |mean_conf - mean_acc| over de bins.

    JEV's claim is kalibratie; een gekalibreerd model heeft per bin
    mean_conf ~= mean_acc, dus een lage ECE. Laya faalde precies hier: hoge
    zekerheid bij lage juistheid.

    ``predictions``: lijst van dicts met ``id``, ``confidence`` (float of None),
    ``laya_choice``, ``error`` (None voor succes).
    ``labels_by_id``: {id: true_label}. Alleen voorspellingen mét label en mét
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
        "curve": curve,
    }


def main():
    api_key = os.environ.get("TYPESAFE_API_KEY")
    if not api_key:
        print("TYPESAFE_API_KEY is niet gezet.")
        print("Je kunt de JEV-arm niet live draaien zonder TypeSafe-sleutel.")
        print("Bouw de sleutel in je omgeving in en draai opnieuw.")
        print("Aanwezigheid tonen met de GEZET-vorm (key:+), nooit de vorm die de")
        print("waarde afdrukt (key:-).")
        return 2

    if not os.path.exists(DATA):
        print(f"findings.jsonl niet gevonden op {DATA}.")
        print("Draai eerst 01_extract_findings.py.")
        return 2

    rows = load_jsonl(DATA)
    if not rows:
        print(f"geen bevindingen in {DATA}.")
        return 2

    # Geschatte invoerkosten voor de run, afgedrukt vóór de aanroepen.
    sample_payload = build_request_payload(rows[0]["message"])
    overhead = approx_tokens(json.dumps(sample_payload, ensure_ascii=False))
    state_tokens = [approx_tokens(json.dumps(r["message"], ensure_ascii=False)) for r in rows]
    total_input = sum(t + overhead for t in state_tokens)
    print(f"findings: {len(rows)}")
    print(f"request overhead (rubrieken+vraag, ~tokens): {overhead}")
    print(f"state tokens: median={statistics.median(state_tokens):.0f} "
          f"p90={pct(sorted(state_tokens),90):.0f} max={max(state_tokens)}")
    print(f"geschatte invoer-tokens run: {total_input} "
          f"-> ${total_input / 1_000_000 * 0.042:.4f} bij $0,042/Mtok (uitvoer gratis)")

    preds = []
    defect_signals = []
    errors = 0
    for i, r in enumerate(rows):
        cat, defect, latency_ms, input_tokens, error = classify_finding(
            r["message"], api_key
        )
        choice = cat.get("choice") if cat else None
        if choice is not None and choice not in CLASSES:
            error = f"UNKNOWN_CHOICE: {choice!r}"
            choice = None
        pred = {
            "id": r["id"],
            "laya_choice": choice,
            "confidence": cat.get("confidence") if cat else None,
            "probabilities": cat.get("probabilities") if cat else None,
            "latency_ms": latency_ms,
            "input_tokens": input_tokens,
            "label_lang": "nl",
            "instruction": CATEGORY_INSTRUCTION,
            "error": error,
        }
        # Hard bewijs dat we precies de laya-sleutels schrijven, niet meer.
        assert tuple(pred.keys()) == LAYA_KEYS, (
            f"jev_prediction sleutels wijken af van Laya: {tuple(pred.keys())}"
        )
        preds.append(pred)
        defect_signals.append({
            "id": r["id"],
            "defect_choice": defect.get("choice") if defect else None,
            "defect_confidence": defect.get("confidence") if defect else None,
            "defect_probabilities": defect.get("probabilities") if defect else None,
            "latency_ms": latency_ms,
            "error": error,
        })
        if error:
            errors += 1
        if (i + 1) % 100 == 0:
            print(f"  {i+1}/{len(rows)} done (errors={errors})")

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w") as fh:
        for p in preds:
            fh.write(json.dumps(p, ensure_ascii=False) + "\n")
    with open(DEFECT_OUT, "w") as fh:
        for d in defect_signals:
            fh.write(json.dumps(d, ensure_ascii=False) + "\n")

    print(f"\n=== JEV results -> {OUT} ===")
    print(f"defect signals -> {DEFECT_OUT}")
    print(f"errors: {errors}/{len(rows)}")

    from collections import Counter
    ok = [p for p in preds if p["laya_choice"]]
    dist = Counter(p["laya_choice"] for p in ok)
    print("class distribution (successful predictions):")
    for k in CLASSES:
        c = dist.get(k, 0)
        denom = len(ok) if ok else 1
        print(f"  {k:14s}: {c:4d}  ({100*c/denom:5.1f}%)")

    # Kalibratie: alleen tegen ground-truth labels. Geen labels => geen curve,
    # en geen verzonnen labels.
    labels = load_ground_truth_labels()
    if labels:
        cal = compute_calibration(preds, labels)
        with open(CALIB_OUT, "w") as fh:
            json.dump(cal, fh, indent=2, ensure_ascii=False)
        print(f"\ncalibration -> {CALIB_OUT}")
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
        with open(CALIB_OUT, "w") as fh:
            json.dump({"status": "no_labels",
                       "n_predictions": len(preds),
                       "note": "Plaats ground_truth_labels.jsonl om ECE te rekenen."},
                      fh, indent=2, ensure_ascii=False)

    if errors:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
