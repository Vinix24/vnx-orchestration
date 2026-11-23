"""besluitenregister B1b: ADR-077 § 3 (overgangstabel, vier standen) vastgepind via Register en cli.main."""
import sys
from datetime import datetime, timezone
from functools import partial
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "packages" / "besluitenregister"))
from besluitenregister import cli, identiteit
from besluitenregister.lezer import lees
from besluitenregister.schrijver import Geweigerd, Register

klok = partial(datetime, 2026, 11, 14, 9, 0, 0, tzinfo=timezone.utc)
KOP, BRON = {"context": "vnx-orchestration", "vastgelegd_door": "t0"}, {"soort": "adr", "ref": "ADR-077", "citaat": ""}
ONBEKEND = "B-20261114-deadbeef"

def besluit(**kw):
    return {"event": "vastgelegd", **KOP, "besloten_op": "2026-11-14", "titel": "Kies A", "besluit": "We kiezen A.",
            "waarom": "Sneller.", "alternatieven": [], "besloten_door": "T0", "reikwijdte": "context", "bron": BRON,
            "vertrouwelijkheid": "intern", "terugweg": "geen: nvt", "links": [], "tags": [], **kw}

def ev(event, bid, **kw):
    return {"event": event, "betreft": bid, **KOP, "besloten_op": "2026-11-15", **kw}

# per gebeurtenis een regel die het schema haalt; vastgelegd-vervangt is een nieuw besluit dat `a` vervangt
LIJST = {"herzien": lambda a, z: ev("herzien", a, waarom="gekeken", herzien_op=None),
         "bevestigd": lambda a, z: ev("bevestigd", a, bron=BRON, herzien_op=None),
         "ingetrokken": lambda a, z: ev("ingetrokken", a, reden="nogmaals", bron=BRON),
         "vervangen": lambda a, z: ev("vervangen", a, door=z, reden="r"),
         "vastgelegd met vervangt": lambda a, z: besluit(titel="Nieuw", vervangt=a)}
# per stand de opzet op besluit a (z is een ander, los besluit); "geen" gebruikt een id dat niet bestaat
OPZET = {"geen": lambda r, a, z: None, "open": lambda r, a, z: None,
         "vervangen open": lambda r, a, z: r.schrijf(besluit(titel="Opvolger", vervangt=a)),
         "vervangen dicht": lambda r, a, z: r.vervang(a, besluit(titel="Opvolger"), "beter"),
         "ingetrokken": lambda r, a, z: r.schrijf(ev("ingetrokken", a, reden="weg", bron=BRON))}
TOEGESTAAN = {("open", "herzien"), ("open", "bevestigd"), ("open", "ingetrokken"), ("open", "vastgelegd met vervangt"),
              ("vervangen open", "vervangen")}
CEL = [(s, e) for s in OPZET for e in LIJST]
NA = {"open": "geldig", "vervangen open": "vervangen", "vervangen dicht": "vervangen", "ingetrokken": "ingetrokken"}

@pytest.fixture
def reg(tmp_path):
    return Register(tmp_path, klok=klok)

def opzet(reg, stand):
    a, z = reg.schrijf(besluit()).besluit_id, reg.schrijf(besluit(titel="Los")).besluit_id
    OPZET[stand](reg, a, z)
    return (ONBEKEND if stand == "geen" else a), z

def run(capsys, *argv):
    code = cli.main([str(x) for x in argv])
    uit = capsys.readouterr()
    return code, uit.out, uit.err

def plak(ctx, **velden):  # een schemageldige regel buiten de schrijver om, zoals de check-tests van B2
    r = {"schema": "besluit/1", **KOP, "besloten_op": "2026-11-15", "vastgelegd_op": "2026-11-15T09:00:00Z", **velden}
    with open(ctx / "besluiten.ndjson", "a", encoding="utf-8") as f:
        f.write(identiteit.canoniek(dict(r, event_id=identiteit.event_id(r))) + "\n")

def plak_regel(ctx, regel, bid=None):
    if regel["event"] == "vastgelegd":
        bid = bid or "B-20261114-0000000a"
        h = identiteit.herkomst(regel["context"], "adr", "ADR-077", regel["titel"])
        regel = dict(regel, besluit_id=bid, betreft=bid, herkomst=h)
    plak(ctx, **regel)

