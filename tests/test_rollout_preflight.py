"""tests/test_rollout_preflight.py — `vnx preflight` (fleet PR-8).

The measurement is built entirely on owners that already exist:
``fleet_pin_audit.classify_project``, ``launchd_project_scope.check_pin_alignment``,
``vnx_cli.commands.release.vnx_release_verify`` and the doctor check
``release:main-drift``. These tests therefore build a FIXTURE fleet under
``tmp_path`` (a fake store, project roots, an installed-plist dir, settings
files) and patch only the external processes: the ``regen_settings.sh``
dry-run and the doctor's release-drift check. The release verifier is mocked
only in the shared ``fleet`` fixture, whose release dirs are not git
checkouts and whose tests are about the other classes; the pin tests build a
real release dir and run the real verifier against a temporary git repo.
Nothing reads ``~/Library/LaunchAgents``, ``~/.vnx-system``, ``~/.vnx`` or a
real project.
"""

from __future__ import annotations

import json
import os
import plistlib
import shutil
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


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True)


def _source_release(path: Path) -> Path:
    """The source checkout the verifier compares against: a real tag and tree."""
    path.mkdir(parents=True)
    _git("init", "-q", "-b", "main", cwd=path)
    _git("config", "user.email", "t@example.com", cwd=path)
    _git("config", "user.name", "tester", cwd=path)
    (path / "VERSION").write_text("1.0.0\n", encoding="utf-8")
    (path / "scripts").mkdir()
    (path / "scripts" / "run.py").write_text("print('v1')\n", encoding="utf-8")
    _git("add", "-A", cwd=path)
    _git("commit", "-q", "-m", "release 1.0.0", cwd=path)
    _git("tag", "v1.0.0", cwd=path)
    return path


def _published_release(store: Path, source: Path, name: str = "v1.0.0") -> Path:
    """A real release dir in the layout ``vnx release verify`` checks: a clone at the tag."""
    target = store / "versions" / name
    target.parent.mkdir(parents=True, exist_ok=True)
    _git("clone", "--quiet", str(source), str(target), cwd=target.parent)
    _git("checkout", "--quiet", name, cwd=target)
    return target


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


