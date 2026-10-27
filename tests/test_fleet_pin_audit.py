"""Tests for ``scripts/fleet_pin_audit.py`` — one verdict per project's pin.

Every test builds its own tmp store, registry and projects; the operator's
``~/.vnx-system``, ``~/.vnx/projects.json`` and the real repos are never read.
"""

import json
import os
import plistlib
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))
sys.path.insert(0, str(REPO_ROOT / "scripts" / "lib"))

import fleet_pin_audit as fpa
import health_beacon

LINK = Path(".claude") / "vnx-system"
PLIST = REPO_ROOT / "scripts" / "launchd" / "com.vnx.fleet-pin-audit.plist"
_NO_LABEL = object()


def _release(store, name, *, label=_NO_LABEL, mode=0o555):
    """One release dir: a ``VERSION`` label (unless asked for none) and a mode.

    A real store's release dirs are read-only and carry a matching ``VERSION``;
    0o555 is that state, 0o755 is the "operator left it unlocked" WARN.
    """
    version_dir = store / "versions" / name
    version_dir.mkdir(parents=True, exist_ok=True)
    if label is not _NO_LABEL:
        text = name if label is None else label
        (version_dir / "VERSION").write_text(f"{text}\n", encoding="utf-8")
    version_dir.chmod(mode)
    return version_dir


def _store(root):
    """A clean store: every release dir labeled and read-only, as installed."""
    for name in ("v1.7.5", "v1.7.6"):
        _release(root, name, label=name.lstrip("v"))
    os.symlink("versions/v1.7.6", root / "current")
    return root


def _pin_inside_release(store, name, pin):
    """Write a ``.vnx-version`` into a locked release dir, then re-lock it."""
    version_dir = store / "versions" / name
    version_dir.chmod(0o755)
    try:
        (version_dir / ".vnx-version").write_text(f"{pin}\n", encoding="utf-8")
    finally:
        version_dir.chmod(0o555)


def _project(root, *, pin=None, link=None):
    root.mkdir(parents=True, exist_ok=True)
    if pin is not None:
        (root / ".vnx-version").write_text(f"{pin}\n", encoding="utf-8")
    if link is not None:
        (root / ".claude").mkdir(exist_ok=True)
        os.symlink(str(link), root / LINK)
    return root


def _registry(tmp_path, projects):
    path = tmp_path / "projects.json"
    path.write_text(json.dumps({"projects": [
        {"name": name, "path": str(root)} for name, root in projects.items()
    ]}), encoding="utf-8")
    return path


def _git_repo(path, *, pin=None):
    """A committed git repo at ``path`` (optionally carrying a pin file)."""
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "tester"], cwd=path, check=True)
    if pin is not None:
        (path / ".vnx-version").write_text(f"{pin}\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-q", "--allow-empty", "-m", "init"],
                   cwd=path, check=True)
    return path


def _worktree(repo, path):
    subprocess.run(["git", "-C", str(repo), "worktree", "add", "-q", str(path)],
                   check=True)
    return path


@pytest.fixture()
def fleet(tmp_path, monkeypatch):
    store = _store(tmp_path / "store")
    fabric = _project(tmp_path / "fabric", pin="1.7.5")
    monkeypatch.setattr(fpa, "FABRIC_SOURCE", fabric)
    projects = {
        "current": _project(tmp_path / "current", pin="1.7.6", link=store / "current"),
        "lagging": _project(tmp_path / "lagging", pin="1.7.5",
                            link=store / "versions" / "v1.7.5"),
        "dead-pin": _project(tmp_path / "dead-pin", pin="1.0.0-rc9"),
        "dead-link": _project(tmp_path / "dead-link",
                              link=store / "versions" / "v1.0.0-rc9"),
        "pin-no-link": _project(tmp_path / "pin-no-link", pin="1.7.6"),
        "link-no-pin": _project(tmp_path / "link-no-pin", link=store / "current"),
        "unpinned": _project(tmp_path / "unpinned"),
        "fabric": fabric,
    }
    return {"tmp": tmp_path, "store": store, "registry": _registry(tmp_path, projects)}


def _run(fleet, capsys, *argv):
    code = fpa.main(["--store-root", str(fleet["store"]),
                     "--registry", str(fleet["registry"]), *argv])
    out, err = capsys.readouterr()
    return code, out, err


