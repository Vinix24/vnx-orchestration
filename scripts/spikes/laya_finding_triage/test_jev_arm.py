#!/usr/bin/env python3
"""Unit-test voor de JEV-arm (03c_jev_classify.py) — LIVE OpenRouter-vorm.

De HTTP-laag wordt vervangen door een vastgelegd antwoord in de ECHTE vorm die
de live aanroep teruggeeft (gemeten 22-09). De test toont:

  1. parse_response vertaalt een vastgelegd antwoord correct, incl. noul-vorm
     (alleen ``noul`` float, geen choice/confidence) en usage.cost.
  2. classify_finding geeft exact de Laya-sleutels (LAYA_KEYS) terug, met de
     juiste choice/confidence, en boekt het defect-signaal + cost apart.
  3. build_request_payload vraagt fan-out (categorie + defect) met volledige
     criteria, tegen het OpenRouter-model, met true/false voor noul.
  4. compute_calibration rekent een ECE uit die klopt op een known dataset.
  5. 04_compare_and_sample.run_stats leest jev_predictions.jsonl zonder fout en
     rapporteert de juiste distributie (appels met appels).
  6. 05_ground_truth_check.laya_stats leest dezelfde uitvoer.
  7. Zonder OPENROUTER_API_KEY stopt main() netjes met exit 2 en schrijft geen
     voorspellingen naar data/ (droge run).
  8. Een HTTP-fout wordt als error geboekt, niet als verzonnen voorspelling.
  9. De API-URL en het model zijn de gemeten route, niet de #1887-aanname.
 10. Retry op 429/5xx: default_http_post herstelt na een tijdelijke fout.

Draai: python3 test_jev_arm.py  (stdlib only, geen pytest vereist).
"""
import importlib.util
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


jev = load_module("jev_classify", os.path.join(HERE, "03c_jev_classify.py"))
compare = load_module("compare_sample", os.path.join(HERE, "04_compare_and_sample.py"))
ground = load_module("ground_truth_check", os.path.join(HERE, "05_ground_truth_check.py"))

FAILS = []


def check(name, cond):
    if cond:
        print(f"  [ok] {name}")
    else:
        print(f"  [x]  {name}")
        FAILS.append(name)


def expect_raise(fn, *args, **kwargs):
    try:
        fn(*args, **kwargs)
        return False
    except Exception:
        return True


# Vastgelegd antwoord in de ECHTE vorm die de live aanroep teruggeeft (gemeten
# 22-09 op een echte bevinding). Een choice-antwoord met
# choice/confidence/probabilities, een noul-antwoord met ALLEEN ``noul`` als
# float (geen choice, geen confidence), plus usage met input_tokens/output_tokens
# en cost. Top-level model/id/provider.
RECORDED_BODY = {
    "model": "typesafe/jev-1.13-20260917",
    "answers": {
        "categorie": {
            "type": "choice",
            "choice": "beveiliging",
            "probabilities": {
                "stijl": 0.02, "correctheid": 0.05,
                "beveiliging": 0.82, "beleid": 0.03, "overig": 0.08,
            },
            "confidence": 0.82,
        },
        "defect": {
            "type": "noul",
            "noul": 0.91,
        },
    },
    "usage": {"input_tokens": 410, "output_tokens": 63, "cost": 1.4826e-05},
    "id": "gen-dec-test",
    "provider": "TypeSafe",
}


def fake_http_post_ok(url, headers, payload, timeout):
    """Geeft het vastgelegde antwoord, en legt dat de payload fan-out heeft."""
    # Bewijs dat de payload beide vragen meestuurt (fan-out).
    assert "categorie" in payload["questions"], "payload mist categorie-vraag"
    assert "defect" in payload["questions"], "payload mist defect-vraag"
    assert payload["questions"]["categorie"]["type"] == "choice"
    assert payload["questions"]["defect"]["type"] == "noul"
    return RECORDED_BODY


def fake_http_post_fail(url, headers, payload, timeout):
    raise RuntimeError("simulated 503")


