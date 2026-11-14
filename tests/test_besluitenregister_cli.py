"""besluitenregister B2: de CLI (ADR-077 § 3 controle, § 4 tellingen, § 5 exportregel), op een tijdelijke contextmap."""
import json, os, subprocess, sys
from pathlib import Path

import pytest

PAKKET = Path(__file__).resolve().parents[1] / "packages" / "besluitenregister"
sys.path.insert(0, str(PAKKET))
from besluitenregister import cli, identiteit
from besluitenregister.lezer import lees

ONBEKEND = "B-20261114-deadbeef"
KOP = ["--besloten-op", "2026-11-14", "--vastgelegd-door", "t0"]

def run(capsys, *argv):
    code = cli.main([str(a) for a in argv])
    uit = capsys.readouterr()
    return code, uit.out, uit.err

def add(capsys, ctx, titel, vertr="intern", slug="vnx-orchestration", *extra):
    code, out, err = run(capsys, "add", "--context", ctx, "--slug", slug, *KOP, "--titel", titel,
                         "--besluit", f"Besluit {titel}.", "--waarom", "Omdat.", "--besloten-door", "T0",
                         "--bron-soort", "adr", "--bron-ref", "ADR-077", "--reikwijdte", "context",
                         "--vertrouwelijkheid", vertr, "--terugweg", "geen: nvt", *extra)
    assert code == 0, err
    return out.split()[0]

def regels(ctx):
    return [r for _, r in lees(ctx / "besluiten.ndjson").regels]

def plak(ctx, **velden):  # een schemageldige regel buiten de schrijver om, voor de foutsoorten van check
    r = {"schema": "besluit/1", "context": "vnx-orchestration", "besloten_op": "2026-11-15",
         "vastgelegd_op": "2026-11-15T09:00:00Z", "vastgelegd_door": "t0", **velden}
    r["event_id"] = identiteit.event_id(r)
    with open(ctx / "besluiten.ndjson", "a", encoding="utf-8") as f:
        f.write(identiteit.canoniek(r) + "\n")

def kopie(ctx, bid, **wijzig):  # een vastgelegd zoals die van bid, met wijzigingen
    v = next(r for r in regels(ctx) if r["event"] == "vastgelegd")
    plak(ctx, **{**{k: x for k, x in v.items() if k not in ("event_id", "vastgelegd_op", "schema")},
                 "besluit_id": bid, "betreft": bid, **wijzig})

def herzien(bid):
    return {"event": "herzien", "betreft": bid, "waarom": "gekeken", "herzien_op": None}

FOUTSOORT = {
    "onbekend betreft": lambda ctx, a: plak(ctx, **herzien(ONBEKEND)),
    "besluit_id op meer dan één vastgelegd": lambda ctx, a: kopie(ctx, a, titel="Dubbel"),
    "cirkel in vervangt": lambda ctx, a: (kopie(ctx, "B-20261114-00000001", titel="X", vervangt="B-20261114-00000002"),
                                          kopie(ctx, "B-20261114-00000002", titel="Y", vervangt="B-20261114-00000001")),
    "gebeurtenis na een eindstand": lambda ctx, a: (
        plak(ctx, event="ingetrokken", betreft=a, reden="weg", bron={"soort": "adr", "ref": "r", "citaat": ""}),
        plak(ctx, **herzien(a))),
    "open vervanging": lambda ctx, a: kopie(ctx, "B-20261114-00000003", titel="Nieuw", vervangt=a),
    "BESLUITEN.md wijkt af": lambda ctx, a: plak(ctx, **herzien(a)),
}

@pytest.mark.parametrize("soort", FOUTSOORT)
def test_check_per_foutsoort(capsys, tmp_path, soort):
    a = add(capsys, tmp_path, "Kies A")
    if soort == "BESLUITEN.md wijkt af":
        run(capsys, "render", "--context", tmp_path)
    FOUTSOORT[soort](tmp_path, a)
    if soort != "BESLUITEN.md wijkt af":
        run(capsys, "render", "--context", tmp_path)
    code, out, _ = run(capsys, "check", "--context", tmp_path)
    fouten = [x for x in out.splitlines() if x.startswith("fout:")]
    assert code == 1 and len(fouten) >= 1 and any(soort in x for x in fouten), out
    assert all(x.count("fout:") == 1 for x in fouten)