def test_every_verdict_shape(fleet, capsys):
    code, out, _ = _run(fleet, capsys, "--json")
    rows = json.loads(out)["projects"]

    assert code == 1  # two dead rows
    assert rows["current"]["verdict"] == "current" and rows["current"]["behind"] == 0
    assert rows["current"]["link"]["in_store"] is True
    assert rows["lagging"]["verdict"] == "lagging" and rows["lagging"]["behind"] == 1
    assert rows["dead-pin"]["verdict"] == "dead" and rows["dead-pin"]["behind"] is None
    assert rows["dead-link"]["verdict"] == "dead"
    assert rows["dead-link"]["link"]["dangling"] is True
    assert rows["pin-no-link"]["verdict"] == "pin-without-link"
    assert rows["link-no-pin"]["verdict"] == "link-without-pin"
    assert rows["link-no-pin"]["pin"] is None
    assert rows["unpinned"]["verdict"] == "unpinned"
    assert rows["fabric"]["verdict"] == "fabric-source"
    assert rows["fabric"]["behind"] is None  # no pin judgment for the source

    report = json.loads(out)
    assert set(report) == {"store", "projects", "summary"}
    assert report["store"]["versions"] == ["1.7.5", "1.7.6"]
    assert report["summary"] == {"projects": 8, "dead": 2, "lagging": 1, "warnings": 3}
    assert set(rows["current"]) == {"path", "pin", "pin_dir", "link", "store_current",
                                    "behind", "checkout_lag", "verdict", "note"}
    assert rows["current"]["checkout_lag"] is None  # not a git repo: unknown, never 0


def test_text_report_one_line_per_project_worst_first(fleet, capsys):
    code, out, _ = _run(fleet, capsys)
    lines = [ln for ln in out.splitlines() if ln.startswith("[")]
    lagging = next(ln for ln in lines if " lagging " in ln)

    assert code == 1
    assert len(lines) == 8
    assert lines[0].split()[1] in ("dead-link", "dead-pin")
    assert "DEAD" in next(ln for ln in lines if " dead-pin " in ln)
    assert "WARN" in lagging
    assert "lag=unknown" in lagging


def test_exit_zero_when_nothing_is_dead(tmp_path, capsys):
    store = _store(tmp_path / "store")
    registry = _registry(tmp_path, {
        "lagging": _project(tmp_path / "lagging", pin="1.7.5"),
        "link-no-pin": _project(tmp_path / "link", link=store / "current"),
        "unpinned": _project(tmp_path / "unpinned"),
    })
    code = fpa.main(["--store-root", str(store), "--registry", str(registry)])
    out, _ = capsys.readouterr()

    assert code == 0
    assert "WARN" in out and "DEAD" not in out


def test_exit_two_when_the_inputs_cannot_be_read(tmp_path, capsys):
    store = _store(tmp_path / "store")
    code = fpa.main(["--store-root", str(store),
                     "--registry", str(tmp_path / "nope.json")])
    out, err = capsys.readouterr()
    assert code == 2 and out == "" and "nope.json" in err

    registry = _registry(tmp_path, {"x": tmp_path / "x"})
    code = fpa.main(["--store-root", str(tmp_path / "no-store"),
                     "--registry", str(registry)])
    _, err = capsys.readouterr()
    assert code == 2 and "versions/" in err


def test_lagging_behind_unknown_when_current_is_not_a_release(tmp_path, capsys):
    store = tmp_path / "store"
    _release(store, "v1.7.5", label="1.7.5")
    (store / "versions" / "edge").mkdir()
    os.symlink("versions/edge", store / "current")
    registry = _registry(tmp_path, {"p": _project(tmp_path / "p", pin="1.7.5")})
    code, out, _ = _run({"store": store, "registry": registry}, capsys, "--json")
    row = json.loads(out)["projects"]["p"]

    assert code == 0  # an unknown distance is not dead
    assert row["verdict"] == "lagging"
    assert row["behind"] is None
    assert row["store_current"] is None


def test_project_dir_audits_one_project_without_a_registry(fleet, capsys):
    project = fleet["registry"].parent / "current"
    code = fpa.main(["--store-root", str(fleet["store"]),
                     "--project-dir", str(project), "--json"])
    out, _ = capsys.readouterr()

    assert code == 0
    assert json.loads(out)["projects"]["current"]["verdict"] == "current"


def _snapshot(root):
    entries = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        entries.extend(os.path.join(dirpath, name) for name in dirnames + filenames)
    return sorted(entries)


def test_audit_writes_nothing(fleet, capsys):
    before = _snapshot(fleet["tmp"])
    _run(fleet, capsys, "--json")

    assert _snapshot(fleet["tmp"]) == before


