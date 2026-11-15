"""besluitenregister B2: de CLI (ADR-077 § 3 controle, § 4 tellingen, § 5 export), op een tijdelijke contextmap."""
import os, subprocess, sys
from pathlib import Path

import pytest

PAKKET = Path(__file__).resolve().parents[1] / "packages" / "besluitenregister"
sys.path.insert(0, str(PAKKET))
from besluitenregister import cli, identiteit
from besluitenregister.lezer import lees

ONBEKEND, KOP = "B-20261114-deadbeef", ["--besloten-op", "2026-11-14", "--vastgelegd-door", "t0"]
VELDEN = ["--waarom", "Omdat.", "--besloten-door", "T0", "--bron-soort", "adr", "--bron-ref", "ADR-077",
          "--reikwijdte", "context", "--terugweg", "geen: nvt"]

def run(capsys, *argv):
    code = cli.main([str(a) for a in argv])
    uit = capsys.readouterr()
    return code, uit.out, uit.err

def add(capsys, ctx, titel, vertr="intern", slug="vnx-orchestration", *extra, sub=("add",)):
    code, out, err = run(capsys, *sub, "--context", ctx, "--slug", slug, *KOP, *VELDEN, "--titel", titel,
                         "--besluit", f"Besluit {titel}.", "--vertrouwelijkheid", vertr, *extra)
    assert code == 0, err
    return out.split()[0]

def render_check(capsys, ctx):
    run(capsys, "render", "--context", ctx)
    return run(capsys, "check", "--context", ctx)[:2]

def plak(ctx, **velden):  # een schemageldige regel buiten de schrijver om, voor de foutsoorten van check
    r = {"schema": "besluit/1", "context": "vnx-orchestration", "besloten_op": "2026-11-15",
         "vastgelegd_op": "2026-11-15T09:00:00Z", "vastgelegd_door": "t0", **velden}
    with open(ctx / "besluiten.ndjson", "a", encoding="utf-8") as f:
        f.write(identiteit.canoniek(dict(r, event_id=identiteit.event_id(r))) + "\n")

def kopie(ctx, bid, **wijzig):  # een vastgelegd zoals de eerste, met een ander id
    v = next(r for _, r in lees(ctx / "besluiten.ndjson").regels)
    plak(ctx, **{**{k: x for k, x in v.items() if k not in ("event_id", "vastgelegd_op", "schema")},
                 "besluit_id": bid, "betreft": bid, **wijzig})

HERZIEN = {"event": "herzien", "waarom": "gekeken", "herzien_op": None}
X, Y = "B-20261114-00000001", "B-20261114-00000002"
FOUTSOORT = {
    "onbekend betreft": lambda ctx, a: plak(ctx, **HERZIEN, betreft=ONBEKEND),
    "besluit_id op meer dan één vastgelegd": lambda ctx, a: kopie(ctx, a, titel="Dubbel"),
    "cirkel in vervangt": lambda ctx, a: (kopie(ctx, X, titel="X", vervangt=Y), kopie(ctx, Y, titel="Y", vervangt=X)),
    "gebeurtenis na een eindstand": lambda ctx, a: (
        plak(ctx, event="ingetrokken", betreft=a, reden="weg", bron={"soort": "adr", "ref": "r", "citaat": ""}),
        plak(ctx, **HERZIEN, betreft=a)),
    "open vervanging": lambda ctx, a: kopie(ctx, X, titel="Nieuw", vervangt=a),
    "BESLUITEN.md wijkt af": lambda ctx, a: None,  # de test schrijft een regel na render
}

@pytest.mark.parametrize("soort", FOUTSOORT)
def test_check_per_foutsoort(capsys, tmp_path, soort):
    a = add(capsys, tmp_path, "Kies A")
    FOUTSOORT[soort](tmp_path, a)
    run(capsys, "render", "--context", tmp_path)
    if soort == "BESLUITEN.md wijkt af":
        plak(tmp_path, **HERZIEN, betreft=a)  # na render: de weergave is verouderd
    code, out, _ = run(capsys, "check", "--context", tmp_path)
    assert code == 1 and any(x.startswith(f"fout: {soort}") for x in out.splitlines()), out

