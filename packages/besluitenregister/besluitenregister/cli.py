"""CLI van het besluitenregister (ADR-077): ``python3 -m besluitenregister <sub> --context <LOGDIR> ...``.

Subcommando's: add, list, show, vervang, trek-in, herzien, bevestig, check, render, export. Alleen de standaardbibliotheek;
schrijven gaat via ``schrijver.Register``, lezen via ``lezer.lees``. ``--context`` is een bestaande map; ``--slug`` is de
contextslug (``klant:renewance``) en volgt uit de eerste regel als het register al regels heeft.

Exitcodes:
  0  ok
  1  check of lezen niet schoon: check met een fout, export met een ongeldige regel, show van een onbekend id
  2  gebruik: onbekende of ontbrekende vlag, export zonder geldig --voor, een map die niet bestaat, geen slug
  3  geweigerd door de kern: geheim (de patroonnaam, nooit de waarde), herkomstbotsing, overgang, schema, combinatie

``render`` schrijft BESLUITEN.md altijd voor publiek intern en hangt niet af van de dag: de stand ``te_herzien`` tonen
alleen ``list`` en ``show`` (met ``--nu``); BESLUITEN.md toont de geldende ``herzien_op``.
"""
import argparse, os, re, sys, tempfile
from datetime import datetime, timezone
from pathlib import Path

from .lezer import lees
from .schrijver import Geweigerd, Register, stand

MD, NDJSON = "BESLUITEN.md", "besluiten.ndjson"
_VOOR = re.compile(r"intern|publiek|klant:[a-z0-9][a-z0-9._-]*")

class Gebruik(Exception):
    """Exit 2."""

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

def _besluiten(regels):
    """besluit_id -> vastgelegd (de eerste), geschiedenis, stand, door en de geldende herzien_op, in bestandsvolgorde."""
    uit = {}
    for x in regels:
        if x["event"] == "vastgelegd" and x["besluit_id"] not in uit:
            uit[x["besluit_id"]] = {"v": x, "hist": []}
    for x in regels:
        if x["betreft"] in uit:
            uit[x["betreft"]]["hist"].append(x)
    for bid, b in uit.items():
        b["stand"], b["door"] = stand(regels, bid)
        b["herzien_op"] = next((x["herzien_op"] for x in reversed(b["hist"]) if "herzien_op" in x), None)
    return uit

def _stand(b, nu=None):
    if b["stand"] == "open":
        return "te_herzien" if nu and b["herzien_op"] and nu >= b["herzien_op"] else "geldig"
    return "ingetrokken" if b["stand"] == "ingetrokken" else "vervangen"

def _tellingen(uit):
    return f"{len(uit.regels)} regels · afgescheurd: {len(uit.afgescheurd)} · ongeldig: {len(uit.ongeldig)}"

def _bron(b):
    return f"{b['soort']} {b['ref']}" + (f" (\"{b['citaat']}\")" if b["citaat"] else "")

