"""CLI van het besluitenregister (ADR-077): ``python3 -m besluitenregister <sub> --context <LOGDIR> ...``.

Subcommando's: add, list, show, vervang, trek-in, herzien, bevestig, check, render, export; stdlib.
Exitcodes: 0 ok; 1 check of lezen niet schoon (check-fout, export met een ongeldige regel, show van een onbekend id);
2 gebruik (vlag, export zonder geldig --voor, map bestaat niet, geen slug); 3 geweigerd door de kern (geheim: de
patroonnaam, nooit de waarde). ``render`` is intern en dagonafhankelijk; ``te_herzien`` alleen in list/show (--nu).
"""
import argparse, os, re, sys, tempfile
from datetime import datetime, timezone
from pathlib import Path

from .dagboek import lees_log, titel
from .geheimpatronen import PATTERNS
from .lezer import lees
from .schrijver import Geweigerd, HerkomstBotsing, Register
from .stand import bevindingen, stand

MD, NDJSON = "BESLUITEN.md", "besluiten.ndjson"
_VOOR = re.compile(r"intern|publiek|klant:[a-z0-9][a-z0-9._-]*")

class Gebruik(Exception): pass  # exit 2

def _veilig(tekst):
    """Tekst van de aanroeper in een melding: met een geheimpatroon erin alleen de patroonnaam (ADR-077 besluit 6)."""
    tekst = str(tekst)
    p = next((p for p in PATTERNS if p.regex.search(tekst)), None)
    return f"<weggelaten: geheimpatroon {p.name}>" if p else tekst

class _Parser(argparse.ArgumentParser):
    def error(self, message):  # argparse herhaalt een onbekende vlag of keuze letterlijk
        super().error(_veilig(message))

def _voor(w):
    if not _VOOR.fullmatch(w):
        raise argparse.ArgumentTypeError("kies intern, publiek of klant:<slug>")
    return w

def _dag(w):
    try:
        return datetime.strptime(w, "%Y-%m-%d").strftime("%Y-%m-%d")
    except ValueError:
        raise argparse.ArgumentTypeError("verwacht JJJJ-MM-DD") from None

def _dag_of_geen(w):
    return None if w == "geen" else _dag(w)

def zichtbaar(voor, vertrouwelijkheid, context):
    """ADR-077 § 5: intern ziet alles, publiek alleen publiek, klant:<slug> ook klant van precies die context."""
    return voor == "intern" or vertrouwelijkheid == "publiek" or (
        vertrouwelijkheid == "klant" and context is not None and context.startswith("klant:") and voor == context)

def _besluiten(regels, nu=None):
    """besluit_id -> vastgelegd (de eerste), geschiedenis en Stand (met `nu`); bestandsvolgorde."""
    uit = {}
    for x in regels:
        if x["event"] == "vastgelegd" and x["besluit_id"] not in uit:
            uit[x["besluit_id"]] = {"v": x, "hist": []}
    for x in regels:
        if x["betreft"] in uit:
            uit[x["betreft"]]["hist"].append(x)
    for bid, b in uit.items():
        b["stand"] = stand(regels, bid, nu)
    return uit

def _tellingen(uit):
    return f"{len(uit.regels)} regels · afgescheurd: {len(uit.afgescheurd)} · ongeldig: {len(uit.ongeldig)}"

def _bron(b):
    return f"{b['soort']} {b['ref']}" + (f" (\"{b['citaat']}\")" if b["citaat"] else "")