def fouten(capsys, ctx):
    return [x for x in run(capsys, "check", "--context", ctx)[1].splitlines() if x.startswith("fout: ")]

def stand_van(capsys, ctx, bid, nu=None):
    uit = run(capsys, "list", "--context", ctx, *(["--nu", nu] if nu else []))[1]
    return next(x.split()[1] for x in uit.splitlines() if x.startswith(bid))

@pytest.mark.parametrize("stand,e", [c for c in CEL if c in TOEGESTAAN])
def test_toegestane_overgang_wordt_geschreven(reg, capsys, stand, e):
    a, z = opzet(reg, stand)
    if e == "vervangen":  # de enige toegestane: met `door` gelijk aan de opvolger
        z = next(r for _, r in lees(reg.pad).regels if r.get("vervangt") == a)["besluit_id"]
    assert reg.schrijf(LIJST[e](a, z)).geschreven == 1
    assert stand_van(capsys, reg.pad.parent, a) == {"herzien": "geldig", "bevestigd": "geldig",
                                                    "ingetrokken": "ingetrokken"}.get(e, "vervangen")

@pytest.mark.parametrize("stand,e", [c for c in CEL if c not in TOEGESTAAN])
def test_geweigerde_overgang_in_schrijver_en_check(reg, capsys, stand, e):
    a, z = opzet(reg, stand)
    voor = reg.pad.read_bytes()
    with pytest.raises(Geweigerd) as fout:
        reg.schrijf(LIJST[e](a, z))
    assert str(fout.value) == f"overgang: {e} in stand {stand}" and reg.pad.read_bytes() == voor
    if stand != "geen":  # check meldt dezelfde regel als hij er toch staat
        plak_regel(reg.pad.parent, LIJST[e](a, z))
        soort = "vervangen zonder nieuwe vastgelegd" if (stand, e) == ("open", "vervangen") else \
            "gebeurtenis na een eindstand"
        assert any(x.startswith(f"fout: {soort}") for x in fouten(capsys, reg.pad.parent))

def test_vervangen_met_andere_door_geweigerd_in_open_vervanging(reg):
    a, z = opzet(reg, "vervangen open")
    with pytest.raises(Geweigerd, match="overgang: vervangen in stand vervangen open"):
        reg.schrijf(ev("vervangen", a, door=z, reden="r"))

@pytest.mark.parametrize("herzien_op", ["2026-11-20", "2026-11-14"])
def test_te_herzien_vanaf_de_dag_zelf_en_nu_geeft_het_oordeel(reg, capsys, herzien_op):
    ctx = reg.pad.parent
    a = reg.schrijf(besluit(herzien_op=herzien_op)).besluit_id
    assert stand_van(capsys, ctx, a, "2026-11-13") == "geldig"
    assert stand_van(capsys, ctx, a, herzien_op) == "te_herzien" == stand_van(capsys, ctx, a, "2026-12-31")
    assert stand_van(capsys, ctx, a, "2026-11-13") == "geldig"
    assert "stand: geldig" in run(capsys, "show", a, "--context", ctx, "--nu", "2026-11-13")[1]
    run(capsys, "render", "--context", ctx)  # render kent geen nu: nooit te_herzien
    assert "stand: geldig" in (ctx / "BESLUITEN.md").read_text()

def test_herzien_en_bevestigd_zetten_de_geldende_datum(reg, capsys):
    ctx = reg.pad.parent
    a = reg.schrijf(besluit(herzien_op="2026-11-20")).besluit_id
    reg.schrijf(ev("herzien", a, waarom="gekeken", herzien_op="2026-12-24"))
    assert stand_van(capsys, ctx, a, "2026-12-01") == "geldig" and stand_van(capsys, ctx, a, "2026-12-24") == "te_herzien"
    reg.schrijf(ev("bevestigd", a, bron=BRON, herzien_op=None))
    assert stand_van(capsys, ctx, a, "2030-01-01") == "geldig"

