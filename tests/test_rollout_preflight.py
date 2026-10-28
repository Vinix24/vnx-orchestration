"""tests/test_rollout_preflight.py — `vnx preflight` (fleet PR-8).

The measurement is built entirely on owners that already exist:
``fleet_pin_audit.classify_project``, ``launchd_project_scope.check_pin_alignment``,
``vnx_cli.commands.release.vnx_release_verify`` and the doctor check
``release:main-drift``. These tests therefore build a FIXTURE fleet under
``tmp_path`` (a fake store, project roots, an installed-plist dir, settings
files) and patch only the three external processes: the ``regen_settings.sh``
dry-run, ``git``-backed release verify and the doctor's release-drift check.
Nothing reads ``~/Library/LaunchAgents``, ``~/.vnx-system``, ``~/.vnx`` or a
real project.
"""

from __future__ import annotations

import json
import os
import plistlib
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))
sys.path.insert(0, str(REPO_ROOT / "scripts" / "lib"))

import rollout_preflight as rp
from vnx_cli.commands import doctor

CLEAN_COMMAND = "bash -c 'exec bash \"$ROOT/scripts/hooks/session_reconcile_cleanup.sh\"'"
RENDER = {"hooks": {"PreToolUse": [
    {"matcher": "Bash", "hooks": [{"type": "command", "command": CLEAN_COMMAND}]}]}}
LEVEL = "release 1.0.0 is level with origin/main (0 commits there are missing from it)"


