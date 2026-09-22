#!/usr/bin/env python3
"""Tests voor 03d_laya_mlx_classify.py.

Deelt de pure logica (sleutelvorm, criteria-bouw, kalibratie-ECE, truncatie-
signalering) van de model-afhankelijke paden. De pure tests laden geen model en
draaien op stdlib alleen. De smoke-test laadt het model wel, op een kleine
sub-set, en wordt overgeslagen als laya_mlx of het model niet beschikbaar is.

Run: .venv/bin/python test_laya_mlx_classify.py
"""
import json
import os
import sys
import tempfile

import importlib.util

HERE = os.path.dirname(os.path.abspath(__file__))

spec = importlib.util.spec_from_file_location(
    "laya_mlx_classify", os.path.join(HERE, "03d_laya_mlx_classify.py")
)
mod = importlib.util.module_from_spec(spec)
# Voorkom dat import laya_mlx mist: leg een stub-neer als het pakket er niet is,
# maar alleen voor de pure tests. De smoke-test controleert zelf op het echte
# pakket en slaat over als het er niet is.
_laya_importable = True
try:
    import laya_mlx  # noqa: F401
except ImportError:
    _laya_importable = False
    import types
    sys.modules.setdefault("laya_mlx", types.ModuleType("laya_mlx"))
spec.loader.exec_module(mod)

FAILS = []


def check(name, cond):
    if cond:
        print(f"  [ok] {name}")
    else:
        print(f"  [x]  {name}")
        FAILS.append(name)


def test_laya_keys_exact():
    """De uitvoersleutels zijn exact LAYA_KEYS, in die volgorde, appels op appels
    met 03b/03c. 04/05 lezen deze met .get()."""
    print("test_laya_keys_exact:")
    check("LAYA_KEYS heeft 9 sleutels", len(mod.LAYA_KEYS) == 9)
    expected = ("id", "laya_choice", "confidence", "probabilities",
                "latency_ms", "input_tokens", "label_lang", "instruction", "error")
    check("LAYA_KEYS volgorde matches 03b/03c", tuple(mod.LAYA_KEYS) == expected)


def test_build_criteria_nl_keeps_definitions():
    """Bij 'nl' krijgen de Nederlandse labels de definitie-tekst, zodat de
    volledige klassedefinities meegaan in de prompt (de variabele van ronde 4)."""
    print("test_build_criteria_nl_keeps_definitions:")
    c = mod.build_criteria("nl")
    check("nl labels zijn stijl/correctheid/beveiliging/beleid",
          set(c.keys()) == {"stijl", "correctheid", "beveiliging", "beleid"})
    check("stijl draagt de definitie-tekst (niet leeg)",
          isinstance(c["stijl"], str) and len(c["stijl"]) > 20)
    check("beleid draagt de definitie-tekst (niet leeg)",
          isinstance(c["beleid"], str) and len(c["beleid"]) > 20)


def test_build_criteria_en_translates_labels_keeps_definitions():
    """Bij 'en' schuiven de labels naar style/correctness/security/policy; de
    definitie-tekst blijft identiek."""
    print("test_build_criteria_en_translates_labels_keeps_definitions:")
    c_nl = mod.build_criteria("nl")
    c_en = mod.build_criteria("en")
    check("en labels zijn style/correctness/security/policy",
          set(c_en.keys()) == {"style", "correctness", "security", "policy"})
    check("definitie-tekst identiek tussen talen (stijl==style)",
          c_nl["stijl"] == c_en["style"])
    check("definitie-tekst identiek tussen talen (beleid==policy)",
          c_nl["beleid"] == c_en["policy"])