def test_parse_response_happy():
    print("test_parse_response_happy:")
    parsed = jev.parse_response(RECORDED_BODY)
    check("categorie.choice == beveiliging", parsed["categorie"]["choice"] == "beveiliging")
    check("categorie.confidence == 0.82", parsed["categorie"]["confidence"] == 0.82)
    check("categorie.probabilities is dict", isinstance(parsed["categorie"]["probabilities"], dict))
    # noul draagt alleen de float, geen choice/confidence (gemeten).
    check("defect.noul == 0.91", parsed["defect"]["noul"] == 0.91)
    check("defect heeft GEEN choice-sleutel", "choice" not in parsed["defect"])
    check("defect heeft GEEN confidence-sleutel", "confidence" not in parsed["defect"])
    check("input_tokens == 410", parsed["input_tokens"] == 410)
    check("output_tokens == 63", parsed["output_tokens"] == 63)
    check("cost == 1.4826e-05", parsed["cost"] == 1.4826e-05)


def test_parse_response_bad_shapes():
    print("test_parse_response_bad_shapes:")
    check("raises op non-dict body", expect_raise(jev.parse_response, "not a dict"))
    check("raises op non-dict answers", expect_raise(jev.parse_response, {"answers": []}))
    # Lege answers mag niet crashen, geeft None-waarden.
    parsed = jev.parse_response({"answers": {}})
    check("lege answers -> choice None", parsed["categorie"]["choice"] is None)
    check("lege answers -> defect.noul None", parsed["defect"]["noul"] is None)
    check("lege answers -> input_tokens None", parsed["input_tokens"] is None)
    check("lege answers -> cost None", parsed["cost"] is None)


def test_build_request_payload_fanout():
    print("test_build_request_payload_fanout:")
    payload = jev.build_request_payload("een bevinding")
    check("state staat erin", payload["state"] == "een bevinding")
    check("model == gemeten typesafe/jev-1.13-20260917",
          payload["model"] == "typesafe/jev-1.13-20260917")
    q = payload["questions"]
    check("twee vragen (fan-out)", set(q.keys()) == {"categorie", "defect"})
    # Eis 1: criteria volledig gevuld, alle vijf klassen, geen nullen.
    crit = q["categorie"]["criteria"]
    check("vijf klassen in criteria", set(crit.keys()) == set(jev.CLASSES))
    for k, v in crit.items():
        check(f"criteria[{k}] is non-empty str", isinstance(v, str) and len(v) > 10)
    # Eis 2: defect is noul met true/false (gemeten, niet ja/nee uit #1887).
    dcrit = q["defect"]["criteria"]
    check("defect criteria true/false", set(dcrit.keys()) == {"true", "false"})
    check("categorie type == choice", q["categorie"]["type"] == "choice")
    check("defect type == noul", q["defect"]["type"] == "noul")


def test_api_url_and_model_are_measured():
    """De route en het model zijn de gemeten waarden, niet de #1887-aanname."""
    print("test_api_url_and_model_are_measured:")
    check("API_URL == openrouter decisions endpoint",
          jev.API_URL == "https://openrouter.ai/api/alpha/decisions")
    check("API_URL != #1887 api.typesafe.ai",
          "api.typesafe.ai" not in jev.API_URL)
    check("MODEL == typesafe/jev-1.13-20260917",
          jev.MODEL == "typesafe/jev-1.13-20260917")
    check("MODEL != #1887 jev-latest", jev.MODEL != "jev-latest")


def test_classify_finding_keys_match_laya():
    print("test_classify_finding_keys_match_laya:")
    cat, defect, latency_ms, input_tokens, cost, error = jev.classify_finding(
        "hardcoded api key in source", "fake-key", http_post=fake_http_post_ok
    )
    check("no error", error is None)
    check("choice == beveiliging", cat["choice"] == "beveiliging")
    check("confidence == 0.82", cat["confidence"] == 0.82)
    check("input_tokens == 410", input_tokens == 410)
    check("cost == 1.4826e-05", cost == 1.4826e-05)
    check("latency_ms is numeric", isinstance(latency_ms, (int, float)))
    check("defect.noul == 0.91", defect["noul"] == 0.91)