def test_check_schoon_na_render_en_render_deterministisch(capsys, tmp_path):
    a = add(capsys, tmp_path, "Kies A")
    assert run(capsys, "vervang", a, "--context", tmp_path, *KOP, "--titel", "Kies B", "--besluit", "B.",
               "--waarom", "Beter.", "--besloten-door", "T0", "--bron-soort", "adr", "--bron-ref", "ADR-077",
               "--reikwijdte", "context", "--vertrouwelijkheid", "intern", "--terugweg", "geen: nvt",
               "--reden", "B is beter")[0] == 0
    b = add(capsys, tmp_path, "Kies C", "intern", "vnx-orchestration", "--herzien-op", "2026-12-01")
    for argv in (["bevestig", b, "--bron-soort", "chat", "--bron-ref", "c", "--citaat", "ja", "--herzien-op", "geen"],
                 ["herzien", b, "--waarom", "gekeken", "--herzien-op", "2027-01-01"]):
        assert run(capsys, *argv, "--context", tmp_path, *KOP)[0] == 0
    run(capsys, "render", "--context", tmp_path)
    eerst = (tmp_path / "BESLUITEN.md").read_bytes()
    run(capsys, "render", "--context", tmp_path)
    assert (tmp_path / "BESLUITEN.md").read_bytes() == eerst and b"vervangen door " in eerst
    code, out, _ = run(capsys, "check", "--context", tmp_path)
    assert code == 0 and "fout:" not in out, out

def test_staart_waarschuwing_en_ongeldig_fout(capsys, tmp_path):
    add(capsys, tmp_path, "Kies A")
    with open(tmp_path / "besluiten.ndjson", "ab") as f:
        f.write(b'{"half":')
    run(capsys, "render", "--context", tmp_path)
    code, out, _ = run(capsys, "check", "--context", tmp_path)
    assert code == 0 and "waarschuwing:" in out and "afgescheurd: 1" in out, out
    add(capsys, tmp_path, "Kies B")  # de schrijver zet de staart in quarantaine
    run(capsys, "render", "--context", tmp_path)
    code, out, _ = run(capsys, "check", "--context", tmp_path)
    assert code == 0 and "1 vermelding(en) in besluiten.ndjson.afgescheurd" in out, out
    with open(tmp_path / "besluiten.ndjson", "ab") as f:
        f.write(b"geen json\n")
    run(capsys, "render", "--context", tmp_path)
    code, out, _ = run(capsys, "check", "--context", tmp_path)
    assert code == 1 and "fout: ongeldige regel: 3" in out, out
    assert run(capsys, "export", "--context", tmp_path, "--voor", "intern")[0] == 1

def test_export_vertrouwelijkheid_adr_077_5(capsys, tmp_path):
    ka, kb = tmp_path / "a", tmp_path / "b"
    ka.mkdir(), kb.mkdir()
    add(capsys, ka, "Intern REEKS-INTERN-A", "intern", "klant:a")
    add(capsys, ka, "Klant REEKS-KLANT-A", "klant", "klant:a")
    add(capsys, ka, "Openbaar REEKS-PUBLIEK", "publiek", "klant:a")
    add(capsys, kb, "Intern REEKS-INTERN-B", "intern", "klant:b")
    reeksen = ("REEKS-INTERN-A", "REEKS-KLANT-A", "REEKS-INTERN-B")
    def treffers(voor):
        tekst = "".join(run(capsys, "export", "--context", k, "--voor", voor)[1] for k in (ka, kb))
        assert "REEKS-PUBLIEK" in tekst
        return [r for r in reeksen if r in tekst]
    assert treffers("klant:b") == [] and treffers("publiek") == [] and treffers("klant:a") == ["REEKS-KLANT-A"]
    assert set(treffers("intern")) == set(reeksen)

def test_export_opvolger_alleen_als_zichtbaar(capsys, tmp_path):
    a = add(capsys, tmp_path, "Oud", "publiek")
    code, out, _ = run(capsys, "vervang", a, "--context", tmp_path, *KOP, "--titel", "Nieuw", "--besluit", "N.",
                       "--waarom", "W.", "--besloten-door", "T0", "--bron-soort", "adr", "--bron-ref", "r",
                       "--reikwijdte", "context", "--vertrouwelijkheid", "intern", "--terugweg", "geen: nvt", "--reden", "r")
    nieuw = out.split()[0]
    assert nieuw not in run(capsys, "export", "--context", tmp_path, "--voor", "publiek")[1]
    assert nieuw in run(capsys, "export", "--context", tmp_path, "--voor", "intern")[1]