@pytest.fixture()
def release_fleet(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    """A fleet whose release dir is a real checkout and really verified.

    Nothing here patches ``verify_release`` or ``vnx_release_verify``: the store
    carries a git clone of a temporary source repo at tag ``v1.0.0``, exactly the
    layout the verifier checks. ``_ENGINE_ROOT`` is pointed at that source so the
    tag is resolved from the fixture and never from this checkout, which happens
    to carry a ``v1.0.0`` tag of its own.
    """
    source = _source_release(tmp_path / "source")
    store = tmp_path / "store"
    _published_release(store, source)
    os.symlink("versions/v1.0.0", store / "current")
    roots = {"alpha": _project(tmp_path, "alpha", "1.0.0")}
    _link(roots["alpha"], store / "versions" / "v1.0.0")
    plist_dir = tmp_path / "LaunchAgents"
    plist_dir.mkdir()
    monkeypatch.setattr(rp, "run_regen_settings_dry_run",
                        lambda root, engine_root=None: _completed(0, json.dumps(RENDER), ""))
    monkeypatch.setattr(rp.doctor, "_check_release_main_drift",
                        lambda root: doctor.Check("release:main-drift", doctor.PASS, LEVEL))
    monkeypatch.setattr(rp, "_ENGINE_ROOT", source)
    monkeypatch.setattr(rp, "DEFAULT_REGISTRY", _registry(tmp_path, roots))
    monkeypatch.setattr(rp, "DEFAULT_STORE_ROOT", store)
    monkeypatch.setattr(rp, "DEFAULT_PLIST_DIR", plist_dir)
    return {"tmp": tmp_path, "store": store, "source": source, "roots": roots,
            "plist_dir": plist_dir, "registry": rp.DEFAULT_REGISTRY}


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


# ── 2b. a pin verifies the INSTALLED dir, whatever the pin spells ──────────
def test_pin_without_the_decorative_v_verifies_the_installed_dir(release_fleet, capsys):
    """Pin ``1.0.0`` installed at ``versions/v1.0.0``: the real verifier passes."""
    assert (release_fleet["roots"]["alpha"] / ".vnx-version").read_text().strip() == "1.0.0"

    code, report = _json(release_fleet, capsys)

    assert code == 0, report
    assert report["projects"]["alpha"]["pin"] == []


def test_pin_spelled_like_the_dir_verifies_the_same_dir(release_fleet, capsys):
    """Pin ``v1.0.0`` naming the same ``versions/v1.0.0``: identical, no finding."""
    (release_fleet["roots"]["alpha"] / ".vnx-version").write_text("v1.0.0\n", encoding="utf-8")

    code, report = _json(release_fleet, capsys)

    assert code == 0, report
    assert report["projects"]["alpha"]["pin"] == []


def test_corrupted_release_dir_is_one_verify_finding_naming_the_pin(release_fleet, capsys):
    """A changed file the verifier checks: exactly one ``verify`` finding."""
    changed = release_fleet["store"] / "versions" / "v1.0.0" / "scripts" / "run.py"
    changed.write_text("print('edited')\n", encoding="utf-8")

    code, report = _json(release_fleet, capsys)

    assert code == 1
    findings = report["projects"]["alpha"]["pin"]
    assert [finding["state"] for finding in findings] == ["verify"]
    assert findings[0]["pin"] == "1.0.0"
    assert "vnx release verify v1.0.0 failed" in findings[0]["detail"]
    assert "changed scripts/run.py" in findings[0]["detail"]


def test_pin_with_no_installed_dir_is_dead_and_never_verified(release_fleet, capsys):
    """No dir answers the pin: one ``dead`` finding, and no verification attempt."""
    (release_fleet["roots"]["alpha"] / ".vnx-version").write_text("9.9.9\n", encoding="utf-8")

    code, report = _json(release_fleet, capsys)

    assert code == 1
    findings = report["projects"]["alpha"]["pin"]
    assert [finding["state"] for finding in findings] == ["dead"]
    assert "9.9.9" in findings[0]["detail"]
    assert all(finding["state"] != "verify" for finding in findings)


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


# ── 11. two roots: a packaged install finds the wrapper in the engine ─────
def _packaged_install(tmp_path: Path) -> tuple[Path, Path]:
    """A wheel layout: ``vnx_cli`` at ``site-packages``, the engine in a sibling.

    Every module is REAL code copied into the layout the wheel ships
    (``vnx_orchestration/scripts``, ``vnx_orchestration/scripts/lib``, ...), so
    no module resolves its own root back into this checkout. Only the wrapper is
    a fixture: a bash function printing the render, standing in for the real
    ``regen_settings.sh``.
    """
    site = tmp_path / "site-packages"
    ignore = shutil.ignore_patterns("__pycache__")
    shutil.copytree(REPO_ROOT / "vnx_cli", site / "vnx_cli", ignore=ignore)
    scripts = site / "vnx_orchestration" / "scripts"
    scripts.mkdir(parents=True)
    for name in ("rollout_preflight.py", "fleet_pin_audit.py", "fleet_role_drift.py"):
        shutil.copy2(REPO_ROOT / "scripts" / name, scripts / name)
    for name in ("lib", "launchd"):
        shutil.copytree(REPO_ROOT / "scripts" / name, scripts / name, ignore=ignore)
    (scripts / "commands").mkdir()
    (scripts / "commands" / "regen_settings.sh").write_text(
        "cmd_regen_settings() { printf '%s\\n' '{\"hooks\": {}}'; }\n", encoding="utf-8")
    return site, scripts


def test_packaged_layout_finds_the_wrapper_in_the_engine(tmp_path: Path):
    _site, scripts = _packaged_install(tmp_path)
    home = tmp_path / "home"
    (home / ".vnx-system" / "versions").mkdir(parents=True)
    (home / "Library" / "LaunchAgents").mkdir(parents=True)
    root = _project(tmp_path, "alpha", None, settings={"hooks": {}})
    registry = home / ".vnx" / "projects.json"
    registry.parent.mkdir(parents=True)
    registry.write_text(json.dumps({"projects": [
        {"project_id": "alpha", "name": "alpha", "path": str(root)}]}), encoding="utf-8")

    env = {key: value for key, value in os.environ.items() if not key.startswith("VNX_")}
    env.pop("PYTHONPATH", None)
    env["HOME"] = str(home)
    proc = subprocess.run([sys.executable, str(scripts / "rollout_preflight.py"), "--json"],
                          capture_output=True, text=True, env=env, cwd=str(tmp_path),
                          timeout=120)

    output = proc.stdout + proc.stderr
    assert proc.returncode == 0, output
    assert "wrapper not found" not in output
    report = json.loads(proc.stdout)
    assert report["projects"]["alpha"]["hooks"] == []


def test_source_checkout_layout_finds_the_wrapper(tmp_path: Path):
    root = _project(tmp_path, "alpha", None)

    proc = rp.run_regen_settings_dry_run(root)

    assert "wrapper not found" not in (proc.stderr or "")
    assert proc.returncode == 0, proc.stderr


# ── 12. fleet resolution: exact ids first, aliases only for a free name ───
def _entry(tmp_path: Path, dirname: str, project_id: str,
           name: str | None = None) -> dict:
    root = tmp_path / dirname
    root.mkdir()
    entry = {"project_id": project_id, "path": str(root)}
    if name is not None:
        entry["name"] = name
    return entry


def test_exact_project_id_beats_an_earlier_projects_name(tmp_path: Path):
    alpha = _entry(tmp_path, "alpha-dir", "alpha", name="beta")
    beta = _entry(tmp_path, "beta-dir", "beta")

    _fleet, selected = rp._resolve_fleet([alpha, beta], ["beta"])

    assert list(selected) == ["beta"]
    assert selected["beta"] == Path(beta["path"])


def test_an_alias_resolves_a_name_that_is_no_exact_id(tmp_path: Path):
    alpha = _entry(tmp_path, "alpha-dir", "alpha", name="beta")

    _fleet, selected = rp._resolve_fleet([alpha], ["beta"])

    assert list(selected) == ["alpha"]


def test_an_alias_two_projects_carry_is_refused(tmp_path: Path):
    alpha = _entry(tmp_path, "alpha-dir", "alpha", name="gamma")
    beta = _entry(tmp_path, "beta-dir", "beta", name="gamma")

    with pytest.raises(rp.MeasurementError) as raised:
        rp._resolve_fleet([alpha, beta], ["gamma"])

    message = str(raised.value)
    assert "gamma" in message
    assert "alpha" in message and "beta" in message


def test_an_unknown_name_still_raises_the_unknown_project_error(tmp_path: Path):
    alpha = _entry(tmp_path, "alpha-dir", "alpha")

    with pytest.raises(rp.MeasurementError, match="unknown project id"):
        rp._resolve_fleet([alpha], ["ghost"])
