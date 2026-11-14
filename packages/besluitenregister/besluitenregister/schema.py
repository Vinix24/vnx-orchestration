"""Schema besluit/1 (ADR-077 besluit 2): velden, gesloten lijsten, validatie."""
import re
from datetime import datetime

SCHEMA = "besluit/1"
EVENTS = ("vastgelegd", "vervangen", "ingetrokken", "herzien", "bevestigd")
SOORTEN = ("chat", "mail", "meeting", "pr", "oi", "adr", "document", "daily-log")
MENSELIJK = ("operator", "klant", "team")  # bij chat/mail/meeting is dan een citaat verplicht
GEMEEN = {"schema", "event", "betreft", "event_id", "context", "besloten_op", "vastgelegd_op", "vastgelegd_door"}
EXTRA = {
    "vastgelegd": {"besluit_id", "herkomst", "titel", "besluit", "waarom", "alternatieven", "besloten_door", "bron",
                   "reikwijdte", "vertrouwelijkheid", "terugweg", "herzien_op", "vervangt", "links", "tags"},
    "vervangen": {"door", "reden"}, "ingetrokken": {"reden", "bron"}, "herzien": {"waarom", "herzien_op"},
    "bevestigd": {"bron", "herzien_op"}}
OPTIONEEL = {"vastgelegd": {"herzien_op", "vervangt"}}

def _m(patroon, v):
    return isinstance(v, str) and re.fullmatch(patroon, v) is not None

def _tekst(v, maximum=None):
    return isinstance(v, str) and v.strip() != "" and (maximum is None or len(v) <= maximum)

def _tijd(v, dag=True):
    vormen = {20: "%Y-%m-%dT%H:%M:%SZ", **({10: "%Y-%m-%d"} if dag else {})}
    try:
        return isinstance(v, str) and len(v) in vormen and bool(datetime.strptime(v, vormen[len(v)]))
    except ValueError:
        return False

def _bron(v):
    return (isinstance(v, dict) and set(v) == {"soort", "ref", "citaat"} and v["soort"] in SOORTEN
            and isinstance(v["ref"], str) and isinstance(v["citaat"], str))

def _alt(v):
    return isinstance(v, list) and all(isinstance(a, dict) and set(a) == {"optie", "waarom_niet"}
                                       and all(isinstance(x, str) for x in a.values()) for a in v)

_ID = r"B-[0-9]{8}-[0-9a-f]{8}"
_CHECKS = {
    "schema": lambda v, r: v == SCHEMA, "event_id": lambda v, r: _m(r"E-[0-9a-f]{64}", v),
    "betreft": lambda v, r: _m(_ID, v), "besluit_id": lambda v, r: _m(_ID, v) and v == r.get("betreft"),
    "context": lambda v, r: _m(r"[a-z0-9][a-z0-9:._-]*", v), "besloten_op": lambda v, r: _tijd(v),
    "vastgelegd_op": lambda v, r: _tijd(v, dag=False),
    "vastgelegd_door": lambda v, r: _m(r"operator|t0|skill:.+|agent:.+", v),
    "herkomst": lambda v, r: _m(r"[0-9a-f]{64}", v), "titel": lambda v, r: _tekst(v, 100),
    "alternatieven": lambda v, r: _alt(v), "bron": lambda v, r: _bron(v),
    "reikwijdte": lambda v, r: v in ("context", "vloot", "klant"),
    "vertrouwelijkheid": lambda v, r: v in ("publiek", "intern", "klant"),
    "herzien_op": lambda v, r: v is None or (_m(r"\d{4}-\d{2}-\d{2}", v) and _tijd(v)),
    "vervangt": lambda v, r: v is None or _m(_ID, v), "door": lambda v, r: _m(_ID, v),
    "links": lambda v, r: isinstance(v, list) and all(isinstance(x, str) for x in v),
    "tags": lambda v, r: isinstance(v, list) and all(_m(r"[a-z0-9-]+", x) for x in v),
}

def valideer(regel):
    """Alle schendingen van besluit/1 als lijst teksten; leeg = geldig. Noemt nooit een waarde."""
    if not isinstance(regel, dict):
        return ["regel: geen JSON-object"]
    event = regel.get("event")
    if event not in EVENTS:
        return ["event: waarde buiten de lijst"]
    toegestaan = GEMEEN | EXTRA[event]
    fouten = [f"{k}: onbekend veld" for k in sorted(set(regel) - toegestaan, key=str)]
    fouten += [f"{k}: ontbreekt" for k in sorted(toegestaan - OPTIONEEL.get(event, set()) - set(regel))]
    fouten += [f"{k}: ongeldige waarde" for k in sorted(toegestaan & set(regel))
               if not _CHECKS.get(k, lambda v, r: _tekst(v))(regel[k], regel)]
    if fouten:
        return fouten
    if regel.get("herzien_op") and regel["herzien_op"] < regel["besloten_op"][:10]:
        fouten.append("herzien_op: voor de datum van besloten_op")
    bron, mens = regel.get("bron"), regel.get("besloten_door", regel["vastgelegd_door"]) in MENSELIJK
    if bron and mens and bron["soort"] in ("chat", "mail", "meeting") and not _tekst(bron["citaat"]):
        fouten.append("bron.citaat: verplicht voor een mens bij chat, mail of meeting")
    if not regel["context"].startswith("klant:") and "klant" in (regel.get("vertrouwelijkheid"), regel.get("reikwijdte")):
        fouten.append("vertrouwelijkheid/reikwijdte: klant alleen in een klant:-context")
    return fouten