def test_prediction_record_exact_laya_keys():
    print("test_prediction_record_exact_laya_keys:")
    cat, defect, latency_ms, input_tokens, cost, error = jev.classify_finding(
        "x", "fake-key", http_post=fake_http_post_ok
    )
    choice = cat.get("choice") if cat else None
    pred = {
        "id": "pr1#blocking#0",
        "laya_choice": choice,
        "confidence": cat.get("confidence") if cat else None,
        "probabilities": cat.get("probabilities") if cat else None,
        "latency_ms": latency_ms,
        "input_tokens": input_tokens,
        "label_lang": "nl",
        "instruction": jev.CATEGORY_INSTRUCTION,
        "error": error,
    }
    check("keys == LAYA_KEYS exact", tuple(pred.keys()) == jev.LAYA_KEYS)
    # 04/05 lezen deze velden via .get(); bewijs dat ze er allemaal staan.
    for k in ("id", "laya_choice", "confidence", "error", "label_lang", "latency_ms"):
        check(f"veld {k} aanwezig", k in pred)


def test_classify_finding_http_error_booked_not_faked():
    print("test_classify_finding_http_error_booked_not_faked:")
    cat, defect, latency_ms, input_tokens, cost, error = jev.classify_finding(
        "x", "fake-key", http_post=fake_http_post_fail
    )
    check("cat is None op HTTP-fout", cat is None)
    check("defect is None op HTTP-fout", defect is None)
    check("error start met HTTP_ERROR", error and error.startswith("HTTP_ERROR"))
    check("input_tokens None op fout", input_tokens is None)
    check("cost None op fout", cost is None)


def test_unknown_choice_flagged():
    print("test_unknown_choice_flagged:")
    body = {
        "answers": {
            "categorie": {"choice": "rareklasse", "confidence": 0.7, "probabilities": {}},
            "defect": {"noul": 0.5},
        },
        "usage": {"input_tokens": 10, "output_tokens": 5, "cost": 1e-06},
    }
    cat, defect, latency_ms, input_tokens, cost, error = jev.classify_finding(
        "x", "fake-key",
        http_post=lambda u, h, p, t: body
    )
    raw_choice = cat["choice"]
    check("ruwe choice == rareklasse (geparset)", raw_choice == "rareklasse")
    check("raw_choice niet in CLASSES", raw_choice not in jev.CLASSES)


def test_compute_calibration_known_ece():
    print("test_compute_calibration_known_ece:")
    # 10 voorspellingen, allemaal confidence 0.9, 5 juist 5 onjuist.
    # bin 9 [0.9-1.0]: mean_conf 0.9, mean_acc 0.5 -> ECE 0.4
    preds = []
    for i in range(5):
        preds.append({"id": f"c{i}", "laya_choice": "stijl", "confidence": 0.9, "error": None})
    for i in range(5):
        preds.append({"id": f"w{i}", "laya_choice": "correctheid", "confidence": 0.9, "error": None})
    labels = {}
    for i in range(5):
        labels[f"c{i}"] = "stijl"      # juist
        labels[f"w{i}"] = "stijl"      # onjuist (voorspelde correctheid)
    cal = jev.compute_calibration(preds, labels, n_bins=10)
    check("n_labeled == 10", cal["n_labeled_predictions"] == 10)
    check("overall_accuracy == 0.5", cal["overall_accuracy"] == 0.5)
    check("ECE == 0.4", cal["ece"] == 0.4)
    # Enkel de gevulde bin staat in de curve met waarden.
    bin9 = cal["curve"][9]
    check("bin 9 count == 10", bin9["count"] == 10)
    check("bin 9 mean_conf == 0.9", bin9["mean_confidence"] == 0.9)
    check("bin 9 mean_acc == 0.5", bin9["mean_accuracy"] == 0.5)

    # Perfect gekalibreerd: conf 0.5, 5/10 juist -> ECE 0.
    preds2 = []
    for i in range(5):
        preds2.append({"id": f"c{i}", "laya_choice": "stijl", "confidence": 0.5, "error": None})
    for i in range(5):
        preds2.append({"id": f"w{i}", "laya_choice": "correctheid", "confidence": 0.5, "error": None})
    cal2 = jev.compute_calibration(preds2, labels, n_bins=10)
    check("ECE == 0.0 perfect gekalibreerd", cal2["ece"] == 0.0)

    # Unlabeled en error-rijen dragen niet bij.
    preds3 = preds + [{"id": "z0", "laya_choice": "stijl", "confidence": 0.99, "error": None}]
    cal3 = jev.compute_calibration(preds3, labels, n_bins=10)
    check("unlabeled rij telt niet mee", cal3["n_labeled_predictions"] == 10)


