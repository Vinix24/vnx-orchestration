"""Besluitpunten uit een daily-log (ADR-077 § 7): alleen de bullets onder `## Beslissingen` of `### Beslissingen...`."""
import re
from pathlib import Path

_KOP, _BULLET, _DATUM = re.compile(r"(#{1,6})\s+(.*?)\s*#*\s*"), re.compile(r"[-*+]\s+(.*)"), re.compile(r"\d{4}-\d{2}-\d{2}")
_HEK = re.compile(r" {0,3}(`{3,}|~{3,})(.*)")
LEEG = {"", "geen", "-", "n.v.t.", "n.v.t"}

def datum(tekst, naam):
    """`date` uit de YAML-frontmatter, anders de JJJJ-MM-DD uit de bestandsnaam; None als beide ontbreken."""
    kop = tekst.split("\n---", 1)[0].splitlines() if tekst.startswith("---\n") else []
    for regel in kop:
        if (m := re.fullmatch(r"date:\s*[\"']?(\d{4}-\d{2}-\d{2})[\"']?\s*", regel)):
            return m.group(1)
    return m.group(0) if (m := _DATUM.search(naam)) else None

def punten(tekst):
    """Elke bullet op het hoogste niveau in elke beslissingssectie; een geneste bullet hoort bij zijn ouder.
    Geeft (gezien, [(positie, tekst)]); positie is `<kop>/<volgnummer>` (volgnummer telt alle bullets van die kop,
    ook de lege, zodat de positie niet verschuift als een leeg punt wordt gevuld)."""
    uit, niveau, huidig, vorige_leeg, hek, gezien, kop, teller = [], None, None, False, None, False, "", {}
    for regel in tekst.splitlines():
        if (m := _HEK.match(regel)):
            if hek is None:
                hek = (m.group(1)[0], len(m.group(1)))
                continue
            if m.group(1)[0] == hek[0] and len(m.group(1)) >= hek[1] and not m.group(2).strip():
                hek = None
            continue
        if hek is not None:
            continue
        k = _KOP.fullmatch(regel)
        if k:
            n = len(k.group(1))
            if niveau is not None and n <= niveau:
                niveau = None
            if niveau is None and n in (2, 3) and k.group(2).startswith("Beslissingen"):
                niveau, gezien, kop = n, True, k.group(2).strip()
            huidig = None
            continue
        if niveau is None:
            continue
        if (b := _BULLET.fullmatch(regel)):
            teller[kop] = teller.get(kop, 0) + 1
            huidig = [b.group(1).strip()]
            uit.append((f"{kop}/{teller[kop]}", huidig))
        elif huidig is not None and regel.strip() and (regel[0] in " \t" or not vorige_leeg):
            huidig.append(regel.rstrip())
        else:
            huidig = None
        vorige_leeg = not regel.strip()
    return gezien, [(pos, "\n".join(p).strip()) for pos, p in uit if p[0].strip().lower() not in LEEG]

def titel(tekst):
    """Eerste zin, hooguit 100 tekens."""
    return re.split(r"(?<=[.!?])\s", tekst.split("\n", 1)[0], maxsplit=1)[0][:100].strip()

def lees_log(pad):
    pad = Path(pad)
    tekst = pad.read_text(encoding="utf-8")
    gezien, lijst = punten(tekst)
    return datum(tekst, pad.name), gezien, lijst
