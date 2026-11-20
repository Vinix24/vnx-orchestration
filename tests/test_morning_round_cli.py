"""ochtendronde D2c: vnx morning-round agenda and the pinned schema morning_agenda/1."""
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts" / "lib"))

import morning_agenda as ma
import ochtendronde_agenda as ag
import operator_queue as oq

P, DAY = "proj-a", "2026-11-19"
NOW = datetime(2026, 11, 19, 7, 0, tzinfo=timezone.utc)
CLI = ROOT / "scripts" / "morning_round_cli.py"


@pytest.fixture
def env(tmp_path):
    sd = tmp_path / "state"
    pids = {}
    for name, bron, ref in (("proj", 3, "dream:one"), ("fleet", 8, "antwoord:abc")):
        oq.open_point(P, bron=bron, ref=ref, vraag=f"q {name}", sluit_bij="x", opened_by="t",
                      asked_at="2026-11-19T06:00:00Z", state_dir=sd)
        pids[name] = oq.point_id(P, bron, ref, 1)
    ag.write_snapshot(P, DAY, state_dir=sd, now="2026-11-19T06:30:00Z")
    snap = sd / "ochtendronde" / f"{DAY}.json"
    data = json.loads(snap.read_text())
    for point in data["points"]:
        point["klasse"] = "project" if point["point_id"] == pids["proj"] else "fleet"
        point["advies"] = "approve it" if point["klasse"] == "project" else ""
    snap.write_text(json.dumps(data))
    oq.close_point(P, pids["fleet"], uitkomst="beantwoord", bewijs="e", state_dir=sd)
    reg = tmp_path / "projects.json"
    reg.write_text(json.dumps({"projects": [{"project_id": P, "data_dir": str(tmp_path / "d")}]}))
    return sd, reg, pids


def run(sd, reg, *extra, project=P, day=DAY):
    return subprocess.run([sys.executable, str(CLI), "agenda", "--project-id", project, "--day", day,
                           "--state-dir", str(sd), "--registry", str(reg), *extra], capture_output=True, text=True)


def test_json_matches_the_pinned_schema(env):
    sd, reg, pids = env
    result = run(sd, reg, "--json")
    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout)
    assert tuple(out) == tuple(sorted(ma.TOP_KEYS)) and out["schema"] == "morning_agenda/1"
    assert out["snapshot_written_at"] == "2026-11-19T06:30:00Z" and out["day"] == DAY
    by_id = {p["point_id"]: p for p in out["points"]}
    assert all(tuple(p) == tuple(sorted(ma.POINT_KEYS)) for p in by_id.values())
    proj, fleet = by_id[pids["proj"]], by_id[pids["fleet"]]
    assert (proj["status"], proj["classification"], proj["advice"], proj["source"]) == ("open", "project", "approve it", "dream")
    assert (fleet["status"], fleet["classification"], fleet["advice"]) == ("decided", "fleet", None)
    assert proj["decide_command"] == "vnx dream review one --approve"
    assert fleet["decide_command"].startswith(f"vnx inbox answer {pids['fleet']} --quote")
    assert isinstance(proj["age_seconds"], int) and proj["age_seconds"] >= 0


def test_text_output_one_line_per_point(env):
    sd, reg, pids = env
    result = run(sd, reg)
    assert result.returncode == 0
    lines = result.stdout.splitlines()
    assert len(lines) == 3
    by_id = {line.split()[0]: line for line in lines[1:]}
    assert "dream project" in by_id[pids["proj"]] and " open: q proj | advice: approve it" in by_id[pids["proj"]]
    assert "question fleet" in by_id[pids["fleet"]] and " decided: q fleet" in by_id[pids["fleet"]]
    assert "advice" not in by_id[pids["fleet"]]


def test_missing_snapshot_is_nonzero_and_not_an_empty_agenda(env):
    sd, reg, _ = env
    result = run(sd, reg, day="2026-11-18")
    assert result.returncode == 1 and "no snapshot for this day" in result.stderr and result.stdout == ""


