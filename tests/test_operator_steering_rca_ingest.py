"""OB-6b: ingest a steering RCA (ADR-072 decisions 8, 12, 13, 14, 16). Invented prompts, tmp_path only, no model runs."""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts" / "lib"))
sys.path.insert(0, str(REPO / "scripts"))

import operator_steering_cases as cl
import operator_steering_rca as rca
import operator_steering_report as osr
import operator_steering_source as src
import steering_proposal_store as store

SLUG, PID, LONG_ID = "-proj-vnx", "vnx-dev", "20261114-080000-herindeling-facturatiedashboard-klantportalen"
PLAIN = "hoe ver zijn we met de herindeling van het facturatiedashboard voor de klantportalen"
TEXT, BASE, NOW = f"{PLAIN} zie {LONG_ID}", datetime(2026, 11, 14, 8, 0, tzinfo=timezone.utc), datetime(2026, 11, 15, 23, 30, tzinfo=timezone.utc)
PROPOSAL = store.proposal_id(PID, "ontbrekend_overzicht", "feature", "scripts/voortgang.py")
QUOTED = f'{PLAIN}, met de "weergave per kwartaal" uit C:\\vnx\\facturen\nen\tdaarna de stand van zaken per klant'
FULL = f"{QUOTED} zie {LONG_ID}"  # each 40-character window around " \\ \n \t differs from its JSON-escaped form


@pytest.fixture
def env(tmp_path, capsys, request):
    """(state dir, projects root, transcript dir, the case block of one staged RCA, the prompt text without its id)."""
    plain = getattr(request, "param", PLAIN)
    (tmp_path / "proj").mkdir()
    (tmp_path / "proj" / ".vnx-project-id").write_text(PID + "\n")
    tdir, state, projects = tmp_path / "projects" / SLUG, tmp_path / "proj" / "state", tmp_path / "projects"
    (tdir / "memory").mkdir(parents=True)
    src.bind(state, projects, [SLUG], tdir / "memory")
    for n in range(3):
        turn = [{"type": "text", "text": "zie #2721"}, {"type": "tool_use", "id": f"toolu_{n}", "name": "Bash", "input": {"command": "ls"}}]
        (tdir / f"s{n}.jsonl").write_text("".join(json.dumps({"type": k, "uuid": f"{k}{n}", "sessionId": f"s{n}", "message": {"role": k, "content": c},
                                          "timestamp": (BASE + timedelta(minutes=m)).strftime("%Y-%m-%dT%H:%M:%SZ")}) + "\n"
                                          for k, m, c in (("assistant", n * 100, turn), ("user", n * 100 + 1, f"{plain} zie {LONG_ID}"))))
    return state, projects, tdir, staged(state, projects, capsys), plain


def staged(state, projects, capsys):
    cid = osr.build_report(state, projects, BASE - timedelta(hours=1), NOW, now=NOW)["gevallen"][0]["case_id"]
    assert rca.main(["--state-dir", str(state), "--projects-root", str(projects), "stage", "--case", cid], now=NOW) == 0 and capsys.readouterr()
    did = next(c.open_dispatch for c in cl.read_ledger(state, PID).values() if c.open_dispatch)
    return json.loads(cl.bundle_spec(state, did).with_name("instruction.md").read_text().split("```json\n")[1].split("```")[0])


def answer(snap, **over):
    ids = snap["context"][0]
    return {"schema": "steering_rca/1", "case_id": snap["case_id"], "snapshot_sha256": snap["snapshot_sha256"], "oorzaak": "ontbrekend_overzicht",
            "artefact_soort": "feature", "doelpad": "scripts/voortgang.py", "voorstel": "Een voortgangsoverzicht per release.",
            "bewijs": {"prompt_ids": snap["prompt_ids"], "context": [ids["tool_call_ids"][0], ids["dispatch_ids"][0], "#2721", 2721]}, **over}


def ingest(env, capsys, *blocks, prose="", dry=False, snap=None, did=None):
    """Write the report, run ingest; every run asserts that no prompt text reaches output, ledger or store."""
    snap = snap or env[3]
    (env[0].parent / "unified_reports").mkdir(exist_ok=True)
    (env[0].parent / "unified_reports" / f"{snap['dispatch_id']}.md").write_text(f"## Summary\n{prose}\n" + "".join(
        f"```json\n{b if isinstance(b, str) else json.dumps(b)}\n```\n" for b in blocks))
    code = rca.main(["ingest", "--dispatch", did or snap["dispatch_id"], "--state-dir", str(env[0]), "--projects-root", str(env[1])]
                    + ["--dry-run"] * dry, now=NOW)
    out = capsys.readouterr()
    written = out.out + out.err + "".join(p.read_text() for p in env[0].glob("steering_*") if p.is_file())
    assert not [env[4][i:i + 20] for i in range(len(env[4]) - 19) if env[4][i:i + 20] in written]
    return code, out.out + out.err


def last(env):
    return json.loads((env[0] / cl.LEDGER_FILE).read_text().splitlines()[-1])


