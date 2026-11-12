"""OB-6a: case ledger and idempotent RCA staging with a weekly cap (ADR-072 decisions 12, 16). Invented prompts, tmp_path only."""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts" / "lib"))
sys.path.insert(0, str(REPO / "scripts"))

import operator_steering as steering_cli
import operator_steering_cases as cl
import operator_steering_rca as rca
import operator_steering_report as osr
import operator_steering_source as src

SLUG, PID, COMMAND = "-proj-vnx", "vnx-dev", "uniek-commando-qrs --vlag"
TEXT = "hoe ver zijn we met de herindeling van het facturatiedashboard voor de klantportalen"
BASE = datetime(2026, 11, 14, 8, 0, tzinfo=timezone.utc)
NOW, NEXT_WEEK = datetime(2026, 11, 15, 23, 30, tzinfo=timezone.utc), datetime(2026, 11, 16, 0, 30, tzinfo=timezone.utc)


@pytest.fixture
def env(tmp_path):
    (tmp_path / "proj").mkdir()
    (tmp_path / "proj" / ".vnx-project-id").write_text(PID + "\n")
    tdir, state, projects = tmp_path / "projects" / SLUG, tmp_path / "proj" / "state", tmp_path / "projects"
    (tdir / "memory").mkdir(parents=True)
    src.bind(state, projects, [SLUG], tdir / "memory")
    for n in range(3):
        call = [{"type": "tool_use", "id": f"toolu_{n}", "name": "Bash", "input": {"command": COMMAND}}]
        rows = [("assistant", n * 100, call), ("user", n * 100 + 1, TEXT)]
        (tdir / f"s{n}.jsonl").write_text("".join(json.dumps({"type": k, "uuid": f"{k}{n}", "sessionId": f"s{n}", "message": {"role": k, "content": c},
                                          "timestamp": (BASE + timedelta(minutes=m)).strftime("%Y-%m-%dT%H:%M:%SZ")}) + "\n" for k, m, c in rows))
    return state, projects


def stage(env, capsys, *extra, now=NOW):
    code = rca.main(["--state-dir", str(env[0]), "--projects-root", str(env[1]), "stage", *extra], now=now)
    out = capsys.readouterr()
    return code, out.out, out.err


def report(env, since=BASE - timedelta(hours=1)):
    return osr.build_report(env[0], env[1], since, NOW, now=NOW)


def pending(state):
    return sorted(p.name for p in (state.parent / "dispatches" / "pending").glob("*"))


def test_case_keeps_its_id_when_its_anchor_leaves_the_window_and_a_second_stage_stages_nothing(env, capsys):
    assert cl.case_id is osr.case_id  # the one case_id (OB-4b), imported, never a copy
    first = report(env)["gevallen"][0]
    assert stage(env, capsys, "--case", first["case_id"])[0] == 0
    later = report(env, since=BASE + timedelta(minutes=50))
    entry = later["gevallen"][0]
    assert entry["case_id"] != first["case_id"] and first["anker_prompt_id"] not in entry["prompt_ids"]
    pairs, new = cl.cases(later, cl.read_ledger(env[0], PID), NOW)
    assert pairs[0][1].case_id == first["case_id"] and new == []
    for args in (("--case", first["case_id"]), ("--case", entry["case_id"], "--since", (BASE + timedelta(minutes=50)).isoformat())):
        code, out, err = stage(env, capsys, *args)
        assert code == 1 and out == "" and f"al een open RCA steering-rca-{first['case_id'][:12]}-2026w46-1" in err
    assert len(pending(env[0])) == 1 and sum(len(c.staged) for c in cl.read_ledger(env[0], PID).values()) == 1


