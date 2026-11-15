"""besluitenregister B1: schema, ids, lezer, schrijver met staartherstel, ontdubbeling, geheim- en overgangsweigering."""
import base64, hashlib, json, sys
from datetime import datetime, timezone
from functools import partial
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "packages" / "besluitenregister"))
from besluitenregister import identiteit
from besluitenregister.geheimpatronen import PATTERNS
from besluitenregister.lezer import lees
from besluitenregister.schema import valideer
from besluitenregister.schrijver import GeheimGeweigerd, Geweigerd, HerkomstBotsing, Register

klok = partial(datetime, 2026, 11, 14, 9, 0, 0, tzinfo=timezone.utc)  # aanroepen geeft de vaste tijd
BRON, KOP = {"soort": "chat", "ref": "x", "citaat": "ja"}, {"context": "vnx-orchestration", "vastgelegd_door": "t0"}

def besluit(**kw):
    return {"event": "vastgelegd", **KOP, "besloten_op": "2026-11-14", "titel": "Kies A", "besluit": "We kiezen A.",
            "waarom": "Sneller.", "alternatieven": [], "besloten_door": "T0", "reikwijdte": "context",
            "bron": {"soort": "adr", "ref": "ADR-077", "citaat": ""}, "vertrouwelijkheid": "intern",
            "terugweg": "geen: nvt", "links": [], "tags": [], **kw}

def ev(event, bid, op="2026-11-15", **kw):  # een gebeurtenis op een bestaand besluit
    return {"event": event, "betreft": bid, **KOP, "besloten_op": op, "vastgelegd_door": "operator", **kw}

NIEUW, bevestig = besluit(titel="Kies B", besluit="We kiezen B."), partial(ev, "bevestigd", bron=BRON, herzien_op=None)

def regels(reg):
    return [r for _, r in lees(reg.pad).regels]
@pytest.fixture
def reg(tmp_path):
    return Register(tmp_path, klok=klok)

def test_hash_gepind():
    r = {"a": 1, "b": "é", "event_id": "x", "vastgelegd_op": "y"}
    assert identiteit.event_id(r) == "E-09ad9fd2fb648cb2f62141215828ea00a62c299db05d20aa9ade2f527a301cc6"
    assert identiteit.herkomst("c", "adr", "ADR-1", "t") == hashlib.sha256(b'["c","adr","ADR-1","t"]').hexdigest()

def test_vastgelegd_krijgt_ids_en_is_geldig(reg):
    uit, r = reg.schrijf(besluit()), regels(reg)[0]
    assert uit == (r["besluit_id"], 1) and r["betreft"] == r["besluit_id"] and valideer(r) == []
    assert r["besluit_id"].startswith("B-20261114-") and r["event_id"] == identiteit.event_id(r)

@pytest.mark.parametrize("wijzig,deel", [
    ({"titel": "x" * 101}, "titel"), ({"reikwijdte": "wereld"}, "reikwijdte"), ({"extra": 1}, "extra"),
    ({"besloten_op": "2026-13-01"}, "besloten_op"), ({"herzien_op": "2026-11-13"}, "herzien_op"),
    ({"vertrouwelijkheid": "klant"}, "klant"), ({"besloten_door": "operator", "bron": {"soort": "chat", "ref": "r", "citaat": " "}}, "citaat")])
def test_schrijver_weigert_schemaschending(reg, wijzig, deel):
    with pytest.raises(Geweigerd, match=deel):
        reg.schrijf(besluit(**wijzig))
    assert not reg.pad.exists()
    assert "betreft: ontbreekt" in valideer({"event": "vervangen"}) and valideer("x") and valideer({"event": "raar"})

def test_dubbele_import_een_besluit_en_afwijkende_tekst_geweigerd(reg):
    bid = reg.schrijf(besluit()).besluit_id
    assert reg.schrijf(besluit()) == (bid, 0)
    with pytest.raises(HerkomstBotsing, match=bid):
        reg.schrijf(besluit(besluit="Iets anders."))
    with pytest.raises(Geweigerd, match="context"):
        reg.schrijf(besluit(context="klant:a", titel="Andere"))
    assert len(regels(reg)) == 1  # twee bevestigingen twee regels, dezelfde twee keer één
    assert [reg.schrijf(bevestig(bid, d)).geschreven for d in ("2026-11-15", "2026-11-16", "2026-11-16")] == [1, 1, 0]
    assert len(regels(reg)) == 3