def test_04_run_stats_reads_jev_output():
    """04_compare_and_sample.run_stats leest jev_predictions.jsonl."""
    print("test_04_run_stats_reads_jev_output:")
    with tempfile.TemporaryDirectory() as tmp:
        jev_path = os.path.join(tmp, "jev_predictions.jsonl")
        rows = []
        for i in range(12):
            choice = ["stijl", "correctheid", "beveiliging", "beleid", "overig"][i % 5]
            rows.append({
                "id": f"pr1#blocking#{i}",
                "laya_choice": choice,
                "confidence": 0.6 + (i % 5) * 0.05,
                "probabilities": {choice: 0.6},
                "latency_ms": 120.0,
                "input_tokens": 400,
                "label_lang": "nl",
                "instruction": jev.CATEGORY_INSTRUCTION,
                "error": None,
            })
        # Eén error-rij erbij.
        rows.append({
            "id": "pr1#blocking#99", "laya_choice": None, "confidence": None,
            "probabilities": None, "latency_ms": 5.0, "input_tokens": None,
            "label_lang": "nl", "instruction": jev.CATEGORY_INSTRUCTION,
            "error": "HTTP_ERROR: boom",
        })
        with open(jev_path, "w") as fh:
            for r in rows:
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")

        # 04 leest alle ids (de findings-ids). We geven de 12 succes-ids mee.
        all_ids = [r["id"] for r in rows]
        by_id, stats = compare.run_stats(jev_path, "nl", all_ids)
        check("rows_read == 13", stats["rows_read"] == 13)
        check("covered_findings == 13", stats["covered_findings"] == 13)
        check("other_errors == 1", stats["other_errors"] == 1)
        check("classified_ok == 12", stats["classified_ok"] == 12)
        # overig zit in de distributie (04's dist() telt alleen de nl-klassen +
        # CAPACITY/OTHER; overig valt eruit bij 04, maar de rij wordt wel gelezen).
        check("stijl counted", stats["distribution"].get("stijl", 0) >= 1)
        check("confidence_median is numeric", isinstance(stats["confidence_median"], (int, float)))
        # by_id heeft elke rij; 04 kan per-id opzoeken zoals het doet.
        check("by_id heeft error-rij", by_id["pr1#blocking#99"]["error"] == "HTTP_ERROR: boom")


