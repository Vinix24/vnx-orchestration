"""Lezer van besluiten.ndjson (ADR-077 besluit 4): leest bytes, splitst op \\n, schrijft nooit."""
import json
from collections import namedtuple
from pathlib import Path

from .schema import valideer

# afgescheurd: [(offset, bytes)] stuk zonder \n; ongeldig: [(regelnummer, meldingen)]; regels: geldig en afgesloten
Uitslag = namedtuple("Uitslag", "regels afgescheurd ongeldig")

def lees(pad):
    data = Path(pad).read_bytes() if Path(pad).exists() else b""
    stukken = data.split(b"\n")
    staart = stukken.pop()  # na de laatste \n: leeg, of een nooit gemelde schrijfactie
    uit = Uitslag([], [], [])
    for nr, stuk in enumerate(stukken, 1):
        try:
            regel = json.loads(stuk.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            uit.ongeldig.append((nr, ["geen geldige JSON"]))
            continue
        fouten = valideer(regel)
        (uit.ongeldig if fouten else uit.regels).append((nr, fouten or regel))
    if staart:
        uit.afgescheurd.append((len(data) - len(staart), staart))
    return uit