def _blok(b, zien):
    v, s = b["v"], b["stand"]
    door = f" door {s.door}" if s.naam == "vervangen" and s.door in zien else ""
    regels = [f"## {v['besluit_id']}: {v['titel']}", "", f"- stand: {s.naam}{door}",
              f"- besloten op {v['besloten_op']} door {v['besloten_door']}",
              f"- reikwijdte: {v['reikwijdte']} · vertrouwelijkheid: {v['vertrouwelijkheid']}",
              f"- herzien op: {s.herzien_op or 'geen'}", f"- bron: {_bron(v['bron'])}"]
    regels += [f"- vervangt: {v.get('vervangt')}"] * (v.get("vervangt") in zien) + [
               f"- terugweg: {v['terugweg']}", f"- links: {', '.join(v['links']) or 'geen'}",
               f"- tags: {', '.join(v['tags']) or 'geen'}", "", v["besluit"], "", f"Waarom: {v['waarom']}"]
    regels += [f"- alternatief {a['optie']}: {a['waarom_niet']}" for a in v["alternatieven"]]
    for x in (x for x in b["hist"] if x["event"] != "vastgelegd"):  # per soort de verplichte velden (schema)
        e, bron = x["event"], _bron(x["bron"]) if "bron" in x else ""
        tekst = {"vervangen": lambda: (f"door {x['door']}, " if x["door"] in zien else "") + x["reden"],
                 "ingetrokken": lambda: f"{x['reden']}, {bron}"}.get(
            e, lambda: f"{x.get('waarom', bron)}, herzien op {x['herzien_op'] or 'geen'}")()
        regels.append(f"- {x['besloten_op']} {e}: {tekst}")
    return regels

def document(uit, voor, van=None, tot=None, reikwijdte=None):
    """Markdown voor publiek `voor`; alleen besluiten die zichtbaar() toelaat, filters op besloten_op en reikwijdte."""
    regels = [r for _, r in uit.regels]
    context = regels[0]["context"] if regels else None
    alle = _besluiten(regels)
    zien = {bid for bid, b in alle.items() if zichtbaar(voor, b["v"]["vertrouwelijkheid"], context)}
    gekozen = [b for bid, b in alle.items() if bid in zien and (not van or b["v"]["besloten_op"][:10] >= van)
               and (not tot or b["v"]["besloten_op"][:10] <= tot) and reikwijdte in (None, b["v"]["reikwijdte"])]
    kop = f"# Besluiten: {context}" if context and voor in ("intern", context) else "# Besluiten"
    telling = _tellingen(uit) if voor == "intern" else f"afgescheurd: {len(uit.afgescheurd)}"
    delen = [kop, "", f"{len(gekozen)} besluiten · {telling}", ""]
    for b in gekozen:
        delen += _blok(b, zien) + [""]
    return "\n".join(delen)

def controle(uit, logdir):
    """(fouten, waarschuwingen) volgens ADR-077 § 3 Controle en § 4 Melding; fouten: soort -> details."""
    regels, fouten = [r for _, r in uit.regels], {}
    def fout(soort, wat):
        fouten.setdefault(soort, []).append(str(wat))
    for nr, _ in uit.ongeldig:
        fout("ongeldige regel", nr)
    ids = [x["besluit_id"] for x in regels if x["event"] == "vastgelegd"]
    bekend = set(ids)
    for bid in sorted({i for i in ids if ids.count(i) > 1}):
        fout("besluit_id op meer dan één vastgelegd", bid)
    for veld, waarde in ((veld, x.get(veld)) for x in regels for veld in ("betreft", "vervangt", "door")):
        if waarde is not None and waarde not in bekend:
            fout(f"onbekend {veld}", waarde)
    opvolg = {x["besluit_id"]: x.get("vervangt") for x in regels if x["event"] == "vastgelegd"}
    for start in sorted(opvolg):
        cur, stappen = opvolg[start], len(opvolg)
        while cur in opvolg and cur != start and (stappen := stappen - 1) > 0:
            cur = opvolg[cur]
        if cur == start:
            fout("cirkel in vervangt", start)
    for soort, wat in bevindingen(regels, bekend):  # de overgangstabel van ADR-077 § 3, in stand.py
        fout(soort, wat)
    md = Path(logdir) / MD
    if not md.is_file() or md.read_bytes() != document(uit, "intern").encode("utf-8"):
        fout("BESLUITEN.md wijkt af van render", "draai render")
    waarschuwingen = [f"afgescheurd stuk op offset {o} ({len(s)} bytes), telt niet" for o, s in uit.afgescheurd]
    kw = Path(logdir) / (NDJSON + ".afgescheurd")
    if (n := kw.read_bytes().count(b"\n") if kw.is_file() else 0):
        waarschuwingen.append(f"{n} vermelding(en) in {kw.name}")
    return fouten, waarschuwingen

