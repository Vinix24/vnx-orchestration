#!/usr/bin/env python3
"""Regex keyword baseline classifier over the 657 findings.

Reconstructie van de 20-september trefwoord-regex. De baseline had smalle
trefwoordlijsten voor security en stijl, een paar beleidstermen, en liet alles
wat niet matchte vallen in een correctheid-vangnet. Die 20-september run liet
56% van de adviserende bevindingen in dat vangnet vallen.

Vier echte klassen plus een expliciete restklasse 'overig' (zoals de dispatch
voorschrijft: restbak expliciet meetellen als eigen klasse).

Classes:
  stijl         - function size, naming, comments, lint, unused import, docstring
  correctheid   - logica, race, atomiciteit, verkeerde uitkomst
  beveiliging   - injection, traversal, yolo, secret, credential, rechten, fail-open
  beleid        - ADR, contract, ongeledgerde mutatie, governance
  overig        - restbak (expliciet, in plaats van stilletjes correctheid te noemen)

Ordering: security > beleid > stijl > correctheid > overig.
"""
import json
import os
import re
import sys
from collections import Counter

DATA = os.path.join(os.path.dirname(__file__), "data", "findings.jsonl")
OUT = os.path.join(os.path.dirname(__file__), "data", "regex_predictions.jsonl")

# Smalle trefwoordlijsten, zoals de 20-september baseline. Brede termen
# ("error", "wrong", "must", "gate", "spec") zijn bewust NIET opgenomen omdat
# die in de originele run ontbraken en juist de restbak groot maakten.
SECURITY_RE = re.compile(
    r"\b(injection|traversal|path[- ]traversal|yolo|secret|credential|"
    r"api[-_ ]?key|hardcod|password|passwd|token\s+leak|"
    r"fail[- ]?open|shell=True|eval\(|os\.system|deserializ|"
    r"xss|csrf|sqli|privilege\s+esc|escalat)",
    re.IGNORECASE,
)

STYLE_RE = re.compile(
    r"\b(function\s+size|70[- ]line|too\s+long|cyclomatic|"
    r"naming|rename|unused\s+import|dead\s+code|"
    r"docstring|lint|flake|pylint|ruff|"
    r"magic\s+number|trailing\s+whitespace|"
    r"type\s+hint|return\s+type\s+annotation)",
    re.IGNORECASE,
)

POLICY_RE = re.compile(
    r"\b(ADR|architecture\s+decision|decision\s+record|"
    r"contract\s+breach|contract\s+violation|unlodged|unl?odged\s+mutation|"
    r"governance|audit\s+trail|provenance)",
    re.IGNORECASE,
)

CORRECTNESS_RE = re.compile(
    r"\b(race\s+condition|atomicity|atomic|deadlock|concurrency\s+bug|"
    r"off[- ]by[- ]one|logic\s+error|logical\s+error|"
    r"silent(?:ly)?\s+(?:fail|swallow|discard)|exception\s+swallow)",
    re.IGNORECASE,
)


def classify(msg):
    if SECURITY_RE.search(msg):
        return "beveiliging"
    if POLICY_RE.search(msg):
        return "beleid"
    if STYLE_RE.search(msg):
        return "stijl"
    if CORRECTNESS_RE.search(msg):
        return "correctheid"
    return "overig"


def main():
    rows = []
    with open(DATA) as fh:
        for line in fh:
            rows.append(json.loads(line))

    preds = []
    for r in rows:
        label = classify(r["message"])
        preds.append({"id": r["id"], "regex_label": label})

    with open(OUT, "w") as fh:
        for p in preds:
            fh.write(json.dumps(p, ensure_ascii=False) + "\n")

    dist = Counter(p["regex_label"] for p in preds)
    total = len(preds)
    print(f"Regex baseline over {total} findings -> {OUT}")
    for k in ["stijl", "correctheid", "beveiliging", "beleid", "overig"]:
        c = dist.get(k, 0)
        print(f"  {k:12s}: {c:4d}  ({100*c/total:5.1f}%)")

    adv = [p for p, r in zip(preds, rows) if r["severity_tier"] == "advisory"]
    adv_rest = sum(1 for p in adv if p["regex_label"] == "overig")
    print(f"advisory in rest bin (overig): {adv_rest}/{len(adv)} ({100*adv_rest/len(adv):.1f}%)")
    # Voor referentie: de 20-september run had correctheid als vangnet (geen
    # aparte overig-klasse). Toen was 56% van advisory in dat vangnet geland.
    adv_correctness = sum(1 for p in adv if p["regex_label"] == "correctheid")
    print(f"advisory in correctness (oude vangnet-equivalent): {adv_correctness}/{len(adv)} ({100*adv_correctness/len(adv):.1f}%)")


if __name__ == "__main__":
    sys.exit(main())