@pytest.fixture()
def scanned(tmp_path, monkeypatch):
    """A registry-only fleet PLUS the pins the registry cannot see."""
    home = tmp_path / "home"
    store = _store(home / "store")
    repo = _git_repo(home / "registered", pin="1.7.6")
    _worktree(repo, home / "registered-wt")
    stranger = _project(home / "stranger", pin="1.7.6")
    broken = _project(home / "broken", pin="1.0.0-rc9")
    # A pin under the configured store root must be skipped.
    _pin_inside_release(store, "v1.7.6", "1.7.6")
    # Same for one under the fabric source this audit runs from.
    fabric = _project(home / "fabric-src", pin="1.7.5")
    monkeypatch.setattr(fpa, "FABRIC_SOURCE", fabric)
    # A symlinked directory holding a pin must not be followed.
    outside = _project(tmp_path / "outside", pin="1.7.5")
    (home / "linked").symlink_to(outside)
    return {"tmp": tmp_path, "home": home, "store": store, "repo": repo,
            "stranger": stranger, "broken": broken, "outside": outside,
            "registry": _registry(tmp_path, {"registered": repo})}


def test_scan_root_finds_pins_outside_the_registry(scanned, capsys):
    code, out, _ = _run(scanned, capsys, "--scan-root", str(scanned["home"]), "--json")
    report = json.loads(out)
    rows = report["projects"]

    assert code == 1  # the strangers are unregistered
    assert set(rows) == {"registered", "registered-wt", "stranger", "broken"}
    assert (rows["broken"]["verdict"], rows["broken"]["pin_verdict"]) == ("unregistered", "dead")
    assert rows["broken"]["owner"] is None and rows["broken"]["registered"] is False
    assert (rows["stranger"]["verdict"], rows["stranger"]["pin_verdict"],
            rows["stranger"]["owner"]) == ("unregistered", "pin-without-link", None)
    # A worktree is judged like its project, and names that project.
    assert rows["registered-wt"]["verdict"] == "pin-without-link"
    assert "pin_verdict" not in rows["registered-wt"]  # the verdict IS the pin one
    assert rows["registered-wt"]["owner"] == "registered"
    assert rows["registered-wt"]["registered"] is False
    # The registry row itself keeps the pre-PR shape.
    assert not {"registered", "pin_verdict"} & set(rows["registered"])
    assert report["summary"]["projects"] == 4
    assert report["summary"]["unregistered"] == 2
    assert report["summary"]["worktrees"] == 1


def test_scan_root_skips_store_fabric_and_symlinks(scanned, capsys):
    code, out, _ = _run(scanned, capsys, "--scan-root", str(scanned["home"]), "--json")
    rows = json.loads(out)["projects"]

    assert code == 1
    assert "v1.7.6" not in rows  # under --store-root
    assert "fabric-src" not in rows  # under FABRIC_SOURCE
    assert "outside" not in rows and "linked" not in rows  # symlink not followed
    assert str(scanned["outside"].resolve()) not in out


def test_scan_root_text_names_owner_or_unregistered(scanned, capsys):
    code, out, _ = _run(scanned, capsys, "--scan-root", str(scanned["home"]))
    lines = out.splitlines()

    assert code == 1
    assert any("registered-wt" in ln and ln.endswith("owner=registered") for ln in lines)
    assert any(ln.startswith("[FIND]") and " unregistered " in ln for ln in lines)
    assert lines[-1].endswith("2 unregistered, 1 worktrees")


def test_scan_root_exits_zero_when_every_pin_is_owned(tmp_path, monkeypatch, capsys):
    home = tmp_path / "home"
    store = _store(tmp_path / "store")
    monkeypatch.setattr(fpa, "FABRIC_SOURCE", _project(tmp_path / "fabric", pin="1.7.5"))
    repo = _git_repo(home / "registered", pin="1.7.6")
    _worktree(repo, home / "registered-wt")
    code, out, _ = _run({"store": store, "registry": _registry(tmp_path, {"registered": repo})},
                        capsys, "--scan-root", str(home), "--json")
    report = json.loads(out)

    assert code == 0
    assert report["projects"]["registered-wt"]["owner"] == "registered"
    assert report["summary"]["worktrees"] == 1
    assert report["summary"]["unregistered"] == 0