def test_check_schoon_na_render_en_render_deterministisch(capsys, tmp_path):
    a = add(capsys, tmp_path, "Kies A")
    add(capsys, tmp_path, "Kies B", "intern", "vnx-orchestration", "--reden", "beter", sub=("vervang", a))
    b = add(capsys, tmp_path, "Kies C", "intern", "vnx-orchestration", "--herzien-op", "2026-12-01")
    for argv in (["bevestig", b, "--bron-soort", "chat", "--bron-ref", "c", "--citaat", "ja", "--herzien-op", "geen"],
                 ["herzien", b, "--waarom", "gekeken", "--herzien-op", "2027-01-01"]):
        assert run(capsys, *argv, "--context", tmp_path, *KOP)[0] == 0
    code, out = render_check(capsys, tmp_path)
    eerst = (tmp_path / "BESLUITEN.md").read_bytes()
    run(capsys, "render", "--context", tmp_path)
    assert (tmp_path / "BESLUITEN.md").read_bytes() == eerst and b"vervangen door " in eerst
    assert code == 0 and "fout:" not in out and "check: ok" in out, out

def test_staart_waarschuwing_en_ongeldig_fout(capsys, tmp_path):
    add(capsys, tmp_path, "Kies A")
    with open(tmp_path / "besluiten.ndjson", "ab") as f:
        f.write(b'{"half":')
    code, out = render_check(capsys, tmp_path)
    assert code == 0 and "waarschuwing: afgescheurd stuk" in out and "afgescheurd: 1" in out, out
    add(capsys, tmp_path, "Kies B")  # de schrijver zet de staart in quarantaine
    code, out = render_check(capsys, tmp_path)
    assert code == 0 and "waarschuwing: 1 vermelding(en) in besluiten.ndjson.afgescheurd" in out, out
    with open(tmp_path / "besluiten.ndjson", "ab") as f:
        f.write(b"geen json\n")
    code, out = render_check(capsys, tmp_path)
    assert code == 1 and "fout: ongeldige regel: 3" in out, out
    assert run(capsys, "export", "--context", tmp_path, "--voor", "intern")[0] == 1

def test_export_vertrouwelijkheid_adr_077_5(capsys, tmp_path):
    ka, kb = tmp_path / "a", tmp_path / "b"
    ka.mkdir(), kb.mkdir()
    for ctx, titel, vertr in ((ka, "REEKS-INTERN-A", "intern"), (ka, "REEKS-PUBLIEK", "publiek"),
                              (ka, "REEKS-KLANT-A", "klant"), (kb, "REEKS-INTERN-B", "intern")):
        add(capsys, ctx, titel, vertr, f"klant:{ctx.name}")
    reeksen = ("REEKS-INTERN-A", "REEKS-KLANT-A", "REEKS-INTERN-B")
    def treffers(voor):
        tekst = "".join(run(capsys, "export", "--context", k, "--voor", voor)[1] for k in (ka, kb))
        assert "REEKS-PUBLIEK" in tekst
        return [r for r in reeksen if r in tekst]
    assert treffers("klant:b") == [] and treffers("publiek") == [] and treffers("klant:a") == ["REEKS-KLANT-A"]
    assert set(treffers("intern")) == set(reeksen)

def test_export_opvolger_alleen_als_zichtbaar_en_filters(capsys, tmp_path):
    a = add(capsys, tmp_path, "Oud", "publiek")
    nieuw = add(capsys, tmp_path, "Nieuw", "intern", "vnx-orchestration", "--reden", "r", sub=("vervang", a))
    assert nieuw not in run(capsys, "export", "--context", tmp_path, "--voor", "publiek")[1]
    assert nieuw in run(capsys, "export", "--context", tmp_path, "--voor", "intern", "--tot", "2026-11-14",
                        "--reikwijdte", "context")[1]
    assert run(capsys, "export", "--context", tmp_path, "--voor", "intern", "--uit", tmp_path / "x.md",
               "--van", "2026-11-15")[0] == 0
    assert "0 besluiten" in (tmp_path / "x.md").read_text()

@pytest.mark.parametrize("voor", [[], ["--voor", "iedereen"], ["--voor", "klant:"]])
def test_export_zonder_geldig_voor_exit_2_en_niets_geschreven(capsys, tmp_path, voor):
    add(capsys, tmp_path, "Kies A")
    code, out, _ = run(capsys, "export", "--context", tmp_path, "--uit", tmp_path / "uit.md", *voor)
    assert code == 2 and out == "" and not (tmp_path / "uit.md").exists()