def test_05_laya_stats_reads_jev_output():
    """05_ground_truth_check.laya_stats leest jev_predictions.jsonl."""
    print("test_05_laya_stats_reads_jev_output:")
    with tempfile.TemporaryDirectory() as tmp:
        jev_path = os.path.join(tmp, "jev_predictions.jsonl")
        rows = []
        # 4 juist als stijl, 1 onjuist, 1 capacity-error.
        for i in range(4):
            rows.append({"id": f"g{i}", "laya_choice": "stijl", "confidence": 0.7,
                         "error": None, "latency_ms": 10.0})
        rows.append({"id": "g4", "laya_choice": "correctheid", "confidence": 0.6,
                     "error": None, "latency_ms": 10.0})
        rows.append({"id": "g5", "laya_choice": None, "confidence": None,
                     "error": "CAPACITY_ERROR", "latency_ms": 1.0})
        with open(jev_path, "w") as fh:
            for r in rows:
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")
        preds_by_id = {r["id"]: r for r in rows}
        gt = [{"id": "g0", "message": "70-line function"},
              {"id": "g1", "message": "70-line function"},
              {"id": "g2", "message": "70-line function"},
              {"id": "g3", "message": "70-line function"},
              {"id": "g4", "message": "70-line function"},
              {"id": "g5", "message": "70-line function"}]
        stats = ground.laya_stats(gt, preds_by_id, correct_label="stijl")
        check("total == 6", stats["total"] == 6)
        check("capacity_errors == 1", stats["capacity_errors"] == 1)
        check("non_cap == 5", stats["non_cap"] == 5)
        check("correct (stijl) == 4", stats["correct"] == 4)
        check("pct_correct_of_total == 66.67", stats["pct_correct_of_total"] == 66.67)
        check("median_confidence == 0.7", stats["median_confidence"] == 0.7)


def test_main_no_key_exits_clean():
    """Droge run: zonder OPENROUTER_API_KEY stopt main() met exit 2 en schrijft niets."""
    print("test_main_no_key_exits_clean:")
    old = os.environ.pop("OPENROUTER_API_KEY", None)
    # Bewaak dat data/ niets krijgt: vang de echte OUT af op een temp pad.
    orig_out = jev.OUT
    orig_data = jev.DATA
    with tempfile.TemporaryDirectory() as tmp:
        jev.OUT = os.path.join(tmp, "jev_predictions.jsonl")
        jev.DATA = os.path.join(tmp, "findings.jsonl")
        # Schrijf een nep-findings bestand zodat de key-check de eerste drempel is.
        with open(jev.DATA, "w") as fh:
            fh.write(json.dumps({"id": "x", "message": "x"}) + "\n")
        rc = jev.main()
        check("exit code == 2", rc == 2)
        check("geen jev_predictions.jsonl geschreven", not os.path.exists(jev.OUT))
        check("geen defect_signals geschreven",
              not os.path.exists(os.path.join(tmp, "jev_defect_signals.jsonl")))
    jev.OUT = orig_out
    jev.DATA = orig_data
    if old is not None:
        os.environ["OPENROUTER_API_KEY"] = old


def test_main_no_key_does_not_print_value():
    """De no-key melding mag de sleutelwaarde niet afdrukken."""
    print("test_main_no_key_does_not_print_value:")
    old = os.environ.pop("OPENROUTER_API_KEY", None)
    import io
    from contextlib import redirect_stdout
    buf = io.StringIO()
    orig_out = jev.OUT
    orig_data = jev.DATA
    with tempfile.TemporaryDirectory() as tmp:
        jev.OUT = os.path.join(tmp, "jev_predictions.jsonl")
        jev.DATA = os.path.join(tmp, "findings.jsonl")
        with open(jev.DATA, "w") as fh:
            fh.write(json.dumps({"id": "x", "message": "x"}) + "\n")
        with redirect_stdout(buf):
            rc = jev.main()
        text = buf.getvalue()
    jev.OUT = orig_out
    jev.DATA = orig_data
    if old is not None:
        os.environ["OPENROUTER_API_KEY"] = old
    check("exit == 2", rc == 2)
    # De melding noemt de GEZET-vorm (key:+) als juiste manier, en vermijdt de
    # vorm die de waarde afdrukt (key:-).
    check("melding noemt GEZET-vorm (key:+)", "GEZET-vorm" in text and "key:+" in text)
    check("melding noemt de af te wijzen vorm (key:-)", "key:-" in text)