def test_unreadable_ledger_names_the_file(env):
    sd, reg, _ = env
    with open(sd / oq.LEDGER_FILENAME, "a") as handle:
        handle.write("garbage\n")
    result = run(sd, reg, "--json")
    assert result.returncode == 1 and oq.LEDGER_FILENAME in result.stderr and result.stdout == ""


def test_unknown_project_is_refused(env):
    sd, reg, _ = env
    result = run(sd, reg, project="proj-zzz")
    assert result.returncode == 2 and "unknown project id" in result.stderr and result.stdout == ""


def test_unreadable_register_and_bad_day_are_refused(env, tmp_path):
    sd, _, _ = env
    assert run(sd, tmp_path / "missing.json").returncode == 2
    assert run(sd, tmp_path / "missing.json", day="not-a-day").returncode == 2


def test_point_missing_from_the_ledger_is_not_shown_as_open(env):
    sd, _, pids = env
    (sd / oq.LEDGER_FILENAME).write_text("")
    with pytest.raises(ma.AgendaUnreadable, match="status"):
        ma.build_agenda(P, DAY, state_dir=sd, now=NOW)


def test_decide_command_by_source():
    assert ma.decide_command("analyzer", "p", "edit:1") == "python3 scripts/apply_suggested_edits.py accept 1"
    assert ma.decide_command("promote", "p", "dlv:abc") == "vnx deliverable promote abc"
    assert ma.decide_command("dream", "p", "dream:c1") == "vnx dream review c1 --approve"
    assert ma.decide_command("steering", "p", "x") is None


def test_dream_ref_as_the_source_opens_it_gives_the_bare_cycle_id():
    from operator_queue_dream_analyzer import REVIEW_SUFFIX
    ref = f"dream:c1{REVIEW_SUFFIX}"
    assert ma.decide_command("dream", "p", ref) == "vnx dream review c1 --approve"
    assert ma.decide_command("dream", "p", "dream:c1") == "vnx dream review c1 --approve"
    assert ma.decide_command("dream", "p", f"dream:{REVIEW_SUFFIX}") is None


def test_analyzer_refs_by_form():
    assert ma.decide_command("analyzer", "p", "edit:7") == "python3 scripts/apply_suggested_edits.py accept 7"
    assert ma.decide_command("analyzer", "p", "archival:pat-9") == 'vnx learning approve pat-9 --approval-id "..." --reason "..."'
    for ref in ("other:1", "edit:", "archival:", "7", "dream:c1"):
        assert ma.decide_command("analyzer", "p", ref) is None, ref


def test_routed_fleet_refs_have_no_command():
    from operator_queue_dream_analyzer import REVIEW_SUFFIX
    for source, ref in (("dream", f"project:owner/dream:c1{REVIEW_SUFFIX}"), ("analyzer", "project:owner/edit:7"),
                        ("analyzer", "project:owner/archival:pat-9")):
        assert ma.decide_command(source, "p", ref) is None, ref


def test_archival_command_parses_in_the_real_cli(monkeypatch):
    import argparse
    import shlex
    import vnx_cli.main as vm
    argv = shlex.split(ma.decide_command("analyzer", "p", "archival:pat-9"))
    captured = {}
    real = argparse.ArgumentParser.parse_args

    def grab(self, args=None, namespace=None):
        captured["ns"] = real(self, args, namespace)
        raise KeyboardInterrupt

    monkeypatch.setattr(argparse.ArgumentParser, "parse_args", grab)
    monkeypatch.setattr(sys, "argv", argv)
    with pytest.raises(KeyboardInterrupt):
        vm.main()
    ns = captured["ns"]
    assert (ns.learning_subcommand, ns.pattern_id) == ("approve", "pat-9")