def test_lezer_staart_ongeldig_en_nooit_schrijven(reg):
    reg.schrijf(besluit())
    geschreven = reg.pad.read_bytes() + b"niet json\n" + b'{"schema":"besluit/1"}\n' + json.dumps(regels(reg)[0]).encode()
    reg.pad.write_bytes(geschreven)
    uit = lees(reg.pad)
    assert [nr for nr, _ in uit.ongeldig] == [2, 3] and len(uit.regels) == 1
    assert uit.afgescheurd == [(len(geschreven) - len(uit.afgescheurd[0][1]), uit.afgescheurd[0][1])]
    assert json.loads(uit.afgescheurd[0][1])["event"] == "vastgelegd"  # geldige JSON zonder \n telt niet
    assert reg.pad.read_bytes() == geschreven  # de lezer schrijft niets
    assert lees(reg.pad.parent / "bestaat-niet") == ([], [], [])

def test_staart_naar_quarantaine_inkorten_en_crash_ertussen(reg):
    reg.schrijf(besluit())
    goed, staart, kw = reg.pad.read_bytes(), b'{"half":', Path(str(reg.pad) + ".afgescheurd")
    vermelding = {"offset": len(goed), "sha256": hashlib.sha256(staart).hexdigest(), "bytes": base64.b64encode(staart).decode()}
    reg.pad.write_bytes(goed + staart)
    reg.schrijf(besluit(titel="Twee"))  # quarantaine, inkorten, dan de nieuwe regel
    assert reg.pad.read_bytes().startswith(goed) and len(regels(reg)) == 2 and not lees(reg.pad).afgescheurd
    assert [json.loads(x) for x in kw.read_text().splitlines()] == [vermelding]
    # crash na quarantaine, vóór inkorten: de vermelding staat er al (met een eigen afgescheurd stuk erachter)
    kw.write_text(json.dumps(vermelding) + "\n" + '{"eigen":')
    reg.pad.write_bytes(goed + staart)
    reg.schrijf(besluit(titel="Drie"))
    assert kw.read_text() == json.dumps(vermelding) + "\n" and len(regels(reg)) == 2