def test_scan_root_that_cannot_be_listed_exits_two(tmp_path, monkeypatch, capsys):
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        pytest.skip("root can read chmod-000 dirs — OSError path unreachable")
    store = _store(tmp_path / "store")
    monkeypatch.setattr(fpa, "FABRIC_SOURCE", _project(tmp_path / "fabric", pin="1.7.5"))
    registry = _registry(tmp_path, {"p": _project(tmp_path / "p", pin="1.7.6")})
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(0o000)
    try:
        code, out, err = _run({"store": store, "registry": registry}, capsys,
                              "--scan-root", str(locked))
    finally:
        locked.chmod(0o755)

    assert code == 2
    assert out == ""
    assert str(locked) in err and "scan root" in err


def test_output_without_scan_root_is_unchanged(fleet, capsys):
    code, out, _ = _run(fleet, capsys)

    assert code == 1
    assert "fleet: 8 projects, 2 dead, 1 lagging, 3 warnings" in out
    assert "unregistered" not in out
    assert "owner=" not in out

    _, out_json, _ = _run(fleet, capsys, "--json")
    report = json.loads(out_json)
    assert set(report["summary"]) == {"projects", "dead", "lagging", "warnings"}
    assert all("registered" not in row and "owner" not in row
               for row in report["projects"].values())


# ---------------------------------------------------------------------------
# The store section: pin_drift.mislabeled / unlabeled / writable
# ---------------------------------------------------------------------------

def _one_project_fleet(tmp_path, store, *, pin="1.7.6", link=None):
    monkeypatch_target = _project(tmp_path / "fabric", pin="1.7.5")
    return {"store": store,
            "registry": _registry(tmp_path, {
                "p": _project(tmp_path / "p", pin=pin, link=link)})}, monkeypatch_target


def test_store_mislabeled_and_writable_are_findings(tmp_path, monkeypatch, capsys):
    store = _store(tmp_path / "store")
    _release(store, "v1.0.1", label="1.0.0")   # mislabeled, read-only
    _release(store, "v1.0.2", label="1.0.2", mode=0o755)  # left unlocked
    fleet, fabric = _one_project_fleet(tmp_path, store)
    monkeypatch.setattr(fpa, "FABRIC_SOURCE", fabric)

    code, out, _ = _run(fleet, capsys)
    lines = out.splitlines()

    assert code == 1
    assert any(ln.startswith("[FIND]") and "v1.0.1" in ln and "mislabeled" in ln
               for ln in lines)
    assert any(ln.startswith("[FIND]") and "v1.0.2" in ln and "writable" in ln
               for ln in lines)

    _, out_json, _ = _run(fleet, capsys, "--json")
    report = json.loads(out_json)
    assert report["store"]["mislabeled"] == [{"dir": "v1.0.1", "version_file": "1.0.0"}]
    assert report["store"]["writable"] == ["v1.0.2"]
    assert report["store"]["unlabeled"] == []


def test_store_unlabeled_is_a_warn_and_keeps_exit_zero(tmp_path, monkeypatch, capsys):
    store = _store(tmp_path / "store")
    _release(store, "v1.0.3", label=_NO_LABEL)  # no VERSION at all
    fleet, fabric = _one_project_fleet(tmp_path, store)
    monkeypatch.setattr(fpa, "FABRIC_SOURCE", fabric)

    code, out, _ = _run(fleet, capsys)

    assert code == 0
    assert any(ln.startswith("[WARN]") and "v1.0.3" in ln and "unlabeled" in ln
               for ln in out.splitlines())
    _, out_json, _ = _run(fleet, capsys, "--json")
    report = json.loads(out_json)
    assert report["store"]["unlabeled"] == ["v1.0.3"]
    assert report["store"]["mislabeled"] == [] and report["store"]["writable"] == []


# ---------------------------------------------------------------------------
# VNX_FLEET_PIN_MAX_LAG_RELEASES
# ---------------------------------------------------------------------------

def _lag_store(tmp_path):
    """Releases 1.0.0 .. 1.0.6 with ``current`` at the newest."""
    store = tmp_path / "store"
    for patch in range(7):
        _release(store, f"v1.0.{patch}", label=f"1.0.{patch}")
    os.symlink("versions/v1.0.6", store / "current")
    return store


