"""Nightly content-boundary measurement (F2 part 3): scanner, beacon, ledger and doctor check.

Every root is a tmp dir: a tmp HOME with ``.claude``, ``.kimi`` and a tmp central-stores root,
a tmp client root and a tmp personal root. No real store, no real transcript is read or written.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
_SCRIPTS = _REPO / "scripts"
sys.path.insert(0, str(_SCRIPTS))
sys.path.insert(0, str(_SCRIPTS / "lib"))

import content_boundary_check as cbc  # noqa: E402
import content_class  # noqa: E402
import vnx_doctor  # noqa: E402

TOKEN = "CanaryTok" + uuid.uuid4().hex[:16]
NOW = 2_000_000_000.0
SINCE = NOW - 3600


class World:
    def __init__(self, tmp: Path):
        self.home = tmp / "home"
        self.stores = tmp / "stores"
        self.data = tmp / "data"
        self.state = tmp / "state"
        self.client_root = tmp / "clients"
        self.personal_root = tmp / "personal"
        self.token_file = tmp / "canary.token"
        self.armed_file = self.personal_root / "canary.md"
        self.boundary_file = tmp / "content_boundary.json"
        for d in (self.home / ".claude" / "projects" / "proj", self.home / ".kimi" / "sessions",
                  self.stores, self.data, self.state, self.client_root, self.personal_root):
            d.mkdir(parents=True, exist_ok=True)
        self.token_file.write_text(TOKEN + "\n", encoding="utf-8")
        os.chmod(self.token_file, 0o600)
        self.armed_file.write_text(f"marker {TOKEN}\n", encoding="utf-8")

    def write_boundary(self, **overrides) -> content_class.Boundary:
        data = {
            "version": 1,
            "client_roots": [str(self.client_root)],
            "personal_roots": [str(self.personal_root)],
            "canary_token_file": str(self.token_file),
            "canary_armed_in": [str(self.armed_file)],
        }
        data.update(overrides)
        self.boundary_file.write_text(json.dumps(data), encoding="utf-8")
        return content_class.load_boundary(self.boundary_file)

    @property
    def client_file(self) -> str:
        return str(self.client_root / "group" / "client" / "notes.md")

    def claude(self, name: str, records: list, mtime: float = NOW - 60) -> Path:
        path = self.home / ".claude" / "projects" / "proj" / f"{name}.jsonl"
        return _write(path, records, mtime)

    def lane(self, lane: str, name: str, records: list, mtime: float = NOW - 60) -> Path:
        path = self.stores / "store1" / "harness-config" / lane / "projects" / "proj" / f"{name}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        return _write(path, records, mtime)

    def kimi(self, name: str, records: list, mtime: float = NOW - 60) -> Path:
        path = self.home / ".kimi" / "sessions" / "hash" / name / "context.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        return _write(path, records, mtime)

    def run(self, boundary: content_class.Boundary, **kw) -> dict:
        return cbc.run_check(home=self.home, stores_root=self.stores, data_dir=self.data,
                             state_dir=self.state, boundary=boundary,
                             since=kw.pop("since", SINCE), now=kw.pop("now", NOW), **kw)

    def beacon_path(self) -> Path:
        return self.data / "health" / "content_boundary.json"

    def ledger_path(self) -> Path:
        return self.state / cbc.LEDGER_NAME


def _write(path: Path, records: list, mtime: float) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    os.utime(path, (mtime, mtime))
    return path


def assistant(model: str, cwd: str = "/tmp/x") -> dict:
    return {"type": "assistant", "cwd": cwd, "message": {"model": model, "content": [{"type": "text", "text": "ok"}]}}


def tool_use(path: str, model: str = "claude-sonnet-5-5") -> dict:
    return {"type": "assistant", "message": {"model": model, "content": [
        {"type": "tool_use", "name": "Read", "input": {"file_path": path}}]}}


def tool_result(text: str) -> dict:
    return {"type": "user", "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "t1", "content": text}]}}


def user_text(text: str) -> dict:
    return {"type": "user", "message": {"role": "user", "content": text}}


def instructions(path: str, kind: str = "User") -> dict:
    return {"type": "attachment", "attachment": {"type": "instructions", "files": [
        {"path": path, "type": kind, "content": "body"}]}}


@pytest.fixture
def world(tmp_path, monkeypatch):
    # Path.home() is the tmp home too: content_class reads ~/.vnx/projects.json when it
    # classifies a cwd outside the roots, and that must not be the operator's registry.
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    return World(tmp_path)


def _leaky_world(w: World) -> content_class.Boundary:
    boundary = w.write_boundary()
    w.claude("ds-config", [assistant("deepseek-v4-pro"), instructions(str(w.home / ".claude" / "CLAUDE.md"))])
    w.claude("glm-access", [assistant("z-ai/glm-5.2"), tool_use(w.client_file, "z-ai/glm-5.2")])
    w.claude("claude-model", [assistant("claude-sonnet-5-5"), tool_use(w.client_file)])
    w.claude("glm-result-only", [assistant("z-ai/glm-5.2"), tool_result(f"see {w.client_file}")])
    w.lane("glm-harness", "lane-canary", [assistant("z-ai/glm-5.2"), user_text(f"leak {TOKEN}")])
    w.kimi("kimi-canary", [
        {"role": "_system_prompt", "content": "skills live here"},
        {"role": "user", "content": f"prompt {TOKEN}"},
    ])
    return boundary


def _total(details: dict, signal: str) -> int:
    return sum(details["counts"][signal].values())


# C1
def test_leaking_fixtures_are_counted_and_the_beacon_fails(world):
    boundary = _leaky_world(world)
    result = world.run(boundary)
    d = result["details"]
    assert result["status"] == "fail"
    assert _total(d, "config_leak") == 1
    assert d["counts"]["config_leak"] == {"deepseek": 1}
    assert _total(d, "restricted_access") == 1
    assert d["counts"]["restricted_access"] == {"glm": 1}
    assert _total(d, "canary") == 2
    assert d["counts"]["canary"] == {"glm-harness": 1, "kimi": 1}
    assert _total(d, "restricted_cwd") == 0
    assert d["gating_total"] == 4
    beacon = json.loads(world.beacon_path().read_text(encoding="utf-8"))
    assert beacon["status"] == "fail"
    assert beacon["component"] == "content_boundary"
    ledger = world.ledger_path().read_text(encoding="utf-8").splitlines()
    assert len(ledger) == 1 and json.loads(ledger[0])["status"] == "fail"


def test_beacon_and_ledger_carry_no_token_path_or_text(world):
    boundary = _leaky_world(world)
    world.run(boundary)
    for artefact in (world.beacon_path(), world.ledger_path()):
        text = artefact.read_text(encoding="utf-8")
        assert TOKEN not in text
        assert str(world.client_root) not in text
        assert str(world.home) not in text
        assert "notes.md" not in text


# C2
def test_clean_fixtures_give_ok_and_scan_every_root(world):
    boundary = world.write_boundary()
    world.claude("ds-clean", [assistant("deepseek-v4-pro"), user_text("hello")])
    world.lane("deepseek-harness", "lane-clean", [assistant("deepseek-flash"), user_text("hello")])
    world.kimi("kimi-clean", [{"role": "_system_prompt", "content": "x"}, {"role": "user", "content": "hello"}])
    result = world.run(boundary)
    d = result["details"]
    assert result["status"] == "ok"
    assert d["gating_total"] == 0
    assert all(_total(d, s) == 0 for s in cbc.SIGNALS)
    assert all(count > 0 for count in d["scanned"].values()), d["scanned"]
    assert d["canary_armed"] is True


# C3
def test_leak_older_than_since_is_historic_and_does_not_gate(world):
    boundary = _leaky_world(world)
    for path in list(world.home.rglob("*.jsonl")) + list(world.stores.rglob("*.jsonl")):
        os.utime(path, (SINCE - 100, SINCE - 100))
    result = world.run(boundary)
    d = result["details"]
    assert result["status"] == "ok"
    assert d["gating_total"] == 0
    assert d["historic_total"] == 4
    assert sum(d["historic"]["canary"].values()) == 2


def test_since_defaults_to_previous_scanned_through(world):
    boundary = _leaky_world(world)
    first = world.run(boundary, since=SINCE)
    assert first["details"]["scanned_through"] == int(NOW)
    assert cbc.read_previous_scanned_through(world.data) == int(NOW)
    for path in list(world.home.rglob("*.jsonl")) + list(world.stores.rglob("*.jsonl")):
        os.utime(path, (NOW - 10, NOW - 10))
    second = cbc.run_check(home=world.home, stores_root=world.stores, data_dir=world.data,
                           state_dir=world.state, boundary=boundary, now=NOW + 86400)
    assert second["details"]["since"] == int(NOW)
    assert second["details"]["gating_total"] == 0
    assert second["details"]["historic_total"] == 4


# C4
@pytest.mark.parametrize("breakage", ["missing_token_file", "token_not_in_armed_file", "loose_mode"])
def test_unarmed_canary_is_reported(world, breakage):
    if breakage == "missing_token_file":
        world.token_file.unlink()
    elif breakage == "token_not_in_armed_file":
        world.armed_file.write_text("nothing here\n", encoding="utf-8")
    else:
        os.chmod(world.token_file, 0o644)
    boundary = world.write_boundary()
    world.claude("ds-clean", [assistant("deepseek-v4-pro")])
    result = world.run(boundary)
    assert result["details"]["canary_armed"] is False
    assert result["details"]["canary_armed_reason"] != "armed"
    (check,) = vnx_doctor.check_content_boundary_beacon(_doctor_paths(world))
    assert check.status == vnx_doctor.WARN
    assert "canary" in check.message


def test_unconfigured_boundary_fails_and_says_unconfigured(world):
    missing = content_class.load_boundary(world.boundary_file)
    assert not missing.configured
    world.claude("ds-clean", [assistant("deepseek-v4-pro")])
    result = world.run(missing)
    assert result["status"] == "fail"
    assert result["details"]["unconfigured"] is True
    beacon = json.loads(world.beacon_path().read_text(encoding="utf-8"))
    assert beacon["details"]["unconfigured"] is True
    assert str(world.boundary_file) not in json.dumps(beacon)
    (check,) = vnx_doctor.check_content_boundary_beacon(_doctor_paths(world))
    assert check.status == vnx_doctor.FAIL and "unconfigured" in check.message


def test_restricted_cwd_counts_for_a_harness_transcript(world):
    boundary = world.write_boundary()
    cwd = str(world.client_root / "group" / "client")
    world.claude("ds-cwd", [assistant("deepseek-v4-pro", cwd=cwd)])
    result = world.run(boundary)
    assert _total(result["details"], "restricted_cwd") == 1
    assert result["status"] == "fail"


def test_config_leak_counts_any_type_under_a_second_config_dir(world):
    boundary = world.write_boundary()
    second = world.home / ".claude-other"
    second.mkdir()
    world.claude("glm-auto", [assistant("z-ai/glm-5.2"), instructions(str(second / "x" / "MEMORY.md"), "AutoMem")])
    world.claude("glm-project", [assistant("z-ai/glm-5.2"), instructions(str(world.home / "repo" / "CLAUDE.md"), "Project")])
    result = world.run(boundary)
    assert result["details"]["counts"]["config_leak"] == {"glm": 1}


def test_kimi_system_prompt_is_reported_and_tool_results_do_not_gate(world):
    boundary = world.write_boundary()
    world.kimi("kimi-system", [
        {"role": "_system_prompt", "content": f"skills for {world.personal_root}/Lifestyle/x"},
        {"role": "user", "content": "hello"},
        {"role": "tool", "tool_call_id": "c1", "content": f"file mentions {world.client_file}"},
    ])
    world.kimi("kimi-access", [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "", "tool_calls": [{"function": {"arguments": json.dumps({"path": world.client_file})}}]},
    ])
    result = world.run(boundary)
    d = result["details"]
    assert d["kimi_system_prompt_reported"] == 1
    assert d["counts"]["restricted_access"] == {"kimi": 1}
    assert d["sessions"]["restricted_access"] == ["kimi-access"]


def test_session_ids_are_capped_per_signal(world):
    boundary = world.write_boundary()
    for i in range(cbc.SESSION_ID_LIMIT + 5):
        world.claude(f"ds-{i:03d}", [assistant("deepseek-v4-pro"), instructions(str(world.home / ".claude" / "CLAUDE.md"))])
    result = world.run(boundary)
    assert _total(result["details"], "config_leak") == cbc.SESSION_ID_LIMIT + 5
    assert len(result["details"]["sessions"]["config_leak"]) == cbc.SESSION_ID_LIMIT


# Privacy: every artefact a real run writes, including its output
def test_no_artefact_of_a_cli_run_carries_the_path_or_the_token(world, tmp_path):
    _leaky_world(world)
    proc = subprocess.run(
        [sys.executable, str(_SCRIPTS / "content_boundary_check.py"),
         "--home", str(world.home), "--stores-root", str(world.stores),
         "--data-dir", str(world.data), "--state-dir", str(world.state),
         "--boundary-file", str(world.boundary_file), "--since", "0"],
        capture_output=True, text=True, timeout=120, cwd=str(tmp_path),
    )
    assert proc.returncode == 0, proc.stderr
    assert "status=fail" in proc.stdout
    outputs = [proc.stdout, proc.stderr]
    for produced in list(world.data.rglob("*")) + list(world.state.rglob("*")):
        if produced.is_file():
            outputs.append(produced.read_text(encoding="utf-8", errors="replace"))
    assert len(outputs) > 4
    forbidden = (TOKEN, str(world.client_root), world.client_file, "notes.md")
    for text in outputs:
        for needle in forbidden:
            assert needle not in text, needle


# C5 (red on base): the doctor names the content boundary when the beacon says fail
def _doctor_paths(world: World) -> dict:
    return {"VNX_DATA_DIR": str(world.data), "VNX_STATE_DIR": str(world.state)}


def _quiet_other_checks(monkeypatch) -> None:
    for name in dir(vnx_doctor):
        if name.startswith("check_") and name != "check_content_boundary_beacon":
            monkeypatch.setattr(vnx_doctor, name, lambda *a, **k: [], raising=False)


def _write_beacon(world: World, status: str, age_seconds: float = 60, **details) -> None:
    beacon = world.beacon_path()
    beacon.parent.mkdir(parents=True, exist_ok=True)
    now = time.time() - age_seconds
    beacon.write_text(json.dumps({
        "component": "content_boundary", "status": status, "last_run_ts": int(now),
        "details": details, "expected_interval_seconds": 86400,
    }), encoding="utf-8")


def test_doctor_run_fails_on_a_failing_content_boundary_beacon(world, monkeypatch):
    _write_beacon(world, "fail", gating_total=3, canary_armed=True)
    _quiet_other_checks(monkeypatch)
    results = vnx_doctor.run_doctor(_doctor_paths(world))
    failing = [r for r in results if isinstance(r, vnx_doctor.CheckResult) and r.status == vnx_doctor.FAIL and "content boundary" in r.message.lower()]
    assert failing, [r for r in results]


def test_doctor_passes_on_a_fresh_clean_armed_beacon(world):
    _write_beacon(world, "ok", gating_total=0, canary_armed=True)
    (check,) = vnx_doctor.check_content_boundary_beacon(_doctor_paths(world))
    assert check.status == vnx_doctor.PASS


def test_doctor_fails_on_a_missing_beacon_when_the_nightly_job_runs(world):
    (world.state / "conversation_analyzer.log").write_text("x\n", encoding="utf-8")
    (check,) = vnx_doctor.check_content_boundary_beacon(_doctor_paths(world))
    assert check.status == vnx_doctor.FAIL


def test_doctor_passes_on_a_missing_beacon_when_the_job_never_ran(world):
    (check,) = vnx_doctor.check_content_boundary_beacon(_doctor_paths(world))
    assert check.status == vnx_doctor.PASS


def test_doctor_fails_on_a_stale_beacon(world):
    (world.state / "conversation_analyzer.log").write_text("x\n", encoding="utf-8")
    _write_beacon(world, "ok", age_seconds=37 * 3600, canary_armed=True)
    (check,) = vnx_doctor.check_content_boundary_beacon(_doctor_paths(world))
    assert check.status == vnx_doctor.FAIL and "stale" in check.message


def test_doctor_fails_on_a_beacon_without_a_timestamp(world):
    _write_beacon(world, "ok", canary_armed=True)
    data = json.loads(world.beacon_path().read_text(encoding="utf-8"))
    del data["last_run_ts"]
    world.beacon_path().write_text(json.dumps(data), encoding="utf-8")
    (check,) = vnx_doctor.check_content_boundary_beacon(_doctor_paths(world))
    assert check.status == vnx_doctor.FAIL


def test_nightly_script_registers_the_phase_through_run_phase():
    text = (_SCRIPTS / "conversation_analyzer_nightly.sh").read_text(encoding="utf-8")
    assert 'run_phase "Phase 1.1" "$PHASE_TIMEOUT" content_boundary_check.py' in text
    assert text.index("Phase 1.1") > text.index('run_phase "Phase 1" ')
    assert text.index('run_phase "Phase 1.1"') < text.index('run_phase "Phase 1.5"')


# ---------------------------------------------------------------------------
# Fix round 2 (PR #2061): incomplete scan, canary in system prompt, any external model, ledger first
# ---------------------------------------------------------------------------

def _run_at(world: World, boundary, now: float, **kw) -> dict:
    return cbc.run_check(home=world.home, stores_root=world.stores, data_dir=world.data,
                         state_dir=world.state, boundary=boundary, now=now, **kw)


def test_unreadable_transcript_is_not_ok_keeps_checkpoint_and_is_rescanned(world, monkeypatch):
    boundary = world.write_boundary()
    first = _run_at(world, boundary, NOW - 86400, since=SINCE - 86400)
    assert first["status"] == "ok"
    kept = cbc.read_previous_scanned_through(world.data)
    assert kept == int(NOW - 86400)

    leak = world.lane("glm-harness", "late-leak", [assistant("z-ai/glm-5.2"), user_text(f"leak {TOKEN}")],
                      mtime=NOW - 600)
    real_scan = cbc.scan_claude_transcript

    def flaky(path, ctx):
        if path == leak:
            raise PermissionError("denied")
        return real_scan(path, ctx)

    monkeypatch.setattr(cbc, "scan_claude_transcript", flaky)
    second = _run_at(world, boundary, NOW)
    assert second["details"]["unreadable_files"] == 1
    assert second["status"] != "ok"
    assert second["details"]["scanned_through"] == kept
    assert cbc.read_previous_scanned_through(world.data) == kept
    (check,) = vnx_doctor.check_content_boundary_beacon(_doctor_paths(world))
    assert check.status != vnx_doctor.PASS

    monkeypatch.setattr(cbc, "scan_claude_transcript", real_scan)
    third = _run_at(world, boundary, NOW + 3600)
    assert third["details"]["since"] == kept
    assert third["details"]["gating_total"] == 1
    assert third["details"]["historic_total"] == 0
    assert third["status"] == "fail"


def test_doctor_fails_on_an_ok_beacon_that_reports_unreadable_files(world):
    _write_beacon(world, "ok", gating_total=0, canary_armed=True, unreadable_files=2)
    (check,) = vnx_doctor.check_content_boundary_beacon(_doctor_paths(world))
    assert check.status == vnx_doctor.FAIL


def test_canary_in_a_kimi_system_prompt_gates(world):
    boundary = world.write_boundary()
    world.kimi("sys-canary", [
        {"role": "_system_prompt", "content": f"instructions {TOKEN}"},
        {"role": "user", "content": "hello"},
    ])
    result = world.run(boundary)
    assert result["details"]["gating_total"] >= 1
    assert result["details"]["counts"]["canary"] == {"kimi": 1}
    assert result["status"] == "fail"


def test_kimi_system_prompt_path_name_is_still_only_reported(world):
    boundary = world.write_boundary()
    world.kimi("sys-path", [
        {"role": "_system_prompt", "content": f"see {world.client_file}"},
        {"role": "user", "content": "hello"},
    ])
    result = world.run(boundary)
    assert result["details"]["gating_total"] == 0
    assert result["details"]["kimi_system_prompt_reported"] == 1


def test_later_external_model_in_a_claude_transcript_sets_the_lane(world):
    boundary = world.write_boundary()
    world.claude("mixed", [assistant("claude-sonnet-5-5"), assistant("deepseek-v4-pro"),
                           user_text(f"leak {TOKEN}")])
    world.claude("pure-claude", [assistant("claude-sonnet-5-5"), user_text(f"leak {TOKEN}")])
    result = world.run(boundary)
    assert result["details"]["counts"]["canary"] == {"deepseek": 1}
    assert result["details"]["scanned"]["claude_projects"] == 1


def test_two_external_lanes_in_one_transcript_count_once_per_lane(world):
    boundary = world.write_boundary()
    world.claude("two", [assistant("deepseek-v4-pro"), assistant("z-ai/glm-5.2"), user_text(f"leak {TOKEN}")])
    result = world.run(boundary)
    assert result["details"]["counts"]["canary"] == {"deepseek": 1, "glm": 1}


def test_failed_ledger_append_does_not_advance_the_beacon(world, monkeypatch):
    boundary = world.write_boundary()
    world.claude("leak", [assistant("deepseek-v4-pro"), user_text(f"leak {TOKEN}")])
    real_open = os.open

    def failing_open(path, *a, **k):
        if str(path).endswith(cbc.LEDGER_NAME):
            raise OSError("ledger unavailable")
        return real_open(path, *a, **k)

    monkeypatch.setattr(os, "open", failing_open)
    with pytest.raises(OSError):
        world.run(boundary)
    monkeypatch.setattr(os, "open", real_open)
    assert not world.beacon_path().exists()
    assert cbc.read_previous_scanned_through(world.data) is None
    retry = world.run(boundary)
    assert retry["details"]["gating_total"] == 1
    assert len(world.ledger_path().read_text().splitlines()) == 1


# ---------------------------------------------------------------------------
# Fix round 3 (PR #2061): the checkpoint moves only after a complete, configured scan
# ---------------------------------------------------------------------------

@pytest.fixture
def not_root():
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        pytest.skip("root lists a chmod 000 directory, so nothing is unreadable")


def _write_raw(path: Path, lines: list, mtime: float = NOW - 60) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(line + "\n" for line in lines), encoding="utf-8")
    os.utime(path, (mtime, mtime))
    return path


def _cut_after(record: dict, needle: str) -> str:
    """The JSON line of ``record`` cut off right after ``needle``: a truncated record."""
    line = json.dumps(record)
    return line[:line.index(needle) + len(needle)]


def _claude_path(world: World, name: str) -> Path:
    return world.home / ".claude" / "projects" / "proj" / f"{name}.jsonl"


def _lane_path(world: World, lane: str, name: str) -> Path:
    return world.stores / "store1" / "harness-config" / lane / "projects" / "proj" / f"{name}.jsonl"


def _kimi_path(world: World, session: str, name: str = "context.jsonl") -> Path:
    return world.home / ".kimi" / "sessions" / "hash" / session / name


def _leak(token_text: str = "") -> list:
    return [assistant("z-ai/glm-5.2"), user_text(token_text or f"leak {TOKEN}")]


# Case 1: an unreadable harness project directory
def test_unreadable_harness_project_dir_keeps_the_checkpoint_and_the_leak_counts_after_repair(world, not_root):
    boundary = world.write_boundary()
    first = _run_at(world, boundary, NOW - 86400, since=SINCE - 86400)
    assert first["status"] == "ok"
    kept = cbc.read_previous_scanned_through(world.data)
    assert kept == int(NOW - 86400)

    leak = world.lane("glm-harness", "hidden-leak", _leak(), mtime=NOW - 600)
    os.chmod(leak.parent, 0o000)
    try:
        second = _run_at(world, boundary, NOW)
    finally:
        os.chmod(leak.parent, 0o755)
    d = second["details"]
    assert second["status"] != "ok"
    assert d["traversal_errors"] == 1
    assert d["incomplete_reasons"] == ["traversal_errors"]
    assert d["incomplete_where"] == [{"condition": "traversal_error", "root": "harness_config",
                                      "lane": "glm-harness", "error": "PermissionError"}]
    assert d["scanned_through"] == kept
    assert cbc.read_previous_scanned_through(world.data) == kept
    (check,) = vnx_doctor.check_content_boundary_beacon(_doctor_paths(world))
    assert check.status == vnx_doctor.FAIL and "traversal_errors" in check.message
    beacon = world.beacon_path().read_text(encoding="utf-8")
    assert str(world.stores) not in beacon and str(world.home) not in beacon

    third = _run_at(world, boundary, NOW + 3600)
    assert third["details"]["since"] == kept
    assert third["details"]["counts"]["canary"] == {"glm-harness": 1}
    assert third["details"]["historic_total"] == 0
    assert third["status"] == "fail"


# Case 2: truncated harness records
def test_truncated_harness_records_naming_a_restricted_path_or_carrying_the_canary_gate(world):
    boundary = world.write_boundary()
    _write_raw(_lane_path(world, "glm-harness", "cut-path"), [
        json.dumps(assistant("z-ai/glm-5.2")),
        _cut_after(tool_use(world.client_file, "z-ai/glm-5.2"), world.client_file),
    ])
    _write_raw(_lane_path(world, "deepseek-harness", "cut-canary"), [
        json.dumps(assistant("deepseek-flash")),
        _cut_after(user_text(f"leak {TOKEN} and more"), TOKEN),
    ])
    result = world.run(boundary)
    d = result["details"]
    assert result["status"] == "fail"
    assert d["counts"]["restricted_access"] == {"glm-harness": 1}
    assert d["counts"]["canary"] == {"deepseek-harness": 1}
    assert d["malformed_records"] == 2
    assert sorted(d["malformed_sessions"]) == ["cut-canary", "cut-path"]
    assert d["incomplete_reasons"] == []


# Case 3: the same for a Kimi transcript
def test_truncated_kimi_records_gate_and_a_truncated_system_prompt_is_only_reported(world):
    boundary = world.write_boundary()
    _write_raw(_kimi_path(world, "cut-path"), [
        json.dumps({"role": "_system_prompt", "content": "skills"}),
        _cut_after({"role": "user", "content": f"read {world.client_file} now"}, world.client_file),
    ])
    _write_raw(_kimi_path(world, "cut-canary"), [
        _cut_after({"role": "assistant", "content": f"x {TOKEN} y"}, TOKEN),
    ])
    _write_raw(_kimi_path(world, "cut-system"), [
        _cut_after({"role": "_system_prompt", "content": f"skills in {world.personal_root}/Lifestyle/x and more"},
                   "Lifestyle/x"),
    ])
    result = world.run(boundary)
    d = result["details"]
    assert result["status"] == "fail"
    assert d["counts"]["restricted_access"] == {"kimi": 1}
    assert d["sessions"]["restricted_access"] == ["cut-path"]
    assert d["counts"]["canary"] == {"kimi": 1}
    assert d["kimi_system_prompt_reported"] == 1
    assert d["malformed_records"] == 3
    assert d["incomplete_reasons"] == []


@pytest.mark.parametrize("root", ["claude", "harness", "kimi"])
def test_a_clean_malformed_line_alone_does_not_keep_the_checkpoint(world, root):
    boundary = world.write_boundary()
    if root == "claude":
        _write_raw(_claude_path(world, "torn"), [json.dumps(assistant("deepseek-v4-pro")),
                                                 '{"type": "user", "message": {"content": "hel'])
    elif root == "harness":
        _write_raw(_lane_path(world, "glm-harness", "torn"), [json.dumps(assistant("z-ai/glm-5.2")), "{not json"])
    else:
        _write_raw(_kimi_path(world, "torn"), ['{"role": "user", "content": "hel'])
    result = world.run(boundary)
    d = result["details"]
    assert result["status"] == "ok"
    assert d["malformed_records"] == 1
    assert d["malformed_sessions"] == ["torn"]
    assert d["scanned_through"] == int(NOW)
    assert cbc.read_previous_scanned_through(world.data) == int(NOW)
    (check,) = vnx_doctor.check_content_boundary_beacon(_doctor_paths(world))
    assert check.status == vnx_doctor.PASS


# Case 4: an unconfigured run, then a configured run
def test_unconfigured_run_keeps_the_checkpoint_and_the_configured_run_scans_its_window(world):
    unconfigured = content_class.load_boundary(world.boundary_file)
    assert not unconfigured.configured
    first_since = NOW - 7200
    world.lane("glm-harness", "during-unconfigured", _leak(), mtime=NOW - 5400)
    first = _run_at(world, unconfigured, NOW - 3600, since=first_since)
    assert first["status"] == "fail"
    assert first["details"]["scanned_through"] == int(first_since)
    assert cbc.read_previous_scanned_through(world.data) == int(first_since)
    assert first["details"]["incomplete_reasons"] == ["boundary_unconfigured"]
    (check,) = vnx_doctor.check_content_boundary_beacon(_doctor_paths(world))
    assert check.status == vnx_doctor.FAIL and "unconfigured" in check.message

    world.lane("deepseek-harness", "between-runs",
               [assistant("deepseek-flash"), tool_use(world.client_file, "deepseek-flash")], mtime=NOW - 1800)
    configured = world.write_boundary()
    second = _run_at(world, configured, NOW)
    d = second["details"]
    assert d["since"] == int(first_since)
    assert d["since_source"] == "beacon"
    assert d["counts"]["canary"] == {"glm-harness": 1}
    assert d["counts"]["restricted_access"] == {"deepseek-harness": 1}
    assert d["historic_total"] == 0
    assert second["status"] == "fail"
    assert d["scanned_through"] == int(NOW)


# Audit sites (case 5)
def test_subagent_transcripts_and_rotated_kimi_contexts_are_reached(world):
    boundary = world.write_boundary()
    _write(world.home / ".claude" / "projects" / "proj" / "sess" / "subagents" / "agent-a1.jsonl",
           [assistant("deepseek-v4-pro"), user_text(f"leak {TOKEN}")], NOW - 60)
    _write(_lane_path(world, "glm-harness", "x").parent / "sess" / "subagents" / "agent-b2.jsonl",
           [assistant("z-ai/glm-5.2"), tool_use(world.client_file, "z-ai/glm-5.2")], NOW - 60)
    _write(_kimi_path(world, "ksess", "context_1.jsonl"), [{"role": "user", "content": f"leak {TOKEN}"}], NOW - 60)
    _write(_kimi_path(world, "ksess", "subagents") / "sub1" / "context.jsonl",
           [{"role": "user", "content": f"read {world.client_file}"}], NOW - 60)
    result = world.run(boundary)
    d = result["details"]
    assert d["counts"]["canary"] == {"deepseek": 1, "kimi": 1}
    assert d["counts"]["restricted_access"] == {"glm-harness": 1, "kimi": 1}
    assert d["scanned"] == {"claude_projects": 1, "harness_config": 1, "kimi": 2}


def test_an_unrecognized_model_counts_as_an_external_lane(world):
    boundary = world.write_boundary()
    world.claude("new-lane", [assistant("moonshot-k3"), user_text(f"leak {TOKEN}")])
    world.claude("synthetic", [assistant("<synthetic>"), assistant("claude-opus-5-5"), user_text(f"leak {TOKEN}")])
    result = world.run(boundary)
    assert result["details"]["counts"]["canary"] == {"unrecognized_model": 1}
    assert result["details"]["scanned"]["claude_projects"] == 1


def test_a_kimi_record_with_an_unknown_role_is_inspected(world):
    boundary = world.write_boundary()
    world.kimi("new-role", [{"role": "system", "content": f"read {world.client_file}"}])
    world.kimi("tool-only", [{"role": "tool", "tool_call_id": "c1", "content": f"see {world.client_file}"}])
    result = world.run(boundary)
    assert result["details"]["counts"]["restricted_access"] == {"kimi": 1}
    assert result["details"]["sessions"]["restricted_access"] == ["new-role"]


def test_a_json_line_that_is_no_object_is_inspected_as_raw_text(world):
    boundary = world.write_boundary()
    _write_raw(_lane_path(world, "glm-harness", "list-line"), [json.dumps(assistant("z-ai/glm-5.2")),
                                                               json.dumps(["note", world.client_file])])
    result = world.run(boundary)
    assert result["details"]["counts"]["restricted_access"] == {"glm-harness": 1}
    assert result["details"]["malformed_records"] == 1


def test_prompt_text_outside_typed_blocks_and_paths_as_keys_count(world):
    boundary = world.write_boundary()
    world.claude("bare-block", [assistant("deepseek-v4-pro"),
                                {"type": "user", "message": {"role": "user", "content": [f"read {world.client_file}"]}}])
    world.claude("key-path", [assistant("z-ai/glm-5.2"), {"type": "assistant", "message": {
        "model": "z-ai/glm-5.2", "content": [{"type": "tool_use", "name": "Edit",
                                              "input": {"edits": {world.client_file: "new text"}}}]}}])
    result = world.run(boundary)
    assert result["details"]["counts"]["restricted_access"] == {"deepseek": 1, "glm": 1}
    assert result["details"]["malformed_records"] == 0


def test_a_record_that_breaks_the_inspection_is_inspected_as_raw_text(world):
    boundary = world.write_boundary()
    record = {"type": "assistant", "cwd": "/tmp/x\u0000y", "message": {"model": "deepseek-v4-pro", "content": [
        {"type": "tool_use", "name": "Read", "input": {"file_path": world.client_file}}]}}
    world.claude("nul-cwd", [record])
    world.claude("nul-config", [assistant("z-ai/glm-5.2"), instructions(str(world.home / ".claude" / "x\u0000.md"))])
    result = world.run(boundary)
    d = result["details"]
    assert d["counts"]["restricted_access"] == {"deepseek": 1}
    assert d["malformed_records"] == 2
    assert d["incomplete_reasons"] == []


def test_a_cut_off_claude_record_still_sets_its_lane_cwd_and_config_leak(world):
    boundary = world.write_boundary()
    _write_raw(_claude_path(world, "cut-lane"),
               [_cut_after(tool_use(world.client_file, "deepseek-v4-pro"), world.client_file)])
    _write_raw(_claude_path(world, "cut-cwd"), [
        json.dumps(assistant("z-ai/glm-5.2")),
        _cut_after({"type": "user", "cwd": str(world.client_root), "message": {"role": "user", "content": "hello"}},
                   "hel"),
    ])
    _write_raw(_claude_path(world, "cut-config"), [
        json.dumps(assistant("z-ai/glm-5.2")),
        _cut_after(instructions(str(world.home / ".claude" / "CLAUDE.md")), "CLAUDE.md"),
    ])
    result = world.run(boundary)
    d = result["details"]
    assert d["counts"]["restricted_access"] == {"deepseek": 1}
    assert d["counts"]["restricted_cwd"] == {"glm": 1}
    assert d["counts"]["config_leak"] == {"glm": 1}
    assert d["malformed_records"] == 3


def test_an_unlistable_home_is_a_traversal_error(world, not_root):
    boundary = world.write_boundary()
    world.claude("ds", [assistant("deepseek-v4-pro"), user_text(f"leak {TOKEN}")])
    os.chmod(world.home, 0o000)
    try:
        result = world.run(boundary)
    finally:
        os.chmod(world.home, 0o755)
    d = result["details"]
    assert result["status"] == "fail"
    assert d["incomplete_reasons"] == ["traversal_errors"]
    assert {w["root"] for w in d["incomplete_where"]} == {"config_dirs", "claude_projects", "kimi"}
    assert d["scanned_through"] == int(SINCE)


def test_an_unreadable_transcript_file_is_named_by_root_and_session(world, not_root):
    boundary = world.write_boundary()
    path = world.claude("locked", [assistant("deepseek-v4-pro")])
    os.chmod(path, 0o000)
    try:
        result = world.run(boundary)
    finally:
        os.chmod(path, 0o644)
    d = result["details"]
    assert result["status"] == "fail"
    assert d["incomplete_reasons"] == ["unreadable_files"]
    assert d["incomplete_where"] == [{"condition": "unreadable_file", "root": "claude_projects",
                                      "session": "locked", "error": "PermissionError"}]


def test_a_dangling_transcript_link_is_counted_and_does_not_keep_the_checkpoint(world):
    boundary = world.write_boundary()
    world.claude("ds", [assistant("deepseek-v4-pro")])
    os.symlink(world.home / "nowhere.jsonl", _claude_path(world, "dangling"))
    result = world.run(boundary)
    d = result["details"]
    assert result["status"] == "ok"
    assert d["non_regular_files"] == 1
    assert d["scanned_through"] == int(NOW)


def test_linked_directories_are_followed_once(world):
    boundary = world.write_boundary()
    projects = world.home / ".claude" / "projects"
    _write(projects / "real-proj" / "linked.jsonl", [assistant("deepseek-v4-pro"), user_text(f"leak {TOKEN}")], NOW - 60)
    os.symlink(projects / "real-proj", projects / "alias-proj")
    os.symlink(projects, projects / "real-proj" / "loop")
    result = world.run(boundary)
    d = result["details"]
    assert d["counts"]["canary"] == {"deepseek": 1}
    assert d["scanned"]["claude_projects"] == 1
    assert d["traversal_errors"] == 0


@pytest.mark.parametrize("corruption", ["not_json", "no_checkpoint"])
def test_a_lost_beacon_checkpoint_falls_back_to_the_ledger(world, corruption):
    boundary = world.write_boundary()
    first = _run_at(world, boundary, NOW - 86400, since=SINCE - 86400)
    assert first["status"] == "ok"
    world.lane("glm-harness", "after-first", _leak(), mtime=NOW - 86400 + 600)
    world.beacon_path().write_text("{" if corruption == "not_json" else json.dumps({"details": {}}), encoding="utf-8")
    second = _run_at(world, boundary, NOW)
    d = second["details"]
    assert d["counts"]["canary"] == {"glm-harness": 1}
    assert d["historic_total"] == 0
    assert d["since"] == int(SINCE - 86400)
    assert d["since_source"] == "ledger"


def test_a_lost_checkpoint_without_a_ledger_scans_everything(world):
    boundary = world.write_boundary()
    world.lane("glm-harness", "old", _leak(), mtime=NOW - 30 * 86400)
    world.beacon_path().parent.mkdir(parents=True, exist_ok=True)
    world.beacon_path().write_text("{", encoding="utf-8")
    d = _run_at(world, boundary, NOW)["details"]
    assert d["counts"]["canary"] == {"glm-harness": 1}
    assert d["since"] == 0
    assert d["since_source"] == "full"


def test_no_beacon_and_no_ledger_is_a_first_run(world):
    boundary = world.write_boundary()
    d = _run_at(world, boundary, NOW)["details"]
    assert d["since"] == int(cbc._first_run_since(NOW))
    assert d["since_source"] == "first_run"


def test_a_checkpoint_after_now_is_not_trusted(world):
    boundary = world.write_boundary()
    _run_at(world, boundary, NOW + 86400, since=SINCE)
    assert cbc.read_previous_scanned_through(world.data) == int(NOW + 86400)
    world.lane("glm-harness", "clock-skew", _leak(), mtime=NOW - 600)
    d = _run_at(world, boundary, NOW)["details"]
    assert d["counts"]["canary"] == {"glm-harness": 1}
    assert d["since"] == int(SINCE)
    assert d["since_source"] == "ledger"


def test_doctor_fails_on_an_ok_beacon_that_reports_traversal_errors(world):
    _write_beacon(world, "ok", gating_total=0, canary_armed=True, traversal_errors=1)
    (check,) = vnx_doctor.check_content_boundary_beacon(_doctor_paths(world))
    assert check.status == vnx_doctor.FAIL
    assert "traversal_errors" in check.message


# ---------------------------------------------------------------------------
# Fix round 4 (PR #2061): a complete ledger record before the checkpoint moves,
# kimi tool arguments decoded, path-free warnings
# ---------------------------------------------------------------------------

def test_a_short_write_never_truncates_the_ledger_and_a_zero_write_never_moves_the_beacon(world, monkeypatch):
    boundary = world.write_boundary()
    world.claude("ds-leak", [assistant("deepseek-v4-pro"), user_text(f"leak {TOKEN}")])
    real_write = os.write

    # A write that makes at most 10 bytes per call must still leave a complete, parseable line.
    def short_write(fd, data):
        return real_write(fd, data[:10])

    monkeypatch.setattr(os, "write", short_write)
    result = world.run(boundary)
    monkeypatch.setattr(os, "write", real_write)
    lines = world.ledger_path().read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["status"] == "fail"
    assert record["details"]["gating_total"] == 1
    assert record["ts"] == int(NOW)

    # A write that returns 0 raises, and the beacon checkpoint stays where the last run left it.
    checkpoint = cbc.read_previous_scanned_through(world.data)
    assert checkpoint == int(NOW)
    monkeypatch.setattr(os, "write", lambda fd, data: 0)
    with pytest.raises(OSError):
        world.run(boundary)
    monkeypatch.setattr(os, "write", real_write)
    assert cbc.read_previous_scanned_through(world.data) == checkpoint
    assert len(world.ledger_path().read_text(encoding="utf-8").splitlines()) == 1


def test_kimi_tool_arguments_are_decoded_before_the_path_check(world):
    accent_root = world.client_root / "clients-é"
    boundary = world.write_boundary(client_roots=[str(accent_root)])
    path = str(accent_root / "group" / "notes.md")
    world.kimi("literal", [
        {"role": "assistant", "content": "", "tool_calls": [
            {"function": {"arguments": json.dumps({"path": path}, ensure_ascii=False)}}]},
    ])
    world.kimi("escaped", [
        {"role": "assistant", "content": "", "tool_calls": [
            {"function": {"arguments": json.dumps({"path": path})}}]},
    ])
    result = world.run(boundary)
    assert result["details"]["counts"]["restricted_access"] == {"kimi": 2}
    assert sorted(result["details"]["sessions"]["restricted_access"]) == ["escaped", "literal"]


def test_an_unreadable_project_marker_is_reported_path_free(world, caplog):
    boundary = world.write_boundary()
    cwd = world.home / "work" / "nested"
    cwd.mkdir(parents=True)
    (cwd / content_class.PROJECT_MARKER).write_bytes(b"\xff\xfe invalid utf-8")
    world.claude("ds-marker", [assistant("deepseek-v4-pro", cwd=str(cwd))])

    with caplog.at_level(logging.WARNING):
        result = world.run(boundary)

    # The run still reports the unreadable marker, without naming its directory.
    assert result["details"]["marker_unreadable"] == 1
    assert any("unreadable" in r.getMessage() for r in caplog.records)
    assert str(cwd) not in caplog.text

    # The nightly runner captures stderr: a CLI run must not leak the path there either.
    proc = subprocess.run(
        [sys.executable, str(_SCRIPTS / "content_boundary_check.py"),
         "--home", str(world.home), "--stores-root", str(world.stores),
         "--data-dir", str(world.data), "--state-dir", str(world.state),
         "--boundary-file", str(world.boundary_file), "--since", "0"],
        capture_output=True, text=True, timeout=120, cwd=str(world.home.parent),
    )
    assert str(cwd) not in proc.stderr
