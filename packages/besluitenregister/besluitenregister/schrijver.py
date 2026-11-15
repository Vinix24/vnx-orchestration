"""Schrijver van besluiten.ndjson (ADR-077 besluit 3, 4 en 6): flock, staartherstel, ontdubbeling, geheimweigering."""
import base64, fcntl, hashlib, json, os
from collections import namedtuple
from datetime import datetime, timezone
from pathlib import Path

from . import identiteit
from .geheimpatronen import PATTERNS
from .lezer import lees
from .schema import EXTRA, GEMEEN, SCHEMA, valideer
from .stand import mag

Uitkomst = namedtuple("Uitkomst", "besluit_id geschreven")  # geschreven: aantal regels dat erbij kwam

class Geweigerd(Exception):
    """Een regel die de schrijver weigert (B2: exit 3). HerkomstBotsing: zelfde herkomst, andere besluittekst;
    GeheimGeweigerd: veld en patroonnaam in de melding, nooit de waarde."""

class HerkomstBotsing(Geweigerd): pass

class GeheimGeweigerd(Geweigerd): pass

_NAMEN = GEMEEN.union(*EXTRA.values(), ("soort", "ref", "citaat", "optie", "waarom_niet"))

def _teksten(w, pad=""):
    """(pad, tekst) voor elke waarde en elke sleutel; een sleutel buiten het schema heet <veldnaam> in het pad."""
    if isinstance(w, str):
        yield pad, w
    for k, v in w.items() if isinstance(w, dict) else enumerate(w) if isinstance(w, list) else ():
        naam = k if isinstance(k, int) or k in _NAMEN else "<veldnaam>"
        sub = f"{pad}.{naam}" if pad else str(naam)
        if isinstance(k, str):
            yield sub, k
        yield from _teksten(v, sub)

def weiger_geheimen(regel):
    for pad, tekst in _teksten(regel):
        if p := next((p for p in PATTERNS if p.regex.search(tekst)), None):
            raise GeheimGeweigerd(f"veld {pad}: geheimpatroon {p.name}")

def weiger_overgang(r, regels):
    if geweigerd := mag(r, regels):
        raise Geweigerd(f"overgang: {geweigerd[0]} in stand {geweigerd[1]}")

def _fsync_append(pad, data=None, lengte=None):
    """Eén write met O_APPEND plus fsync, of (lengte gegeven) ftruncate plus fsync."""
    fd = os.open(pad, os.O_WRONLY | (os.O_APPEND | os.O_CREAT if lengte is None else 0), 0o644)
    try:
        if lengte is not None:
            os.ftruncate(fd, lengte)
        elif os.write(fd, data) != len(data):
            raise OSError("korte write")
        os.fsync(fd)
    finally:
        os.close(fd)

class Register:
    def __init__(self, logdir, klok=None):
        self.pad = Path(logdir) / "besluiten.ndjson"
        self.klok = klok or (lambda: datetime.now(timezone.utc))

    def _kwarantaine(self, offset, staart):
        pad, sha = str(self.pad) + ".afgescheurd", hashlib.sha256(staart).hexdigest()
        data = Path(pad).read_bytes() if os.path.exists(pad) else b""
        kop = data[: data.rfind(b"\n") + 1]
        if kop != data:  # eigen afgescheurde staart van het quarantainebestand: inkorten, geen vermelding
            _fsync_append(pad, lengte=len(kop))
        if not any(f'"offset": {offset},' in x and f'"sha256": "{sha}"' in x for x in kop.decode("utf-8", "replace").split("\n")):
            vermelding = {"offset": offset, "sha256": sha, "bytes": base64.b64encode(staart).decode("ascii")}
            _fsync_append(pad, (json.dumps(vermelding, sort_keys=True) + "\n").encode("utf-8"))

    def _herstel_staart(self):
        data = self.pad.read_bytes() if self.pad.exists() else b""
        offset = data.rfind(b"\n") + 1
        if offset < len(data):
            self._kwarantaine(offset, data[offset:])
            _fsync_append(self.pad, lengte=offset)

    def _maak(self, velden, regels):
        """Een regel uit de velden van de aanroeper; (None, besluit_id) als hij al bestaat."""
        r = dict(velden, schema=SCHEMA, vastgelegd_op=self.klok().strftime("%Y-%m-%dT%H:%M:%SZ"))
        weiger_geheimen(r)  # vóór elke andere weigering: die mag daarna een veld of id noemen (ADR-077 besluit 6)
        if regels and regels[0]["context"] != r.get("context"):
            raise Geweigerd("context wijkt af van de eerste regel in het bestand")
        if r.get("event") == "vastgelegd":
            bron = r.get("bron")
            if isinstance(bron, dict) and all(isinstance(r.get(k), str) for k in ("context", "titel")):
                r["herkomst"] = identiteit.herkomst(r["context"], bron.get("soort"), bron.get("ref"), r["titel"])
                for oud in (x for x in regels if x["event"] == "vastgelegd" and x["herkomst"] == r["herkomst"]):
                    if oud["besluit"] != r.get("besluit"):
                        raise HerkomstBotsing(f"herkomst bestaat al met andere tekst: {oud['besluit_id']}")
                    return None, oud["besluit_id"]
            if isinstance(r.get("besloten_op"), str):
                r["besluit_id"] = r["betreft"] = identiteit.nieuw_besluit_id(
                    r["besloten_op"], {x["besluit_id"] for x in regels if x["event"] == "vastgelegd"})
        r["event_id"] = identiteit.event_id(r)
        if any(x["event_id"] == r["event_id"] for x in regels):
            return None, r.get("betreft")
        weiger_overgang(r, regels)
        if fouten := valideer(r):
            raise Geweigerd("; ".join(fouten))
        return r, r["betreft"]

    def schrijf(self, *velden):
        """Regels (dict, of functie van de eerdere besluit_id's) in één write onder het slot; Uitkomst van de eerste."""
        with open(str(self.pad) + ".lock", "a") as slot:
            fcntl.flock(slot, fcntl.LOCK_EX)
            self._herstel_staart()
            regels = [r for _, r in lees(self.pad).regels]
            ids, nieuw = [], []
            for v in velden:
                r, besluit_id = self._maak(v(ids) if callable(v) else v, regels)
                ids.append(besluit_id)
                if r:
                    regels.append(r)
                    nieuw.append(r)
            if nieuw:
                _fsync_append(self.pad, "".join(identiteit.canoniek(r) + "\n" for r in nieuw).encode("utf-8"))
            return Uitkomst(ids[0], len(nieuw))

    def vervang(self, oud_id, velden, reden):
        """Nieuwe vastgelegd plus afsluitende vervangen in één write; herhalen herstelt een afgescheurde sluitregel."""
        nieuw = dict(velden, event="vastgelegd", vervangt=oud_id)
        kop = {k: nieuw.get(k) for k in ("context", "besloten_op", "vastgelegd_door")}
        def sluit(ids):
            if ids[0] == oud_id:  # ontdubbeling gaf het oude besluit terug: geen opvolger
                raise Geweigerd(f"vervang: besluit {oud_id} zou door zichzelf vervangen worden")
            return dict(kop, event="vervangen", betreft=oud_id, door=ids[0], reden=reden)

        return self.schrijf(nieuw, sluit)
