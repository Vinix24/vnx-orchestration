#!/usr/bin/env python3
"""refactor_equivalence.py — bewijs dat een verplaatsing gedrag-neutraal was.

Het probleem dat dit oplost: bij een pure code-verplaatsing bestaat er per definitie geen
test die rood staat op de oude code en groen op de nieuwe. Dat zou betekenen dat er gedrag
veranderde, precies wat je NIET wilt. De eis uit OI-893 ("een probe die alleen ja kan zeggen
is geen meting") blijft echter overeind, dus er moet een ander hard bewijs zijn.

Dit script levert dat: het vergelijkt de genormaliseerde AST-dump van elke verplaatste functie
VOOR en NA. Identiek = de logica is byte-voor-byte dezelfde, ongeacht waar hij nu staat.
Verschillend = er is iets gewijzigd, en dan zegt het WAT.

Waarom AST en niet een tekst-diff: inspringing, lege regels en commentaar veranderen bij een
verplaatsing legitiem. De AST-dump negeert die en vergelijkt alleen de structuur die het
gedrag bepaalt. Docstrings blijven WEL meegenomen (die zijn onderdeel van het object).

Gebruik:
    # vergelijk een git-ref met de werkkopie
    python3 refactor_equivalence.py --before-ref origin/main \
        --functions _parse_pr_number,_parse_pr_numbers \
        --before-file scripts/lib/track_reconciler.py \
        --after-file  scripts/lib/track_reconciler_pr_lookup.py

Exit 0 = alle functies identiek. Exit 1 = minstens een verschil (of een functie niet gevonden).
Exit 2 = gebruiksfout.
"""
from __future__ import annotations

import argparse
import ast
import subprocess
import sys
from pathlib import Path


def _read_ref(ref: str, path: str) -> str:
    """Lees een bestand op een git-ref. Faalt LUID: een lege string zou stil slagen."""
    r = subprocess.run(
        ["git", "show", f"{ref}:{path}"], capture_output=True, text=True
    )
    if r.returncode != 0:
        raise SystemExit(f"[equivalence] kan {path} niet lezen op ref {ref}: {r.stderr.strip()}")
    return r.stdout


def _find(src: str, name: str, origin: str) -> ast.AST:
    """Zoek een top-level functie `name`, of een methode via 'Klasse.methode'.

    Alleen `tree.body` (module-scope) en `ClassDef.body` van de genoemde klasse worden
    doorzocht. Geneste functies/closures met dezelfde naam tellen NIET mee — een tool die
    stil een geneste functie vergelijkt alsof het de top-level functie is, roept IDENTIEK
    over de verkeerde code. Zijn er op hetzelfde niveau meerdere definities met dezelfde
    naam (overschrijving), dan is de vergelijking ambigu en faalt de tool luid in plaats
    van de eerste te pakken.
    """
    tree = ast.parse(src)
    if "." in name:
        cls_name, meth = name.split(".", 1)
        classes = [n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == cls_name]
        if not classes:
            raise SystemExit(f"[equivalence] klasse {cls_name} niet gevonden in {origin}")
        candidates = [
            m
            for c in classes
            for m in c.body
            if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef)) and m.name == meth
        ]
    else:
        candidates = [
            n
            for n in tree.body
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name
        ]
    if not candidates:
        raise SystemExit(f"[equivalence] functie {name} niet gevonden in {origin}")
    if len(candidates) > 1:
        raise SystemExit(
            f"[equivalence] {name} is ambigu in {origin}: {len(candidates)} definities "
            f"op hetzelfde niveau — geen vergelijking mogelijk zonder welke bedoeld is"
        )
    return candidates[0]


def _normalise(node: ast.AST) -> str:
    """AST-dump zonder positie-informatie: regelnummers verschuiven bij verplaatsing."""
    return ast.dump(node, annotate_fields=True, include_attributes=False)


def main() -> int:
    p = argparse.ArgumentParser(description="Bewijs gedrag-neutraliteit van een code-verplaatsing")
    p.add_argument("--before-ref", default="origin/main", help="git-ref met de oude code")
    p.add_argument("--before-file", required=True, help="pad van het bestand OP die ref")
    p.add_argument("--after-file", required=True, help="pad in de WERKKOPIE waar de code nu staat")
    p.add_argument("--after-ref", default=None, help="optioneel: vergelijk twee refs i.p.v. werkkopie")
    p.add_argument("--functions", required=True, help="comma-lijst; 'Klasse.methode' mag")
    args = p.parse_args()

    names = [n.strip() for n in args.functions.split(",") if n.strip()]
    if not names:
        print("[equivalence] geen functies opgegeven", file=sys.stderr)
        return 2

    before_src = _read_ref(args.before_ref, args.before_file)
    if args.after_ref:
        after_src = _read_ref(args.after_ref, args.after_file)
        after_origin = f"{args.after_ref}:{args.after_file}"
    else:
        ap = Path(args.after_file)
        if not ap.is_file():
            print(f"[equivalence] werkkopie ontbreekt: {ap}", file=sys.stderr)
            return 2
        after_src = ap.read_text(encoding="utf-8")
        after_origin = str(ap)

    identiek, verschillend = [], []
    for name in names:
        a = _normalise(_find(before_src, name, f"{args.before_ref}:{args.before_file}"))
        b = _normalise(_find(after_src, name, after_origin))
        (identiek if a == b else verschillend).append(name)

    for n in identiek:
        print(f"  IDENTIEK    {n}")
    for n in verschillend:
        print(f"  GEWIJZIGD   {n}   <-- de body is NIET byte-gelijk; dit is geen pure verplaatsing")

    print(
        f"\n[equivalence] {len(identiek)}/{len(names)} functies bewezen gedrag-neutraal "
        f"({args.before_ref}:{args.before_file} -> {after_origin})"
    )
    return 0 if not verschillend else 1


if __name__ == "__main__":
    sys.exit(main())