def test_calibration_ece_perfect():
    """Een perfect gekalibreerde set (confidence == accuracy per bin) geeft ECE 0.
    confidence 1.0 met allemaal juist -> bin 9, mean_conf=1.0, mean_acc=1.0, bijdrage 0."""
    print("test_calibration_ece_perfect:")
    preds = [
        {"id": "a", "confidence": 1.0, "laya_choice": "stijl", "error": None},
        {"id": "b", "confidence": 1.0, "laya_choice": "stijl", "error": None},
        {"id": "c", "confidence": 1.0, "laya_choice": "correctheid", "error": None},
        {"id": "d", "confidence": 1.0, "laya_choice": "correctheid", "error": None},
    ]
    labels = {"a": "stijl", "b": "stijl", "c": "correctheid", "d": "correctheid"}
    cal = mod.compute_calibration(preds, labels, n_bins=10)
    check("ECE == 0 voor perfect gekalibreerde set", cal["ece"] == 0.0)
    check("overall_accuracy == 1.0", cal["overall_accuracy"] == 1.0)
    check("n_labeled_predictions == 4", cal["n_labeled_predictions"] == 4)


def test_calibration_ece_worst_case():
    """Maximale miskalibratie: hoge zekerheid, lage juistheid -> hoge ECE.
    Dit is de ronde-3-falen-vorm (85% zekerheid, 4% juistheid) in het klein."""
    print("test_calibration_ece_worst_case:")
    preds = [
        {"id": str(i), "confidence": 0.95, "laya_choice": "stijl", "error": None}
        for i in range(10)
    ]
    # Alle labels onjuist (geen enkele stijl).
    labels = {str(i): "correctheid" for i in range(10)}
    cal = mod.compute_calibration(preds, labels, n_bins=10)
    check("ECE > 0.8 bij 95% zekerheid en 0% juistheid", cal["ece"] > 0.8)
    check("overall_accuracy == 0.0", cal["overall_accuracy"] == 0.0)


def test_calibration_ignores_errors_and_unlabeled():
    """Voorspellingen met een error of zonder label dragen niet bij aan de ECE."""
    print("test_calibration_ignores_errors_and_unlabeled:")
    preds = [
        {"id": "a", "confidence": 0.9, "laya_choice": "stijl", "error": None},
        {"id": "b", "confidence": 0.9, "laya_choice": "stijl", "error": "OTHER_ERROR"},
        {"id": "c", "confidence": 0.5, "laya_choice": "correctheid", "error": None},
        {"id": "d", "confidence": 0.5, "laya_choice": "beleid", "error": None},
    ]
    # d heeft geen label. b heeft een error. Overhouden: a en c = 2.
    labels = {"a": "stijl", "b": "stijl", "c": "correctheid"}
    cal = mod.compute_calibration(preds, labels, n_bins=10)
    check("n_labeled_predictions == 2 (error en ongelabeld weg)", cal["n_labeled_predictions"] == 2)


def test_calibration_no_labels_returns_zero_used():
    """Zonder labels zijn er 0 gebruikte voorspellingen en geen curve."""
    print("test_calibration_no_labels_returns_zero_used:")
    preds = [
        {"id": "a", "confidence": 0.9, "laya_choice": "stijl", "error": None},
    ]
    cal = mod.compute_calibration(preds, {}, n_bins=10)
    check("n_labeled_predictions == 0 zonder labels", cal["n_labeled_predictions"] == 0)
    check("overall_accuracy is None zonder labels", cal["overall_accuracy"] is None)
    check("ECE == 0.0 zonder labels (niets bijgedragen)", cal["ece"] == 0.0)


def test_calibration_bins_match_03c():
    """De binning is gelijk aan 03c: 10 gelijke bins over [0,1], bin-index
    min(int(conf*n_bins), n_bins-1) zodat conf=1.0 in de top-bin valt."""
    print("test_calibration_bins_match_03c:")
    preds = [
        {"id": "hi", "confidence": 1.0, "laya_choice": "stijl", "error": None},
        {"id": "lo", "confidence": 0.05, "laya_choice": "stijl", "error": None},
    ]
    labels = {"hi": "stijl", "lo": "stijl"}
    cal = mod.compute_calibration(preds, labels, n_bins=10)
    # conf=1.0 -> bin 9 (top); conf=0.05 -> bin 0.
    bins_with = {b["bin"] for b in cal["curve"] if b["count"] > 0}
    check("conf=1.0 valt in top-bin (9)", 9 in bins_with)
    check("conf=0.05 valt in bin 0", 0 in bins_with)
    check("n_bins == 10 (zelfde als 03c)", cal["n_bins"] == 10)


