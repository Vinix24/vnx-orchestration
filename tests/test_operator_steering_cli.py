"""OB-4b: `report`, `show --case` and the `vnx learning steering` route (ADR-072 decisions 2, 11, 12, 16).

Every prompt, note and command below is invented. Fixtures live under tmp_path; no live store,
transcript or ~/.claude path is opened.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts" / "lib"))
sys.path.insert(0, str(REPO / "scripts"))

import operator_steering as cli
import operator_steering_report as osr
import operator_steering_source as src
from operator_steering_class import STEERING_CLASSES

SLUG = "-proj-vnx"
TEXT = "hoe ver zijn we met de migratie van de rapportagemodule"
COMMAND = "uniek-commando-xyz --flag"


def _ts(minutes: int) -> str:
    base = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(hours=12)
    return (base + timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _user(uuid, text, minutes, session):
    return {"type": "user", "uuid": uuid, "sessionId": session, "timestamp": _ts(minutes),
            "message": {"role": "user", "content": text}}


def _assistant(call_id, minutes, session):
    block = [{"type": "text", "text": "ik start dispatch dlv-0123456789ab voor #2721"},
             {"type": "tool_use", "id": call_id, "name": "Bash", "input": {"command": COMMAND + "\nregel twee"}}]
    return {"type": "assistant", "uuid": "a-" + call_id, "sessionId": session, "timestamp": _ts(minutes),
            "message": {"role": "assistant", "content": block}}


@pytest.fixture
def env(tmp_path):
    (tmp_path / "proj").mkdir()
    (tmp_path / "proj" / ".vnx-project-id").write_text("vnx-dev\n")
    tdir = tmp_path / "projects" / SLUG
    (tdir / "memory").mkdir(parents=True)
    state, projects = tmp_path / "proj" / "state", tmp_path / "projects"
    src.bind(state, projects, [SLUG], tdir / "memory")
    for n, session in enumerate(("s1", "s2", "s3")):
        rows = [_assistant(f"t{n}", n * 100, session), _user(f"u{n}", TEXT + " " * n + "!" * n, n * 100 + 1, session)]
        (tdir / f"{session}.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    return state, projects, tdir


def run(env, capsys, *args):
    state, projects, _ = env
    code = cli.main(["--state-dir", str(state), "--projects-root", str(projects), *args])
    out = capsys.readouterr()
    return code, out.out, out.err


def listing(path: Path):
    return sorted(str(p.relative_to(path)) for p in path.rglob("*"))


def test_red_run_marker_old_main_had_no_report_command():
    # Red run on origin/main (26e063130): `python3 scripts/operator_steering.py report` gave an argparse
    # "invalid choice: 'report'" and `vnx learning steering report` printed "Unknown command". Here: it exists.
    assert hasattr(cli, "_cmd_report") and hasattr(osr, "case_id")


def test_case_id_is_pinned_and_stable():
    first = osr.case_id("vnx-dev", "statusvraag", "0123456789abcdef")
    assert first == osr.case_id("vnx-dev", "statusvraag", "0123456789abcdef")
    assert first == "e1d48b43a3401c14e4ea1e740db4a3230e7f04d0f268c37d5c34d377c3e2942d"
    assert first != osr.case_id("vnx-dev", "correctie", "0123456789abcdef")
    assert first != osr.case_id("other", "statusvraag", "0123456789abcdef")


def test_report_json_has_all_classes_coverage_and_case_ids(env, capsys):
    code, out, _ = run(env, capsys, "report", "--since", "3d", "--json")
    assert code == 0
    report = json.loads(out)
    assert set(report["tellingen"]) >= {c.name for c in STEERING_CLASSES}
    assert "uitgesloten" in report and isinstance(report["dekking"], float)
    assert report["gevallen"], "three sessions of the same status question are a case"
    ids = [c["case_id"] for c in report["gevallen"]]
    assert all(len(i) == 64 for i in ids)
    again = json.loads(run(env, capsys, "report", "--since", "3d", "--json")[1])
    assert [c["case_id"] for c in again["gevallen"]] == ids


def test_show_flag_prints_text_but_the_written_report_holds_none(env, capsys):
    state, _, _ = env
    code, out, _ = run(env, capsys, "report", "--since", "3d", "--show")
    assert code == 0 and TEXT in out
    written = (state / osr.REPORT_FILE).read_text()
    assert TEXT not in written and COMMAND not in written and "rapportagemodule" not in written
    code, out, _ = run(env, capsys, "report", "--since", "3d", "--md")
    assert code == 0 and TEXT not in out and "| statusvraag |" in out


def test_show_case_prints_text_and_context_and_writes_nothing(env, capsys):
    state, _, tdir = env
    report = json.loads(run(env, capsys, "report", "--since", "3d", "--json")[1])
    case = report["gevallen"][0]
    before = (listing(state), listing(tdir.parent))
    code, out, _ = run(env, capsys, "show", "--case", case["case_id"], "--since", "3d")
    assert code == 0
    assert TEXT in out and case["prompt_ids"][0] in out
    assert f"Bash: {COMMAND}" in out and "regel twee" not in out
    assert "dlv-0123456789ab" in out and "#2721" in out
    assert (listing(state), listing(tdir.parent)) == before
    code, out, err = run(env, capsys, "show", "--case", "0" * 64, "--since", "3d")
    assert code == 1 and out == "" and len(err.strip().splitlines()) == 1


def test_symlinked_transcript_is_an_error_and_prints_no_text(env, capsys, tmp_path):
    state, _, tdir = env
    outside = tmp_path / "buiten.jsonl"
    outside.write_text(json.dumps(_user("x1", "BUITEN-ZIN-niet-tonen", 5, "sx")) + "\n")
    good = json.loads(run(env, capsys, "report", "--since", "3d", "--json")[1])["gevallen"][0]["case_id"]
    (tdir / "link.jsonl").symlink_to(outside)
    code, out, err = run(env, capsys, "report", "--since", "3d", "--show")
    assert code == 2 and "error" in err and "BUITEN-ZIN" not in out + err
    assert json.loads((state / osr.REPORT_FILE).read_text())["bronstaat"]["transcripts"]["state"] == "error"
    code, out, err = run(env, capsys, "show", "--case", good, "--since", "3d")
    assert code == 1 and out == "" and "BUITEN-ZIN" not in err


def test_unbound_source_prints_its_state_and_exits_2(tmp_path, capsys):
    (tmp_path / "projects").mkdir()
    code = cli.main(["--state-dir", str(tmp_path / "state"), "--projects-root", str(tmp_path / "projects"),
                     "report", "--since", "3d", "--json"])
    out = capsys.readouterr()
    assert code == 2 and "unbound" in out.err and out.out == ""


def test_bad_window_is_a_usage_error(env, capsys):
    code, out, err = run(env, capsys, "report", "--since", "gisteren", "--json")
    assert code == 2 and "tijdvenster" in err and out == ""


def test_learning_route_reaches_the_same_output(env, capsys, monkeypatch):
    import argparse

    from vnx_cli.main import _register_learning_subparser
    from vnx_cli.commands.learning import vnx_learning

    def parse(argv):
        parser = argparse.ArgumentParser(prog="vnx")
        _register_learning_subparser(parser.add_subparsers(dest="command"))
        return parser.parse_args(argv)

    state, projects, _ = env
    direct = run(env, capsys, "report", "--since", "3d", "--json")[1]
    parsed = parse(
        ["learning", "steering", "--", "--state-dir", str(state), "--projects-root", str(projects),
         "report", "--since", "3d", "--json"])
    assert vnx_learning(parsed) == 0
    routed = capsys.readouterr().out
    assert json.loads(routed)["gevallen"] == json.loads(direct)["gevallen"]
    bad = parse(["learning", "steering"])
    assert vnx_learning(bad) == 2


def _low_coverage_env(env):
    _, _, tdir = env
    rows = [_user(f"lc{n}", "alfa beta gamma delta", 400 + n, "slc") for n in range(20)]
    (tdir / "slc.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n")


def test_low_coverage_is_a_report_not_an_error(env, capsys):
    _low_coverage_env(env)
    code, out, err = run(env, capsys, "report", "--since", "3d", "--json")
    report = json.loads(out)
    assert code == 0 and report["status"] == "low_coverage" and "low_coverage" in err
    case = report["gevallen"][0]["case_id"]
    code, out, _ = run(env, capsys, "show", "--case", case, "--since", "3d")
    assert code == 0 and out.startswith(f"case {case}")


@pytest.mark.parametrize("command", [("report", "--since", "3d", "--json"), ("show", "--case", "x", "--since", "3d")])
def test_a_vanished_memory_dir_refuses_both_commands(env, capsys, command):
    import shutil

    shutil.rmtree(env[2] / "memory")
    code, out, err = run(env, capsys, *command)
    assert code == (2 if command[0] == "report" else 1) and "memory absent" in err and out == ""


def test_learning_route_honours_global_project_dir(env, capsys, tmp_path, monkeypatch):
    import argparse

    from vnx_cli.main import _register_learning_subparser
    from vnx_cli.commands.learning import vnx_learning

    state = env[0]
    monkeypatch.setattr("vnx_cli.commands.learning._resolve_state_dir", lambda project_dir: state if project_dir == tmp_path / "proj" else tmp_path / "elders")
    monkeypatch.chdir(tmp_path)
    parser = argparse.ArgumentParser(prog="vnx")
    _register_learning_subparser(parser.add_subparsers(dest="command"))
    args = parser.parse_args(["learning", "--project-dir", str(tmp_path / "proj"), "steering", "--", "--projects-root", str(env[1]), "report", "--since", "3d", "--json"])
    assert vnx_learning(args) == 0
    assert json.loads(capsys.readouterr().out)["status"] in ("ok", "low_coverage")
