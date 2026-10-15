"""Nightly content-boundary measurement (F2 part 3): scanner, beacon, ledger and doctor check.

Every root is a tmp dir: a tmp HOME with ``.claude``, ``.kimi`` and a tmp central-stores root,
a tmp client root and a tmp personal root. No real store, no real transcript is read or written.
"""

from __future__ import annotations

import json
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


def tool_use(path: str) -> dict:
    return {"type": "assistant", "message": {"model": "x", "content": [
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
def world(tmp_path):
    return World(tmp_path)


def _leaky_world(w: World) -> content_class.Boundary:
    boundary = w.write_boundary()
    w.claude("ds-config", [assistant("deepseek-v4-pro"), instructions(str(w.home / ".claude" / "CLAUDE.md"))])
    w.claude("glm-access", [assistant("z-ai/glm-5.2"), tool_use(w.client_file)])
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
