#!/usr/bin/env python3
"""refactor_surface.py — bewijs dat het publieke oppervlak na een verplaatsing intact is.

Tweede helft van de regressie-bewijslast naast refactor_equivalence.py. Die laatste bewijst
dat de LOGICA niet veranderde; dit script bewijst dat elke CONSUMER de verplaatste namen nog
op de oude plek kan importeren.

Waarom dit apart moet: een verplaatsing met een vergeten re-export slaagt in de tests van het
verplaatste bestand zelf en breekt pas bij de eerste consumer die hem aanroept. Dat is precies
het patroon dat op 2026-08-01 meermaals is gemeten (OI-919, OI-846, OI-657: een test die een
verdwenen oppervlak bleef toetsen, of code die stil een ander pad nam).

Het script doet drie dingen:
  1. Importeert de ORIGINELE module en controleert dat elke opgegeven naam er nog uit te halen
     is (de re-export). Faalt dit, dan breekt elke bestaande importeur.
  2. Importeert de NIEUWE module en controleert dat de naam daar ook echt staat.
  3. Controleert dat beide naar HETZELFDE object wijzen (identiteit, niet alleen aanwezigheid).
     Zonder deze derde stap slaagt een re-export die per ongeluk een andere functie met dezelfde
     naam exporteert.

Gebruik:
    python3 refactor_surface.py \
        --original scripts/lib/track_reconciler.py \
        --new      scripts/lib/track_reconciler_pr_lookup.py \
        --names    _parse_pr_number,_parse_pr_numbers,_git_toplevel

Exit 0 = oppervlak intact. Exit 1 = minstens een naam ontbreekt of wijst naar een ander object.
"""
from __future__ import annotations

import argparse
import importlib
import sys
from pathlib import Path

# scripts/lib op sys.path voor de project-root-helper én als basis-importpad. Het
# script staat zelf in scripts/, dus dit pad volgt uit de eigen ligging en is
# onafhankelijk van de cwd van de aanroeper (gate-worktrees, tests/).
sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))

from project_root import resolve_project_root  # noqa: E402


def _module_name(path: str) -> str:
    return Path(path).stem


def main() -> int:
    p = argparse.ArgumentParser(description="Bewijs dat het import-oppervlak intact bleef")
    p.add_argument("--original", required=True, help="het bestand waaruit verplaatst is")
    p.add_argument("--new", required=True, help="het bestand waarheen verplaatst is")
    p.add_argument("--names", required=True, help="comma-lijst van verplaatste namen")
    p.add_argument(
        "--lib-dir",
        default=None,
        help="dir om op sys.path te zetten (default: <project-root>/scripts/lib, "
             "opgelost via scripts/lib/project_root.py, nooit cwd-relatief)",
    )
    args = p.parse_args()

    if args.lib_dir is not None:
        lib_dir = Path(args.lib_dir).resolve()
    else:
        lib_dir = resolve_project_root(__file__) / "scripts" / "lib"
    sys.path.insert(0, str(lib_dir))

    names = [n.strip() for n in args.names.split(",") if n.strip()]
    if not names:
        print("[surface] geen namen opgegeven", file=sys.stderr)
        return 2

    try:
        orig = importlib.import_module(_module_name(args.original))
    except Exception as exc:  # vnx-silent-except: fail-loud, wordt direct gerapporteerd
        print(f"[surface] FAAL: originele module niet importeerbaar: {exc}", file=sys.stderr)
        return 1
    try:
        new = importlib.import_module(_module_name(args.new))
    except Exception as exc:  # vnx-silent-except: fail-loud, wordt direct gerapporteerd
        print(f"[surface] FAAL: nieuwe module niet importeerbaar: {exc}", file=sys.stderr)
        return 1

    fouten = []
    for n in names:
        in_orig = hasattr(orig, n)
        in_new = hasattr(new, n)
        same = in_orig and in_new and getattr(orig, n) is getattr(new, n)
        if in_orig and in_new and same:
            print(f"  OK          {n}  (re-export wijst naar hetzelfde object)")
        else:
            reden = []
            if not in_orig:
                reden.append("ONTBREEKT in de originele module: elke bestaande importeur breekt")
            if not in_new:
                reden.append("ONTBREEKT in de nieuwe module")
            if in_orig and in_new and not same:
                reden.append("re-export wijst naar een ANDER object dan de nieuwe module")
            print(f"  FAAL        {n}  <-- {'; '.join(reden)}")
            fouten.append(n)

    print(f"\n[surface] {len(names) - len(fouten)}/{len(names)} namen bewezen intact")
    return 0 if not fouten else 1


if __name__ == "__main__":
    sys.exit(main())
