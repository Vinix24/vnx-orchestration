"""Golf schoon R22: pin ``scripts/lib/orphan_sweep.py::sweep`` (result, kill order,
error order, every log line). tmux and PID backends are injected; the stores are
real temp files. The lib module shares its basename with the CLI: load by path.
"""

from __future__ import annotations

import importlib.util
import json
import logging
import sys
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(_SCRIPTS / "lib"))

# ``sweep`` does ``import crash_recovery_sweep`` and scripts/ holds a CLI of that
# name too: pin the lib module under the bare name before any test runs.
_crs = sys.modules.get("crash_recovery_sweep")
if _crs is None or Path(getattr(_crs, "__file__", "")).parent.name != "lib":
    _crs_spec = importlib.util.spec_from_file_location(
        "crash_recovery_sweep", str(_SCRIPTS / "lib" / "crash_recovery_sweep.py")
    )
    _crs = importlib.util.module_from_spec(_crs_spec)
    sys.modules["crash_recovery_sweep"] = _crs
    _crs_spec.loader.exec_module(_crs)

_spec = importlib.util.spec_from_file_location(
    "orphan_sweep_lib_r22", str(_SCRIPTS / "lib" / "orphan_sweep.py")
)
osweep = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = osweep  # @dataclass resolves its module via sys.modules
_spec.loader.exec_module(osweep)


def _register(state_dir: Path, event: str, dispatch_id: str) -> None:
    path = state_dir / "dispatch_register.ndjson"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"timestamp": "2026-11-14T00:00:00.000000Z", "event": event,
                            "dispatch_id": dispatch_id}) + "\n")


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.delenv("VNX_CURRENT_DISPATCH_ID", raising=False)
    data_dir = tmp_path / "data"
    state_dir = data_dir / "state"
    (data_dir / "dispatches" / "active").mkdir(parents=True)
    state_dir.mkdir(parents=True)
    repo, account = tmp_path / "repo", tmp_path / "account"
    repo.mkdir(), account.mkdir()
    for sid in ("f-done-ok", "g-done-fail", "h-done-raise"):
        _register(state_dir, "dispatch_completed", sid)
    _register(state_dir, "dispatch_created", "e-alive-live")
    return repo, data_dir, account


_SESSIONS = [
    "vnx-h-done-raise", "vnx-a-dead-ok", "other-session", "vnx-c-dead-raise",
    "vnx-b-dead-fail", "vnx-d-unknown", "vnx-e-alive-live", "vnx-f-done-ok",
    "vnx-g-done-fail", "vnx-prot",
]
_PROBE = {
    "vnx-a-dead-ok": False, "vnx-b-dead-fail": False, "vnx-c-dead-raise": False,
    "vnx-d-unknown": None, "vnx-e-alive-live": True, "vnx-f-done-ok": True,
    "vnx-g-done-fail": True, "vnx-h-done-raise": True, "vnx-prot": False,
}


def _run(store, caplog, *, dry_run, sessions=_SESSIONS):
    repo, data_dir, account = store
    kills: list = []
    probes: list = []

    def kill(name):
        kills.append(name)
        if name.endswith("-raise"):
            raise RuntimeError(f"boom {name}")
        return name.endswith("-ok")

    def probe(name):
        probes.append(name)
        return _PROBE[name]

    caplog.set_level(logging.DEBUG, logger=osweep.__name__)
    res = osweep.sweep(
        repo_root=repo,
        data_dir=data_dir,
        current_dispatch_id="prot",
        dry_run=dry_run,
        list_sessions=(lambda: None) if sessions is None else (lambda: list(sessions)),
        probe_liveness=probe,
        kill_session=kill,
        pid_alive=lambda pid: False,
        capture_pane=lambda s: "",
        account_data_root=account,
    )
    logs = [
        (r.levelname, r.getMessage())
        for r in caplog.records if r.name == osweep.__name__
    ]
    return res.to_dict(), kills, probes, logs


_EXPECTED_SKIPPED_ALIVE_LIVE = [
    "vnx-b-dead-fail", "vnx-c-dead-raise", "vnx-d-unknown",
    "vnx-e-alive-live", "vnx-g-done-fail", "vnx-h-done-raise",
]


