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
    assert proj["decide_command"] == "vnx dream review dream:one"
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
    assert ma.decide_command("analyzer", "p", "edit:1") == "python3 scripts/apply_suggested_edits.py edit:1"
    assert ma.decide_command("promote", "p", "dlv:abc") == "vnx promote abc"
    assert ma.decide_command("steering", "p", "x") is None