def test_geheim_in_citaat_exit_3_zonder_waarde(capsys, tmp_path):
    a = add(capsys, tmp_path, "Kies A")
    voor, geheim = (tmp_path / "besluiten.ndjson").read_bytes(), "ghp_" + "a1" * 12
    code, out, err = run(capsys, "bevestig", a, "--context", tmp_path, *KOP, "--bron-soort", "chat",
                         "--bron-ref", "c", "--citaat", geheim, "--herzien-op", "geen")
    assert code == 3 and "github_token" in err and geheim not in out + err
    assert (tmp_path / "besluiten.ndjson").read_bytes() == voor

def test_list_show_trek_in_leeg_en_ontbrekende_map(capsys, tmp_path):
    code, out, _ = run(capsys, "list", "--context", tmp_path)
    assert code == 0 and "0 regels · afgescheurd: 0 · ongeldig: 0" in out
    assert run(capsys, "list", "--context", tmp_path / "nee")[0] == 2
    assert run(capsys, *["add", "--context", tmp_path / "nee", "--slug", "s", *KOP, *VELDEN, "--titel", "t",
                         "--besluit", "b", "--vertrouwelijkheid", "intern"])[0] == 2
    a = add(capsys, tmp_path, "Kies A", "intern", "vnx-orchestration", "--herzien-op", "2026-11-20",
            "--alternatief", "B", "trager", "--link", "OI-1", "--tag", "dev")
    assert f"{a}  te_herzien" in run(capsys, "list", "--context", tmp_path, "--nu", "2026-11-20")[1]
    code, out, _ = run(capsys, "show", a, "--context", tmp_path, "--nu", "2026-11-19")
    assert code == 0 and "stand: geldig" in out and "B: trager" in out and "1 regels" in out
    assert run(capsys, "show", ONBEKEND, "--context", tmp_path)[0] == 1
    assert run(capsys, "trek-in", a, "--context", tmp_path, *KOP, "--reden", "weg", "--bron-soort", "adr",
               "--bron-ref", "r")[0] == 0
    assert "stand: ingetrokken" in run(capsys, "show", a, "--context", tmp_path)[1]

def test_entrypoint_python_m(tmp_path):
    p = subprocess.run([sys.executable, "-m", "besluitenregister", "list", "--context", str(tmp_path)], timeout=60,
                       env=dict(os.environ, PYTHONPATH=str(PAKKET)), capture_output=True, text=True, cwd=tmp_path)
    assert p.returncode == 0 and "0 regels" in p.stdout, p.stderr

def _uit_pad(soort, ctx, buiten):
    (buiten / "ln").symlink_to(ctx / "besluiten.ndjson")
    return {"ndjson": ctx / "besluiten.ndjson", "symlink": buiten / "ln",
            "punten": ctx / "sub" / ".." / "besluiten.ndjson", "afgescheurd": ctx / "besluiten.ndjson.afgescheurd",
            "md": ctx / "BESLUITEN.md", "map": ctx}[soort]

@pytest.mark.parametrize("soort", ["ndjson", "symlink", "punten", "afgescheurd", "md", "map"])
def test_export_uit_weigert_eigen_registerbestanden(capsys, tmp_path, soort):
    ctx, buiten = tmp_path / "ctx", tmp_path / "buiten"
    (ctx / "sub").mkdir(parents=True)
    buiten.mkdir()
    add(capsys, ctx, "Kies A")
    run(capsys, "render", "--context", ctx)
    (ctx / "besluiten.ndjson.afgescheurd").write_bytes(b"staart\n")
    voor = {p.name: p.read_bytes() for p in ctx.iterdir() if p.is_file()}
    pad = _uit_pad(soort, ctx, buiten)
    code, out, err = run(capsys, "export", "--context", ctx, "--voor", "intern", "--uit", pad)
    assert code == 2 and out == "" and str(pad) in err
    assert {p.name: p.read_bytes() for p in ctx.iterdir() if p.is_file()} == voor

def test_export_uit_naar_ander_bestand_in_de_context_werkt(capsys, tmp_path):
    add(capsys, tmp_path, "Kies A")
    voor = (tmp_path / "besluiten.ndjson").read_bytes()
    assert run(capsys, "export", "--context", tmp_path, "--voor", "intern", "--uit", tmp_path / "export-intern.md")[0] == 0
    assert "Kies A" in (tmp_path / "export-intern.md").read_text()
    assert (tmp_path / "besluiten.ndjson").read_bytes() == voor