def _completed(code: int, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(["bash"], code, stdout, stderr)


def _release(store: Path, name: str) -> None:
    version_dir = store / "versions" / name
    version_dir.mkdir(parents=True, exist_ok=True)
    (version_dir / "VERSION").write_text(name.lstrip("v") + "\n", encoding="utf-8")


def _project(tmp_path: Path, name: str, pin: str | None, settings: dict | None = None) -> Path:
    root = tmp_path / "projects" / name
    root.mkdir(parents=True)
    if pin is not None:
        (root / ".vnx-version").write_text(pin + "\n", encoding="utf-8")
    (root / ".claude").mkdir()
    payload = RENDER if settings is None else settings
    (root / ".claude" / "settings.json").write_text(json.dumps(payload), encoding="utf-8")
    return root


def _link(root: Path, target: Path) -> None:
    os.symlink(str(target), str(root / ".claude" / "vnx-system"))


def _registry(tmp_path: Path, projects: dict[str, Path]) -> Path:
    path = tmp_path / "projects.json"
    path.write_text(json.dumps({"projects": [
        {"project_id": name, "name": name, "path": str(root)}
        for name, root in projects.items()]}), encoding="utf-8")
    return path


def _plist(plist_dir: Path, project_id: str, command: str) -> Path:
    plist_dir.mkdir(parents=True, exist_ok=True)
    path = plist_dir / f"com.vnx.gate-obligation-runner.{project_id}.plist"
    payload = {
        "Label": f"com.vnx.gate-obligation-runner.{project_id}",
        "ProgramArguments": ["bash", "-c", command],
        "EnvironmentVariables": {"VNX_PROJECT_ID": project_id},
    }
    with open(path, "wb") as handle:
        plistlib.dump(payload, handle)
    return path


@pytest.fixture()
def fleet(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    store = tmp_path / "store"
    for name in ("v1.0.0", "v1.0.1"):
        _release(store, name)
    os.symlink("versions/v1.0.1", store / "current")
    roots = {"alpha": _project(tmp_path, "alpha", "1.0.0"),
             "beta": _project(tmp_path, "beta", "1.0.1")}
    for name, root in roots.items():
        _link(root, store / "versions" / f"v{root.joinpath('.vnx-version').read_text().strip()}")
    plist_dir = tmp_path / "LaunchAgents"
    plist_dir.mkdir()
    # External seams: the real wrapper subprocess, git-backed verify and the
    # doctor's release-drift measurement are never run against the host.
    monkeypatch.setattr(rp, "run_regen_settings_dry_run",
                        lambda root, engine_root=None: _completed(0, json.dumps(RENDER), ""))
    monkeypatch.setattr(rp.release, "vnx_release_verify", lambda args: 0)
    monkeypatch.setattr(rp.doctor, "_check_release_main_drift",
                        lambda root: doctor.Check("release:main-drift", doctor.PASS, LEVEL))
    # The CLI surface is `--project`/`--json` only (decision 5); the fixture
    # supplies the fleet locations through the module defaults it reads.
    monkeypatch.setattr(rp, "DEFAULT_REGISTRY", _registry(tmp_path, roots))
    monkeypatch.setattr(rp, "DEFAULT_STORE_ROOT", store)
    monkeypatch.setattr(rp, "DEFAULT_PLIST_DIR", plist_dir)
    return {"tmp": tmp_path, "store": store, "roots": roots, "plist_dir": plist_dir,
            "registry": rp.DEFAULT_REGISTRY}


def _run(fleet: dict, capsys: pytest.CaptureFixture, *argv: str) -> tuple[int, str, str]:
    # Tests add projects by mutating fleet["roots"]; the registry is rewritten
    # here so every run sees the current fleet through the same path.
    fleet["registry"].write_text(json.dumps({"projects": [
        {"project_id": name, "name": name, "path": str(root)}
        for name, root in fleet["roots"].items()]}), encoding="utf-8")
    code = rp.main(list(argv))
    out, err = capsys.readouterr()
    return code, out, err


def _json(fleet: dict, capsys: pytest.CaptureFixture, *argv: str) -> tuple[int, dict]:
    code, out, _ = _run(fleet, capsys, "--json", *argv)
    return code, json.loads(out)


# ── 1. a clean fleet: every class empty, exit 0 ────────────────────────────
def test_clean_fleet_reports_every_class_empty(fleet, capsys):
    code, out, err = _run(fleet, capsys)
    assert code == 0, out + err

    code, report = _json(fleet, capsys)
    assert code == 0
    for entry in report["projects"].values():
        for name in rp.REPORT_CLASSES:
            assert entry[name] == []
    assert report["summary"]["projects"] == 2
    assert set(report["summary"]) == {"projects", *rp.REPORT_CLASSES}


# ── 2. a dead pin is a pin finding, exit 1 ─────────────────────────────────
def test_dead_pin_blocks_with_a_pin_finding(fleet, capsys):
    root = _project(fleet["tmp"], "dead", "9.9.9")
    fleet["roots"]["dead"] = root

    code, report = _json(fleet, capsys)

    assert code == 1
    findings = report["projects"]["dead"]["pin"]
    assert [finding["state"] for finding in findings] == ["dead"]
    assert "9.9.9" in findings[0]["detail"]
    assert report["exit_code"] == 1


# ── 2b. a pinned release dir that fails verify is a pin finding ────────────
def test_release_verify_failure_blocks_with_a_pin_finding(fleet, capsys, monkeypatch):
    def failing(args):
        print(f"FAIL: versions/{args.version} is not the tree of tag", file=sys.stderr)
        return 1

    monkeypatch.setattr(rp.release, "vnx_release_verify", failing)
    code, report = _json(fleet, capsys)

    assert code == 1
    findings = report["projects"]["alpha"]["pin"]
    assert findings and findings[0]["state"] == "verify"
    assert "vnx release verify 1.0.0 failed" in findings[0]["detail"]
    assert "not the tree of tag" in findings[0]["detail"]


# ── 3. installed plists on current / unmeasurable are launchd findings ─────
def test_launchd_current_and_unmeasurable_are_findings(fleet, capsys):
    current = str(fleet["store"] / "current")
    _plist(fleet["plist_dir"], "alpha", f"VNX_HOME={current} exec bash run.sh")
    _plist(fleet["plist_dir"], "beta", 'VNX_HOME="$(dirname "$0")/engine" exec bash run.sh')

    code, out, err = _run(fleet, capsys)
    assert code == 1, out + err
    assert "pin_ignored_current" in out
    assert "pin_unmeasurable" in out

    code, report = _json(fleet, capsys)
    states = {finding["state"] for finding in report["projects"]["alpha"]["launchd"]}
    states |= {finding["state"] for finding in report["projects"]["beta"]["launchd"]}
    assert states == {"pin_ignored_current", "pin_unmeasurable"}


# ── 4. hook commands that differ from the render, and a failed dry-run ─────
def test_hook_command_change_names_old_and_new(fleet, capsys, monkeypatch):
    root = fleet["roots"]["alpha"]
    old_command = "bash -c 'old-hook'"
    changed = {"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [
        {"type": "command", "command": old_command}]}]}}
    (root / ".claude" / "settings.json").write_text(json.dumps(changed), encoding="utf-8")
    rendered = dict(RENDER, _vnx_hook_report={"PreToolUse": {
        "kept": [],
        "replaced": [{"matcher": "Bash", "old_command": old_command,
                      "new_command": CLEAN_COMMAND}],
        "added": []}})

    def runner(project, engine_root=None):
        # The engine's report is per project: only alpha's hooks differ.
        return _completed(0, json.dumps(rendered if project == root else RENDER), "")

    monkeypatch.setattr(rp, "run_regen_settings_dry_run", runner)

    code, report = _json(fleet, capsys)

    assert code == 1
    finding = report["projects"]["alpha"]["hooks"][0]
    assert finding["state"] == "changed"
    assert finding["old"] == old_command
    assert finding["new"] == CLEAN_COMMAND
    assert report["projects"]["beta"]["hooks"] == []


def test_missing_settings_file_reports_the_rendered_hooks(fleet, capsys):
    root = _project(fleet["tmp"], "bare", "1.0.0")
    (root / ".claude" / "settings.json").unlink()
    fleet["roots"]["bare"] = root

    code, report = _json(fleet, capsys)

    assert code == 1
    finding = report["projects"]["bare"]["hooks"][0]
    assert finding["old"] is None and finding["new"] == CLEAN_COMMAND


