"""De overgangstabel en de afgeleide stand van ADR-077 § 3; de enige plek waar de tabel staat (stdlib)."""
from collections import namedtuple

VERVANGT = "vastgelegd met vervangt"  # een nieuwe vastgelegd met vervangt: de gebeurtenis voor het vervangen besluit
# (fase, gebeurtenis) -> nieuwe fase. Fases: geen, open (geldig of te_herzien), vervangen open, vervangen dicht,
# ingetrokken. Elke cel die hier niet staat is geweigerd.
TABEL = {("geen", "vastgelegd"): "open", ("open", "herzien"): "open", ("open", "bevestigd"): "open",
         ("open", "ingetrokken"): "ingetrokken", ("open", VERVANGT): "vervangen open",
         ("vervangen open", "vervangen"): "vervangen dicht"}
NAAM = {"open": "geldig", "vervangen open": "vervangen", "vervangen dicht": "vervangen", "ingetrokken": "ingetrokken"}

Stand = namedtuple("Stand", "naam fase door herzien_op")  # naam: geldig, te_herzien, vervangen, ingetrokken of geen

def stap(fase, gebeurtenis, regel=None, door=None):
    """De fase na `gebeurtenis`, of None als de tabel hem weigert; een `vervangen` moet `door` van de open vervanging dragen."""
    naar = TABEL.get((fase, gebeurtenis)) if isinstance(gebeurtenis, str) else None
    if gebeurtenis == "vervangen" and (regel or {}).get("door") != door:  # geen regel of andere door: geweigerd
        return None
    return naar

def stand(regels, bid, nu=None):
    """Stand van bid uit de regels in bestandsvolgorde; alleen overgangen uit de tabel tellen. `nu` is een date of
    ISO-tekst; zonder `nu` (None) is een open besluit nooit te_herzien. herzien_op is die van de laatste regel met
    `betreft` bid die het veld draagt."""
    fase, door, herzien_op = "geen", None, None
    for x in regels:
        e = x["event"]
        if x["betreft"] == bid and "herzien_op" in x:
            herzien_op = x["herzien_op"]
        kandidaten = ((e, x["besluit_id"] == bid), (VERVANGT, x.get("vervangt") == bid)) if e == "vastgelegd" \
            else ((e, x["betreft"] == bid),)
        for gebeurtenis, geldt in kandidaten:
            if geldt and (naar := stap(fase, gebeurtenis, x, door)):
                fase, door = naar, (x["besluit_id"] if gebeurtenis == VERVANGT else door)
                break
    nu = str(nu)[:10] if nu else None
    naam = NAAM.get(fase, "geen")
    if fase == "open" and nu and herzien_op and nu >= herzien_op:
        naam = "te_herzien"
    return Stand(naam, fase, door, herzien_op)

def mag(regel, regels):
    """None als de schrijver `regel` mag toevoegen, anders de weigering als (gebeurtenis, fase); wat geen id noemt
    (valideer weigert het daarna met de veldnaam) geeft None."""
    e = VERVANGT if regel.get("event") == "vastgelegd" else regel.get("event")
    bid = regel.get("vervangt" if e == VERVANGT else "betreft")
    if isinstance(bid, str):
        s = stand(regels, bid)
        if stap(s.fase, e, regel, s.door) is None:
            return e, s.fase
    return None

def bevindingen(regels, bekend):
    """(soort, wat) voor elke regel die de schrijver zou weigeren, in bestandsvolgorde (check, ADR-077 § 3 Controle);
    `bekend` zijn de besluit_id's met een vastgelegd. Fouten die check al apart meldt (dubbel id, onbekend vervangt of
    betreft) vallen hier weg."""
    uit, fase, door = [], {}, {}
    def toets(b, e, x, wat):
        if (naar := stap(fase[b], e, x, door.get(b))) is not None:
            fase[b] = naar
            return True
        uit.append(("vervangen zonder nieuwe vastgelegd", b) if (fase[b], e) == ("open", "vervangen")
                   else ("gebeurtenis na een eindstand", wat))
    for x in regels:
        e, b = x["event"], x["betreft"]
        if e == "vastgelegd":
            if b in fase:
                continue  # dubbel id: al gemeld
            fase[b], v = stap("geen", e), x.get("vervangt")
            if v is not None and v in bekend:
                if v not in fase:
                    uit.append(("gebeurtenis vóór de vastlegging", f"{b} vervangt {v}"))
                else:
                    if toets(v, VERVANGT, x, f"{b} vervangt {v}"):
                        door[v] = b
        elif b not in fase:
            if b in bekend:
                uit.append(("gebeurtenis vóór de vastlegging", f"{e} op {b}"))
        else:
            toets(b, e, x, f"{e} op {b}")
    return uit + [("open vervanging (draai vervang opnieuw)", f"{b} door {door[b]}")
                  for b, f in fase.items() if f == "vervangen open"]