def test_load_ground_truth_labels_missing_returns_empty():
    """Ontbrekend labels-bestand geeft een lege dict, geen crash."""
    print("test_load_ground_truth_labels_missing_returns_empty:")
    d = mod.load_ground_truth_labels("/nonexistent/path/labels.jsonl")
    check("lege dict bij ontbrekend bestand", d == {})


def test_load_ground_truth_labels_parses():
    """Een labels-bestand met {id, true_label} per regel wordt gelezen."""
    print("test_load_ground_truth_labels_parses:")
    with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as fh:
        fh.write(json.dumps({"id": "a", "true_label": "stijl"}) + "\n")
        fh.write(json.dumps({"id": "b", "true_label": "beleid"}) + "\n")
        path = fh.name
    try:
        d = mod.load_ground_truth_labels(path)
        check("label a == stijl", d.get("a") == "stijl")
        check("label b == beleid", d.get("b") == "beleid")
    finally:
        os.unlink(path)


def test_smoke_model_predicts_on_subset():
    """Smoke-test: laadt het model en draai op 3 bevindingen. Vereist laya_mlx
    en het model; wordt overgeslagen als die er niet zijn (geen model-deling
    in de pure-testomgeving)."""
    print("test_smoke_model_predicts_on_subset:")
    if not _laya_importable:
        check("laya_mlx niet geinstalleerd; smoke-test overgeslagen", True)
        return
    data = os.path.join(HERE, "data", "findings.jsonl")
    if not os.path.exists(data):
        check("findings.jsonl niet gevonden; smoke-test overgeslagen", True)
        return
    rows = []
    with open(data) as fh:
        for i, line in enumerate(fh):
            if i >= 3:
                break
            rows.append(json.loads(line))
    import laya_mlx as laya
    agent = laya.load("aac6fef/laya-multilingual-mlx")
    questions = {"c": {"type": "choice",
                      "instructions": mod.CATEGORY_INSTRUCTION,
                      "criteria": mod.build_criteria("nl")}}
    got_keys = set()
    for r in rows:
        res = agent.predict(r["message"], questions)
        ans = res["answers"]["c"]
        # De response-vorm die 03d aanneemt: choice, confidence, probabilities.
        check(f"predict {r['id']} geeft een choice", ans.get("choice") is not None)
        check(f"predict {r['id']} geeft confidence", ans.get("confidence") is not None)
        check(f"predict {r['id']} geeft probabilities", ans.get("probabilities") is not None)
        check(f"predict {r['id']} geeft input_tokens in usage",
              res.get("usage", {}).get("input_tokens") is not None)
        # Bouw het pred-record na en vergelijk de sleutels.
        pred = {
            "id": r["id"],
            "laya_choice": ans.get("choice"),
            "confidence": ans.get("confidence"),
            "probabilities": ans.get("probabilities"),
            "latency_ms": 0.0,
            "input_tokens": res.get("usage", {}).get("input_tokens"),
            "label_lang": "nl",
            "instruction": mod.CATEGORY_INSTRUCTION,
            "error": None,
        }
        got_keys = set(pred.keys())
    check("pred-record sleutels == LAYA_KEYS", tuple(sorted(got_keys)) == tuple(sorted(mod.LAYA_KEYS)))


def main():
    test_laya_keys_exact()
    test_build_criteria_nl_keeps_definitions()
    test_build_criteria_en_translates_labels_keeps_definitions()
    test_calibration_ece_perfect()
    test_calibration_ece_worst_case()
    test_calibration_ignores_errors_and_unlabeled()
    test_calibration_no_labels_returns_zero_used()
    test_calibration_bins_match_03c()
    test_load_ground_truth_labels_missing_returns_empty()
    test_load_ground_truth_labels_parses()
    test_smoke_model_predicts_on_subset()
    print()
    if FAILS:
        print(f"FAILED: {len(FAILS)} checks -> {FAILS}")
        sys.exit(1)
    print("All checks passed.")


if __name__ == "__main__":
    main()
