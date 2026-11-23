"""``vnx doctor`` carries the dream checks of ADR-066 (dream PR-6).

Hermetic: HOME points at ``tmp_path`` (plist, data root and events live there) and the
one subprocess seam of ``dream_liveness`` is patched, so the real ``launchctl`` is never asked.
"""
import argparse
import json
import plistlib
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "lib"))

import dream_liveness as dl
from vnx_cli.commands import doctor
from vnx_cli.commands.doctor import PASS, WARN, vnx_doctor

PROJECT_ID = "dreamtest"
NAMES = {"dream:liveness", "dream:job", "dream:reviews"}


def _run_seam(loaded=True):
    real = subprocess.run

    def run(argv, timeout=dl.SUBPROCESS_TIMEOUT):
        if argv[0] == "launchctl":
            return (0, "last exit code = 0") if loaded else (113, "")
        proc = real(argv, capture_output=True, text=True, timeout=timeout, check=False)
        return proc.returncode, proc.stdout
    return run


def _local_stamp(days_ago: int) -> str:
    day = datetime.now().astimezone() - timedelta(days=days_ago)
    return day.replace(hour=3, minute=0, second=30, microsecond=0).isoformat()


def _fixture(tmp_path, monkeypatch, last_night=None, loaded=True):
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    data_root = home / ".vnx-data" / PROJECT_ID
    events = data_root / "events" / "dream"
    events.mkdir(parents=True)
    log = events / "launchd.out.log"
    plist = {"Label": dl.PLIST_LABEL,
             "ProgramArguments": ["/bin/vnx", "dream", "run", "--project-id", PROJECT_ID],
             "EnvironmentVariables": {"VNX_DREAM_SCHEDULER_ENABLED": "1"},
             "StartCalendarInterval": {"Hour": 3, "Minute": 0},
             "StandardOutPath": str(log), "StandardErrorPath": str(events / "launchd.err.log")}
    agents = home / "Library" / "LaunchAgents"
    agents.mkdir(parents=True)
    (agents / f"{dl.PLIST_LABEL}.plist").write_bytes(plistlib.dumps(plist))
    lines = []
    # tonight's window opens at 03:00 local: once it has opened it needs its own attempt
    opened = datetime.now().astimezone().replace(hour=3, minute=0, second=30, microsecond=0)
    first = 0 if datetime.now().astimezone() > opened + timedelta(seconds=30) else 1
    for ago in range(first, 9):
        cycle = f"dream-{ago}"
        lines.append(f"dream run: cycle={cycle} project={PROJECT_ID}")
        event = {"event_type": "dream_cycle_completed", "cycle_id": cycle,
                 "timestamp": _local_stamp(ago), "merged_count": 1}
        if ago == first and last_night:
            event = {"event_type": "dream_cycle_skipped", "cycle_id": cycle,
                     "timestamp": _local_stamp(ago), "reason": last_night}
        (events / f"{ago}.ndjson").write_text(json.dumps(event) + "\n")
    log.write_text("\n".join(lines) + "\n")
    monkeypatch.setattr(dl, "_run", _run_seam(loaded))
    project = tmp_path / "proj"
    project.mkdir()
    return project


def _doctor(project, capsys, strict=False):
    code = vnx_doctor(argparse.Namespace(project_dir=str(project), json=True, strict=strict))
    return code, json.loads(capsys.readouterr().out)["checks"]


def _dream(checks):
    return {c["name"]: c for c in checks if c["name"].startswith("dream")}


def test_the_three_dream_checks_are_in_the_check_list(tmp_path, monkeypatch, capsys):
    project = _fixture(tmp_path, monkeypatch)
    _, checks = _doctor(project, capsys)
    assert NAMES <= {c["name"] for c in checks}


def test_blocked_skip_as_last_night_warns_and_strict_exits_one(tmp_path, monkeypatch, capsys):
    project = _fixture(tmp_path, monkeypatch, last_night="scheduler_disabled")
    code, checks = _doctor(project, capsys, strict=True)
    liveness = _dream(checks)["dream:liveness"]
    assert liveness["status"] == WARN and "blocked_skip" in liveness["detail"]
    assert code == 1