def test_lagging_at_or_below_the_threshold_stays_a_warn(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("VNX_FLEET_PIN_MAX_LAG_RELEASES", "3")
    store = _lag_store(tmp_path)
    fleet = {"store": store,
             "registry": _registry(tmp_path, {
                 "below": _project(tmp_path / "below", pin="1.0.4")})}

    code, out, _ = _run(fleet, capsys)
    line = next(ln for ln in out.splitlines() if " below " in ln)

    assert code == 0
    assert line.startswith("[WARN]") and "behind=2" in line
    assert "over lag threshold" not in line


def test_lagging_above_the_threshold_is_a_finding_naming_count_and_threshold(
        tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("VNX_FLEET_PIN_MAX_LAG_RELEASES", "3")
    store = _lag_store(tmp_path)
    fleet = {"store": store,
             "registry": _registry(tmp_path, {
                 "above": _project(tmp_path / "above", pin="1.0.1")})}

    code, out, _ = _run(fleet, capsys)
    line = next(ln for ln in out.splitlines() if " above " in ln)
    _, out_json, _ = _run(fleet, capsys, "--json")
    summary = json.loads(out_json)["summary"]

    assert code == 1
    assert line.startswith("[FIND]") and "behind=5" in line
    assert "over lag threshold" in line and "max 3" in line
    # A finding is not also counted as a warning.
    assert (summary["lagging"], summary["warnings"]) == (1, 0)


def test_lagging_with_an_unknown_distance_stays_a_warn(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("VNX_FLEET_PIN_MAX_LAG_RELEASES", raising=False)
    store = tmp_path / "store"
    _release(store, "v1.7.5", label="1.7.5")
    (store / "versions" / "edge").mkdir()
    os.symlink("versions/edge", store / "current")
    fleet = {"store": store,
             "registry": _registry(tmp_path, {
                 "p": _project(tmp_path / "p", pin="1.7.5")})}

    code, out, _ = _run(fleet, capsys)

    assert code == 0
    line = next(ln for ln in out.splitlines() if " p " in ln)
    assert line.startswith("[WARN]") and "behind=unknown" in line


def test_invalid_threshold_falls_back_to_the_default_loudly(monkeypatch, capsys):
    monkeypatch.setenv("VNX_FLEET_PIN_MAX_LAG_RELEASES", "banana")

    assert fpa.max_lag_releases() == fpa.DEFAULT_MAX_LAG_RELEASES
    assert fpa.DEFAULT_MAX_LAG_RELEASES == 3
    assert "VNX_FLEET_PIN_MAX_LAG_RELEASES" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# --write-state: the fleet_pin_audit health beacon
# ---------------------------------------------------------------------------

def _state_dir(tmp_path):
    state = tmp_path / "data" / "state"
    state.mkdir(parents=True)
    return state, tmp_path / "data" / "health" / "fleet_pin_audit.json"


def _beacon_run(store, registry, state, scan, capsys):
    return fpa.main(["--store-root", str(store), "--registry", str(registry),
                     "--write-state", "--state-dir", str(state),
                     "--scan-root", str(scan), "--json"])


def test_write_state_without_a_scan_root_is_refused(tmp_path, capsys):
    store = _store(tmp_path / "store")
    registry = _registry(tmp_path, {"p": _project(tmp_path / "p", pin="1.7.6")})
    state, beacon = _state_dir(tmp_path)

    code = fpa.main(["--store-root", str(store), "--registry", str(registry),
                     "--write-state", "--state-dir", str(state)])
    out, err = capsys.readouterr()

    assert code == 2
    assert out == "" and "--scan-root" in err
    assert not beacon.exists() and not (tmp_path / "data" / "health").exists()


@pytest.mark.parametrize("expected_code,status", [(0, "ok"), (1, "fail")])
def test_write_state_records_the_verdict(tmp_path, capsys, expected_code, status):
    store = _store(tmp_path / "store")
    scan = tmp_path / "scan"
    scan.mkdir()
    dead = expected_code == 1
    registry = _registry(tmp_path, {
        "p": _project(tmp_path / "p", pin="1.0.0-rc9" if dead else "1.7.6",
                      link=None if dead else store / "current")})
    state, beacon = _state_dir(tmp_path)

    code = _beacon_run(store, registry, state, scan, capsys)

    assert code == expected_code
    payload = json.loads(beacon.read_text(encoding="utf-8"))
    assert payload["component"] == "fleet_pin_audit"
    assert payload["status"] == status
    assert payload["expected_interval_seconds"] == 86400
    assert payload["details"]["scan_roots"] == [str(scan)]
    assert not (state / "health").exists(), "the beacon belongs in the data dir, not the state dir"
    beacons = health_beacon.all_beacons(tmp_path / "data", expected=["fleet_pin_audit"])
    assert beacons["fleet_pin_audit"]["health"] == status


def test_write_state_on_an_unmeasurable_run_records_unmeasured(tmp_path, capsys):
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        pytest.skip("root can read chmod-000 dirs — OSError path unreachable")
    store = _store(tmp_path / "store")
    registry = _registry(tmp_path, {"p": _project(tmp_path / "p", pin="1.7.6")})
    state, beacon = _state_dir(tmp_path)
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(0o000)
    try:
        code = _beacon_run(store, registry, state, locked, capsys)
    finally:
        locked.chmod(0o755)

    assert code == 2
    payload = json.loads(beacon.read_text(encoding="utf-8"))
    assert payload["status"] == "unmeasured"
    assert "scan root" in payload["details"]["reason"]
    beacons = health_beacon.all_beacons(tmp_path / "data", expected=["fleet_pin_audit"])
    assert beacons["fleet_pin_audit"]["health"] != "ok"


def test_write_state_defaults_to_the_resolved_state_dir(tmp_path, monkeypatch, capsys):
    import vnx_paths

    store = _store(tmp_path / "store")
    scan = tmp_path / "scan"
    scan.mkdir()
    registry = _registry(tmp_path, {
        "p": _project(tmp_path / "p", pin="1.7.6", link=store / "current")})
    state, beacon = _state_dir(tmp_path)
    monkeypatch.setattr(vnx_paths, "resolve_paths", lambda: {"VNX_STATE_DIR": str(state)})

    code = fpa.main(["--store-root", str(store), "--registry", str(registry),
                     "--write-state", "--scan-root", str(scan), "--json"])

    assert code == 0
    assert beacon.exists() and not (state / "health").exists()


def test_a_failed_beacon_write_does_not_change_the_verdict(tmp_path, monkeypatch, capsys):
    store = _store(tmp_path / "store")
    scan = tmp_path / "scan"
    scan.mkdir()
    registry = _registry(tmp_path, {"p": _project(tmp_path / "p", pin="1.0.0-rc9")})
    state, _beacon = _state_dir(tmp_path)

    def _boom(*_args, **_kwargs):
        raise RuntimeError("beacon down")

    monkeypatch.setattr(health_beacon.HealthBeacon, "heartbeat", _boom)

    code = _beacon_run(store, registry, state, scan, capsys)
    _out, err = capsys.readouterr()

    assert code == 1
    assert "could not write state" in err


# ---------------------------------------------------------------------------
# The launchd driver
# ---------------------------------------------------------------------------

class TestLaunchdDriver:
    """The beacon only exists if something drives it (absence-is-loud)."""

    def _load(self):
        with PLIST.open("rb") as handle:
            return plistlib.load(handle)

    def test_parses_and_is_machine_wide(self):
        data = self._load()
        assert data["Label"] == "com.vnx.fleet-pin-audit"
        assert data["ProgramArguments"][:2] == ["/bin/bash", "-c"]

    def test_runs_the_audit_with_a_full_scope_and_the_beacon(self):
        command = self._load()["ProgramArguments"][2]
        assert "scripts/fleet_pin_audit.py" in command
        assert "--write-state" in command
        assert "--scan-root" in command
        assert "--write-state" in self._help_text()

    def test_start_interval_is_below_the_beacon_window(self):
        interval = self._load()["StartInterval"]
        assert interval < 86400, "a StartInterval at/above 86400 makes the beacon stale by construction"
        assert self._load()["RunAtLoad"] is True

    def test_no_store_or_home_path_is_hardcoded(self):
        raw = PLIST.read_text(encoding="utf-8")
        assert "/.vnx-data" not in raw and "/Users/" not in raw
        env = self._load()["EnvironmentVariables"]
        assert env["VNX_HOME"] == "${VNX_HOME}"
        assert env["VNX_PROJECT_ID"] == "${VNX_PROJECT_ID}"

    def test_logs_go_to_tmp_and_the_comment_says_why(self):
        data = self._load()
        raw = PLIST.read_text(encoding="utf-8")
        assert data["StandardOutPath"].startswith("/tmp/")
        assert data["StandardErrorPath"].startswith("/tmp/")
        assert "absence-is-loud" in raw and "non-zero exit is expected" in raw

    def _help_text(self):
        return subprocess.run(
            [sys.executable, str(REPO_ROOT / "scripts" / "fleet_pin_audit.py"), "--help"],
            capture_output=True, text=True, timeout=30).stdout