@pytest.mark.parametrize("rejected_at, outcome", [(None, "revised"), ("2026-11-14T00:00:00Z", "reopened"), ("2026-11-15T00:00:00Z", "suppressed_rejected")])
def test_a_valid_block_is_revision_one_and_a_second_rca_in_other_words_is_revision_two(env, capsys, rejected_at, outcome):
    assert ingest(env, capsys, answer(env[3]))[0] == 0 and [last(env)[k] for k in ("event", "uitkomst", "proposal_id")] == ["rca_ingested", "added", PROPOSAL]
    if rejected_at:  # decision 14: only a prompt whose transcript ts is after the rejection reopens it
        store.decide(env[0], PID, PROPOSAL, "rejected", reason="niet nu", decided_by="operator", now=rejected_at)
    second = staged(env[0], env[1], capsys)
    assert second["dispatch_id"].endswith("-2") and ingest(env, capsys, answer(second, voorstel="Een overzicht per versie."), snap=second)[0] == 0
    (entry,) = store.proposals(env[0], PID)
    assert (entry["proposal_id"], len(entry["revisions"]), last(env)["uitkomst"], entry["revisions"][0]["evidence_prompt_ids"]) == (
        PROPOSAL, 2, outcome, env[3]["prompt_ids"])


def test_no_report_no_case_a_crash_before_the_ledger_and_an_unreadable_ledger(env, capsys, monkeypatch):
    run, ledger = ["--state-dir", str(env[0]), "--projects-root", str(env[1]), "ingest", "--dispatch", env[3]["dispatch_id"]], env[0] / cl.LEDGER_FILE
    assert rca.main(run, now=NOW) == 1 and "geen leesbaar rapport" in capsys.readouterr().err
    before = ledger.read_bytes()
    assert ingest(env, capsys, answer(env[3]), did="steering-rca-onbekend")[0] == 1 and ledger.read_bytes() == before
    monkeypatch.setattr(cl, "book", lambda *_: (_ for _ in ()).throw(OSError("schijf vol")))
    assert ingest(env, capsys, answer(env[3]))[0] == 3 and ledger.read_bytes() == before
    monkeypatch.undo()  # store first, ledger second: the re-ingest finds its revision and counts it unchanged
    assert ingest(env, capsys, answer(env[3]))[0] == 0 and last(env)["uitkomst"] == "unchanged"
    assert ingest(env, capsys, answer(env[3]))[0] == 1 and [len(p["revisions"]) for p in store.proposals(env[0], PID)] == [1]
    snap = staged(env[0], env[1], capsys)  # an unreadable store: exit 3, the case stays open
    (env[0] / store.DECISIONS_FILENAME).write_text("{kapot\n")
    assert ingest(env, capsys, answer(snap), snap=snap)[0] == 3 and last(env)["event"] == "rca_staged"
    ledger.write_bytes(ledger.read_bytes() + b"{kapot\n")
    assert rca.main(run, now=NOW) == 3


def test_dry_run_and_insufficient_evidence_write_nothing_to_the_store(env, capsys):
    before, files = (env[0] / cl.LEDGER_FILE).read_bytes(), (env[0] / store.DECISIONS_FILENAME, env[0] / store.PROJECTION_FILENAME)
    assert ingest(env, capsys, answer(env[3]), dry=True) == (0, f"rca_ingested {env[3]['dispatch_id']}: geldig {PROPOSAL} (dry-run: niets geschreven)\n")
    code, out = ingest(env, capsys, answer(env[3], oorzaak="x"), dry=True)
    assert code == 4 and "oorzaak_onbekend (dry-run: niets geschreven)" in out and (env[0] / cl.LEDGER_FILE).read_bytes() == before
    none = answer(env[3], oorzaak="insufficient_evidence", artefact_soort="", doelpad=None, voorstel="", bewijs=[])
    assert ingest(env, capsys, none)[0] == 0 and (last(env)["event"], last(env)["uitkomst"]) == ("rca_ingested", "insufficient_evidence")
    assert not any(f.exists() for f in files)


