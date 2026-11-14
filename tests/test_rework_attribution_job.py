"""Nightly rework attribution job (ADR-071 besluit 7 and 13): beacon, catch-up, installer.

`tmp_path` only. Both steps are patched at the `_run_step` seam; launchctl is never run.
"""
from __future__ import annotations

import json
import plistlib
import shutil
import sqlite3
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
for sub in ("scripts", "scripts/lib", "scripts/launchd"):
    sys.path.insert(0, str(REPO_ROOT / sub))

import launchd_project_scope as lps  # noqa: E402
import rework_attribution_job as job  # noqa: E402
import rework_blame_edges as blame  # noqa: E402

PID = "vnx-dev"
SHAS = [c * 40 for c in "abcd"]


def _beacon(data: Path) -> dict:
    return json.loads((data / "health" / f"{job.COMPONENT_NAME}.json").read_text(encoding="utf-8"))


def _steps(monkeypatch, attribute_rc=0, report_rc=0):
    calls = []

    def fake(script, args):
        calls.append((script, args))
        if script == "attribute_rework.py":
            return attribute_rc, json.dumps({"stopped": "git failed"} if attribute_rc else {"edges_written": 1})
        if report_rc == 0:
            Path(args[args.index("--out") + 1]).write_text("{}", encoding="utf-8")
        return report_rc, "" if report_rc == 0 else "missing input"

    monkeypatch.setattr(job, "_run_step", fake)
    return calls


def _render(monkeypatch, tmp_path, home: Path | None = None) -> str:
    monkeypatch.setattr(job._dream, "_path_env", lambda: "/usr/bin:/bin")
    monkeypatch.setattr(job, "resolve_central_data_dir", lambda pid: tmp_path / "data" / pid)
    if home is not None:
        monkeypatch.setattr(job, "SCRIPT_DIR", home / "scripts")
    return job.install(PID, dry_run=True)