@pytest.mark.parametrize("voor", [[], ["--voor", "iedereen"], ["--voor", "klant:"]])
def test_export_zonder_geldig_voor_exit_2_en_niets_geschreven(capsys, tmp_path, voor):
    add(capsys, tmp_path, "Kies A")
    code, out, _ = run(capsys, "export", "--context", tmp_path, "--uit", tmp_path / "uit.md", *voor)
    assert code == 2 and out == "" and not (tmp_path / "uit.md").exists()

def test_export_filters_en_uitbestand(capsys, tmp_path):
    add(capsys, tmp_path, "Kies A")
    assert run(capsys, "export", "--context", tmp_path, "--voor", "intern", "--uit", tmp_path / "x.md",
               "--van", "2026-11-15")[0] == 0
    tekst = (tmp_path / "x.md").read_text()
    assert "Kies A" not in tekst and "0 besluiten" in tekst
    assert "Kies A" in run(capsys, "export", "--context", tmp_path, "--voor", "intern", "--tot", "2026-11-14",
                           "--reikwijdte", "context")[1]

def test_geheim_in_citaat_exit_3_zonder_waarde(capsys, tmp_path):
    add(capsys, tmp_path, "Kies A")
    voor, geheim = (tmp_path / "besluiten.ndjson").read_bytes(), "ghp_" + "a1" * 12
    code, out, err = run(capsys, "bevestig", ONBEKEND.replace("deadbeef", "0" * 8), "--context", tmp_path, *KOP,
                         "--bron-soort", "chat", "--bron-ref", "c", "--citaat", geheim, "--herzien-op", "geen")
    assert code == 3  # eerst de overgang (onbekend besluit); dan het geheim op een bestaand besluit
    a = regels(tmp_path)[0]["besluit_id"]
    code, out, err = run(capsys, "bevestig", a, "--context", tmp_path, *KOP, "--bron-soort", "chat",
                         "--bron-ref", "c", "--citaat", geheim, "--herzien-op", "geen")
    assert code == 3 and "github_token" in err and geheim not in out + err
    assert (tmp_path / "besluiten.ndjson").read_bytes() == voor

def test_list_show_leeg_en_ontbrekende_map(capsys, tmp_path):
    code, out, _ = run(capsys, "list", "--context", tmp_path)
    assert code == 0 and "0 regels · afgescheurd: 0 · ongeldig: 0" in out
    assert run(capsys, "add", "--context", tmp_path / "nee", "--titel", "x")[0] == 2
    assert run(capsys, "list", "--context", tmp_path / "nee")[0] == 2
    a = add(capsys, tmp_path, "Kies A", "intern", "vnx-orchestration", "--herzien-op", "2026-11-20",
            "--alternatief", "B", "trager", "--link", "OI-1", "--tag", "dev")
    code, out, _ = run(capsys, "list", "--context", tmp_path, "--nu", "2026-11-20")
    assert code == 0 and f"{a}  te_herzien" in out and "1 regels" in out
    code, out, _ = run(capsys, "show", a, "--context", tmp_path, "--nu", "2026-11-19")
    assert code == 0 and "stand: geldig" in out and "B: trager" in out and "afgescheurd: 0" in out
    assert run(capsys, "show", ONBEKEND, "--context", tmp_path)[0] == 1
    assert run(capsys, "trek-in", a, "--context", tmp_path, *KOP, "--reden", "weg", "--bron-soort", "adr",
               "--bron-ref", "r")[0] == 0
    assert "stand: ingetrokken" in run(capsys, "show", a, "--context", tmp_path)[1]

def test_entrypoint_python_m(tmp_path):
    env = dict(os.environ, PYTHONPATH=str(PAKKET))
    p = subprocess.run([sys.executable, "-m", "besluitenregister", "list", "--context", str(tmp_path)],
                       env=env, capture_output=True, text=True, cwd=tmp_path, timeout=60)
    assert p.returncode == 0 and "0 regels" in p.stdout, p.stderr