def test_after_rca_refused_the_case_may_be_staged_again(env, capsys):
    cid = report(env)["gevallen"][0]["case_id"]
    assert stage(env, capsys, "--case", cid)[0] == 0
    first = cl.read_ledger(env[0], PID)[cid].open_dispatch
    with pytest.raises(ValueError, match="open staat"):
        cl.book(env[0], PID, [cl.event("rca_refused", cid, PID, NOW, dispatch_id="ander-id", reden="x")])
    cl.book(env[0], PID, [cl.event("rca_refused", cid, PID, NOW, dispatch_id=first, reden="ongeldig blok")])
    assert stage(env, capsys, "--case", cid)[0] == 0 and pending(env[0]) == [first, first[:-1] + "2"]


def _lines(state):
    return [json.loads(line) for line in (state / cl.LEDGER_FILE).read_text().splitlines()]


def test_rca_staged_is_in_the_ledger_before_stage_fn_publishes(env):
    rep, seen = report(env), []  # stage_fn records the last ledger line it sees and the ids it was handed
    _case, did = cl.stage_case(env[0], rep, rep["gevallen"][0]["case_id"], NOW, lambda _c, _e, d, s: seen.append((_lines(env[0])[-1], d, s)))
    assert [(r["event"], r.get("dispatch_id"), r.get("snapshot_sha256")) == ("rca_staged", d, s) for r, d, s in seen] == [True] and seen[0][1] == did


@pytest.mark.parametrize("published", [False, True])
def test_a_failed_publish_exits_non_zero_and_books_stage_failed_unless_its_spec_is_on_disk(env, capsys, monkeypatch, published):
    cid, real = report(env)["gevallen"][0]["case_id"], rca.stage_spec_bundle  # published: the real bundle is written, then the raise
    monkeypatch.setattr(rca, "stage_spec_bundle", lambda **kw: (_ for _ in ()).throw(OSError(f"schijf vol ({published and real(**kw)})")))
    code, _, err = stage(env, capsys, "--case", cid)
    did = next(r["dispatch_id"] for r in _lines(env[0]) if r["event"] == "rca_staged")
    refused = [(r["dispatch_id"], r["reden"]) for r in _lines(env[0]) if r["event"] == "rca_refused"]
    assert code == 1 and "schijf vol" in err and pending(env[0]) == [did] * published and refused == [(did, "stage_failed")] * (not published)
    monkeypatch.undo()  # without a spec the case is stageable again; with one it is the open RCA
    assert stage(env, capsys, "--case", cid)[0] == int(published) and pending(env[0]) == [did[:-1] + ("1" if published else "2")]


def test_an_interrupted_staging_is_published_again_with_the_same_ids_and_books_nothing(env, capsys):
    rep, ledger = report(env), env[0] / cl.LEDGER_FILE
    with pytest.raises(SystemExit):  # the process dies between the ledger append and the publish
        cl.stage_case(env[0], rep, rep["gevallen"][0]["case_id"], NOW, lambda *_: sys.exit("crash"))
    before, (staged,) = ledger.read_text(), [s for c in cl.read_ledger(env[0], PID).values() for s in c.staged]
    did, cid = staged["dispatch_id"], staged["case_id"]
    assert pending(env[0]) == [] and stage(env, capsys, "--case", cid)[0] == 0 and ledger.read_text() == before
    spec = json.loads(cl.bundle_spec(env[0], did).read_text())
    assert (spec["dispatch_id"], pending(env[0]), spec["tags"][-1]) == (did, [did], f"snapshot_sha256:{staged['snapshot_sha256']}")
    code, _, err = stage(env, capsys, "--case", cid)
    assert code == 1 and f"al een open RCA {did}" in err and ledger.read_text() == before