@pytest.mark.parametrize("source,ref", [("promote", "dlv:abc"), ("analyzer", "edit:1"), ("dream", "dream:c1"),
                                         ("decision", "pr:3073"), ("question", "antwoord:0123456789ab")])
def test_every_decide_command_parses_in_the_real_cli(source, ref, monkeypatch):
    cmd = ma.decide_command(source, "p1", ref)
    assert cmd, source
    import shlex
    argv = shlex.split(cmd)
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    if argv[:2] == ["vnx", "deliverable"]:
        import planning_cli
        ns = planning_cli._build_parser().parse_args(argv[1:])
        assert (ns.action, ns.dispatch_id) == ("promote", "abc")
    elif argv[:2] == ["vnx", "inbox"]:
        import inbox_cli
        ns = inbox_cli._parser().parse_args(argv[2:])
        assert (ns.cmd, ns.point_id) == ("answer", "p1")
    elif argv[:2] == ["vnx", "dream"]:
        import argparse
        import vnx_cli.main as vm
        captured = {}
        real = argparse.ArgumentParser.parse_args

        def grab(self, args=None, namespace=None):
            captured["ns"] = real(self, args, namespace)
            raise KeyboardInterrupt

        monkeypatch.setattr(argparse.ArgumentParser, "parse_args", grab)
        monkeypatch.setattr(sys, "argv", argv)
        with pytest.raises(KeyboardInterrupt):
            vm.main()
        ns = captured["ns"]
        assert (ns.cycle_id, ns.approve, ns.reject) == ("c1", True, False)
    else:
        import argparse
        import apply_suggested_edits as ase
        captured = {}
        real = argparse.ArgumentParser.parse_args

        def grab(self, args=None, namespace=None):
            captured["ns"] = real(self, args, namespace)
            raise KeyboardInterrupt

        monkeypatch.setattr(argparse.ArgumentParser, "parse_args", grab)
        monkeypatch.setattr(sys, "argv", argv[1:])
        with pytest.raises(KeyboardInterrupt):
            ase.main()
        assert (captured["ns"].command, captured["ns"].ids) == ("accept", "1")


def test_decide_command_is_printed_only_for_a_ref_the_inbox_answers(tmp_path):
    import operator_queue_ask as oqa
    sd = tmp_path / "state"
    refs = {"antwoord:0123456789ab": (8, None), "pr:3073": (2, {"ceiling": 5, "reason": "one_working_unit"}), "oi:7": (2, None),
            "track:t1": (2, None), "config:k": (2, None), "verdict:v1": (2, None)}
    seen = {}
    for ref, (bron, extra) in refs.items():
        assert oq.open_point(P, bron=bron, ref=ref, vraag="q", sluit_bij="x", opened_by="t", state_dir=sd, extra=extra)
        seen[ref] = oq.point_id(P, bron, ref, 1)
    for ref, pid in seen.items():
        source = "question" if ref.startswith("antwoord:") else "decision"
        cmd = ma.decide_command(source, pid, ref)
        if ref.startswith(("antwoord:", "pr:")):
            assert cmd == f'vnx inbox answer {pid} --quote "..."', ref
            assert oqa.answer(P, pid, quote="yes", vastgelegd_door="t", state_dir=sd), ref
        else:
            assert cmd is None, ref
            with pytest.raises(oq.QueueRefused, match="the source decides"):
                oqa.answer(P, pid, quote="yes", vastgelegd_door="t", state_dir=sd)


def test_point_missing_on_the_second_read_is_agenda_unreadable(env, monkeypatch):
    sd, _, pids = env
    real = oq.read_state

    def second_read_drops_a_point(project_id, **kw):
        state = real(project_id, **kw)
        state["points"] = {k: v for k, v in state["points"].items() if k != pids["proj"]}
        return state

    monkeypatch.setattr(ma.oq, "read_state", second_read_drops_a_point)
    with pytest.raises(ma.AgendaUnreadable, match=pids["proj"]):
        ma.build_agenda(P, DAY, state_dir=sd, now=NOW)