def _blok(b, zien, nu=None):
    v, s = b["v"], _stand(b, nu)
    door = f" door {b['door']}" if s == "vervangen" and b["door"] in zien else ""
    regels = [f"## {v['besluit_id']}: {v['titel']}", "", f"- stand: {s}{door}",
              f"- besloten op {v['besloten_op']} door {v['besloten_door']}",
              f"- reikwijdte: {v['reikwijdte']} · vertrouwelijkheid: {v['vertrouwelijkheid']}",
              f"- herzien op: {b['herzien_op'] or 'geen'}", f"- bron: {_bron(v['bron'])}"]
    if v.get("vervangt") in zien:
        regels.append(f"- vervangt: {v['vervangt']}")
    regels += [f"- terugweg: {v['terugweg']}", f"- links: {', '.join(v['links']) or 'geen'}",
               f"- tags: {', '.join(v['tags']) or 'geen'}", "", v["besluit"], "", f"Waarom: {v['waarom']}"]
    regels += [f"- alternatief {a['optie']}: {a['waarom_niet']}" for a in v["alternatieven"]]
    for x in b["hist"]:
        e = x["event"]
        if e == "vastgelegd":
            continue
        if e == "vervangen":
            tekst = (f"door {x['door']}, " if x["door"] in zien else "") + x["reden"]
        elif e == "ingetrokken":
            tekst = f"{x['reden']}, {_bron(x['bron'])}"
        else:
            tekst = (x["waarom"] if e == "herzien" else _bron(x["bron"])) + f", herzien op {x['herzien_op'] or 'geen'}"
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
    for x in regels:
        for veld in ("betreft", "vervangt", "door"):
            if x.get(veld) is not None and x[veld] not in bekend:
                fout(f"onbekend {veld}", x[veld])
    opvolg = {x["besluit_id"]: x.get("vervangt") for x in regels if x["event"] == "vastgelegd"}
    for start in sorted(opvolg):
        gezien, cur = set(), opvolg[start]
        while cur in opvolg and cur not in gezien and cur != start:
            gezien.add(cur)
            cur = opvolg[cur]
        if cur == start:
            fout("cirkel in vervangt", start)
    _levenscyclus(regels, bekend, fout)
    md = Path(logdir) / MD
    if not md.is_file() or md.read_bytes() != document(uit, "intern").encode("utf-8"):
        fout("BESLUITEN.md wijkt af van render", "draai render")
    waarschuwingen = [f"afgescheurd stuk op offset {o} ({len(s)} bytes), telt niet" for o, s in uit.afgescheurd]
    kw = Path(logdir) / (NDJSON + ".afgescheurd")
    if (n := kw.read_bytes().count(b"\n") if kw.is_file() else 0):
        waarschuwingen.append(f"{n} vermelding(en) in {kw.name}")
    return fouten, waarschuwingen

def _levenscyclus(regels, bekend, fout):
    """Overgangstabel van ADR-077 § 3 in bestandsvolgorde; meldt elke regel die de schrijver zou weigeren."""
    s, door = {}, {}
    for x in regels:
        e, b = x["event"], x["betreft"]
        if e == "vastgelegd":
            if b in s:
                continue  # dubbel id: al gemeld
            s[b], v = "open", x.get("vervangt")
            if v is None or v not in bekend:
                continue  # onbekend vervangt: al gemeld
            if v not in s:
                fout("gebeurtenis vóór de vastlegging", f"{b} vervangt {v}")
            elif s[v] == "open":
                s[v], door[v] = "vervangen open", b
            else:
                fout("gebeurtenis na een eindstand", f"{b} vervangt {v}")
        elif b not in s:
            if b in bekend:
                fout("gebeurtenis vóór de vastlegging", f"{e} op {b}")
        elif e == "vervangen" and s[b] == "vervangen open" and x["door"] == door[b]:
            s[b] = "vervangen dicht"
        elif s[b] != "open":
            fout("gebeurtenis na een eindstand", f"{e} op {b}")
        elif e == "vervangen":
            fout("vervangen zonder nieuwe vastgelegd", b)
        elif e == "ingetrokken":
            s[b] = "ingetrokken"
    for b, st in s.items():
        if st == "vervangen open":
            fout("open vervanging (draai vervang opnieuw)", f"{b} door {door[b]}")

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

def _uitvoeren(a):
    logdir = Path(a.context)
    if not logdir.is_dir():
        raise Gebruik(f"--context: map bestaat niet: {logdir}")
    uit = lees(logdir / NDJSON)
    regels = [r for _, r in uit.regels]
    if a.sub in ("add", "vervang", *GEBEURTENIS):
        reg, kop = Register(logdir), _kop(a, regels)
        res = (reg.schrijf(_nieuw(a, kop)) if a.sub == "add" else reg.vervang(a.id, _nieuw(a, kop), a.reden)
               if a.sub == "vervang" else reg.schrijf(GEBEURTENIS[a.sub](a, kop)))
        print(f"{res.besluit_id} ({res.geschreven} regel(s) geschreven)")
        return 0
    if a.sub in ("list", "show"):
        alle, nu = _besluiten(regels), a.nu or datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if a.sub == "show" and a.id not in alle:
            print(f"onbekend besluit: {a.id}", file=sys.stderr)
            return 1
        for bid, b in alle.items():
            if a.sub == "list":
                print(f"{bid}  {_stand(b, nu)}  {b['v']['besloten_op']}  {b['v']['titel']}")
            elif bid == a.id:
                print("\n".join(_blok(b, set(alle), nu)))
        print(_tellingen(uit))
        return 0
    if a.sub == "render":
        _schrijf_atomair(logdir / MD, document(uit, "intern"))
        print(f"{MD} geschreven · {_tellingen(uit)}")
        return 0
    if a.sub == "check":
        fouten, waarschuwingen = controle(uit, logdir)
        print(_tellingen(uit))
        for w in waarschuwingen:
            print(f"waarschuwing: {w}")
        for soort, wat in fouten.items():
            print(f"fout: {soort}: {', '.join(wat)}")
        print("check: fout" if fouten else "check: ok")
        return 1 if fouten else 0
    if uit.ongeldig:  # export: een ongeldige regel kan een intrekking zijn (ADR-077 § 4)
        print(f"export geweigerd: ongeldige regel(s) {', '.join(str(n) for n, _ in uit.ongeldig)}", file=sys.stderr)
        return 1
    tekst = document(uit, a.voor, a.van, a.tot, a.reikwijdte)
    if a.uit:
        _schrijf_atomair(Path(a.uit), tekst)
    else:
        sys.stdout.write(tekst)
    return 0