# --- B3: add --from-daily-log (ADR-077 § 7, plan § Lezers en schrijvers) ---
LOG = """---
type: dev_daily_log
date: 2026-11-14
---

## Gedaan

- Dit is geen besluit.

## Beslissingen

- Eerste punt. Met uitleg.
  - Genest punt hoort bij de ouder.
- geen
- Tweede punt zonder punt aan het eind

## Open vragen

- Ook geen besluit.

### Beslissingen (achtste T0)

- Derde punt.

### Beslissingen (negende T0)

- n.v.t.
"""

def importeer(capsys, ctx, log, *extra):
    return run(capsys, "add", "--context", ctx, "--from-daily-log", log, "--slug", "vnx-orchestration", *extra)

def schrijf_log(tmp_path, tekst=LOG, naam="2026-11-14.md"):
    ctx = tmp_path / "ctx"
    ctx.mkdir(exist_ok=True)
    (ctx / naam).write_text(tekst, encoding="utf-8")
    return ctx, ctx / naam

def test_from_daily_log_drie_besluiten_tweede_import_schrijft_niets(capsys, tmp_path):
    ctx, log = schrijf_log(tmp_path)
    code, out, err = importeer(capsys, ctx, log)
    assert code == 0, err
    regels = [r for _, r in lees(ctx / "besluiten.ndjson").regels]
    assert [r["titel"] for r in regels] == ["Eerste punt.", "Tweede punt zonder punt aan het eind", "Derde punt."]
    assert [r["bron"] for r in regels][0] == {"soort": "daily-log", "ref": "2026-11-14.md#Beslissingen/1", "citaat": ""}
    assert all(r["bron"]["soort"] == "daily-log" and r["bron"]["ref"].startswith("2026-11-14.md#Beslissingen") for r in regels)
    assert all(r["besloten_op"] == "2026-11-14" and r["vastgelegd_door"] == "skill:daily-log"
               and r["besloten_door"] == "operator" for r in regels)
    assert "Genest punt hoort bij de ouder." in regels[0]["besluit"] and "Dit is geen" not in str(regels)
    voor = (ctx / "besluiten.ndjson").read_bytes()
    code, out2, _ = importeer(capsys, ctx, log)
    assert code == 0 and (ctx / "besluiten.ndjson").read_bytes() == voor
    assert all(r["besluit_id"] in out2 for r in regels) and "0 geschreven" in out2

def test_from_daily_log_gewijzigd_punt_exit_3_en_stopt(capsys, tmp_path):
    ctx, log = schrijf_log(tmp_path)
    importeer(capsys, ctx, log)
    voor = (ctx / "besluiten.ndjson").read_bytes()
    log.write_text(LOG.replace("- Derde punt.", "- Derde punt. Nu anders."))
    code, _, err = importeer(capsys, ctx, log)
    assert code == 3 and "punt 3 van 3" in err and "#Beslissingen (achtste T0)/1" in err and "0 geschreven" in err
    assert "Nu anders" not in err  # de positie, nooit de tekst
    assert (ctx / "besluiten.ndjson").read_bytes() == voor

def test_from_daily_log_zonder_sectie_exit_0_niets_geschreven(capsys, tmp_path):
    ctx, log = schrijf_log(tmp_path, "---\ndate: 2026-11-14\n---\n\nBeslissingen:\n\n- geen sectie\n\n## Gedaan\n\n- x\n")
    code, out, _ = importeer(capsys, ctx, log)
    assert code == 0 and "0 besluiten" in out and not (ctx / "besluiten.ndjson").exists()

def test_from_daily_log_datum_uit_bestandsnaam_en_zonder_datum_exit_2(capsys, tmp_path):
    ctx, log = schrijf_log(tmp_path, "## Beslissingen\n\n- Een punt.\n", "2026-11-13.md")
    assert importeer(capsys, ctx, log)[0] == 0
    assert next(r for _, r in lees(ctx / "besluiten.ndjson").regels)["besloten_op"] == "2026-11-13"
    ctx2, log2 = schrijf_log(tmp_path / "x", "## Beslissingen\n\n- Een punt.\n", "notities.md") if (tmp_path / "x").mkdir() is None else None
    code, _, err = importeer(capsys, ctx2, log2)
    assert code == 2 and "datum" in err and not (ctx2 / "besluiten.ndjson").exists()