def test_sixth_case_in_an_iso_week_is_refused_and_the_next_utc_week_resets(env, capsys):
    assert (cl.iso_week(NOW), cl.iso_week(NEXT_WEEK)) == ("2026-W46", "2026-W47")
    assert cl.iso_week(NEXT_WEEK.replace(tzinfo=timezone(timedelta(hours=1)))) == "2026-W46"
    ids = [(cl.case_id(PID, "correctie", f"{n:016x}"), f"{n:016x}") for n in range(5)]
    cl.book(env[0], PID, [e for cid, a in ids for e in (cl.event("case_opened", cid, PID, NOW, klasse="correctie", anker_prompt_id=a),
                          cl.event("rca_staged", cid, PID, NOW, dispatch_id=f"d{a}", snapshot_sha256="0" * 64, iso_week="2026-W46"))])
    cid = report(env)["gevallen"][0]["case_id"]
    for _ in range(2):
        code, _, err = stage(env, capsys, "--case", cid)
        assert code == 1 and "weekplafond bereikt: 5 van 5 rca_staged in 2026-W46" in err and pending(env[0]) == []
    assert stage(env, capsys, "--case", cid, now=NEXT_WEEK)[0] == 0 and pending(env[0]) == [f"steering-rca-{cid[:12]}-2026w47-1"]
    assert [s["iso_week"] for c in cl.read_ledger(env[0], PID).values() for s in c.staged] == ["2026-W46"] * 5 + ["2026-W47"]


_OPEN = cl.event("case_opened", cl.case_id(PID, "correctie", "a" * 16), PID, NOW, klasse="correctie", anker_prompt_id="a" * 16)
_BAD = ["{niet json", {"event": "onbekend"}, {"event": "case_members", "prompt_ids": None}, {"case_id": None},
        {"event": "case_members", "ts": None, "prompt_ids": ["p"]}, {"project_id": "ander"}, {"event": "rca_ingested", "dispatch_id": "d1"},
        {"event": "rca_staged", "dispatch_id": "d1", "snapshot_sha256": "kort", "iso_week": "2026-W46"}, {}]


@pytest.mark.parametrize("bad", _BAD)
def test_a_corrupt_ledger_line_fails_loud_with_its_line_number(env, capsys, bad):
    path = env[0] / cl.LEDGER_FILE
    path.write_text(json.dumps(_OPEN) + "\n" + (bad if isinstance(bad, str) else json.dumps({**_OPEN, **bad})) + "\n")
    before = path.read_text()
    with pytest.raises(cl.LedgerUnreadable, match=r"steering_cases\.ndjson:2: "):
        cl.read_ledger(env[0], PID)
    code, _, err = stage(env, capsys, "--case", report(env)["gevallen"][0]["case_id"])
    assert code == 1 and ":2: " in err and pending(env[0]) == [] and path.read_text() == before


def test_no_prompt_text_in_spec_instruction_or_ledger_and_show_reads_it_live(env, capsys):
    state, projects = env
    cid = report(env)["gevallen"][0]["case_id"]
    code, dry, _ = stage(env, capsys, "--case", cid, "--dry-run")
    assert code == 0 and "steering_rca/1" in dry and not (state / cl.LEDGER_FILE).exists() and pending(state) == []
    assert stage(env, capsys, "--case", cid)[0] == 0
    bundle = state.parent / "dispatches" / "pending" / pending(state)[0]
    spec = json.loads((bundle / "dispatch-spec.json").read_text())
    assert (spec["role"], spec["model"], spec["provider"], spec["project_id"]) == ("research-analyst", "sonnet", "claude", PID)
    written = dry + "".join(p.read_text() for p in (bundle / "dispatch-spec.json", bundle / "instruction.md", state / cl.LEDGER_FILE))
    assert [TEXT[i:i + 40] for i in range(len(TEXT) - 39) if TEXT[i:i + 40] in written] == [] and COMMAND not in written
    block = json.loads((bundle / "instruction.md").read_text().split("```json\n")[1].split("```")[0])
    assert f"snapshot_sha256:{block['snapshot_sha256']}" in spec["tags"] and block["context"][0]["tool_call_ids"]
    show = ["--state-dir", str(state), "--projects-root", str(projects), "show", "--case", block["show_case_id"],
            "--since", block["venster"]["since"], "--until", block["venster"]["until"]]
    assert steering_cli.main(show) == 0 and TEXT in capsys.readouterr().out