def test_vervang_en_herstel_na_afgescheurde_afsluitende_regel(reg):
    oud = reg.schrijf(besluit()).besluit_id
    with pytest.raises(Geweigerd, match="zichzelf"):  # dezelfde velden: ontdubbeling geeft oud terug
        reg.vervang(oud, besluit(), "zelfde")
    assert [x["event"] for x in regels(reg)] == ["vastgelegd"]
    uit, r = reg.vervang(oud, NIEUW, "B is beter"), regels(reg)
    assert uit.geschreven == 2 and [x["event"] for x in regels(reg)] == ["vastgelegd", "vastgelegd", "vervangen"]
    assert r[1]["vervangt"] == oud and r[2]["betreft"] == oud and r[2]["door"] == uit.besluit_id
    assert reg.vervang(oud, NIEUW, "B is beter") == (uit.besluit_id, 0)
    data = reg.pad.read_bytes()
    reg.pad.write_bytes(data[: len(data) - len(data.rsplit(b"\n", 2)[1]) // 2 - 1])  # afsluitende regel afgescheurd
    assert reg.vervang(oud, NIEUW, "B is beter") == (uit.besluit_id, 1)
    assert [x["event"] for x in regels(reg)] == ["vastgelegd", "vastgelegd", "vervangen"]
    assert Path(str(reg.pad) + ".afgescheurd").read_text().count("\n") == 1

@pytest.mark.parametrize("naam,voorbeeld", [
    ("secret_assignment", "API_KEY=abc"), ("sk_key", "sk-test0123456789abcdef"), ("github_token", "ghp_" + "a1" * 12),
    ("aws_key_id", "AKIA" + "A1" * 8), ("slack_token", "xoxb-0123456789ab"), ("bearer", "Bearer abcdefgh1234"),
    ("authorization_header", "Authorization: nepwaarde"), ("pem_block", "-----BEGIN PRIVATE KEY-----\nnep"),
    ("jwt", "eyJabc.def.ghi"), ("long_value", "x=" + "a" * 40)])
def test_geheim_per_patroon_geweigerd_zonder_waarde(reg, naam, voorbeeld):
    assert naam in [p.name for p in PATTERNS if p.regex.search(voorbeeld)]
    with pytest.raises(GeheimGeweigerd) as e:
        reg.schrijf(besluit(bron={"soort": "chat", "ref": "r", "citaat": voorbeeld}, besloten_door="T0"))
    assert "bron.citaat" in str(e.value) and "geheimpatroon" in str(e.value)
    assert voorbeeld not in str(e.value) and not reg.pad.exists()

POGING = {  # (reg, a, z): a = het betreffende besluit, z = een los ander besluit
    "herzien": lambda r, a, z: r.schrijf(ev("herzien", a, waarom="gekeken", herzien_op=None)),
    "bevestigd": lambda r, a, z: r.schrijf(bevestig(a)),
    "ingetrokken": lambda r, a, z: r.schrijf(ev("ingetrokken", a, reden="nogmaals", bron=BRON)),
    "vervangen-door-z": lambda r, a, z: r.schrijf(ev("vervangen", a, door=z, reden="r")),
    "vastgelegd-vervangt": lambda r, a, z: r.schrijf(besluit(titel="X", vervangt=a)),
    "tweede-vervangen": lambda r, a, z: r.vervang(a, NIEUW, "andere reden")}  # na dicht: de tweede regel van vervang
OPZET = {"dicht": lambda r, a: r.vervang(a, NIEUW, "B is beter"),  # open en geen: alleen de twee vastgelegd
         "ingetrokken": lambda r, a: r.schrijf(ev("ingetrokken", a, reden="weg", bron=BRON)),  # codex: + bevestigd
         "half": lambda r, a: r.schrijf(besluit(titel="H", vervangt=a))}  # vervangen open, afsluitende regel mist

@pytest.mark.parametrize("opzet,poging", [(o, p) for o in ("ingetrokken", "dicht", "half") for p in POGING] + [
    ("open", "vervangen-door-z"), ("geen", "bevestigd"), ("geen", "vastgelegd-vervangt")])
def test_overgang_na_eindstand_of_zonder_besluit_geweigerd(reg, opzet, poging):
    a, z = reg.schrijf(besluit()).besluit_id, reg.schrijf(besluit(titel="Z")).besluit_id
    OPZET.get(opzet, lambda r, a: None)(reg, a)
    voor = reg.pad.read_bytes()
    with pytest.raises(Geweigerd, match="overgang"):
        POGING[poging](reg, "B-20261114-deadbeef" if opzet == "geen" else a, z)
    assert reg.pad.read_bytes() == voor

TOKEN = "ghp_" + "A" * 36  # pas tijdens de test opgebouwd: het testbestand draagt geen letterlijk geheim

@pytest.mark.parametrize("velden", [  # elk pad dat de scan eerder pas na een andere weigering bereikte
    lambda: besluit(besluit=f"{TOKEN} gebruiken."),  # herkomst bestaat al met andere tekst
    lambda: ev(TOKEN, "B-20261114-deadbeef"),  # overgang noemde het event
    lambda: besluit(**{TOKEN: "x"}),  # onbekend veld noemde de sleutel
    lambda: besluit(bron={"soort": "adr", "ref": "ADR-077", "citaat": "", TOKEN: "x"}),  # geneste sleutel
    lambda: besluit(vervangt=TOKEN), lambda: besluit(context=TOKEN.lower(), titel="Ander")],
    ids=["herkomst", "overgang", "sleutel", "geneste-sleutel", "vervangt", "context"])
def test_geheim_gaat_voor_elke_andere_weigering(reg, velden):
    reg.schrijf(besluit())
    voor = reg.pad.read_bytes()
    with pytest.raises(GeheimGeweigerd, match="geheimpatroon github_token") as e:
        reg.schrijf(velden())
    assert TOKEN not in str(e.value) and TOKEN.lower() not in str(e.value) and reg.pad.read_bytes() == voor