def test_failed_dry_run_is_a_hooks_finding(fleet, capsys, monkeypatch):
    monkeypatch.setattr(rp, "run_regen_settings_dry_run",
                        lambda root, engine_root=None: _completed(
                            1, "", "ERROR: pin 9.9.9 is dead"))

    code, report = _json(fleet, capsys)

    assert code == 1
    for project in ("alpha", "beta"):
        finding = report["projects"][project]["hooks"][0]
        assert finding["state"] == "dry-run-failed"
        assert "pin 9.9.9 is dead" in finding["detail"]


# ── 5. a diverged main blocks; outside a git checkout it is skipped ────────
def test_diverged_main_blocks_and_names_the_doctor_check(fleet, capsys, monkeypatch):
    detail = "release 1.0.0 is 5 commits behind origin/main in /store/v1.0.0: abc1234 fix"
    monkeypatch.setattr(rp.doctor, "_check_release_main_drift",
                        lambda root: doctor.Check("release:main-drift", doctor.FAIL, detail))

    code, out, err = _run(fleet, capsys)
    assert code == 1, out + err
    assert "release:main-drift" in out and "5 commits behind" in out

    code, report = _json(fleet, capsys)
    finding = report["projects"]["alpha"]["main_drift"][0]
    assert finding["check"] == "release:main-drift"
    assert "5 commits behind" in finding["detail"]


def test_main_drift_outside_a_git_checkout_is_skipped(fleet, capsys, monkeypatch):
    skip = "skipped: /store/v1.0.0 is not a git checkout — there is no release/main comparison to make"
    monkeypatch.setattr(rp.doctor, "_check_release_main_drift",
                        lambda root: doctor.Check("release:main-drift", doctor.PASS, skip))

    code, out, _ = _run(fleet, capsys)

    assert code == 0
    assert "skipped:" in out
    assert "not a git checkout" in out
    code, report = _json(fleet, capsys)
    assert report["projects"]["alpha"]["main_drift"] == []


# ── 6. a project without a pin: pin and launchd unmeasured, not blocking ───
def test_project_without_a_pin_reports_pin_and_launchd_unmeasured(fleet, capsys):
    root = _project(fleet["tmp"], "nopin", None)
    fleet["roots"]["nopin"] = root

    code, report = _json(fleet, capsys)

    assert code == 0
    entry = report["projects"]["nopin"]
    assert entry["unmeasured"] == ["launchd", "pin"]
    assert entry["pin"] == [] and entry["launchd"] == []
    assert report["exit_code"] == 0


# ── 7. a module that raises is exit 2, never 0 ────────────────────────────
def test_a_raising_module_exits_two(fleet, capsys, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("alignment exploded")

    monkeypatch.setattr(rp.launchd_project_scope, "check_pin_alignment", boom)

    code, out, err = _run(fleet, capsys)

    assert code == 2
    assert out == ""
    assert "check_pin_alignment failed" in err
    assert "alignment exploded" in err


# ── 8. --json round-trips and exit_code equals the process exit ────────────
def test_json_carries_every_class_and_the_exit_code(fleet, capsys):
    _plist(fleet["plist_dir"], "alpha", f"VNX_HOME={fleet['store'] / 'current'} exec true")

    code, report = _json(fleet, capsys)

    assert code == 1
    assert set(report) == {"projects", "summary", "exit_code"}
    assert report["exit_code"] == code
    for entry in report["projects"].values():
        assert set(entry) == set(rp.REPORT_CLASSES)
    assert set(report["summary"]) == {"projects", *rp.REPORT_CLASSES}


# ── 9. shim-routed hooks are one info line, never a blocker ────────────────
def test_shim_routed_hooks_are_information_only(fleet, capsys, monkeypatch):
    shim = {"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [{
        "type": "command",
        "command": "sh \"$HOME/.vnx-system/current/scripts/hooks/vnx_hook_shim.sh\" "
                   "t0_context_guard.py"}]}]}}
    root = _project(fleet["tmp"], "shim", "1.0.0", settings=shim)
    fleet["roots"]["shim"] = root
    # The render equals each project's current settings: only the shim info differs.
    monkeypatch.setattr(rp, "run_regen_settings_dry_run",
                        lambda root, engine_root=None: _completed(
                            0, json.dumps(rp.read_settings(root)), ""))

    code, out, _ = _run(fleet, capsys)

    assert code == 0
    info_lines = [line for line in out.splitlines() if line.strip().startswith("info:")]
    assert len(info_lines) == 1
    assert ".vnx-system/current" in info_lines[0]


# ── 10. --project narrows; an unknown id exits 2 and names it ──────────────
def test_project_narrows_and_unknown_id_exits_two(fleet, capsys):
    code, report = _json(fleet, capsys, "--project", "alpha")
    assert code == 0
    assert list(report["projects"]) == ["alpha"]

    code, out, err = _run(fleet, capsys, "--project", "ghost")
    assert code == 2
    assert out == ""
    assert "ghost" in err