def test_from_daily_log_geheim_exit_3_zonder_waarde(capsys, tmp_path):
    geheim = "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"
    ctx, log = schrijf_log(tmp_path, f"---\ndate: 2026-11-14\n---\n## Beslissingen\n\n- Token {geheim} gebruiken.\n")
    code, out, err = importeer(capsys, ctx, log)
    assert code == 3 and geheim not in out + err and not (ctx / "besluiten.ndjson").exists()

def test_from_daily_log_check_schoon_na_render(capsys, tmp_path):
    ctx, log = schrijf_log(tmp_path)
    importeer(capsys, ctx, log)
    code, out = render_check(capsys, ctx)  # check eist een BESLUITEN.md gelijk aan render
    assert code == 0 and "check: ok" in out

def test_add_zonder_from_daily_log_mist_verplichte_vlag_exit_2(capsys, tmp_path):
    assert run(capsys, "add", "--context", tmp_path, "--titel", "x")[0] == 2

def test_from_daily_log_gewijzigd_punt_zelfde_plek_exit_3_een_besluit(capsys, tmp_path):
    kop = "---\ndate: 2026-11-14\n---\n\n## Beslissingen\n\n"
    ctx, log = schrijf_log(tmp_path, kop + "- Use SQLite.\n")
    assert importeer(capsys, ctx, log)[0] == 0
    voor = (ctx / "besluiten.ndjson").read_bytes()
    code, out, _ = importeer(capsys, ctx, log)  # ongewijzigd: niets geschreven, bestaand id getoond
    assert code == 0 and (ctx / "besluiten.ndjson").read_bytes() == voor and "0 geschreven" in out
    log.write_text(kop + "- Use PostgreSQL.\n", encoding="utf-8")
    code, _, err = importeer(capsys, ctx, log)
    bid = next(r for _, r in lees(ctx / "besluiten.ndjson").regels)["besluit_id"]
    assert code == 3 and "punt 1 van 1" in err and "2026-11-14.md#Beslissingen/1" in err and bid in err
    assert "PostgreSQL" not in err and (ctx / "besluiten.ndjson").read_bytes() == voor
    assert len(lees(ctx / "besluiten.ndjson").regels) == 1

@pytest.mark.parametrize("hek", ["```", "~~~", "````"])
def test_from_daily_log_bullets_in_een_hek_zijn_geen_besluit(capsys, tmp_path, hek):
    tekst = f"---\ndate: 2026-11-14\n---\n\n## Beslissingen\n\n- Echt.\n\n{hek}markdown\n- Example only.\n{hek}\n\n- Ook echt.\n"
    ctx, log = schrijf_log(tmp_path, tekst)
    assert importeer(capsys, ctx, log)[0] == 0
    regels = [r for _, r in lees(ctx / "besluiten.ndjson").regels]
    assert [r["besluit"] for r in regels] == ["Echt.", "Ook echt."]
    assert "Example" not in str(regels) and hek not in str(regels)

TOKEN = "ghp_" + "A" * 36  # pas tijdens de test opgebouwd: het testbestand draagt geen letterlijk geheim

def test_from_daily_log_punt_gewijzigd_naar_geheim_exit_3_zonder_waarde(capsys, tmp_path):
    kop = "---\ndate: 2026-11-14\n---\n\n## Beslissingen\n\n"
    ctx, log = schrijf_log(tmp_path, kop + "- Use SQLite.\n")
    assert importeer(capsys, ctx, log)[0] == 0
    voor = (ctx / "besluiten.ndjson").read_bytes()
    log.write_text(kop + f"- {TOKEN} gebruiken. Daarna verder.\n", encoding="utf-8")
    code, out, err = importeer(capsys, ctx, log)
    assert code == 3 and "github_token" in err and TOKEN not in out + err
    assert (ctx / "besluiten.ndjson").read_bytes() == voor

def _pad_met_token(ctx, map_=False):
    (ctx / TOKEN).mkdir() if map_ else None
    return ctx / TOKEN