_REFUSED = [([{"bewijs": ["f" * 16]}], "bewijs_buiten_snapshot", "f" * 16), ([{"bewijs": {"c": ["toolu_vreemd"]}}], "bewijs_buiten_snapshot", "toolu_vreemd"),
            ([{"bewijs": [2722]}], "bewijs_buiten_snapshot", "2722"), ([{"bewijs": [PLAIN[:30]]}], "bewijs_buiten_snapshot", "<waarde van 30 tekens>"),
            ([{"snapshot_sha256": "0" * 64}], "snapshot_sha256_onjuist", ""), ([{"snapshot_sha256": None}], "snapshot_sha256_onjuist", ""),
            ([{"case_id": "0" * 64}], "case_id_onjuist", ""), ([{"case_id": None}], "case_id_onjuist", ""),
            ([{"oorzaak": "onbekend"}], "oorzaak_onbekend", "onbekend"), ([{"oorzaak": None}], "oorzaak_onbekend", ""),
            ([{"schema": "steering_rca/2"}], "schema_onbekend", ""), ([{}, {}], "meerdere_blokken", ""), (["case"], "geen_blok", ""),
            ([], "geen_blok", ""), ([{}, "{niet json"], "blok_onleesbaar", ""), ([{}, "[" * 100000], "blok_onleesbaar", ""),
            ([{"artefact_soort": "script"}], "artefact_soort_onbekend", ""),
            ([{"oorzaak": "insufficient_evidence", "artefact_soort": "", "voorstel": ""}], "insufficient_evidence_niet_leeg", ""),
            ([{"artefact_soort": None}], "artefact_soort_onbekend", ""), ([{"voorstel": " "}], "voorstel_onvolledig", ""),
            ([{"doelpad": None}], "voorstel_onvolledig", ""), ([{"bewijs": "f" * 16}], "bewijs_ongeldig", ""),
            ([{"bewijs": None}], "bewijs_ongeldig", ""), ([{"bewijs": {"p": [True]}}], "bewijs_ongeldig", ""), ([{"bewijs": [TEXT[:40]]}], "citaat", "")]


@pytest.mark.parametrize("over, reason, named", _REFUSED)  # a dict is a change to a valid answer; "case" echoes the case block
def test_each_invalid_answer_is_refused_with_its_own_code_and_booked(env, capsys, over, reason, named):
    blocks = [env[3] if o == "case" else o if isinstance(o, str) else answer(env[3], **o) for o in over]
    code, out = ingest(env, capsys, *blocks)
    assert (code, last(env)["event"], last(env)["reden"]) == (4, "rca_refused", reason) and f"geweigerd: {reason}: " in out and named in out
    assert not (env[0] / store.DECISIONS_FILENAME).exists()


@pytest.mark.parametrize("damage, prose, reason", [
    (None, TEXT[:40], "citaat"), (None, TEXT[:39], None), (None, f"zie {LONG_ID} en #2721", None),  # the masked id is no quote
    (lambda e: ((e[2].parent / "elders.jsonl").write_text("{}\n"), (e[2] / "zz.jsonl").symlink_to(e[2].parent / "elders.jsonl")), "", "bron_onleesbaar"),
    (lambda e: os.remove(e[2] / "s0.jsonl"), "", "bron_onleesbaar"),  # above: one file outside the bound dir; here: a case prompt gone
    (lambda e: os.truncate(p := cl.bundle_spec(e[0], e[3]["dispatch_id"]).with_name("instruction.md"), p.stat().st_size - 1), "", "snapshot_onleesbaar"),
    (lambda e: os.remove(cl.bundle_spec(e[0], e[3]["dispatch_id"])), "", "snapshot_onleesbaar"),
    (lambda e: os.renames(cl.bundle_spec(e[0], e[3]["dispatch_id"]).parent, e[0].parent / "dispatches" / "completed" / e[3]["dispatch_id"]), "", None)])
def test_a_quote_a_failing_live_read_or_a_changed_snapshot_is_refused_never_passed(env, capsys, damage, prose, reason):
    if damage:  # the last one: a fired bundle settles in completed/ and still counts
        damage(env)
    assert ingest(env, capsys, answer(env[3]), prose=prose)[0] == (4 if reason else 0) and last(env).get("reden") == reason


def _win(ch):
    return QUOTED[QUOTED.index(ch) - 20:][:40]


@pytest.mark.parametrize("env", [QUOTED], indirect=True, ids=["quoted"])
@pytest.mark.parametrize("over, prose, reason", [  # decision 12 holds for every decoded string, not only for the raw report
    ({"voorstel": FULL}, "", "citaat"), ({"voorstel": _win('"')}, "", "citaat"), ({"voorstel": _win("\\")}, "", "citaat"),
    ({"voorstel": _win("\n")}, "", "citaat"), ({"doelpad": _win("\t")}, "", "citaat"), ({_win('"'): 1}, "", "citaat"),
    ({"extra": [[1, {"k": [_win("\\")]}]]}, "", "citaat"), ({}, QUOTED[:40].replace(" ", "\n"), "citaat"),
    ({}, QUOTED[:40].replace(" ", "  "), "citaat"), ({"extra": [QUOTED[:20], QUOTED[20:40]], QUOTED[:20]: QUOTED[20:40]}, "", None),
    ({}, "", None)])  # above: two halves of one window are no citation; here: a report without any quote is ingested as before
def test_a_quote_in_any_decoded_string_or_in_rewrapped_prose_is_refused(env, capsys, over, prose, reason):
    block = json.dumps(answer(env[3], **over)).replace(json.dumps(FULL), '"' + "".join(f"\\u{ord(c):04x}" for c in FULL) + '"')
    assert ingest(env, capsys, block, prose=prose)[0] == (4 if reason else 0) and last(env).get("reden") == reason
    assert (env[0] / store.DECISIONS_FILENAME).exists() == (reason is None)