def test_atomic_write_partial_failure_preserves_target():
    """Bij onderbreking van de schrijfactie draagt het doelbestand ofwel de oude
    inhoud ofwel de volledige nieuwe inhoud, nooit een halve.

    De writer faalt halverwege. Met ``atomic_write`` bereikt ``os.replace`` het
    doel niet, dus het doel behoudt de oude inhoud. Op de oude code
    (``open(path, 'w')`` direct) werd het doel eerst afgekapt en bleef een halve
    inhoud achter. Daarom staat deze test ROOD op de code vóór de fix.
    """
    print("test_atomic_write_partial_failure_preserves_target:")
    with tempfile.TemporaryDirectory() as tmp:
        target = os.path.join(tmp, "jev_predictions.jsonl")
        with open(target, "w") as fh:
            fh.write('{"id":"old","laya_choice":"stijl"}\n')
        old = open(target).read()

        def failing_writer(fh):
            fh.write('{"id":"new1"}\n')
            raise OSError("simulated interruption")

        raised = False
        try:
            jev.atomic_write(target, failing_writer)
        except OSError:
            raised = True
        check("writer-fout propageert (niet geslikt)", raised)
        after = open(target).read()
        check("doel draagt oude inhoud, niet halve nieuwe", after == old)
        check("geen afgekapt nieuw record in doel", '{"id":"new1"}' not in after)


def test_atomic_write_success_writes_full_content():
    """Een geslaagde schrijfactie levert de volledige nieuwe inhoud en ruimt de
    tmp op. Geen .tmp achtergebleven."""
    print("test_atomic_write_success_writes_full_content:")
    with tempfile.TemporaryDirectory() as tmp:
        target = os.path.join(tmp, "jev_calibration.json")
        with open(target, "w") as fh:
            fh.write('{"old": true}')
        payload = {"status": "no_labels", "n_predictions": 3, "ece": None}
        jev.atomic_write(target, lambda fh: json.dump(payload, fh, indent=2))
        after = open(target).read()
        check("volledige nieuwe inhoud", json.loads(after) == payload)
        check("geen .tmp achtergebleven", not os.path.exists(target + ".tmp"))


def test_default_http_post_retries_on_429():
    """default_http_post herstelt na een tijdelijke 429 en geeft dan het antwoord."""
    print("test_default_http_post_retries_on_429:")
    import urllib.error

    calls = {"n": 0}

    class _Ctx:
        def __init__(self, raw):
            self._raw = raw

        def read(self):
            return self._raw.encode("utf-8")

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def fake_urlopen(req, timeout=60):
        calls["n"] += 1
        if calls["n"] < 3:
            raise urllib.error.HTTPError(req.full_url, 429, "Too Many Requests",
                                         {}, None)
        return _Ctx(json.dumps(RECORDED_BODY))

    # default_http_post gebruikt urllib.request.urlopen als context manager.
    # Monkeypatch die en herstel achteraf.
    orig_urlopen = jev.urllib.request.urlopen
    orig_sleep = jev.time.sleep
    try:
        jev.urllib.request.urlopen = fake_urlopen
        jev.time.sleep = lambda _s: None
        body = jev.default_http_post(jev.API_URL, {}, {"x": 1}, 5)
        check("recovered body is RECORDED_BODY", body == RECORDED_BODY)
        check("drie pogingen gedaan (2x 429 + 1x ok)", calls["n"] == 3)
    finally:
        jev.urllib.request.urlopen = orig_urlopen
        jev.time.sleep = orig_sleep


def main():
    test_parse_response_happy()
    test_parse_response_bad_shapes()
    test_build_request_payload_fanout()
    test_api_url_and_model_are_measured()
    test_classify_finding_keys_match_laya()
    test_prediction_record_exact_laya_keys()
    test_classify_finding_http_error_booked_not_faked()
    test_unknown_choice_flagged()
    test_compute_calibration_known_ece()
    test_04_run_stats_reads_jev_output()
    test_05_laya_stats_reads_jev_output()
    test_main_no_key_exits_clean()
    test_main_no_key_does_not_print_value()
    test_atomic_write_partial_failure_preserves_target()
    test_atomic_write_success_writes_full_content()
    test_default_http_post_retries_on_429()
    print()
    if FAILS:
        print(f"FAILED: {len(FAILS)} checks -> {FAILS}")
        sys.exit(1)
    print("All checks passed.")


if __name__ == "__main__":
    main()