def test_live_run_classifies_kills_and_logs_in_order(store, caplog):
    d, kills, probes, logs = _run(store, caplog, dry_run=False)

    scanned = sorted(s for s in _SESSIONS if s.startswith("vnx-"))
    assert d["tmux_sessions_scanned"] == scanned
    assert probes == [s for s in scanned if s != "vnx-prot"]
    assert kills == [
        "vnx-a-dead-ok", "vnx-b-dead-fail", "vnx-c-dead-raise",
        "vnx-f-done-ok", "vnx-g-done-fail", "vnx-h-done-raise",
    ]
    assert d["tmux_killed"] == ["vnx-a-dead-ok"]
    assert d["tmux_skipped_protected"] == ["vnx-prot"]
    assert d["tmux_skipped_alive"] == _EXPECTED_SKIPPED_ALIVE_LIVE
    killed = d["tmux_completed_orphans_killed"]
    assert [e["session"] for e in killed] == ["vnx-f-done-ok"]
    assert killed[0]["dispatch_id"] == "f-done-ok"
    assert killed[0]["reason"].startswith("register:dispatch_completed")
    preserved = d["tmux_completed_orphans_preserved"]
    assert [(e["session"], e["dispatch_id"]) for e in preserved] == [
        ("vnx-e-alive-live", "e-alive-live"),
        ("vnx-g-done-fail", "g-done-fail"),
        ("vnx-h-done-raise", "h-done-raise"),
    ]
    assert preserved[1]["reason"] == "kill_failed"
    assert preserved[2]["reason"] == "kill_failed"
    assert preserved[0]["reason"] != "kill_failed"
    tmux_errors = [e for e in d["errors"] if e["kind"] in ("tmux", "tmux_completed")]
    assert tmux_errors == [
        {"kind": "tmux", "session": "vnx-c-dead-raise", "error": "boom vnx-c-dead-raise"},
        {"kind": "tmux_completed", "session": "vnx-h-done-raise",
         "error": "boom vnx-h-done-raise"},
    ]
    reason_f = killed[0]["reason"]
    assert logs[:-1] == [
        ("INFO", "orphan_sweep: killed dead tmux session vnx-a-dead-ok"),
        ("WARNING", "orphan_sweep: kill failed for vnx-b-dead-fail; leaving session"),
        ("WARNING", "orphan_sweep: kill failed for vnx-c-dead-raise; leaving session"),
        ("INFO", f"orphan_sweep: killed completed-orphan tmux session vnx-f-done-ok ({reason_f})"),
        ("WARNING", "orphan_sweep: kill failed for completed-orphan vnx-g-done-fail; leaving session"),
        ("WARNING", "orphan_sweep: kill failed for completed-orphan vnx-h-done-raise; leaving session"),
        ("INFO", "orphan_sweep: SKIP tmux vnx-prot — protected dispatch"),
    ]
    level, summary = logs[-1]
    assert level == "INFO"
    assert summary == (
        "orphan_sweep: complete — tmux killed=1 skipped=6 protected=1 "
        "completed_orphans_killed=1 completed_orphans_preserved=3; "
        "worktrees removed=0 preserved=0 skipped_live=0; active recovered=0 "
        f"scanned=0 capped=False; active v3 counts={d['active_v3']}; "
        f"dry_run=False; errors={len(d['errors'])}"
    )


def test_dry_run_mutates_nothing_and_logs_would_kill(store, caplog):
    d, kills, _probes, logs = _run(store, caplog, dry_run=True)

    assert kills == []
    assert d["dry_run"] is True
    assert d["tmux_killed"] == ["vnx-a-dead-ok", "vnx-b-dead-fail", "vnx-c-dead-raise"]
    assert d["tmux_skipped_alive"] == ["vnx-d-unknown", "vnx-e-alive-live"]
    assert [e["session"] for e in d["tmux_completed_orphans_killed"]] == [
        "vnx-f-done-ok", "vnx-g-done-fail", "vnx-h-done-raise",
    ]
    assert [e["session"] for e in d["tmux_completed_orphans_preserved"]] == ["vnx-e-alive-live"]
    reasons = {e["session"]: e["reason"] for e in d["tmux_completed_orphans_killed"]}
    assert logs[:-1] == [
        ("INFO", "orphan_sweep: DRY-RUN would kill tmux session vnx-a-dead-ok"),
        ("INFO", "orphan_sweep: DRY-RUN would kill tmux session vnx-b-dead-fail"),
        ("INFO", "orphan_sweep: DRY-RUN would kill tmux session vnx-c-dead-raise"),
    ] + [
        ("INFO", f"orphan_sweep: DRY-RUN would kill completed-orphan tmux session {s} ({reasons[s]})")
        for s in ("vnx-f-done-ok", "vnx-g-done-fail", "vnx-h-done-raise")
    ] + [
        ("INFO", "orphan_sweep: SKIP tmux vnx-prot — protected dispatch"),
    ]
    assert "dry_run=True" in logs[-1][1]


def test_unmeasurable_listing_records_error_first_and_scans_nothing(store, caplog):
    d, kills, probes, logs = _run(store, caplog, dry_run=False, sessions=None)

    assert kills == [] and probes == []
    assert d["tmux_sessions_scanned"] == []
    assert d["errors"][0]["kind"] == "tmux"
    assert d["errors"][0]["error"].startswith(
        "tmux session listing was not measurable this run (tmux missing"
    )
    assert logs[0] == (
        "WARNING",
        "orphan_sweep: tmux session listing unmeasurable — skipping "
        "worktree reap this run (fail-open)",
    )
    assert logs[-1][1].startswith("orphan_sweep: complete — tmux killed=0 skipped=0 protected=0 ")