LEK = {  # elk pad waarlangs tekst van de aanroeper vóór de geheimscan in een melding kwam: (argv, exitcode)
    "show": lambda c, b: (["show", TOKEN, "--context", c], 1),
    "keuze": lambda c, b: (["export", "--context", c, "--voor", "intern", "--reikwijdte", TOKEN], 2),
    "onbekend argument": lambda c, b: (["list", "--context", c, TOKEN], 2),
    "context": lambda c, b: (["list", "--context", c / TOKEN], 2),
    "uit": lambda c, b: (["export", "--context", c, "--voor", "intern", "--uit", _pad_met_token(c, True)], 2),
    "log bestaat niet": lambda c, b: (["add", "--context", c, "--from-daily-log", c / f"{TOKEN}.md"], 2),
    "log zonder datum": lambda c, b: (["add", "--context", c, "--from-daily-log", _log(c, f"{TOKEN}.md", "## Beslissingen\n\n- x\n")], 2),
    "log zonder sectie": lambda c, b: (["add", "--context", c, "--from-daily-log", _log(c, f"2026-11-14-{TOKEN}.md", "x\n")], 0),
    "vervang": lambda c, b: (["vervang", TOKEN, "--context", c, *KOP, *VELDEN, "--titel", "t", "--besluit", "b",
                              "--vertrouwelijkheid", "intern", "--reden", "beter"], 3),
    "trek-in": lambda c, b: (["trek-in", TOKEN, "--context", c, *KOP, "--reden", "weg", "--bron-soort", "adr",
                              "--bron-ref", "r"], 3),
}

def _log(ctx, naam, tekst):
    (ctx / naam).write_text(tekst, encoding="utf-8")
    return ctx / naam

@pytest.mark.parametrize("pad", sorted(LEK))
def test_geen_melding_herhaalt_een_geheim_van_de_aanroeper(capsys, tmp_path, pad):
    bid = add(capsys, tmp_path, "Kies A")
    voor = (tmp_path / "besluiten.ndjson").read_bytes()
    argv, verwacht = LEK[pad](tmp_path, bid)
    code, out, err = run(capsys, *argv)
    assert code == verwacht and "github_token" in out + err and TOKEN not in out + err
    assert (tmp_path / "besluiten.ndjson").read_bytes() == voor

NIEUW = [*KOP, *VELDEN, "--titel", "t", "--besluit", "b", "--vertrouwelijkheid", "intern"]
VOLLEDIG = {  # elk subcommando met al zijn verplichte vlaggen; "A" wordt het id van een bestaand besluit
    "add": ["add", *NIEUW], "vervang": ["vervang", "A", *NIEUW, "--reden", "r"],
    "trek-in": ["trek-in", "A", *KOP, "--reden", "r", "--bron-soort", "adr", "--bron-ref", "r"],
    "herzien": ["herzien", "A", *KOP, "--waarom", "w", "--herzien-op", "geen"],
    "bevestig": ["bevestig", "A", *KOP, "--bron-soort", "chat", "--bron-ref", "c", "--herzien-op", "geen"],
    "export": ["export", "--voor", "intern"]}
WEGLATEN = [(s, v) for s, argv in VOLLEDIG.items() for v in ["--context", *(x for x in argv if x.startswith("--"))]]

def _argv(ctx, sub, bid, zonder=None):
    argv = [bid if x == "A" else x for x in VOLLEDIG[sub]] + ["--context", ctx]
    if zonder:
        del argv[argv.index(zonder):argv.index(zonder) + 2]
    return argv

@pytest.mark.parametrize("sub", VOLLEDIG)
def test_volledige_aanroep_per_subcommando_werkt(capsys, tmp_path, sub):
    code, _, err = run(capsys, *_argv(tmp_path, sub, add(capsys, tmp_path, "Kies A")))
    assert code == 0, err

@pytest.mark.parametrize("sub,vlag", WEGLATEN)
def test_ontbrekende_verplichte_vlag_exit_2_niets_geschreven(capsys, tmp_path, sub, vlag):
    bid = add(capsys, tmp_path, "Kies A")
    voor = (tmp_path / "besluiten.ndjson").read_bytes()
    code, out, err = run(capsys, *_argv(tmp_path, sub, bid, vlag))
    assert code == 2 and vlag in err and out == "", err
    assert (tmp_path / "besluiten.ndjson").read_bytes() == voor

def test_add_deelt_geen_action_met_een_ander_subcommando(capsys, tmp_path):
    keuzes = next(x for x in cli._parser()._actions if x.dest == "sub").choices
    van_add = {id(x) for x in keuzes["add"]._actions}
    assert all(not van_add & {id(x) for x in sp._actions} for naam, sp in keuzes.items() if naam != "add")
    argv = [x for x in _argv(tmp_path, "add", "") if x not in ("--titel", "t", "--besluit", "b")]
    code, _, err = run(capsys, *argv)
    assert code == 2 and "verplicht: --titel, --besluit" in err and not (tmp_path / "besluiten.ndjson").exists()
