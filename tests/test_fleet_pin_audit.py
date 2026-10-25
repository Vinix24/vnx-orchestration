"""Tests for ``scripts/fleet_pin_audit.py`` — one verdict per project's pin.

Every test builds its own tmp store, registry and projects; the operator's
``~/.vnx-system``, ``~/.vnx/projects.json`` and the real repos are never read.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))
sys.path.insert(0, str(REPO_ROOT / "scripts" / "lib"))

import fleet_pin_audit as fpa

LINK = Path(".claude") / "vnx-system"


def _store(root):
    for name in ("v1.7.5", "v1.7.6"):
        (root / "versions" / name).mkdir(parents=True, exist_ok=True)
    os.symlink("versions/v1.7.6", root / "current")
    return root


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
    (store / "versions" / "v1.7.5").mkdir(parents=True)
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
    (store / "versions" / "v1.7.6" / ".vnx-version").write_text(
        "1.7.6\n", encoding="utf-8")
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