def test_install_dry_run_renders_the_pinned_plist_and_writes_nothing(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setattr(job, "resolve_central_data_dir", lambda pid: tmp_path / "data" / pid)
    assert job.main(["install", "--project-id", PID, "--dry-run"]) == 0
    plist = plistlib.loads(capsys.readouterr().out.encode())
    assert plist["Label"] == "com.vnx.rework-attribution"
    assert plist["StartCalendarInterval"] == {"Hour": 5, "Minute": 30}
    args = plist["ProgramArguments"]
    assert args[1].endswith("scripts/launchd/vnx_pinned_launcher.sh")
    assert args[2:4] == [PID, "scripts/rework_attribution_job.py"]
    assert plist["EnvironmentVariables"]["VNX_PROJECT_ID"] == PID
    assert not (tmp_path / "home").exists()


def test_install_under_the_test_runner_refuses_a_real_launchctl(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(job, "resolve_central_data_dir", lambda pid: tmp_path / "data" / pid)
    monkeypatch.setattr(shutil, "which", lambda name: "/bin/launchctl-not-the-shim")
    launched = []
    monkeypatch.setattr(job._dream.subprocess, "run", lambda *a, **k: launched.append(a))
    with pytest.raises(job.vnx_paths.TestIsolationGuardError):
        job.install(PID, dry_run=False)
    assert launched == []
    assert not (tmp_path / "Library").exists()


def test_both_steps_ok_gives_an_ok_beacon_and_a_report(tmp_path, monkeypatch):
    calls = _steps(monkeypatch)
    data = tmp_path / "data"
    (data / "state").mkdir(parents=True)
    assert job.run_job(PID, data_dir=data) == 0
    assert [c[0] for c in calls] == ["attribute_rework.py", "rework_report.py"]
    assert (data / "state" / job.REPORT_FILE).is_file()
    beacon = _beacon(data)
    assert beacon["status"] == "ok" and beacon["details"]["window"] == "complete"
    assert beacon["expected_interval_seconds"] == 90000


def test_a_failing_attribution_step_is_degraded_skips_the_report_and_exits_zero(tmp_path, monkeypatch):
    calls = _steps(monkeypatch, attribute_rc=2)
    data = tmp_path / "data"
    (data / "state").mkdir(parents=True)
    assert job.run_job(PID, data_dir=data) == 0
    assert [c[0] for c in calls] == ["attribute_rework.py"]
    assert not (data / "state" / job.REPORT_FILE).exists()
    beacon = _beacon(data)
    assert beacon["status"] == "degraded" and beacon["details"]["window"] == "incomplete"
    assert "git failed" in beacon["details"]["reason"]


@pytest.mark.parametrize("raised", [OSError("no python"), job.subprocess.TimeoutExpired("x", 1)])
def test_a_step_that_cannot_run_is_degraded_not_a_crash(tmp_path, monkeypatch, raised):
    def boom(script, args):
        raise raised
    monkeypatch.setattr(job, "_run_step", boom)
    assert job.run_job(PID, data_dir=tmp_path) == 0
    assert _beacon(tmp_path)["status"] == "degraded"


def test_a_failing_report_step_is_degraded_with_its_reason(tmp_path, monkeypatch):
    _steps(monkeypatch, report_rc=1)
    assert job.run_job(PID, data_dir=tmp_path) == 0
    beacon = _beacon(tmp_path)
    assert beacon["status"] == "degraded" and "rework_report.py exit 1: missing input" in beacon["details"]["reason"]


def test_a_missed_night_is_caught_up_from_the_watermark(tmp_path, monkeypatch):
    """The real run_blame_edges and its watermark file, driven through the job's seam."""
    data = tmp_path / "data"
    state = data / "state"
    state.mkdir(parents=True)
    main_line = {"commits": SHAS[:3]}
    processed = []

    def fake_git(repo, args):
        start = args[-1].split("..")[0]
        chain = main_line["commits"]
        return "\n".join(chain[chain.index(start) + 1:])

    rc = sqlite3.connect(tmp_path / "rc.db")
    rc.executescript(
        "CREATE TABLE provenance_registry (dispatch_id TEXT PRIMARY KEY, commit_sha TEXT, pr_number INTEGER,"
        " registered_at TEXT NOT NULL DEFAULT '2026-01-01');"
        "CREATE TABLE dispatches (dispatch_id TEXT PRIMARY KEY, metadata_json TEXT);"
    )
    monkeypatch.setattr(blame, "_process_commit", lambda repo, sha, *rest: processed.append(sha))

    def fake(script, args):
        if script == "rework_report.py":
            return 0, ""
        since = args[args.index("--since") + 1] if "--since" in args else None
        summary = blame.run_blame_edges(tmp_path, rc, sqlite3.connect(":memory:"), PID,
                                        watermark_path=state / blame.WATERMARK_FILE, since=since, git=fake_git)
        return (2 if summary.get("stopped") else 0), json.dumps(summary)

    monkeypatch.setattr(job, "_run_step", fake)
    job.run_job(PID, data_dir=data, since=SHAS[0])
    assert processed == SHAS[1:3]
    main_line["commits"] = SHAS  # the night in between was missed; two more commits landed since
    job.run_job(PID, data_dir=data)
    assert processed == SHAS[1:4]
    assert json.loads((state / blame.WATERMARK_FILE).read_text())["commit"] == SHAS[3]
    assert _beacon(data)["status"] == "ok"


def test_the_rendered_plist_passes_the_preflight_pin_alignment(tmp_path, monkeypatch):
    store = tmp_path / ".vnx-system"
    release = store / "versions" / "v1.7.5"
    (release / "scripts").mkdir(parents=True)
    project = tmp_path / "projects" / PID
    project.mkdir(parents=True)
    (project / ".vnx-version").write_text("1.7.5\n", encoding="utf-8")
    plists = tmp_path / "LaunchAgents"
    plists.mkdir()
    (plists / f"{job.LABEL}.plist").write_text(_render(monkeypatch, tmp_path, home=release), encoding="utf-8")

    result = lps.check_pin_alignment(plists, {PID: project}, versions_dir=store / "versions")

    assert [e["classification"] for e in result["entries"]] == [lps.PIN_FOLLOWED]
    assert result["ok"] is True

    (plists / f"{job.LABEL}.plist").write_text(
        _render(monkeypatch, tmp_path, home=store / "current"), encoding="utf-8")
    assert lps.check_pin_alignment(plists, {PID: project}, versions_dir=store / "versions")["ok"] is False