def _parser():
    p = argparse.ArgumentParser(prog="besluitenregister", description="Besluitenregister per context (ADR-077).")
    ctx = argparse.ArgumentParser(add_help=False)
    ctx.add_argument("--context", required=True, help="de contextmap <LOGDIR>, moet bestaan")
    mut = argparse.ArgumentParser(add_help=False, parents=[ctx])
    mut.add_argument("--slug", help="contextslug; standaard die van de eerste regel")
    mut.add_argument("--besloten-op", required=True)
    mut.add_argument("--vastgelegd-door", required=True, help="operator, t0, skill:<naam> of agent:<naam>")
    bron = argparse.ArgumentParser(add_help=False)
    bron.add_argument("--bron-soort", required=True)
    bron.add_argument("--bron-ref", required=True)
    bron.add_argument("--citaat", default="")
    nieuw = argparse.ArgumentParser(add_help=False, parents=[mut, bron])
    for veld in ("titel", "besluit", "waarom", "besloten-door", "reikwijdte", "vertrouwelijkheid", "terugweg"):
        nieuw.add_argument(f"--{veld}", required=True)
    nieuw.add_argument("--alternatief", nargs=2, action="append", default=[], metavar=("OPTIE", "WAAROM_NIET"))
    nieuw.add_argument("--link", action="append", default=[])
    nieuw.add_argument("--tag", action="append", default=[])
    nieuw.add_argument("--herzien-op", type=_dag_of_geen)
    sub = p.add_subparsers(dest="sub", required=True)
    sub.add_parser("add", parents=[nieuw])
    sub.add_parser("vervang", parents=[nieuw]).add_argument("--reden", required=True)
    sub.add_parser("trek-in", parents=[mut, bron]).add_argument("--reden", required=True)
    sub.add_parser("herzien", parents=[mut]).add_argument("--waarom", required=True)
    sub.add_parser("bevestig", parents=[mut, bron])
    for naam in ("list", "show"):
        sub.add_parser(naam, parents=[ctx]).add_argument("--nu", type=_dag, help="UTC-datum, standaard vandaag")
    sub.add_parser("check", parents=[ctx])
    sub.add_parser("render", parents=[ctx])
    exp = sub.add_parser("export", parents=[ctx])
    exp.add_argument("--voor", required=True, type=_voor, help="intern, publiek of klant:<slug>")
    exp.add_argument("--uit")
    exp.add_argument("--van", type=_dag)
    exp.add_argument("--tot", type=_dag)
    exp.add_argument("--reikwijdte", choices=("context", "vloot", "klant"))
    for naam in ("vervang", "trek-in", "herzien", "bevestig", "show"):
        sub.choices[naam].add_argument("id", metavar="BESLUIT_ID")
    for naam in ("herzien", "bevestig"):
        sub.choices[naam].add_argument("--herzien-op", required=True, type=_dag_of_geen, help="JJJJ-MM-DD of geen")
    return p

def main(argv=None):
    try:
        return _uitvoeren(_parser().parse_args(argv))
    except SystemExit as e:  # argparse: gebruik (2) of --help (0)
        return e.code if isinstance(e.code, int) else 2
    except Gebruik as e:
        print(f"gebruik: {e}", file=sys.stderr)
        return 2
    except Geweigerd as e:
        print(f"geweigerd: {e}", file=sys.stderr)
        return 3