def test_vervangen_toont_door_en_ingetrokken_niet(reg, capsys):
    ctx, (a, z) = reg.pad.parent, opzet(reg, "vervangen dicht")
    nieuw = next(r for _, r in lees(reg.pad).regels if r.get("vervangt") == a)["besluit_id"]
    assert f"- stand: vervangen door {nieuw}" in run(capsys, "show", a, "--context", ctx)[1]
    assert stand_van(capsys, ctx, a) == "vervangen"
    reg.schrijf(ev("ingetrokken", z, reden="weg", bron=BRON))
    assert "- stand: ingetrokken\n" in run(capsys, "show", z, "--context", ctx)[1]

def test_open_vervanging_door_check_gemeld_en_door_vervang_hersteld(reg, capsys):
    ctx, a = reg.pad.parent, reg.schrijf(besluit()).besluit_id
    nieuw = reg.schrijf(besluit(titel="B", vervangt=a)).besluit_id  # afsluitende regel mist
    run(capsys, "render", "--context", ctx)
    assert fouten(capsys, ctx) == [f"fout: open vervanging (draai vervang opnieuw): {a} door {nieuw}"]
    assert reg.vervang(a, besluit(titel="B"), "beter").geschreven == 1
    run(capsys, "render", "--context", ctx)
    assert run(capsys, "check", "--context", ctx)[:2] == (0, run(capsys, "check", "--context", ctx)[1])

def test_een_besluit_wordt_maar_een_keer_vervangen(reg):
    a = reg.schrijf(besluit()).besluit_id
    reg.vervang(a, besluit(titel="B"), "beter")
    for poging in (lambda: reg.vervang(a, besluit(titel="C"), "nog beter"), lambda: reg.schrijf(besluit(titel="D", vervangt=a))):
        with pytest.raises(Geweigerd, match="overgang: vastgelegd met vervangt in stand vervangen dicht"):
            poging()
    assert sum(1 for _, r in lees(reg.pad).regels if r["event"] == "vervangen") == 1

def test_vervangt_naar_onbekend_of_later_id_geweigerd_en_cirkel_gemeld(reg, capsys):
    ctx = reg.pad.parent
    with pytest.raises(Geweigerd, match="overgang: vastgelegd met vervangt in stand geen"):
        reg.schrijf(besluit(vervangt=ONBEKEND))
    with pytest.raises(Geweigerd, match="overgang: vastgelegd met vervangt in stand geen"):  # zichzelf bestaat nog niet
        reg.schrijf(besluit(besloten_op="2026-11-14", vervangt="B-20261114-00000001"))
    assert not reg.pad.exists() or reg.pad.read_bytes() == b""
    plak_regel(ctx, besluit(titel="X", vervangt="B-20261114-0000000b"), "B-20261114-0000000a")
    plak_regel(ctx, besluit(titel="Y", vervangt="B-20261114-0000000a"), "B-20261114-0000000b")
    soorten = [x.split(":")[1].strip() for x in fouten(capsys, ctx)]
    assert "cirkel in vervangt" in soorten and "gebeurtenis vóór de vastlegging" in soorten

def test_stand_module_nu_als_date_tekst_of_geen():  # direct op de module van B1b
    from datetime import date
    from besluitenregister.stand import bevindingen, mag, stand
    v = {"event": "vastgelegd", "betreft": "A", "besluit_id": "A", "herzien_op": "2026-11-20"}
    assert [stand([v], "A", nu).naam for nu in (None, "", "2026-11-19", date(2026, 11, 19))] == ["geldig"] * 4
    assert [stand([v], "A", nu).naam for nu in ("2026-11-20", date(2026, 11, 20), "2026-11-20T01:00:00Z")] == ["te_herzien"] * 3
    assert stand([v], "Z").naam == "geen" and mag({"event": "herzien", "betreft": "Z"}, [v]) == ("herzien", "geen")
    assert mag({"event": "vastgelegd", "vervangt": None}, [v]) is None and mag({"event": 7, "betreft": "A"}, [v]) == (7, "open")
    assert bevindingen([v, dict(v, event="herzien", betreft="Z")], {"A"}) == []