def test_every_source_fine_and_a_night_with_progress_is_three_pass(tmp_path, monkeypatch, capsys):
    project = _fixture(tmp_path, monkeypatch)
    code, checks = _doctor(project, capsys)
    found = _dream(checks)
    assert {n: c["status"] for n, c in found.items()} == {n: PASS for n in NAMES}, found
    # the dream checks add no failure: the exit code follows the other checks alone
    assert code == (1 if any(c["status"] == "FAIL" for c in checks) else 0)


def test_job_not_loaded_warns_only_the_job_check(tmp_path, monkeypatch, capsys):
    project = _fixture(tmp_path, monkeypatch, loaded=False)
    _, checks = _doctor(project, capsys)
    found = _dream(checks)
    assert found["dream:job"]["status"] == WARN and "job_not_loaded" in found["dream:job"]["detail"]
    assert found["dream:reviews"]["status"] == PASS


def test_unreadable_review_warns_only_the_reviews_check(tmp_path, monkeypatch, capsys):
    project = _fixture(tmp_path, monkeypatch)
    review_dir = tmp_path / "home" / ".vnx-data" / PROJECT_ID / "state" / "dream"
    review_dir.mkdir(parents=True)
    (review_dir / "x-pending-review.json").write_text("{not json")
    _, checks = _doctor(project, capsys)
    found = _dream(checks)
    assert found["dream:reviews"]["status"] == WARN and "unreadable=1" in found["dream:reviews"]["detail"]
    assert found["dream:job"]["status"] == PASS


@pytest.mark.parametrize("hide", ["import", "assess", "load_sources"])
def test_unmeasured_never_passes_and_never_crashes_the_doctor(tmp_path, monkeypatch, capsys, hide):
    project = _fixture(tmp_path, monkeypatch)
    if hide == "import":
        monkeypatch.setitem(sys.modules, "dream_liveness", None)
    else:
        def boom(*args, **kwargs):
            raise RuntimeError("source read failed")
        monkeypatch.setattr(dl, hide, boom)
    _, checks = _doctor(project, capsys)
    found = _dream(checks)
    assert set(found) == NAMES
    assert all(c["status"] == WARN and c["detail"].startswith("unmeasured:") for c in found.values())
    assert any(not c["name"].startswith("dream") for c in checks)


def test_assess_is_called_once_per_doctor_run(tmp_path, monkeypatch, capsys):
    project = _fixture(tmp_path, monkeypatch)
    calls = []
    real = dl.assess
    monkeypatch.setattr(dl, "assess", lambda *a, **k: calls.append(1) or real(*a, **k))
    _doctor(project, capsys)
    assert len(calls) == 1


@pytest.mark.parametrize("doc", [
    {}, {"state": None}, {"state": "unknown", "findings": []}, {"state": "ok", "findings": None},
    {"state": "ok", "sources": {"events": "unreadable"}, "findings": []},
    {"state": "ok", "sources": {"events": "ok"}, "findings": [{"id": "never_heard_of", "measured": None}]},
])
def test_liveness_projection_stays_blocking_on_missing_or_malformed_evidence(doc):
    assert doctor._dream_check("dream:liveness", doc).status == WARN


@pytest.mark.parametrize("name,keys", [("dream:job", ("plist", "log")), ("dream:reviews", ("reviews", "db"))])
@pytest.mark.parametrize("value", [None, "unreadable", "missing", "unknown"])
def test_group_projection_is_blocking_when_a_needed_source_is_not_ok(name, keys, value):
    sources = {k: "ok" for k in keys}
    sources[keys[0]] = value
    assert doctor._dream_check(name, {"state": "ok", "sources": sources, "findings": []}).status == WARN
    sources[keys[0]] = "ok"
    assert doctor._dream_check(name, {"state": "ok", "sources": sources, "findings": []}).status == PASS