def _schrijf_atomair(pad, tekst):
    fd, tmp = tempfile.mkstemp(dir=pad.parent, prefix=f".{pad.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(tekst.encode("utf-8"))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, pad)
    except BaseException:
        os.unlink(tmp)
        raise

def _uit_pad(uit, logdir):
    pad = Path(uit).resolve()  # volgt symlinks en haalt .. weg
    if pad.is_dir() or pad in {(logdir / n).resolve() for n in (NDJSON, NDJSON + ".afgescheurd", MD)}:
        raise Gebruik(f"--uit: weigert {_veilig(uit)}: dat is een bestand van het register zelf of een map")
    return pad

def _kop(a, regels):
    slug = a.slug or (regels[0]["context"] if regels else None)
    if not slug:
        raise Gebruik("--slug ontbreekt: het register heeft nog geen regels")
    return {"context": slug, "besloten_op": a.besloten_op, "vastgelegd_door": a.vastgelegd_door}

def _bronvelden(a):
    return {"soort": a.bron_soort, "ref": a.bron_ref, "citaat": a.citaat}

def _nieuw(a, kop):
    v = dict(kop, event="vastgelegd", titel=a.titel, besluit=a.besluit, waarom=a.waarom,
             alternatieven=[{"optie": o, "waarom_niet": w} for o, w in a.alternatief], besloten_door=a.besloten_door,
             bron=_bronvelden(a), reikwijdte=a.reikwijdte, vertrouwelijkheid=a.vertrouwelijkheid,
             terugweg=a.terugweg, links=a.link, tags=a.tag)
    return v if a.herzien_op is None else dict(v, herzien_op=a.herzien_op)

GEBEURTENIS = {
    "trek-in": lambda a, k: dict(k, event="ingetrokken", betreft=a.id, reden=a.reden, bron=_bronvelden(a)),
    "herzien": lambda a, k: dict(k, event="herzien", betreft=a.id, waarom=a.waarom, herzien_op=a.herzien_op),
    "bevestig": lambda a, k: dict(k, event="bevestigd", betreft=a.id, bron=_bronvelden(a), herzien_op=a.herzien_op)}

def _import_log(a, logdir):
    """add --from-daily-log (ADR-077 § 7): elk besluitpunt via Register.schrijf; gestopt bij de eerste weigering."""
    log = Path(a.from_daily_log)
    if not log.is_file():
        raise Gebruik(f"--from-daily-log: bestand bestaat niet: {_veilig(log)}")
    dag, gezien, punten = lees_log(log)
    if dag is None:
        raise Gebruik(f"{_veilig(log.name)}: geen datum in de frontmatter (date:) of in de bestandsnaam, niets geschreven")
    if not punten:
        print(f"{_veilig(log.name)}: " + ("geen besluitpunten onder Beslissingen" if gezien else "geen sectie Beslissingen")
              + ", 0 besluiten geschreven")
        return 0
    reg, slug = Register(logdir), a.slug or next(iter(r["context"] for _, r in lees(logdir / NDJSON).regels), None)
    if not slug:
        raise Gebruik("--slug ontbreekt: het register heeft nog geen regels")
    ids, geschreven = [], 0
    for nr, (pos, tekst) in enumerate(punten, 1):
        v = {"context": slug, "besloten_op": dag, "vastgelegd_door": a.vastgelegd_door or "skill:daily-log",
             "event": "vastgelegd", "titel": titel(tekst), "besluit": tekst, "alternatieven": [],
             "waarom": f"Overgenomen uit daily-log {log.name}", "besloten_door": a.besloten_door or "operator",
             "bron": {"soort": "daily-log", "ref": f"{log.name}#{pos}", "citaat": ""}, "reikwijdte": "context",
             "vertrouwelijkheid": "intern", "terugweg": "geen: overgenomen uit de daily-log", "links": [], "tags": []}
        try:
            res = reg.schrijf(v)
        except Geweigerd as e:
            # de positie, nooit de tekst; alleen bij een botsing, die de kern pas na de geheimscan meldt
            wie = f" ({v['bron']['ref']})" if isinstance(e, HerkomstBotsing) else ""
            print(f"geweigerd bij punt {nr} van {len(punten)}{wie}: {e}; {geschreven} geschreven vóór dit punt",
                  file=sys.stderr)
            return 3
        ids.append(res.besluit_id)
        geschreven += res.geschreven
    print(*ids, f"{len(punten)} punt(en), {geschreven} geschreven", sep="\n")
    return 0

def _uitvoeren(a):
    logdir = Path(a.context)
    if not logdir.is_dir():
        raise Gebruik(f"--context: map bestaat niet: {_veilig(logdir)}")
    uit = lees(logdir / NDJSON)
    regels = [r for _, r in uit.regels]
    if a.sub == "add" and a.from_daily_log:
        return _import_log(a, logdir)
    if a.sub in ("add", "vervang", *GEBEURTENIS):
        reg, kop = Register(logdir), _kop(a, regels)
        res = (reg.schrijf(_nieuw(a, kop)) if a.sub == "add" else reg.vervang(a.id, _nieuw(a, kop), a.reden)
               if a.sub == "vervang" else reg.schrijf(GEBEURTENIS[a.sub](a, kop)))
        print(f"{res.besluit_id} ({res.geschreven} regel(s) geschreven)")
        return 0
    if a.sub in ("list", "show"):
        nu = a.nu or datetime.now(timezone.utc).strftime("%Y-%m-%d")
        alle = _besluiten(regels, nu)
        if a.sub == "show" and a.id not in alle:
            print(f"onbekend besluit: {_veilig(a.id)}", file=sys.stderr)
            return 1
        print(*([f"{bid}  {b['stand'].naam}  {b['v']['besloten_op']}  {b['v']['titel']}" for bid, b in alle.items()]
                if a.sub == "list" else _blok(alle[a.id], set(alle))), _tellingen(uit), sep="\n")
        return 0
    if a.sub == "render":
        _schrijf_atomair(logdir / MD, document(uit, "intern"))
        print(f"{MD} geschreven · {_tellingen(uit)}")
        return 0
    if a.sub == "check":
        fouten, waarschuwingen = controle(uit, logdir)
        print("\n".join([_tellingen(uit), *(f"waarschuwing: {w}" for w in waarschuwingen),
                         *(f"fout: {soort}: {', '.join(wat)}" for soort, wat in fouten.items()),
                         f"check: {'fout' if fouten else 'ok'}"]))
        return 1 if fouten else 0
    if uit.ongeldig:  # export: een ongeldige regel kan een intrekking zijn (ADR-077 § 4)
        print(f"export geweigerd: ongeldige regel(s) {', '.join(str(n) for n, _ in uit.ongeldig)}", file=sys.stderr)
        return 1
    doel = _uit_pad(a.uit, logdir) if a.uit else None
    tekst = document(uit, a.voor, a.van, a.tot, a.reikwijdte)
    _schrijf_atomair(doel, tekst) if doel else sys.stdout.write(tekst)
    return 0

def _ouders(streng):
    """De ouderparsers; argparse deelt hun Action-objecten met elk subcommando dat ze erft, dus wie een vlag
    niet verplicht wil (add, om --from-daily-log), bouwt een eigen stel met streng=False in plaats van te muteren.
    Geeft (ouders, de dest van elke daardoor niet verplichte vlag): main() eist die zelf zonder --from-daily-log."""
    ouders, los = {}, []
    def ouder(naam, *verplicht, basis=()):
        o = ouders[naam] = argparse.ArgumentParser(add_help=False, parents=[ouders[x] for x in basis])
        for veld in verplicht:
            vast = streng or veld == "context"
            o.add_argument(f"--{veld}", required=vast, help=None if vast else "verplicht zonder --from-daily-log")
            los.extend([] if vast else [veld.replace("-", "_")])
        return o
    ouder("ctx", "context")
    ouder("mut", "besloten-op", "vastgelegd-door", basis=("ctx",)).add_argument("--slug")
    ouder("bron", "bron-soort", "bron-ref").add_argument("--citaat", default="")
    nieuw = ouder("nieuw", "titel", "besluit", "waarom", "besloten-door", "reikwijdte", "vertrouwelijkheid",
                  "terugweg", basis=("mut", "bron"))
    for veld in ("link", "tag"):
        nieuw.add_argument(f"--{veld}", action="append", default=[])
    nieuw.add_argument("--alternatief", nargs=2, action="append", default=[], metavar=("OPTIE", "WAAROM_NIET"))
    nieuw.add_argument("--herzien-op", type=_dag_of_geen)
    return ouders, los

def _parser():
    p = _Parser(prog="besluitenregister", description="Besluitenregister per context (ADR-077).")
    (ouders, _), (eigen, vereist) = _ouders(True), _ouders(False)
    sub = p.add_subparsers(dest="sub", required=True)
    sp = {naam: sub.add_parser(naam, parents=[(eigen if naam == "add" else ouders)[x] for x in basis])
          for naam, basis in {
        "add": ("nieuw",), "vervang": ("nieuw",), "trek-in": ("mut", "bron"), "herzien": ("mut",),
        "bevestig": ("mut", "bron"), "list": ("ctx",), "show": ("ctx",), "check": ("ctx",), "render": ("ctx",),
        "export": ("ctx",)}.items()}
    for naam in ("vervang", "trek-in", "herzien", "bevestig", "show"):
        sp[naam].add_argument("id", metavar="BESLUIT_ID")
    dag, geen, rw = {"type": _dag}, {"type": _dag_of_geen}, {"choices": ("context", "vloot", "klant")}  # "?": optioneel
    for naam, vlag, kw in (("vervang", "reden", {}), ("trek-in", "reden", {}), ("herzien", "waarom", {}),
                           ("herzien", "herzien-op", geen), ("bevestig", "herzien-op", geen), ("list", "?nu", dag),
                           ("export", "voor", {"type": _voor}), ("show", "?nu", dag), ("export", "?uit", {}),
                           ("export", "?van", dag), ("export", "?tot", dag), ("export", "?reikwijdte", rw)):
        sp[naam].add_argument(f"--{vlag.lstrip('?')}", required=not vlag.startswith("?"), **kw)
    sp["add"].set_defaults(vereist=vereist)  # per parser (_defaults); geen Action heeft dest "vereist"
    sp["add"].add_argument("--from-daily-log", metavar="LOG.md",
                           help="importeer elk punt onder Beslissingen uit deze daily-log")
    return p

def main(argv=None):
    try:
        a = _parser().parse_args(argv)
        if a.sub == "add" and not a.from_daily_log and (mist := [d for d in a.vereist if getattr(a, d) is None]):
            raise Gebruik("verplicht: " + ", ".join("--" + d.replace("_", "-") for d in mist))
        return _uitvoeren(a)
    except SystemExit as e:  # argparse: gebruik (2) of --help (0)
        return e.code if isinstance(e.code, int) else 2
    except Gebruik as e:
        print(f"gebruik: {e}", file=sys.stderr)
        return 2
    except Geweigerd as e:
        print(f"geweigerd: {e}", file=sys.stderr)
        return 3
